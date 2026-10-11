"""Read-only, finite migration preview; never a runtime authorization adapter.

See docs/design/CATEGORIES_MIGRATION_REPORT.md for the conversion and limits.
Only the database reader performs I/O. Neither evaluator reads live state.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from gateway import category_policy as category
from gateway.policy import AppProfile, CapabilitySelector, PolicyDecision, PolicyRequest, evaluate
from gateway.profiles import load_profile


# These are legacy selector field names, not engine or category meanings.
SELECTORS = (
    ("tags", "capability_tags"), ("exposure", "capability_exposure"),
    ("trust_tiers", "capability_trust_tier"),
    ("resource_tiers", "capability_resource_tier"),
)


@dataclass(frozen=True)
class Case:
    label: str
    request: PolicyRequest
    service_id: str
    root_id: str
    phase: str = "dispatch"
    fixture: str = ""

    @property
    def grant(self) -> category.Grant:
        # Bind every non-category authorization input, including for caller-
        # supplied cases that reuse the same synthetic service/root IDs.
        digest = _context_digest(self.request)
        service = self.service_id.removesuffix("@report") + f"-{digest}@report"
        root = self.root_id.removesuffix("@report") + f"-{digest}@report"
        kind = "workflow" if self.request.workflow is not None else "release"
        return category.Grant(self.request.intent, service, **{kind: root})


def _context_digest(request: PolicyRequest) -> str:
    context = (request.backend, request.model, request.workflow, request.protocol,
               request.remote_provider, request.requires_cloud_fallback,
               request.remote_paid_fallback, request.native_passthrough,
               request.native_backend, request.native_operation)
    return hashlib.sha256(json.dumps(context).encode()).hexdigest()[:16]


def _permission_requests(request: PolicyRequest) -> list[PolicyRequest]:
    """Bounded modes, not a Cartesian product of arbitrary native operations."""
    variants = [request, replace(request, requires_cloud_fallback=True),
                replace(request, native_passthrough=True, native_backend=request.backend,
                        native_operation="POST /generate")]
    if request.remote_provider is not None:
        variants += [replace(request, remote_paid_fallback=True),
                     replace(request, requires_cloud_fallback=True, remote_paid_fallback=True)]
    return variants


@dataclass(frozen=True)
class Conversion:
    plan: category.Plan
    categories: Mapping[str, category.Category]
    grant_index: frozenset[category.Grant]


@dataclass(frozen=True)
class Comparison:
    case: Case
    exact: PolicyDecision
    gated: PolicyDecision
    v3: category.Decision


@dataclass(frozen=True)
class PlanReport:
    plan_id: str
    comparisons: tuple[Comparison, ...]
    gate_changes: int
    v3_changes: int
    gate_examples: tuple[Comparison, ...]
    v3_examples: tuple[Comparison, ...]


def _without_categories(profile: AppProfile) -> AppProfile:
    return replace(profile, allow=CapabilitySelector(intents=profile.allow.intents))


def convert_plan(profile: AppProfile, matrix: list[Case]) -> Conversion:
    """Materialize non-category permission as exact finite grants, never wildcards.

    Category values are the union of configured and observed values. Every
    restricted selector becomes one universal require-in row without a waiver.
    Synthetic IDs denote this report's snapshot, not real reviewed revisions.
    """
    grants = []
    seen_grants = set()
    categories = {}
    requirements = []
    for selector, request_field in SELECTORS:
        allowed = getattr(profile.allow, selector)
        values = set(allowed or ())
        for case in matrix:
            observed = getattr(case.request, request_field)
            if observed is not None:
                values.update(observed if selector == "tags" else (observed,))
        if selector == "tags":
            values.update(("migration-allowed", _disallowed_tag(profile)))
        category_id = "legacy-" + selector
        categories[category_id] = category.Category(category_id, category_id,
                                                    values=tuple(sorted(values)))
        if allowed is not None:
            requirements.append(category.Row(selector, category_id, "in", tuple(sorted(allowed))))
    grant_profile = _without_categories(profile)
    for case in matrix:
        if evaluate(grant_profile, case.request).allowed:
            grant = case.grant
            if grant not in seen_grants:
                grants.append(grant)
                seen_grants.add(grant)
    return Conversion(category.Plan(profile.id, tuple(grants), tuple(requirements)),
                      categories, frozenset(seen_grants))


def _assertions(request: PolicyRequest) -> tuple[category.Assertion, ...]:
    assertions = []
    for selector, field in SELECTORS:
        value = getattr(request, field)
        values = (value or ()) if selector == "tags" else (() if value is None else (value,))
        for item in sorted(values):
            assertions.append(category.Assertion("legacy-" + selector, item, "owner",
                              True, "report", "report", "report-owner", "report-v1"))
    return tuple(assertions)


def compare(profile: AppProfile, converted: Conversion, case: Case) -> Comparison:
    exact = evaluate(profile, case.request)
    gated = exact
    if profile.allow.tags is not None and not case.request.capability_tags:
        gated = PolicyDecision("denied", ("migration:completeness",), ("METADATA_INCOMPLETE",))
    kind = "workflow" if case.request.workflow is not None else "release"
    assertions = _assertions(case.request)
    if case.fixture == "unknown-category":
        assertions += (category.Assertion("unknown-category", "unknown", "owner", True,
                       "report", "report", "report-owner", "report-v1"),)
    grant = case.grant
    root = category.Subject(grant[kind], kind, assertions)
    members = (root,)
    if case.fixture in {"route-change", "helper", "retry", "cache", "release", "retrieval",
                        "download", "share", "action"}:
        # The recorded root remains allowed; the next effect adds a disallowed
        # subject to the bundle, which must also satisfy every requirement.
        disallowed = _disallowed_tag(profile)
        extra = category.Subject("next-effect@report", "route", (
            category.Assertion("legacy-tags", disallowed, "owner", True,
                               "report", "report", "report-owner", "report-v1"),))
        members += (extra,)
    service = category.Service(grant.service, members)
    # Every generated selector is exact and valid. Nonmatching grants cannot
    # affect this request; project them out before the kernel's validation so
    # a matrix with thousands of grants does not require quadratic evaluation.
    person_plan = app_plan = replace(converted.plan,
        grants=(grant,) if grant in converted.grant_index else ())
    if case.fixture in {"person-mismatch", "empty-grants"}:
        person_plan = replace(person_plan, grants=())
    if case.fixture == "app-mismatch":
        app_plan = replace(app_plan, grants=())
    decision = category.evaluate(converted.categories, [person_plan], [app_plan],
                                 service, case.request.intent, phase=case.phase)
    return Comparison(case, exact, gated, decision)


def _disallowed_tag(profile: AppProfile) -> str:
    disallowed = "migration-disallowed"
    while disallowed in (profile.allow.tags or ()):
        disallowed += "-other"
    return disallowed


def adversarial_cases(profile: AppProfile, base: Case) -> list[Case]:
    allowed = sorted(profile.allow.tags or ())
    allowed_tag = allowed[0] if allowed else "migration-allowed"
    disallowed = _disallowed_tag(profile)
    variants = (
        ("missing-tags", None), ("none-tags", None), ("empty-tags", frozenset()),
        ("allowed-tags", frozenset({allowed_tag})),
        ("disallowed-tags", frozenset({disallowed})),
        ("mixed-tags", frozenset({allowed_tag, disallowed})),
        ("multiple-assertions", frozenset({allowed_tag, disallowed})),
    )
    cases = [replace(base, label=label, request=replace(base.request, capability_tags=tags))
             for label, tags in variants]
    request = replace(base.request, capability_tags=frozenset({allowed_tag}))
    for fixture in ("unknown-category", "person-mismatch", "app-mismatch", "empty-grants",
                    "route-change", "helper", "retry", "cache", "release", "retrieval",
                    "download", "share", "action"):
        phase = fixture if fixture in category.BOUNDARIES else "dispatch"
        cases.append(replace(base, label=fixture, request=request, phase=phase, fixture=fixture))
    # Isolate these controls from category completeness differences. Earlier
    # protocol/model/provider restrictions can still mask a permission branch.
    for index, variant in enumerate(_permission_requests(request)):
        cases.append(replace(base, label=f"permission-mode-{index}", request=variant))
    native = profile.native_passthrough
    backend = sorted(native.allowed_backends)[0] if native.allowed_backends else request.backend
    operation = sorted(native.allowed_operations)[0] if native.allowed_operations else "POST /generate"
    if operation == "*":
        operation = "POST /migration-sample"
    elif operation.endswith(" *"):
        operation = operation[:-1] + "/migration-sample"
    else:
        operation = operation.replace("*", "migration-sample")
    candidate = replace(request, native_passthrough=True,
                        native_backend=backend, native_operation=operation)
    cases.append(replace(base, label="permission-native-configured", request=candidate))
    denied_backend = "migration-other-backend"
    while denied_backend in native.allowed_backends:
        denied_backend += "-other"
    denied_operation = "POST /migration-other-operation"
    while denied_operation in native.allowed_operations:
        denied_operation += "-other"
    cases.append(replace(base, label="permission-native-other-backend",
                         request=replace(candidate, native_backend=denied_backend)))
    cases.append(replace(base, label="permission-native-other-operation",
                         request=replace(candidate, native_operation=denied_operation)))
    return cases


def report_plan(profile: AppProfile, matrix: list[Case], *, examples: int = 3) -> PlanReport:
    if examples < 0:
        raise ValueError("examples must be nonnegative")
    cases = list(matrix)
    if cases:
        base = next((c for c in cases if evaluate(_without_categories(profile), c.request).allowed),
                    cases[0])
        cases += adversarial_cases(profile, base)
    converted = convert_plan(profile, cases)
    comparisons = tuple(compare(profile, converted, c) for c in cases)
    gate = tuple(c for c in comparisons if c.exact.allowed != c.gated.allowed)
    v3 = tuple(c for c in comparisons if c.exact.allowed != (c.v3.outcome == "allow"))
    return PlanReport(profile.id, comparisons, len(gate), len(v3), gate[:examples], v3[:examples])


def open_database(path: Path) -> sqlite3.Connection:
    """mode=ro prevents writes and prevents creation of a missing database."""
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def load_database(conn: sqlite3.Connection) -> tuple[list[AppProfile], dict]:
    # Do not use admin_store: its connection initializes/migrates the database.
    profiles = [replace(load_profile(json.loads(doc), default_id=identifier), id=identifier)
                for identifier, doc in conn.execute("SELECT id, profile_json FROM profiles ORDER BY id")]
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key=?",
                       ("service_registry",)).fetchone()
    if row is None:
        raise ValueError("service registry is missing")
    return profiles, json.loads(row[0])


def registry_matrix(registry: Mapping) -> list[Case]:
    """All declared capabilities x model/workflow targets x frontend protocols.

    Services without enumerated models contribute a service-only target.
    Metadata is taken only from records; absence is never guessed.
    """
    capabilities = set()
    protocols = set()
    targets = []
    records = registry.get("capabilities", {}).values()
    for record in records:
        if record.get("intent"):
            capabilities.add(record["intent"])
    for frontend in registry.get("protocol_frontends", {}).values():
        capabilities.update(frontend.get("intents", []))
        if frontend.get("protocol"):
            protocols.add(frontend["protocol"])
    for service_id, service in sorted(registry.get("services", {}).items()):
        capabilities.update(service.get("capabilities", []))
        config = service.get("config", {})
        models = config.get("models", [])
        provider = config.get("provider_id")
        for model in models:
            metadata = model.get("model_metadata", {})
            targets.append((service_id, model["id"], None, metadata, provider))
            alias = model.get("model_group")
            if alias and alias != model["id"]:
                targets.append((service_id, alias, None, metadata, provider))
        if not models:
            targets.append((service_id, None, None, config.get("model_metadata", {}), provider))
    for workflow_id, workflow in sorted(registry.get("workflows", {}).items()):
        manifest = workflow.get("manifest", {})
        summary = workflow.get("summary", {})
        intent = summary.get("intent") or manifest.get("intent")
        if intent:
            capabilities.add(intent)
        backends = set(summary.get("supported_backends", [])) | set(manifest.get("bindings", {}))
        if not backends:
            raise ValueError("workflow has no backend bindings")
        metadata = {field: summary.get(field, manifest.get(field)) for field in
                    ("tags", "exposure", "trust_tier", "resource_tier")}
        for backend in sorted(backends):
            service = registry.get("services", {}).get(backend, {})
            provider = service.get("config", {}).get("provider_id")
            targets.append((backend, None, workflow_id, metadata, provider))
    # Capability records may carry targets not enumerated in service config.
    for record in records:
        targets.append((record.get("backend"), record.get("model"), record.get("workflow"),
                        record, record.get("remote_provider")))
    cases = []
    seen = set()
    for capability in sorted(capabilities):
        for backend, model, workflow, metadata, provider in targets:
            for protocol in sorted(protocols) if protocols else (None,):
                request = PolicyRequest(protocol=protocol, intent=capability, model=model,
                    workflow=workflow, backend=backend, remote_provider=provider,
                    capability_tags=metadata.get("tags"),
                    capability_exposure=metadata.get("exposure"),
                    capability_trust_tier=metadata.get("trust_tier"),
                    capability_resource_tier=metadata.get("resource_tier"))
                for variant in _permission_requests(request):
                    if variant in seen:
                        continue
                    seen.add(variant)
                    digest = _context_digest(variant)
                    root = "workflow" if workflow is not None else "release"
                    cases.append(Case(f"matrix-{len(cases) + 1}", variant,
                                      f"service-{digest}@report", f"{root}-{digest}@report"))
    return cases


@dataclass(frozen=True)
class Callsite:
    path: str
    line: int
    function: str
    routes: tuple[str, ...]
    metadata: str


def scan_callsites(root: Path) -> list[Callsite]:
    """AST scan; dynamic data is unresolved, never claimed complete."""
    paths = [root / "proxy.py", *sorted((root / "gateway").glob("*.py"))]
    sites = []

    class Visitor(ast.NodeVisitor):
        function = "<module>"
        routes = ()

        def visit_FunctionDef(self, node):
            previous = self.function, self.routes
            self.function = node.name
            self.routes = tuple(d.args[0].value for d in node.decorator_list
                if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant)
                and isinstance(d.args[0].value, str) and d.args[0].value.startswith("/"))
            self.generic_visit(node)
            self.function, self.routes = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else "")
            if name in {"make_policy_request", "PolicyRequest"}:
                keyword = next((k for k in node.keywords if k.arg == "capability_tags"), None)
                metadata = "omitted"
                if keyword:
                    value = keyword.value
                    metadata = "dynamic"
                    if isinstance(value, ast.Constant) and value.value is None:
                        metadata = "none"
                    elif isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                        metadata = "present" if value.elts else "empty"
                    elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in {"set", "frozenset"}:
                        if not value.args or (isinstance(value.args[0], (ast.List, ast.Tuple, ast.Set)) and not value.args[0].elts):
                            metadata = "empty"
                elif any(k.arg is None for k in node.keywords) or node.args:
                    metadata = "dynamic"
                sites.append(Callsite(str(path.relative_to(root)), node.lineno,
                                      self.function, self.routes, metadata))
            self.generic_visit(node)

    for path in paths:
        if path.exists() and path.name != "category_migration.py":
            Visitor().visit(ast.parse(path.read_text()))
    return sites
