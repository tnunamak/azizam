"""Design v3 reference semantics, not product authorization or disk durability.

Only set categories and pinned release/route/workflow bundles are supported.
Authentication, evidence authenticity and exhaustive path discovery are inputs.
The owner runs the standard-library unittest suite; this module makes no calls.
"""
from copy import deepcopy
from dataclasses import dataclass, field, replace

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
    waivers: tuple = ()


def deny(reason, rule="evaluation", category="", member="", waivers=()):
    return Decision("deny", reason, rule, category, member, tuple(waivers))


def pinned(identifier):
    # Opaque revision convention; no authenticity or digest verification implied.
    return isinstance(identifier, str) and (
        ("@" in identifier and all(identifier.rsplit("@", 1))) or
        (identifier.startswith("sha256:") and len(identifier) > 7))


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    kind: str = "set"
    values: tuple = ()
    applies_to: tuple = ("release", "route", "workflow")

    def validate(self):
        if self.kind != "set":
            raise PolicyError("A14: only set categories are supported")
        if not self.id or not self.label or not self.applies_to:
            raise PolicyError("incomplete category")
        if not set(self.applies_to) <= KINDS:
            raise PolicyError("unsupported subject kind")
        if (any(not isinstance(v, str) or not v for v in self.values)
                or len(set(self.values)) != len(self.values)):
            raise PolicyError("category needs unique value IDs")

    def accepts(self, value):
        return isinstance(value, str) and value in self.values

    def applies(self, subject):
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


def accept_evidence(evidence, *, actor, owner, observed_at, accepted_at,
                    mapping_version, mapped_value=None):
    """A11: explicit owner acceptance or owner edit; never automatic mapping."""
    if not actor or actor != owner:
        raise PolicyError("owner action required")
    if not all((evidence.source, observed_at, accepted_at, mapping_version)):
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
    assertions: tuple = ()


@dataclass(frozen=True)
class Service:
    id: str
    members: tuple  # First member is the root release or workflow.
    dependencies: tuple = ()  # Embedded, reviewed Service revisions, never live aliases.
    checkable: bool = True


def resolve(card, stack=()):
    """R2: each selected alternative is a separate immutable closure."""
    if not isinstance(card, Service) or not pinned(card.id):
        raise PolicyError("unresolved or unpinned service")
    if card.id in stack:
        raise PolicyError("dependency cycle")
    if not card.members or card.members[0].kind not in {"release", "workflow"}:
        raise PolicyError("missing root release/workflow")
    if type(card.checkable) is not bool:
        raise PolicyError("invalid enforcement boundary")
    if not card.checkable:
        raise PolicyError("cannot enforce before effect")
    result = list(card.members)
    for dependency in card.dependencies:
        result.extend(resolve(dependency, stack + (card.id,)))
    seen = {}
    for subject in result:
        if not pinned(subject.id) or subject.kind not in KINDS:
            raise PolicyError("unsupported or unpinned subject")
        if subject.id in seen and seen[subject.id] != subject:
            raise PolicyError("conflicting revision in closure")
        seen[subject.id] = subject
    return tuple(seen.values())


@dataclass(frozen=True)
class Row:
    id: str
    category: str
    op: str
    values: tuple = ()


@dataclass(frozen=True)
class Plan:
    id: str
    grants: tuple = ()
    require: tuple = ()
    deny: tuple = ()
    missing: dict = field(default_factory=dict)  # Requirement row ID -> "allow".
    mode: str = "v3"  # Explicit legacy mode, never inferred.
    legacy_tags: tuple = ()


def _validate_row(row, categories, denying=False):
    if not row.id or row.category not in categories:
        raise PolicyError("unknown category or unnamed rule")
    if row.op not in ({"in", "missing"} if denying else {"in", "exists", "missing"}):
        raise PolicyError("unsupported operator")
    if row.op != "in" and row.values:
        raise PolicyError("presence operator cannot have candidates")
    if any(not categories[row.category].accepts(v) for v in row.values):
        raise PolicyError("unknown rule value")


def _validate(categories, plans, subjects):
    for key, category in categories.items():
        category.validate()
        if key != category.id:
            raise PolicyError("category key mismatch")
    for subject in subjects:
        if not pinned(subject.id) or subject.kind not in KINDS:
            raise PolicyError("unsupported subject")
        for assertion in subject.assertions:
            if not assertion.accepted:
                continue  # Even malformed, unaccepted evidence is inert.
            category = categories.get(assertion.category)
            if category is None or not category.accepts(assertion.value):
                raise PolicyError("invalid accepted value")
            if not category.applies(subject):
                raise PolicyError("assertion applicability mismatch")
            if not all((assertion.source, assertion.observed_at, assertion.accepted_at,
                        assertion.accepting_owner, assertion.mapping_version)):
                raise PolicyError("incomplete accepted provenance")
    for plan in plans:
        if not plan.id or plan.mode not in {"v3", "legacy"}:
            raise PolicyError("invalid plan")
        for selector in plan.grants:
            if (not isinstance(selector, dict)
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


def _values(subject, category):
    return {a.value for a in subject.assertions
            if a.accepted and a.category == category.id}


def _match(row, values, universal):
    if row.op == "missing":
        return not values
    if row.op == "exists":
        return bool(values)
    if not values:
        return None  # Tri-state unknown, not false.
    return values <= set(row.values) if universal else bool(values & set(row.values))


def legacy_exact(allowed, tags=None, *, grants=True):
    """Historical any-tag result; falsey tags skip the check (see FINDINGS)."""
    if not grants:
        return deny("NO_GRANT", "legacy:grant")
    if not allowed or not tags or set(allowed) & set(tags):
        return Decision("allow", "ALLOW")
    return deny("LEGACY_TAG_MISMATCH", "legacy:tags")


def legacy_gated(allowed, tags=None, *, metadata_complete=True, grants=True):
    if not metadata_complete or not tags:
        return deny("METADATA_INCOMPLETE", "completeness")
    return legacy_exact(allowed, tags, grants=grants)


def evaluate(categories, person_plans, app_plans, service, capability, *,
             person="person", app="app", phase="dispatch",
             tags=None, metadata_complete=True, completeness_gate=True):
    """One evaluator for every boundary, previews and explanations.

    Plans are trusted identity-lookup results, supplied separately for person/app.
    Output boundaries pass the recorded service bundle, never output categories.
    """
    try:
        if phase not in BOUNDARIES or not isinstance(capability, str) or not capability:
            raise PolicyError("invalid request boundary/capability")
        if not person or not app or person_plans is None or app_plans is None:
            return deny("IDENTITY_UNAVAILABLE", "identity")
        if not person_plans or not app_plans:
            return deny("NO_PLANS", "identity")
        plans = tuple(person_plans) + tuple(app_plans)
        subjects = resolve(service)
        _validate(categories, plans, subjects)
        if not metadata_complete:
            return deny("METADATA_INCOMPLETE", "completeness")
        if not completeness_gate:
            raise PolicyError("unsafe completeness gate")
        waivers = []
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
                return deny("NO_GRANT", plan.id + ":grant")
            if plan.mode == "legacy":
                decision = legacy_gated(plan.legacy_tags, tags,
                                        metadata_complete=metadata_complete)
                if decision.outcome == "deny":
                    return replace(decision, rule=plan.id + ":" + decision.rule)
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
        # Reference fail-closed boundary. No exception message is a content reason.
        return deny("POLICY_UNAVAILABLE")


def evaluate_routes(categories, person_plans, app_plans, alternatives, capability, **kwargs):
    return {key: evaluate(categories, person_plans, app_plans, service, capability, **kwargs)
            for key, service in alternatives.items()}


@dataclass(frozen=True)
class PolicyVersion:
    id: str
    categories: dict
    people: dict  # Identity -> tuple of attached plans.
    apps: dict
    services: dict
    completeness_gate: bool = True
    retained: dict = field(default_factory=dict)  # Output ID -> recorded Service bundle.


def decision_for(version, request):
    if version is None:
        return deny("POLICY_UNAVAILABLE", "snapshot")
    try:
        person, app = request.get("person"), request.get("app")
        phase = request.get("phase", "dispatch")
        if not person or not app:
            return deny("IDENTITY_UNAVAILABLE", "identity")
        if person not in version.people or app not in version.apps:
            return deny("IDENTITY_UNAVAILABLE", "identity")
        if phase in {"release", "retrieval", "download", "share"}:
            service = version.retained[request["output"]]
        else:
            service = version.services[request["service"]]
        return evaluate(version.categories, version.people[person], version.apps[app],
                        service, request["capability"], person=person, app=app, phase=phase,
                        tags=request.get("tags"), metadata_complete=request.get("metadata_complete", True),
                        completeness_gate=version.completeness_gate)
    except Exception:
        return deny("POLICY_UNAVAILABLE")


def change_preview(before, after, requests):
    """Finite known request inventory, including explicit retained-output requests."""
    result = {"gains": [], "losses": [], "reason_changes": [],
              "scope": deepcopy(list(requests))}
    for request in result["scope"]:
        old, new = decision_for(before, request), decision_for(after, request)
        if old.outcome != new.outcome:
            result["gains" if new.outcome == "allow" else "losses"].append(deepcopy(request))
        if (old.reason, old.rule, old.category, old.member, old.waivers) != (
                new.reason, new.rule, new.category, new.member, new.waivers):
            result["reason_changes"].append(
                {"request": deepcopy(request), "before": old, "after": new})
    return result


class Registry:
    """Draft-only defaults composer. Effective data must enter a previewed snapshot."""
    def __init__(self, defaults, overrides=None, deleted=None):
        self._defaults = deepcopy(defaults)
        self.overrides = deepcopy(overrides or {})
        self.deleted = set(deleted or ())

    @property
    def defaults(self):
        return deepcopy(self._defaults)  # Bundled base is read-only to callers.

    def effective(self):
        return {k: deepcopy(value) for k, value in (self.defaults | self.overrides).items()
                if k not in self.deleted}

    def reset(self, key):
        self.overrides.pop(key, None)

    def delete(self, key):
        self.deleted.add(key)

    def undelete(self, key):
        self.deleted.discard(key)

    def update_defaults(self, defaults):
        self._defaults = deepcopy(defaults)

    def tick(self, today):
        return {key: bool(isinstance(value, dict) and value.get("observed_at")
                          and value["observed_at"] < today)
                for key, value in self.effective().items()}


def validate_snapshot(snapshot):
    if not snapshot.id or snapshot.completeness_gate is not True:
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


def closure_signature(service):
    # Facts may be corrected in a new policy snapshot, but a dependency change
    # requires a NEW service revision. The signature describes the execution graph.
    return (service.id, tuple((s.id, s.kind) for s in service.members),
            tuple(closure_signature(d) for d in service.dependencies), service.checkable)


def service_catalog(snapshot):
    catalog = {}

    def visit(service):
        signature = closure_signature(service)
        if service.id in catalog and catalog[service.id] != signature:
            raise PolicyError("one service revision has conflicting closures")
        catalog[service.id] = signature
        for dependency in service.dependencies:
            visit(dependency)

    for service in tuple(snapshot.services.values()) + tuple(snapshot.retained.values()):
        visit(service)
    return catalog


def preview_inventory(before, after, requests):
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
                           for grant in plan.grants})
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


@dataclass(frozen=True)
class Candidate:
    token: int
    base: str | None
    snapshot: PolicyVersion
    preview: dict


class History:
    """In-memory durable-store specification: only committed snapshots are live.

    Interruption injection is before commit, never partial pointer mutation. Cache
    invalidation precedes commit/ack. Production disk I/O and locking are not tested.
    """
    def __init__(self, initial=None):
        self._versions = []
        self._pending = {}
        self._sequence = 0
        self.cache = {}
        if initial is not None:
            validate_snapshot(initial)
            self._versions.append(deepcopy(initial))

    def current(self):
        return deepcopy(self._versions[-1]) if self._versions else None

    def preview(self, snapshot, requests, *, actor, owner):
        if not actor or actor != owner:
            raise PolicyError("owner action required")
        validate_snapshot(snapshot)
        self._sequence += 1
        current = self.current()
        base = current.id if current else None
        candidate = Candidate(self._sequence, base, deepcopy(snapshot),
                              change_preview(current, snapshot, preview_inventory(current, snapshot, requests)))
        self._pending[candidate.token] = deepcopy(candidate)
        return deepcopy(candidate)

    def activate(self, candidate, *, actor, owner, interrupt=None):
        if not actor or actor != owner:
            raise PolicyError("owner action required")
        if self._pending.get(candidate.token) != candidate:
            raise PolicyError("candidate was not previewed or was modified")
        current = self.current()
        if candidate.base != (current.id if current else None):
            raise PolicyError("STALE_BASE")
        validate_snapshot(candidate.snapshot)  # Restore and activation revalidate.
        if any(v.id == candidate.snapshot.id for v in self._versions):
            raise PolicyError("snapshot revision must be new")
        old_services = {}
        for version in self._versions:
            old_services.update(service_catalog(version))
        for identifier, signature in service_catalog(candidate.snapshot).items():
            if identifier in old_services and old_services[identifier] != signature:
                raise PolicyError("closure changed without a new service revision")
        committed = deepcopy(candidate.snapshot)
        if interrupt == "before_commit":
            raise PolicyError("interrupted before commit")
        self.cache.clear()
        if interrupt == "after_cache_clear":
            raise PolicyError("interrupted before commit")
        if interrupt is not None:
            raise PolicyError("unknown interruption")
        self._versions.append(committed)
        del self._pending[candidate.token]
        return self.current()

    def restore(self, old_id, new_id, requests, *, actor, owner):
        old = next((v for v in self._versions if v.id == old_id), None)
        if old is None:
            raise PolicyError("unknown historical snapshot")
        return self.preview(replace(deepcopy(old), id=new_id), requests, actor=actor, owner=owner)

    def read_cache(self, key):
        current = self.current()
        return deepcopy(self.cache.get((current.id, key))) if current else None

    def write_cache(self, key, value):
        current = self.current()
        if current is None:
            raise PolicyError("no committed snapshot")
        self.cache[(current.id, key)] = deepcopy(value)


def migration_comparison(legacy_allowed, request, version, *, legacy_grants=True):
    """No implicit category conversion: caller supplies the owner-proposed version."""
    return {"legacy_exact": legacy_exact(legacy_allowed, request.get("tags"),
                                         grants=legacy_grants),
            "legacy_gated": legacy_gated(legacy_allowed, request.get("tags"),
                                         metadata_complete=request.get("metadata_complete", True),
                                         grants=legacy_grants),
            "v3": decision_for(version, request)}
