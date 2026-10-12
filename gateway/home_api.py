"""Session-scoped Home discovery and a single real public API connection test.

Keys remain hash-only. The browser supplies a reveal-once or pasted key for a
test; it is never persisted, returned, or sent outside the public API dispatch.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, providers, sqlite_startup
from gateway.admin_api import AdminAuthError
from gateway.admin_runtime import access_for_person, identity_from_access_row, person_for_app_credential
from gateway.alias_policy import service_model_records
from gateway.availability import availability_evidence_snapshot, project_service, project_capabilities
from gateway.capabilities import Capability, summarize_capability, capability_with_policy_view
from gateway.model_presentation import (product_capability, capability_presentation,
                                        group_product_capabilities, product_status_summary)
from gateway.request_source import gateway_ui_request
from gateway.runtime_auth import RuntimeIdentity, hash_api_key, make_policy_request
from gateway.service_registry import is_remote_provider_config
from gateway.user_runtime import (
    UserPortalError, build_user_activity_snapshot, build_user_portal_snapshot,
    get_user_app_credential, get_user_receipt,
)


def _json(content: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status, headers={"cache-control": "no-store"})


def _error(exc: UserPortalError | AdminAuthError) -> JSONResponse:
    return _json({"error": {"code": exc.code, "message": exc.message}}, exc.status_code)


def _identity(app: Mapping[str, Any], credential: Mapping[str, Any] | None,
              path: Path, *, person_id: str) -> RuntimeIdentity | None:
    """Resolve an App's access without marking a key used by a read-only Home request."""
    with admin_store.runtime_auth_connection(path) as conn:
        row = admin_store.runtime_access_row(conn, app_id=str(app["id"]))
    if row is None:
        return None
    identity = identity_from_access_row(
        row, key_hash=str((credential or {}).get("hash") or ""), name=str(app.get("name") or "Your access"),
        person_id=person_for_app_credential(app, credential) if credential is not None else person_id,
    )
    return None if identity.denial else identity


def _new_app_identity(path: Path, *, person_id: str) -> RuntimeIdentity:
    """A new App of this person is "Same as my access plan" (design §2.9)."""
    return RuntimeIdentity(name="Your access", key_hash="", access=access_for_person(person_id, path=path),
                           person_id=person_id, accountable_person_id=person_id)


def _availability(capability: Capability, evidence: Mapping[str, Mapping[str, Any]], registry: Mapping[str, Any] | None = None) -> tuple[str, str]:
    projection = project_service(capability.backend, service_records=registry, evidence=evidence, fallback_capability=capability)
    evidence_kind = (projection.evidence or {}).get("kind")
    code = {
        ("unavailable", "disabled"): "unavailable_disabled",
        ("unavailable", "unconfigured"): "unavailable_unconfigured",
    }.get((projection.status, evidence_kind), projection.status)
    return projection.status, code


def _has_success(receipts_path: Path) -> bool:
    # An EXISTS query avoids mistaking a limited recent page for all history.
    with sqlite_startup.connect_readonly(receipts_path) as conn:
        return conn.execute("SELECT EXISTS(SELECT 1 FROM receipts WHERE status = 'succeeded' "
                            "AND parent_request_id IS NULL)").fetchone()[0] == 1


def _external_client_evidence(receipts_path: Path) -> tuple[dict[str, Any], bool]:
    """Observed app or local-network use, and whether an older request might be.

    Only a recorded source proves it: new UI tests carry gateway-ui and
    internal hops never qualify. Legacy receipts predate source attribution,
    so their origin is unknown and they cannot complete the step.
    """
    with sqlite_startup.connect_readonly(receipts_path) as conn:
        observed, unattributed = conn.execute(
            "SELECT MAX(CASE WHEN json_extract(receipt_json, '$.source_kind') IN ('app', 'local-network') "
            "THEN created_at END), "
            "MAX(CASE WHEN json_extract(receipt_json, '$.source_kind') IS NULL THEN created_at END) "
            "FROM receipts WHERE status = 'succeeded' AND parent_request_id IS NULL"
        ).fetchone()
    return {"recorded": observed is not None, "source": "receipt", "observed_at_s": observed}, unattributed is not None


def _setup_evidence(path: Path, subject: str) -> dict[str, dict[str, Any]]:
    """Persisted acknowledgements are distinct from observed gateway traffic."""
    with sqlite_startup.connect_readonly(path) as conn:
        rows = conn.execute("SELECT action, MAX(created_at) FROM audit_events "
                            "WHERE actor_id = ? AND action IN "
                            "('setup.settings_copied', 'setup.external_client_confirmed', 'setup.receipt_viewed', 'setup.checklist_dismissed') "
                            "GROUP BY action", (subject,))
        return {action.removeprefix("setup."): {"recorded": True, "source": "user_confirmation",
                                               "observed_at_s": timestamp}
                for action, timestamp in rows}


def _invitation_created(path: Path) -> bool:
    with sqlite_startup.connect_readonly(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_links'").fetchone():
            return False
        return bool(conn.execute("SELECT EXISTS(SELECT 1 FROM account_links WHERE kind='invite')").fetchone()[0])


def _resolved_capabilities(identity: RuntimeIdentity, provider,
                           registry: Mapping[str, Any] | None = None) -> list[Capability]:
    """Evaluate aliases against their concrete target, as public dispatch does.

    The general discovery catalog labels aliases with backend ``gateway``.
    That label cannot authorize or measure the service which will execute one.
    A missing or ambiguous target remains unavailable to this surface.
    """
    raw = list(provider(identity))
    alias_names = {(cap.intent, cap.model) for cap in raw if "alias" in cap.tags}
    def target_policy(cap, model, concrete=None):
        concrete = concrete or cap
        resolved = replace(cap, backend=concrete.backend, health=concrete.health,
                           lifecycle=concrete.lifecycle)
        service = ((registry or {}).get("services") or {}).get(concrete.backend) or {}
        config = service.get("config") or {}
        if "remote" in cap.tags or "remote" in concrete.tags or is_remote_provider_config(config):
            # Remote verdicts include canonical provider grants and admin plan
            # restrictions. The local binding check cannot reproduce those.
            upstream = concrete if cap.backend == "gateway" or not concrete.policy_available else cap
            return replace(resolved, policy_available=upstream.policy_available,
                           policy_sources=upstream.policy_sources,
                           policy_reasons=upstream.policy_reasons)
        return capability_with_policy_view(resolved, identity.access, request=make_policy_request(
            protocol=_PROTOCOLS.get(product_capability(cap), "openai.chat"),
            intent=cap.intent, model=model, backend=concrete.backend,
            workflow=concrete.workflow, capability_exposure=concrete.exposure,
            capability_trust_tier=concrete.trust_tier,
            capability_resource_tier=concrete.resource_tier, capability_tags=concrete.tags),
            identity=identity)
    out = []
    for cap in raw:
        targets = [tag.removeprefix("resolves_to:") for tag in cap.tags if tag.startswith("resolves_to:")]
        if "alias" not in cap.tags or not targets:
            if (cap.intent, cap.model) in alias_names:
                continue  # Public aliases take precedence over same-named models.
            if cap.workflow and cap.model is None and identity.access.lists_items:
                cap = target_policy(cap, cap.workflow)
            out.append(cap)
            continue
        # Service-selected aliases already carry the selected service ID from
        # declarative alias data; their target is an opaque service option ID,
        # rather than a model name to look up in another catalog.
        if cap.backend != "gateway" and any(tag.startswith("engine:") for tag in cap.tags):
            service = ((registry or {}).get("services") or {}).get(cap.backend)
            out.append(cap if isinstance(service, Mapping) and service.get("enabled", True)
                       else replace(cap, policy_available=False))
            continue
        target = targets[0]
        matches = [candidate for candidate in raw if "alias" not in candidate.tags
                   and (candidate.model in {target, f"role:{target}"} or candidate.workflow == target)
                   and candidate.intent == cap.intent]
        # Service discovery often describes an endpoint without enumerating its
        # models. The registry's logical model rows remain routing truth.
        services = (registry or {}).get("services") or {}
        if isinstance(services, Mapping):
            for service_id, service in services.items():
                if not isinstance(service, Mapping) or not service.get("enabled", True):
                    continue
                config = service.get("config") or {}
                if is_remote_provider_config(config):
                    continue  # Remote targets require an upstream policy projection.
                if not any(row.get("enabled") is not False and target in {row.get("id"), row.get("model_group")}
                           for row in service_model_records(config)):
                    continue
                generic = next((candidate for candidate in raw if candidate.backend == service_id
                                and candidate.model is None and candidate.intent == cap.intent), None)
                matches.append(replace(generic or cap, backend=service_id, model=target))
        backends = {candidate.backend for candidate in matches}
        if len(backends) != 1:
            out.append(replace(cap, policy_available=False))
            continue
        concrete = matches[0]
        out.append(target_policy(cap, target, concrete))
    if identity.access.lists_items:
        # A model-less endpoint alone cannot prove that a constrained key has
        # any usable target. Require a permitted named capability or a logical
        # model declared by the service's routing data.
        named = {(cap.intent, cap.backend) for cap in out if cap.policy_available and cap.model is not None}
        services = (registry or {}).get("services") or {}
        for index, cap in enumerate(out):
            if cap.model is not None or cap.workflow is not None or not cap.policy_available:
                continue
            service = services.get(cap.backend) or {}
            model_rows = service_model_records(service.get("config") or {})
            declared = any(target_policy(cap, row.get("model_group") or row.get("id")).policy_available
                           for row in model_rows if row.get("enabled") is not False and (row.get("model_group") or row.get("id"))
                           and (cap.intent, row.get("model_group") or row.get("id")) not in alias_names)
            if (cap.intent, cap.backend) not in named and not declared:
                out[index] = replace(cap, policy_available=False)
    return out


def _declared_local_fast(cap: Capability, registry: Mapping[str, Any]) -> bool:
    """Only declared local execution can generate during a no-charge test."""
    services = registry.get("services") or {}
    if not isinstance(services, Mapping):
        return False
    service = services.get(cap.backend)
    if not isinstance(service, Mapping) or not service.get("enabled", True):
        return False
    config = service.get("config") or {}
    if not isinstance(config, Mapping):
        return False
    local_flags = ("gateway_lifecycle", "local_lifecycle", "lifecycle_backed")
    target = next((tag.removeprefix("resolves_to:") for tag in cap.tags if tag.startswith("resolves_to:")), cap.model)
    models = [row for row in service_model_records(config)
              if target in {row.get("id"), row.get("model_group")}]
    return bool(any(config.get(flag) is True for flag in local_flags)
                or any(row.get(flag) is True for row in models for flag in local_flags))


def _capability_rows(capabilities, evidence, registry=None, alias_views=None, workflow_records=()) -> list[dict[str, Any]]:
    rows = []
    visible = project_capabilities(capabilities, service_records=registry, evidence=evidence,
                                   workflow_records=workflow_records)
    for cap in visible:
        if not cap.policy_available:
            continue
        composed = (cap.availability if isinstance(cap.availability, Mapping)
                    and (cap.availability.get("evidence") or {}).get("source") == "dependencies" else None)
        if composed is not None:
            # A gateway-composed workflow is as available as the engines it
            # needs; the "gateway" pseudo-backend itself is never checked.
            state = str(composed.get("status") or "unknown")
            reason = str(composed.get("reason_code") or state)
            product_availability = dict(composed)
        else:
            state, reason = _availability(cap, evidence, registry)
            product_availability = project_service(cap.backend, service_records=registry, evidence=evidence,
                                                   fallback_capability=cap).to_public_dict()
        metadata = next((alias for view in (alias_views or {}).values() for alias in view.get("stable", [])
                         if alias.get("alias") == cap.model and "alias" in cap.tags), None)
        summary = summarize_capability(cap, "client")
        summary.pop("reason", None)
        service = ((registry or {}).get("services") or {}).get(cap.backend) or {}
        remote = ("remote" in cap.tags or cap.backend_kind == "remote"
                  or cap.cost_class not in {None, "free-local"}
                  or is_remote_provider_config(service.get("config") or {}))
        if not remote and _declared_local_fast(cap, registry or {}):
            summary["backend_kind"] = "local"
        elif remote:
            summary["backend_kind"] = "remote"
        product_availability.pop("reason", None)
        product_availability.pop("engine", None)
        product_availability["reason_code"] = reason
        rows.append({**summary, **capability_presentation(cap, metadata),
                     "status": state, "available": state in {"ready", "idle"}, "reason_code": reason,
                     "availability": product_availability})
    return rows


def _alias_rows(rows, views) -> list[dict[str, Any]]:
    aliases = []
    for operation, view in views.items():
        for alias in view.get("stable", []):
            matches = [row for row in rows if row.get("model") == alias.get("alias")
                       and "alias" in row.get("tags", [])
                       and f"resolves_to:{alias.get('resolves_to')}" in row.get("tags", [])
                       and row.get("intent") == _OPERATION_INTENTS.get(operation, operation)]
            if matches:
                best = next((row for row in matches if row["available"]), matches[0])
                aliases.append({"id": alias["alias"], "operation": operation,
                                "available": best["available"], "status": best["status"], "reason_code": best["reason_code"],
                                "local": best.get("backend_kind") == "local"})
    return aliases


def create_home_router(*, store, receipts_path: Callable[[], Path], require_session,
                       require_action, csrf_token, capabilities,
                       aliases, public_base_url, status_provider, admin_auth,
                       spend_ledger_path: Callable[[], Path | None] = lambda: None,
                       availability_services: Callable[[Mapping[str, Any]], Mapping[str, Any]] = lambda registry: registry,
                       workflow_records: Callable[[], Any] = lambda: ()) -> APIRouter:
    """Register session discovery, connection checks and owner setup evidence.

    ``availability_services`` adds engines that only composed workflows use
    (such as the face sidecar) to the registry view availability reads;
    ``workflow_records`` lets those workflows project from their engines.

    ``spend_ledger_path`` returns the provider spend ledger that cap
    enforcement uses; without it, spend against a cap is reported as unknown.
    """
    router = APIRouter()

    async def live_status_payload() -> dict[str, Any]:
        status = await status_provider()
        backends = [dict(row) for row in status.get("backends", []) if isinstance(row, Mapping)]
        # Probe responses can exist for disabled services. Their enablement
        # remains authoritative in the registry, even when an endpoint answers.
        services = store.get_service_registry().get("services") or {}
        known = {str(row.get("service_id") or row.get("key") or row.get("name")) for row in backends}
        for service_id, service in services.items():
            if not isinstance(service, Mapping) or service_id in known:
                continue
            backends.append({"service_id": service_id, "status": "unknown"})
        for row in backends:
            service_id = str(row.get("service_id") or row.get("key") or row.get("name") or "")
            service = services.get(service_id)
            if isinstance(service, Mapping) and not service.get("enabled", True):
                row["status"] = "disabled"
        return {**status, "backends": backends}

    @router.get("/me/api/home", include_in_schema=False)
    async def home(request: Request):
        try:
            session = require_session(request)
            account = build_user_portal_snapshot(session, path=store.path)
            if not account.get("authenticated"):
                raise UserPortalError("login_required", "Sign in to view Home.", status_code=401)
        except UserPortalError as exc:
            return _error(exc)
        registry = store.get_service_registry()
        status_payload = await live_status_payload()
        injected = status_payload.get("_availability_evidence")
        availability_evidence = (
            dict(injected)
            if isinstance(injected, Mapping)
            else availability_evidence_snapshot(
                service_records=registry,
                status_payload=status_payload,
                receipt_store_path=receipts_path(),
            )
        )
        alias_views = aliases()
        rows_registry = availability_services(registry)
        records = tuple(workflow_records())
        allowed: dict[tuple[str, str | None, str | None, str], Capability] = {}
        credential_aliases: dict[str, list[dict[str, Any]]] = {}
        owned_apps = admin_store.list_apps_for_user(session.subject, path=store.path)
        for app in owned_apps:
            if app.get("disabled_at"):
                continue
            credentials = admin_store.list_active_credentials(subject_kind="app", app_id=app["id"], path=store.path)
            for credential in credentials or [None]:
                identity = _identity(app, credential, store.path, person_id=session.subject)
                if identity is None:
                    continue
                scoped = _resolved_capabilities(identity, capabilities, registry)
                if credential is not None:
                    credential_aliases[credential["id"]] = _alias_rows(_capability_rows(scoped, availability_evidence, rows_registry, alias_views, records), alias_views)
                for cap in scoped:
                    if cap.policy_available:
                        allowed[(cap.intent, cap.model, cap.workflow, cap.backend)] = cap
        new_identity = _new_app_identity(store.path, person_id=session.subject)
        new_caps = _resolved_capabilities(new_identity, capabilities, registry)
        new_app_aliases = _alias_rows(_capability_rows(new_caps, availability_evidence, rows_registry, alias_views, records), alias_views)
        for cap in new_caps:
            if cap.policy_available:
                allowed[(cap.intent, cap.model, cap.workflow, cap.backend)] = cap
        rows = _capability_rows(allowed.values(), availability_evidence, rows_registry, alias_views, records)
        alias_rows = _alias_rows(rows, alias_views)
        rows = group_product_capabilities(rows)
        summary = product_status_summary(rows)
        base = (public_base_url(request) or "").rstrip("/")
        activity = build_user_activity_snapshot(session, limit=5, path=store.path, receipts_path=receipts_path())
        gateway = {"owner_name": admin_store.owner_display_name(path=store.path)}
        # The person's own spending against their plan's caps (their total
        # across every App they answer for); null when they have no plan.
        person_plan = new_identity.access.person_plan
        plan_spend = (providers.counter_spend(person_plan.caps, column="person_id", subject_id=session.subject,
                                              path=spend_ledger_path())
                      if person_plan is not None else None)
        return _json({**account, "gateway": gateway, "plan_spend": plan_spend,
                      "has_access_plan": new_identity.access.complete,
                      "csrf_token": csrf_token(session), "public_base_url": base,
                      "api_base_url": f"{base}/v1" if base else "/v1", "capabilities": rows,
                      "aliases": alias_rows, "credential_aliases": credential_aliases,
                      "new_app_aliases": new_app_aliases, "status": summary,
                      "receipts": activity["receipts"], "creations": None})

    @router.get("/me/api/status", include_in_schema=False)
    async def member_status(request: Request):
        response = await home(request)
        if response.status_code != 200:
            return response
        # Explicit allowlists keep host telemetry, service identities and raw
        # backend error messages outside this member contract.
        snapshot = json.loads(response.body)
        rows = []
        for row in snapshot["capabilities"]:
            availability = row.get("availability") or {}
            evidence = availability.get("evidence") or {}
            rows.append({**{key: row.get(key) for key in ("capability", "capability_title", "icon_key", "title", "recommended_model", "use_names", "summary", "intent", "model", "workflow", "status", "available")},
                         "reason_code": row["reason_code"],
                         "measured": bool(availability.get("measured")),
                         **({"use_name": row["use_name"]} if row.get("use_name") else {}),
                         **({"observed_at_s": evidence["observed_at_s"]} if evidence.get("observed_at_s") is not None else {}),
                         # Home's evidence is already an allowlist (no request
                         # IDs, endpoints or error text); copy it unchanged.
                         "availability": {"evidence": dict(evidence)}})
        receipts = [{**{key: row.get(key) for key in ("request_id", "status", "intent")},
                     "created_at": row.get("created_at", row.get("_stored_at")),
                     "href": f"/usage/?receipt={quote(str(row['request_id']), safe='')}"}
                    for row in snapshot["receipts"]]
        return _json({"scope": "member", "gateway": snapshot["gateway"], "status": snapshot["status"], "capabilities": rows,
                      "receipts": receipts,
                      "help": ""})

    @router.post("/admin/api/setup-evidence", include_in_schema=False)
    async def setup_evidence(request: Request):
        try:
            context = admin_auth(request)
            if "owner" not in context.roles:
                raise AdminAuthError("Owner access required.", status_code=403, code="owner_required")
            session = require_session(request)
            action_error = require_action(request, session)
            if action_error is not None:
                return action_error
            try:
                body = await request.json()
            except ValueError:
                body = None
            event = body.get("event") if isinstance(body, Mapping) else None
            if not isinstance(event, str) or event not in {"settings_copied", "external_client_confirmed", "receipt_viewed"}:
                raise UserPortalError("invalid_setup_evidence", "Choose a supported setup acknowledgement.")
            metadata = {}
            if event == "receipt_viewed":
                request_id = body.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    raise UserPortalError("receipt_required", "Choose one of your receipts.")
                get_user_receipt(session, request_id, path=store.path, receipts_path=receipts_path())
                metadata["request_id"] = request_id
            else:
                app_id = body.get("app_id")
                app = admin_store.get_app(app_id, path=store.path) if isinstance(app_id, str) else None
                if not app or app.get("owner_user_id") != session.subject or app.get("disabled_at"):
                    raise UserPortalError("app_not_found", "App not found for this account.", status_code=404)
                metadata["app_id"] = app_id
            admin_store.record_audit(f"setup.{event}", actor_kind="user", actor_id=session.subject,
                                     target_kind="setup", target_id=session.subject,
                                     metadata={**metadata, "source": "user_confirmation"}, path=store.path)
            return _json({"event": event, "recorded": True, "source": "user_confirmation"})
        except (UserPortalError, AdminAuthError) as exc:
            return _error(exc)

    @router.post("/me/api/connection-test", include_in_schema=False)
    async def connection_test(request: Request):
        try:
            session = require_session(request)
            action_error = require_action(request, session)
            if action_error is not None:
                return action_error
            try:
                body = await request.json()
            except ValueError:
                body = None
            key = body.get("api_key") if isinstance(body, Mapping) else None
            if not isinstance(key, str) or not key.strip() or len(key) > 4096:
                raise UserPortalError("api_key_required", "Paste your key or create a new key to test.")
            key = key.strip()
            credential = admin_store.find_credential_by_hash(hash_api_key(key), path=store.path)
            supplied_id = body.get("credential_id")
            if credential is None or (supplied_id is not None and supplied_id != credential.get("id")):
                raise UserPortalError("credential_not_found", "API key not found for this account", status_code=404)
            credential = get_user_app_credential(session, credential["id"], path=store.path)
            if credential.get("expires_at") is not None and int(credential["expires_at"]) <= int(time.time()):
                raise UserPortalError("credential_not_found", "API key not found for this account", status_code=404)
            app = admin_store.get_app(credential["app_id"], path=store.path)
            identity = _identity(app, credential, store.path, person_id=session.subject)
            if identity is None:
                raise UserPortalError("credential_unavailable", "This key's access plan is unavailable.", status_code=403)
        except UserPortalError as exc:
            return _error(exc)
        registry = store.get_service_registry()
        configured_fast = next((row for row in (aliases().get("chat") or {}).get("stable", [])
                                if row.get("alias") == "fast"), {})
        target_tag = f"resolves_to:{configured_fast.get('resolves_to')}"
        fast_allowed = any(cap.policy_available and cap.model == "fast" and "alias" in cap.tags
                           and target_tag in cap.tags and product_capability(cap) == "chat"
                           and _declared_local_fast(cap, registry)
                           for cap in _resolved_capabilities(identity, capabilities, registry))
        path = "/v1/chat/completions" if fast_allowed else "/v1/models"
        started = time.perf_counter()
        try:
            # The public route and its middleware perform actual auth, policy,
            # routing, admission and receipt writes. No cookies or admin token.
            transport = httpx.ASGITransport(app=request.app, client=("192.0.2.1", 0), raise_app_exceptions=False)
            with gateway_ui_request():
                async with httpx.AsyncClient(transport=transport, base_url="http://connection-test.invalid") as client:
                    async with asyncio.timeout(30):
                        if fast_allowed:
                            response = await client.post(path, headers={"authorization": f"Bearer {key}"},
                                                         json={"model": "fast", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1, "stream": False})
                        else:
                            response = await client.get(path, headers={"authorization": f"Bearer {key}"})
            ok = response.is_success
            reason = ("A one-token local chat request succeeded." if fast_allowed else "Your key can reach the model catalog. Chat was not tested because a permitted local fast model was not confirmed.") if ok else f"The gateway returned HTTP {response.status_code}. Check your key's access and service status."
            request_id = response.headers.get("x-azizam-request-id")
        except (TimeoutError, httpx.HTTPError):
            ok, reason, request_id = False, "The connection test timed out or could not reach the gateway.", None
        return _json({"ok": ok, "latency_ms": round((time.perf_counter() - started) * 1000),
                      "reason": reason, "path": path, "model": "fast" if fast_allowed else None,
                      "request_id": request_id})

    @router.post("/admin/api/setup-dismiss", include_in_schema=False)
    async def dismiss_setup(request: Request):
        try:
            context = admin_auth(request)
            if "owner" not in context.roles:
                raise AdminAuthError("Owner access required.", status_code=403, code="owner_required")
            session = require_session(request)
            action_error = require_action(request, session)
            if action_error is not None:
                return action_error
            admin_store.record_audit("setup.checklist_dismissed", actor_kind="user", actor_id=context.subject,
                                     target_kind="setup", target_id=context.subject,
                                     metadata={"source": "user_confirmation"}, path=store.path)
            return _json({"dismissed": True, "recorded": True, "source": "user_confirmation"})
        except (UserPortalError, AdminAuthError) as exc:
            return _error(exc)

    @router.get("/admin/api/setup-progress", include_in_schema=False)
    async def setup_progress(request: Request):
        try:
            context = admin_auth(request)
            if "owner" not in context.roles:
                raise AdminAuthError("Owner access required.", status_code=403, code="owner_required")
        except AdminAuthError as exc:
            return _error(exc)
        registry = store.get_service_registry()
        status_payload = await live_status_payload()
        injected = status_payload.get("_availability_evidence")
        availability_evidence = (
            dict(injected)
            if isinstance(injected, Mapping)
            else availability_evidence_snapshot(
                service_records=registry,
                status_payload=status_payload,
                receipt_store_path=receipts_path(),
            )
        )
        services = registry.get("services", [])
        if isinstance(services, Mapping):
            services = [{"id": key, **row} for key, row in services.items()]
        connected = any(
            service.get("enabled", True)
            and project_service(str(service.get("id")), service_records=registry, evidence=availability_evidence).status == "ready"
            for service in services
        )
        succeeded = _has_success(receipts_path())
        evidence = _setup_evidence(store.path, context.subject)
        external_client, unattributed_use = _external_client_evidence(receipts_path())
        app_confirmed = "external_client_confirmed" in evidence or external_client["recorded"]
        invited = _invitation_created(store.path)
        steps = [
            {"id": "claim_owner", "done": True, "optional": False, "reason": "The owner account is claimed.", "href": "/setup"},
            {"id": "connect_service", "done": connected, "optional": False, "reason": "A connected service is healthy." if connected else "Connect a service and check its health.", "href": "/engines/"},
            {"id": "test_request", "done": succeeded, "optional": False, "reason": "A request has succeeded." if succeeded else "Run a test request with your key.", "href": "/#connect"},
            {"id": "connect_app", "done": app_confirmed, "optional": False, "reason": "A successful request from an app key or the local network was observed." if external_client["recorded"] else "You confirmed that your external client works. This is not an automated client test." if app_confirmed else "An older request may have come from another app. Confirm that your app works." if unattributed_use else "Use your key in an external client, then confirm that it works.", "href": "/#connect"},
            {"id": "invite_someone", "done": invited, "optional": True, "reason": "An invitation has been created." if invited else "Invite someone to use your gateway.", "href": "/people/"},
        ]
        dismissed = "checklist_dismissed" in evidence
        evidence["observed_external_client"] = external_client
        evidence.setdefault("checklist_dismissed", {"recorded": False, "source": "user_confirmation", "observed_at_s": None})
        evidence["gateway_request"] = {"recorded": succeeded, "source": "receipt"}
        for event in ("settings_copied", "external_client_confirmed", "receipt_viewed"):
            evidence.setdefault(event, {"recorded": False, "source": "user_confirmation", "observed_at_s": None})
        return _json({"complete": all(step["done"] for step in steps if not step["optional"]),
                      "dismissed": dismissed, "steps": steps, "evidence": evidence})

    return router


_OPERATION_INTENTS = {"chat": "chat.completions", "embeddings": "text.embeddings", "images": "image.generate",
                      "image_edits": "image.edit", "videos": "video.generate", "speech": "speech.synthesis",
                      "transcriptions": "speech.transcription", "audio_generate": "audio.generate", "rerank": "rerank"}

_PROTOCOLS = {"chat": "openai.chat", "embed": "openai.embeddings", "image": "openai.images",
              "video": "openai.video", "speech": "openai.audio", "transcribe": "openai.audio", "audio": "openai.audio",
              "music": "openai.audio", "sfx": "openai.audio"}
