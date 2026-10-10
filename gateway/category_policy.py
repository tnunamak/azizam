"""Pure owner-defined category and access-plan authorization (design v3).

Callers supply authenticated identities, reviewed pinned closures and accepted
facts. This module performs no I/O, authentication, request wiring or persistence.
Runtime, preview and migration explanations all use the same evaluator. IDs and
category meanings are opaque data; timestamp strings are provenance, not timers.
Snapshot activation, immutable revision history and cache invalidation belong to
the caller's storage boundary, not this kernel.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Generic, TypeVar, TypedDict

T = TypeVar("T")
Waiver = tuple[str, str, str]


@dataclass(frozen=True, eq=False)
class _FrozenMap(Mapping[str, T], Generic[T]):
    """Small immutable mapping with ordinary mapping equality and copy support."""

    _entries: tuple[tuple[str, T], ...]

    def __init__(self, values: Mapping[str, T]) -> None:
        object.__setattr__(self, "_entries", tuple(values.items()))

    def __getitem__(self, key: str) -> T:
        for candidate, value in self._entries:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._entries)

    def __len__(self) -> int:
        return len(self._entries)


class PolicyRequest(TypedDict, total=False):
    """Finite preview fixture; identities are authenticated by the caller."""

    person: str
    app: str
    service: str
    output: str
    capability: str
    phase: str
    tags: Sequence[str] | None
    metadata_complete: bool


@dataclass(frozen=True)
class Grant(Mapping[str, str]):
    """Exact selector: fields AND, selectors OR, optional IDs bind the root."""

    capability: str
    service: str
    release: str | None = None
    workflow: str | None = None

    def __iter__(self) -> Iterator[str]:
        return iter(("capability", "service") + tuple(
            key for key in ("release", "workflow") if getattr(self, key) is not None
        ))

    def __getitem__(self, key: str) -> str:
        if key not in ("capability", "service", "release", "workflow"):
            raise KeyError(key)
        value = getattr(self, key)
        if value is None:
            raise KeyError(key)
        return value

    def __len__(self) -> int:
        return sum(1 for _ in self)


KINDS = frozenset(("release", "route", "workflow"))
BOUNDARIES = frozenset(("dispatch", "action", "helper", "retry", "release",
                        "retrieval", "download", "share", "cache"))


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    rule: str = ""
    category: str = ""
    member: str = ""
    waivers: tuple[Waiver, ...] = ()


def deny(reason: str, rule: str = "evaluation", category: str = "",
         member: str = "", waivers: Iterable[Waiver] = ()) -> Decision:
    return Decision("deny", reason, rule, category, member, tuple(waivers))


def pinned(identifier: object) -> bool:
    # Opaque revision convention; no authenticity or digest verification implied.
    return isinstance(identifier, str) and (
        ("@" in identifier and all(identifier.rsplit("@", 1))) or
        (identifier.startswith("sha256:") and len(identifier) > 7))


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    kind: str = "set"
    values: tuple[str, ...] = ()
    applies_to: tuple[str, ...] = ("release", "route", "workflow")

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))
        object.__setattr__(self, "applies_to", tuple(self.applies_to))

    def validate(self) -> None:
        if self.kind != "set":
            raise PolicyError("A14: only set categories are supported")
        if (not isinstance(self.id, str) or not self.id
                or not isinstance(self.label, str) or not self.label or not self.applies_to):
            raise PolicyError("incomplete category")
        if not set(self.applies_to) <= KINDS:
            raise PolicyError("unsupported subject kind")
        if (any(not isinstance(v, str) or not v for v in self.values)
                or len(set(self.values)) != len(self.values)):
            raise PolicyError("category needs unique value IDs")

    def accepts(self, value: object) -> bool:
        return isinstance(value, str) and value in self.values

    def applies(self, subject: Subject) -> bool:
        return subject.kind in self.applies_to


@dataclass(frozen=True)
class Assertion:
    category: str
    value: object
    source: str
    accepted: bool = False
    observed_at: str = ""
    accepted_at: str = ""
    accepting_owner: str = ""
    mapping_version: str = ""


def accept_evidence(evidence: Assertion, *, actor: str, owner: str,
                    observed_at: str, accepted_at: str, mapping_version: str,
                    mapped_value: str | None = None) -> Assertion:
    """A11: explicit owner acceptance or owner edit; never automatic mapping."""
    if not isinstance(actor, str) or not actor or actor != owner:
        raise PolicyError("owner action required")
    if not all(isinstance(value, str) and value for value in
               (evidence.source, observed_at, accepted_at, mapping_version)):
        raise PolicyError("complete provenance required")
    if evidence.source != "owner" and mapped_value is None:
        raise PolicyError("outside evidence requires explicit owner mapping")
    return replace(evidence, value=evidence.value if mapped_value is None else mapped_value,
                   accepted=True, observed_at=observed_at, accepted_at=accepted_at,
                   accepting_owner=actor, mapping_version=mapping_version)


@dataclass(frozen=True)
class Subject:
    id: str
    kind: str
    assertions: tuple[Assertion, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "assertions", tuple(self.assertions))


@dataclass(frozen=True)
class Service:
    id: str
    members: tuple[Subject, ...]  # First member is the root release or workflow.
    dependencies: tuple[Service, ...] = ()  # Reviewed revisions, never live aliases.
    checkable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(self.members))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))


def resolve(card: Service) -> tuple[Subject, ...]:
    """Resolve one reviewed alternative; preserve first-occurrence member order.

    Mutable aliases, cycles, uncheckable effects and conflicting definitions of
    one revision fail closed. Revision syntax is not authenticity verification.
    """
    subjects: dict[str, Subject] = {}
    services: dict[str, tuple[object, ...]] = {}

    def visit(service: Service, stack: tuple[str, ...]) -> None:
        if not isinstance(service, Service) or not pinned(service.id):
            raise PolicyError("unresolved or unpinned service")
        if service.id in stack:
            raise PolicyError("dependency cycle")
        if (not service.members or not isinstance(service.members[0], Subject)
                or service.members[0].kind not in {"release", "workflow"}):
            raise PolicyError("missing root release/workflow")
        if type(service.checkable) is not bool or not service.checkable:
            raise PolicyError("cannot enforce before effect")
        if any(not isinstance(d, Service) for d in service.dependencies):
            raise PolicyError("unresolved dependency")
        signature = (
            tuple((s.id, s.kind) for s in service.members),
            tuple(d.id for d in service.dependencies),
            service.checkable,
        )
        if service.id in services and services[service.id] != signature:
            raise PolicyError("conflicting service revision in closure")
        services[service.id] = signature
        for subject in service.members:
            if (not isinstance(subject, Subject) or not pinned(subject.id)
                    or subject.kind not in KINDS):
                raise PolicyError("unsupported or unpinned subject")
            if subject.id in subjects and subjects[subject.id] != subject:
                raise PolicyError("conflicting subject revision in closure")
            subjects[subject.id] = subject
        for dependency in service.dependencies:
            visit(dependency, stack + (service.id,))

    visit(card, ())
    return tuple(subjects.values())


@dataclass(frozen=True)
class Row:
    id: str
    category: str
    op: str
    values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))


@dataclass(frozen=True)
class Plan:
    id: str
    grants: tuple[Grant | Mapping[str, str], ...] = ()
    require: tuple[Row, ...] = ()
    deny: tuple[Row, ...] = ()
    missing: Mapping[str, str] = field(default_factory=dict)  # Requirement row ID -> "allow".
    mode: str = "v3"  # Explicit legacy mode, never inferred.
    legacy_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "grants", tuple(
            g if isinstance(g, Grant) else _FrozenMap(g) if isinstance(g, Mapping) else g
            for g in self.grants
        ))
        object.__setattr__(self, "require", tuple(self.require))
        object.__setattr__(self, "deny", tuple(self.deny))
        object.__setattr__(self, "missing", _FrozenMap(self.missing))
        object.__setattr__(self, "legacy_tags", tuple(self.legacy_tags))


def _validate_row(row: Row, categories: Mapping[str, Category],
                  denying: bool = False) -> None:
    if not isinstance(row.id, str) or not row.id or row.category not in categories:
        raise PolicyError("unknown category or unnamed rule")
    if row.op not in ({"in", "missing"} if denying else {"in", "exists", "missing"}):
        raise PolicyError("unsupported operator")
    if row.op != "in" and row.values:
        raise PolicyError("presence operator cannot have candidates")
    if any(not categories[row.category].accepts(v) for v in row.values):
        raise PolicyError("unknown rule value")


def _validate(categories: Mapping[str, Category], plans: Iterable[Plan],
              subjects: Iterable[Subject]) -> None:
    for key, category in categories.items():
        category.validate()
        if key != category.id:
            raise PolicyError("category key mismatch")
    for subject in subjects:
        if not pinned(subject.id) or subject.kind not in KINDS:
            raise PolicyError("unsupported subject")
        for assertion in subject.assertions:
            if type(assertion.accepted) is not bool:
                raise PolicyError("invalid acceptance flag")
            if not assertion.accepted:
                continue  # Even malformed, unaccepted evidence is inert.
            category = categories.get(assertion.category)
            if category is None or not category.accepts(assertion.value):
                raise PolicyError("invalid accepted value")
            if not category.applies(subject):
                raise PolicyError("assertion applicability mismatch")
            if not all(isinstance(value, str) and value for value in (
                    assertion.source, assertion.observed_at, assertion.accepted_at,
                    assertion.accepting_owner, assertion.mapping_version)):
                raise PolicyError("incomplete accepted provenance")
    for plan in plans:
        if not isinstance(plan.id, str) or not plan.id or plan.mode not in {"v3", "legacy"}:
            raise PolicyError("invalid plan")
        for selector in plan.grants:
            if (not isinstance(selector, Mapping)
                    or not {"capability", "service"} <= selector.keys()
                    or not selector.keys() <= {"capability", "service", "release", "workflow"}
                    or any(not isinstance(v, str) or not v for v in selector.values())
                    or not pinned(selector["service"])
                    or any(not pinned(selector[k]) for k in ("release", "workflow") if k in selector)):
                raise PolicyError("A9: exact capability and root service required")
        ids = [row.id for row in plan.require + plan.deny]
        if len(set(ids)) != len(ids):
            raise PolicyError("duplicate rule ID")
        for row in plan.require:
            _validate_row(row, categories)
        for row in plan.deny:
            _validate_row(row, categories, True)
        requirements = {row.id for row in plan.require}
        if any(key not in requirements or value != "allow"
               for key, value in plan.missing.items()):
            raise PolicyError("A4: waiver must name one requirement")
        if plan.mode == "legacy" and (plan.require or plan.deny or plan.missing):
            raise PolicyError("legacy conversion must be explicit")
        if plan.mode == "v3" and plan.legacy_tags:
            raise PolicyError("legacy tags require legacy mode")
        if any(not isinstance(tag, str) for tag in plan.legacy_tags):
            raise PolicyError("invalid legacy tag")


def _values(subject: Subject, category: Category) -> set[str]:
    return {a.value for a in subject.assertions
            if a.accepted and a.category == category.id}


def _match(row: Row, values: set[str], universal: bool) -> bool | None:
    if row.op == "missing":
        return not values
    if row.op == "exists":
        return bool(values)
    if not values:
        return None  # Tri-state unknown, not false.
    return values <= set(row.values) if universal else bool(values & set(row.values))


def legacy_exact(allowed: Sequence[str], tags: Sequence[str] | None = None, *,
                 grants: bool = True) -> Decision:
    """Historical tag stage: falsey tags skip; pre-tag grant checks are supplied.

    This intentionally preserves the old bypass for migration comparisons only.
    Protected requests use the gated evaluator instead.
    """
    if type(grants) is not bool:
        return deny("POLICY_UNAVAILABLE")
    if not grants:
        return deny("NO_GRANT", "legacy:grant")
    if not allowed or not tags or set(allowed) & set(tags):
        return Decision("allow", "ALLOW")
    return deny("LEGACY_TAG_MISMATCH", "legacy:tags")


def legacy_gated(allowed: Sequence[str], tags: Sequence[str] | None = None, *,
                 metadata_complete: bool = True, grants: bool = True) -> Decision:
    if type(metadata_complete) is not bool or type(grants) is not bool:
        return deny("POLICY_UNAVAILABLE")
    if not metadata_complete or not tags:
        return deny("METADATA_INCOMPLETE", "completeness")
    return legacy_exact(allowed, tags, grants=grants)


def evaluate(categories: Mapping[str, Category],
             person_plans: Sequence[Plan] | None, app_plans: Sequence[Plan] | None,
             service: Service, capability: str, *, person: str | None = "person",
             app: str | None = "app", phase: str = "dispatch",
             tags: Sequence[str] | None = None, metadata_complete: bool = True,
             completeness_gate: bool = True) -> Decision:
    """One evaluator for every boundary, previews and explanations.

    Plans are trusted identity-lookup results, supplied separately for person/app.
    Output boundaries pass the recorded service bundle, never output categories.
    """
    try:
        if phase not in BOUNDARIES or not isinstance(capability, str) or not capability:
            raise PolicyError("invalid request boundary/capability")
        if (not isinstance(person, str) or not person
                or not isinstance(app, str) or not app
                or person_plans is None or app_plans is None):
            return deny("IDENTITY_UNAVAILABLE", "identity")
        if not person_plans or not app_plans:
            return deny("NO_PLANS", "identity")
        plans = tuple(person_plans) + tuple(app_plans)
        subjects = resolve(service)
        _validate(categories, plans, subjects)
        if type(metadata_complete) is not bool or type(completeness_gate) is not bool:
            raise PolicyError("invalid completeness flags")
        if not metadata_complete:
            return deny("METADATA_INCOMPLETE", "completeness")
        if not completeness_gate:
            raise PolicyError("unsafe completeness gate")
        waivers: list[Waiver] = []
        # Errors have already been checked; deny is never waived.
        for plan in plans:
            for row in plan.deny:
                category = categories[row.category]
                for subject in subjects:
                    if not category.applies(subject):
                        continue
                    match = _match(row, _values(subject, category), False)
                    if match is None or match:
                        return deny("DENY_INPUT_UNKNOWN" if match is None else "EXPLICIT_DENY",
                                    plan.id + ":" + row.id, category.id, subject.id)
        root = service.members[0]
        selectors = {"capability": capability, "service": service.id,
                     root.kind: root.id}
        for plan in plans:
            if not any(all(selectors.get(k) == v for k, v in grant.items())
                       for grant in plan.grants):
                return deny("NO_GRANT", plan.id + ":grant", waivers=waivers)
            if plan.mode == "legacy":
                decision = legacy_gated(plan.legacy_tags, tags,
                                        metadata_complete=metadata_complete)
                if decision.outcome == "deny":
                    return replace(decision, rule=plan.id + ":" + decision.rule,
                                   waivers=tuple(waivers))
            for row in plan.require:
                category = categories[row.category]
                targets = [s for s in subjects if category.applies(s)]
                if not targets:
                    return deny("NO_APPLICABLE_SUBJECT", plan.id + ":" + row.id,
                                category.id, waivers=waivers)
                for subject in targets:
                    values = _values(subject, category)
                    if _match(row, values, True):
                        continue
                    if not values and row.op != "missing" and plan.missing.get(row.id) == "allow":
                        waivers.append((plan.id, row.id, subject.id))
                        continue
                    return deny("REQUIRE_MISSING" if not values else "REQUIRE_MISMATCH",
                                plan.id + ":" + row.id, category.id, subject.id, waivers)
        return Decision("allow", "ALLOW", waivers=tuple(waivers))
    except Exception:
        # Fail closed: operational errors never masquerade as content reasons.
        return deny("POLICY_UNAVAILABLE")


def evaluate_routes(categories: Mapping[str, Category],
                    person_plans: Sequence[Plan] | None, app_plans: Sequence[Plan] | None,
                    alternatives: Mapping[str, Service], capability: str,
                    **kwargs: object) -> dict[str, Decision]:
    return {key: evaluate(categories, person_plans, app_plans, service, capability, **kwargs)
            for key, service in alternatives.items()}


@dataclass(frozen=True)
class PolicyVersion:
    id: str
    categories: Mapping[str, Category]
    people: Mapping[str, tuple[Plan, ...]]  # Identity -> attached plans.
    apps: Mapping[str, tuple[Plan, ...]]
    services: Mapping[str, Service]
    completeness_gate: bool = True
    retained: Mapping[str, Service] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("categories", "services", "retained"):
            object.__setattr__(self, name, _FrozenMap(getattr(self, name)))
        for name in ("people", "apps"):
            object.__setattr__(self, name, _FrozenMap({
                key: tuple(plans) for key, plans in getattr(self, name).items()
            }))


def decision_for(version: PolicyVersion | None, request: PolicyRequest) -> Decision:
    if version is None:
        return deny("POLICY_UNAVAILABLE", "snapshot")
    try:
        person, app = request.get("person"), request.get("app")
        phase = request.get("phase", "dispatch")
        if not isinstance(person, str) or not person or not isinstance(app, str) or not app:
            return deny("IDENTITY_UNAVAILABLE", "identity")
        if person not in version.people or app not in version.apps:
            return deny("IDENTITY_UNAVAILABLE", "identity")
        if phase in {"release", "retrieval", "download", "share"}:
            service = version.retained[request["output"]]
        else:
            service = version.services[request["service"]]
        if not isinstance(version.id, str) or not version.id:
            raise PolicyError("invalid snapshot revision")
        if phase not in {"release", "retrieval", "download", "share"} and service.id != request["service"]:
            raise PolicyError("service key mismatch")
        return evaluate(version.categories, version.people[person], version.apps[app],
                        service, request["capability"], person=person, app=app, phase=phase,
                        tags=request.get("tags"), metadata_complete=request.get("metadata_complete", True),
                        completeness_gate=version.completeness_gate)
    except Exception:
        return deny("POLICY_UNAVAILABLE")


class ReasonChange(TypedDict):
    request: PolicyRequest
    before: Decision
    after: Decision


class ChangePreview(TypedDict):
    gains: list[PolicyRequest]
    losses: list[PolicyRequest]
    reason_changes: list[ReasonChange]
    scope: list[PolicyRequest]


def change_preview(before: PolicyVersion | None, after: PolicyVersion | None,
                   requests: Iterable[PolicyRequest] = ()) -> ChangePreview:
    """Compare explicit fixtures plus all known identity/service/output combinations.

    Gainers, losers and explanation changes describe only the returned finite
    scope. No activation, storage or future-request coverage is implied.
    """
    result: ChangePreview = {"gains": [], "losses": [], "reason_changes": [],
                             "scope": preview_inventory(before, after, requests)}
    for request in result["scope"]:
        old, new = decision_for(before, request), decision_for(after, request)
        if old.outcome != new.outcome:
            result["gains" if new.outcome == "allow" else "losses"].append(deepcopy(request))
        if (old.reason, old.rule, old.category, old.member, old.waivers) != (
                new.reason, new.rule, new.category, new.member, new.waivers):
            result["reason_changes"].append(
                {"request": deepcopy(request), "before": old, "after": new})
    return result


class Registry(Generic[T]):
    """Draft-only defaults composer. Effective data must enter a previewed snapshot."""
    def __init__(self, defaults: Mapping[str, T],
                 overrides: Mapping[str, T] | None = None,
                 deleted: Iterable[str] | None = None) -> None:
        self._defaults = deepcopy(dict(defaults))
        self.overrides: dict[str, T] = deepcopy(dict(overrides or {}))
        self.deleted = set(deleted or ())

    @property
    def defaults(self) -> dict[str, T]:
        return deepcopy(self._defaults)  # Bundled base is read-only to callers.

    def effective(self) -> dict[str, T]:
        return {k: deepcopy(value) for k, value in (self.defaults | self.overrides).items()
                if k not in self.deleted}

    def reset(self, key: str) -> None:
        self.overrides.pop(key, None)

    def delete(self, key: str) -> None:
        self.deleted.add(key)

    def undelete(self, key: str) -> None:
        self.deleted.discard(key)

    def update_defaults(self, defaults: Mapping[str, T]) -> None:
        self._defaults = deepcopy(dict(defaults))

    def tick(self, today: str) -> dict[str, bool]:
        return {key: bool(isinstance(value, dict) and value.get("observed_at")
                          and value["observed_at"] < today)
                for key, value in self.effective().items()}


def validate_snapshot(snapshot: PolicyVersion) -> None:
    if (not isinstance(snapshot.id, str) or not snapshot.id
            or snapshot.completeness_gate is not True):
        raise PolicyError("unsafe snapshot")
    plans = tuple(p for group in (snapshot.people, snapshot.apps)
                  for attached in group.values() for p in attached)
    subjects = []
    for key, service in snapshot.services.items():
        if key != service.id:
            raise PolicyError("service key mismatch")
        subjects.extend(resolve(service))
    for service in snapshot.retained.values():
        subjects.extend(resolve(service))
    _validate(snapshot.categories, plans, subjects)
    service_catalog(snapshot)


def closure_signature(service: Service) -> tuple[object, ...]:
    # Facts may be corrected in a new policy snapshot, but a dependency change
    # requires a NEW service revision. The signature describes the execution graph.
    return (service.id, tuple((s.id, s.kind) for s in service.members),
            tuple(closure_signature(d) for d in service.dependencies), service.checkable)


def service_catalog(snapshot: PolicyVersion) -> dict[str, tuple[object, ...]]:
    catalog: dict[str, tuple[object, ...]] = {}

    def visit(service: Service) -> None:
        signature = closure_signature(service)
        if service.id in catalog and catalog[service.id] != signature:
            raise PolicyError("one service revision has conflicting closures")
        catalog[service.id] = signature
        for dependency in service.dependencies:
            visit(dependency)

    for service in tuple(snapshot.services.values()) + tuple(snapshot.retained.values()):
        visit(service)
    return catalog


def preview_inventory(before: PolicyVersion | None, after: PolicyVersion | None,
                      requests: Iterable[PolicyRequest] = ()) -> list[PolicyRequest]:
    """Cover known identities, grants, selected routes and retained-output boundaries.

    Explicit fixtures carry metadata/tag variants. This finite Cartesian inventory
    does not predict future identities, services or request metadata.
    """
    versions = [v for v in (before, after) if v is not None]
    people = sorted({p for v in versions for p in v.people})
    apps = sorted({a for v in versions for a in v.apps})
    services = sorted({s for v in versions for s in v.services})
    outputs = sorted({o for v in versions for o in v.retained})
    capabilities = sorted({grant["capability"] for v in versions
                           for group in (v.people, v.apps)
                           for plans in group.values() for plan in plans
                           for grant in plan.grants
                           if isinstance(grant, Mapping)
                           and isinstance(grant.get("capability"), str)
                           and grant["capability"]})
    result = deepcopy(list(requests))
    for person in people:
        for app in apps:
            for capability in capabilities:
                base = dict(person=person, app=app, capability=capability)
                for service in services:
                    for phase in sorted(BOUNDARIES - {"release", "retrieval", "download", "share"}):
                        request = dict(base, service=service)
                        if phase != "dispatch":
                            request["phase"] = phase
                        if request not in result:
                            result.append(request)
                for output in outputs:
                    for phase in ("release", "retrieval", "download", "share"):
                        request = dict(base, output=output, phase=phase)
                        if request not in result:
                            result.append(request)
    return result


def migration_comparison(legacy_allowed: Sequence[str], request: PolicyRequest,
                         version: PolicyVersion | None, *,
                         legacy_grants: bool = True) -> dict[str, Decision]:
    """No implicit category conversion: caller supplies the owner-proposed version."""
    return {"legacy_exact": legacy_exact(legacy_allowed, request.get("tags"),
                                         grants=legacy_grants),
            "legacy_gated": legacy_gated(legacy_allowed, request.get("tags"),
                                         metadata_complete=request.get("metadata_complete", True),
                                         grants=legacy_grants),
            "v3": decision_for(version, request)}


def preview_as(version: PolicyVersion | None, person: str, app: str,
               capability: str, *, phase: str = "dispatch",
               tags: Sequence[str] | None = None,
               metadata_complete: bool = True) -> dict[str, Decision]:
    """Evaluate each known selected service/route or recorded output separately.

    Only the supplied finite catalog is covered; no future request is implied.
    Every decision and explanation comes from the runtime evaluator.
    """
    if version is None:
        return {}
    retained = phase in {"release", "retrieval", "download", "share"}
    catalog = version.retained if retained else version.services
    results: dict[str, Decision] = {}
    for key in catalog:
        request: PolicyRequest = {
            "person": person, "app": app, "capability": capability,
            "phase": phase, "tags": tags, "metadata_complete": metadata_complete,
        }
        if retained:
            request["output"] = key
        else:
            request["service"] = key
        results[key] = decision_for(version, request)
    return results
