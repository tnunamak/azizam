"""Access plans for tests: build plans, identities and authenticated requests.

``everything()`` is the plan an owner holds; ``plan(...)`` overrides parts of
it. ``authenticated(identity)`` makes ``proxy`` resolve any App key (or no
key) to ``identity``, the way a real key of that App would.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from gateway.access_plans import CAPABILITIES, AccessPlan, CapabilityGrant, DirectEngineAccess, PlanCaps
from gateway.policy import EffectiveAccess
from gateway.service_registry import canonical_provider_id
from gateway.runtime_auth import RuntimeIdentity


def everything(plan_id: str = "plan_everything", **overrides: Any) -> AccessPlan:
    base = AccessPlan(
        id=plan_id,
        name=overrides.pop("name", "Everything"),
        can_use={capability: CapabilityGrant(all_items=True) for capability in CAPABILITIES},
        direct_engine_access=DirectEngineAccess(all_engines=True, operations=frozenset({"*"})),
        paid_providers=frozenset(),
        provider_fallback=False,
        can_load_models=True,
        receipt_detail="full",
        media_metadata="full",
    )
    return replace(base, **overrides)


def plan(plan_id: str = "plan_test", **overrides: Any) -> AccessPlan:
    """A plan with every local capability and nothing else unless overridden."""
    defaults = {
        "name": "Local only",
        "direct_engine_access": DirectEngineAccess(),
        "receipt_detail": "basic",
        "media_metadata": "none",
    }
    return everything(plan_id, **{**defaults, **overrides})


def plan_for(
    plan_id: str = "plan_test",
    *,
    caps: tuple[str, ...] | None = None,
    models: list[str] | None = None,
    workflows: list[str] | None = None,
    load: bool = True,
    engines: list[str] | str | None = None,
    operations: list[str] | None = None,
    paid: list[str] | None = None,
    fallback: bool = False,
    receipts: str = "basic",
    metadata: str = "none",
    usd_per_request: str | None = None,
    usd_per_day: str | None = None,
    usd_per_month: str | None = None,
) -> AccessPlan:
    """A plan described by what it allows; ``caps=None`` means every capability.

    ``models``/``workflows`` list the only items each capability grants.
    ``engines`` (a list or ``"all"``) with ``operations`` grants direct engine
    access; without ``engines`` there is none.
    """
    listed = models is not None or workflows is not None
    grant = (CapabilityGrant(models=frozenset(models or ()), workflows=frozenset(workflows or ()))
             if listed else CapabilityGrant(all_items=True))
    direct = DirectEngineAccess()
    if engines is not None:
        direct = DirectEngineAccess(
            all_engines=engines == "all",
            engines=frozenset() if engines == "all" else frozenset(engines),
            operations=frozenset(operations or ()),
        )
    return AccessPlan(
        id=plan_id,
        name=plan_id,
        can_use={cap: grant for cap in (CAPABILITIES if caps is None else caps)},
        direct_engine_access=direct,
        # Parsed plans hold canonical provider ids; so do these.
        paid_providers=frozenset(canonical_provider_id(item) for item in paid or ()),
        provider_fallback=fallback,
        caps=PlanCaps(*(None if value is None else Decimal(value)
                        for value in (usd_per_request, usd_per_day, usd_per_month))),
        can_load_models=load,
        receipt_detail=receipts,
        media_metadata=metadata,
    )


def access(person: AccessPlan | None, app: AccessPlan | None | str = "same") -> EffectiveAccess:
    return EffectiveAccess(person_plan=person, app_plan=person if app == "same" else app)


def identity(
    plan_or_access: AccessPlan | EffectiveAccess | None = None,
    *,
    name: str = "test-app",
    key_hash: str = "sha256:test",
    app_id: str | None = "app_test",
    person_id: str | None = None,
    accountable_person_id: str | None = "usr_test",
    downstream: dict | None = None,
) -> RuntimeIdentity:
    if plan_or_access is None:
        plan_or_access = everything()
    effective = plan_or_access if isinstance(plan_or_access, EffectiveAccess) else access(plan_or_access)
    return RuntimeIdentity(
        name=name,
        key_hash=key_hash,
        downstream=downstream or {},
        access=effective,
        app_id=app_id,
        person_id=person_id,
        accountable_person_id=accountable_person_id,
    )


class authenticated:
    """Every data-plane request authenticates as ``runtime_identity``.

    Public paths still get the empty context, exactly as in production. Use it
    as a context manager, or like a ``patch`` object with start()/stop().
    """

    def __init__(self, runtime_identity: RuntimeIdentity | None = None) -> None:
        self.identity = runtime_identity or identity()
        self._patches: list[Any] = []

    def start(self) -> RuntimeIdentity:
        import proxy

        resolved = self.identity

        def authenticate(request, request_path):
            if proxy.is_public_path(request_path):
                return proxy.NO_AUTH_CONTEXT
            return proxy.AuthContext(identity=resolved)

        self._patches = [
            patch.object(proxy, "authenticate_request", side_effect=authenticate),
            patch.object(proxy, "runtime_identity_for_app_credential_hash", return_value=resolved),
        ]
        for item in self._patches:
            item.start()
        return resolved

    def stop(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._patches = []

    def __enter__(self) -> RuntimeIdentity:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def plan_document(name: str = "Test plan", **plan_for_kwargs: Any) -> dict[str, Any]:
    """A complete plan document, described the way ``plan_for`` describes plans."""
    from gateway.access_plans import plan_to_document

    doc = plan_to_document(plan_for(**plan_for_kwargs))
    doc["name"] = name
    doc.pop("id", None)
    return doc


def store_plan(name: str = "Test plan", *, path: Any, **plan_for_kwargs: Any) -> dict[str, Any]:
    """Store a plan in the admin db at ``path`` and return its record (with ``id``)."""
    from gateway import admin_store

    return admin_store.create_access_plan(plan_document(name, **plan_for_kwargs), path=path)


def service_app(name: str, *, path: Any, owner_user_id: str | None = None,
                plan_id: str | None = None, **plan_for_kwargs: Any) -> dict[str, Any]:
    """A service App with a stored plan, accountable to a person who holds it.

    Without ``owner_user_id`` the App belongs to a test "Service owner".
    Without ``plan_id`` a new plan is stored from ``plan_for_kwargs``.
    """
    from gateway import accounts, admin_store

    if plan_id is None:
        plan_id = store_plan(f"{name} plan", path=path, **plan_for_kwargs)["id"]
    if owner_user_id is None:
        owner = admin_store.upsert_oidc_user("https://tests.example", "service-owner",
                                             display_name="Service owner", path=path)
        owner_user_id = owner["id"]
        if admin_store.person_plan_id(owner_user_id, path=path) is None:
            accounts.initialize(path)
            accounts.change_person(owner_user_id, "test", path=path, plan_id=plan_id)
    return admin_store.create_app(name, kind="service", owner_user_id=owner_user_id,
                                  access_plan=plan_id, path=path)


def access_context(plan_or_access: AccessPlan | EffectiveAccess | None = None, **identity_kwargs: Any):
    """An authenticated request context for tests that call a dispatcher directly.

    It holds the plans of a real App key (everything, by default), so the call
    passes the same checks a request with a key would.
    """
    import proxy

    return proxy.AuthContext(identity=identity(plan_or_access, **identity_kwargs))
