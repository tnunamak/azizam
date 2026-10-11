"""Offline migration comparisons use the real legacy and category evaluators."""

import json
import runpy
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from gateway.category_migration import (
    Case, compare, convert_plan, load_database, open_database, registry_matrix,
    report_plan, scan_callsites,
)
from gateway.policy import (
    AppProfile, CapabilitySelector, FallbackPolicy, NativePassthroughPolicy,
    PolicyRequest, RemoteProviderPolicy,
)
from gateway import category_policy as category


def case(**kwargs):
    return Case("fixture", PolicyRequest(intent="chat", model="model", **kwargs),
                "service@report", "model@report")


@pytest.fixture
def restricted():
    return AppProfile(id="p", allow=CapabilitySelector(tags=frozenset({"allowed"})))


@pytest.mark.parametrize("mode", ["native", "cloud", "paid"])
@pytest.mark.parametrize("allowed", [False, True])
def test_non_category_modes_do_not_reuse_regular_grants(mode, allowed):
    profile = AppProfile(id="p", remote=RemoteProviderPolicy(providers=frozenset({"paid"}),
        allow_paid_fallback=allowed), fallback=FallbackPolicy(cloud_allowed=allowed),
        native_passthrough=NativePassthroughPolicy(enabled=allowed,
            allowed_backends=frozenset({"backend"}), allowed_operations=frozenset({"POST /generate"})))
    regular = case(backend="backend", remote_provider="paid")
    changes = {"native": {"native_passthrough": True, "native_backend": "backend",
                          "native_operation": "POST /generate"},
               "cloud": {"requires_cloud_fallback": True},
               "paid": {"remote_paid_fallback": True}}[mode]
    variant = replace(regular, request=replace(regular.request, **changes))
    converted = convert_plan(profile, [regular, variant])
    assert compare(profile, converted, regular).v3.outcome == "allow"
    result = compare(profile, converted, variant)
    assert result.exact.allowed == allowed
    assert (result.v3.outcome == "allow") == allowed
    if allowed:
        assert len(converted.plan.grants) == 2
    else:
        assert result.v3.reason == "NO_GRANT"


@pytest.mark.parametrize("changes", [
    {"native_backend": "other"}, {"native_operation": "POST /other"},
])
def test_native_backend_and_operation_are_part_of_grant_identity(changes):
    profile = AppProfile(id="p", native_passthrough=NativePassthroughPolicy(enabled=True,
        allowed_backends=frozenset({"backend"}), allowed_operations=frozenset({"POST /generate"})))
    allowed = case(backend="backend", native_passthrough=True,
                   native_backend="backend", native_operation="POST /generate")
    denied = replace(allowed, request=replace(allowed.request, **changes))
    converted = convert_plan(profile, [allowed, denied])
    assert compare(profile, converted, allowed).v3.outcome == "allow"
    result = compare(profile, converted, denied)
    assert not result.exact.allowed and result.v3.reason == "NO_GRANT"


def test_matrix_and_report_sample_native_and_fallback_controls():
    registry = {"services": {"backend": {"capabilities": ["chat"], "config": {
        "provider_id": "paid", "models": [{"id": "m"}]}}}}
    matrix = registry_matrix(registry)
    assert any(c.request.native_passthrough for c in matrix)
    assert any(c.request.requires_cloud_fallback for c in matrix)
    assert any(c.request.remote_paid_fallback for c in matrix)
    assert len({(c.service_id, c.root_id) for c in matrix}) == len(matrix)
    for enabled in (False, True):
        profile = AppProfile(id="p", remote=RemoteProviderPolicy(providers=frozenset({"paid"}),
            allow_paid_fallback=enabled), fallback=FallbackPolicy(cloud_allowed=enabled),
            native_passthrough=NativePassthroughPolicy(enabled=enabled,
                allowed_backends=frozenset({"backend"}), allowed_operations=frozenset({"POST /generate"})))
        report = report_plan(profile, matrix)
        permission_cases = [c for c in report.comparisons if c.case.label.startswith("permission-")]
        assert permission_cases
        assert any(not c.exact.allowed for c in permission_cases)
        if enabled:
            assert any(c.exact.allowed for c in permission_cases)
        assert all(c.exact.allowed == (c.v3.outcome == "allow") for c in permission_cases)


@pytest.mark.parametrize("tags,expected", [
    (None, (True, False, False)),
    (frozenset(), (False, False, False)),
    (frozenset({"allowed"}), (True, True, True)),
    (frozenset({"other"}), (False, False, False)),
    (frozenset({"allowed", "other"}), (True, True, False)),
])
def test_exact_gate_and_universal_category_values(restricted, tags, expected):
    request = case(capability_tags=tags)
    converted = convert_plan(restricted, [request])
    result = compare(restricted, converted, request)
    assert (result.exact.allowed, result.gated.allowed, result.v3.outcome == "allow") == expected
    if tags is None:
        assert result.gated.reasons == ("METADATA_INCOMPLETE",)
        assert result.v3.reason == "REQUIRE_MISSING"
    if tags == frozenset({"allowed", "other"}):
        assert result.v3.reason == "REQUIRE_MISMATCH"


def test_unrestricted_plan_does_not_need_tags():
    profile = AppProfile(id="p")
    request = case()
    result = compare(profile, convert_plan(profile, [request]), request)
    assert result.exact.allowed and result.gated.allowed and result.v3.outcome == "allow"


@pytest.mark.parametrize("fixture,reason", [
    ("unknown-category", "POLICY_UNAVAILABLE"),
    ("person-mismatch", "NO_GRANT"),
    ("app-mismatch", "NO_GRANT"),
    ("empty-grants", "NO_GRANT"),
    ("route-change", "REQUIRE_MISMATCH"),
    ("helper", "REQUIRE_MISMATCH"),
    ("retry", "REQUIRE_MISMATCH"),
    ("cache", "REQUIRE_MISMATCH"),
    ("release", "REQUIRE_MISMATCH"),
    ("retrieval", "REQUIRE_MISMATCH"),
    ("download", "REQUIRE_MISMATCH"),
    ("share", "REQUIRE_MISMATCH"),
    ("action", "REQUIRE_MISMATCH"),
])
def test_report_adversarial_fixtures(restricted, fixture, reason):
    base = case(capability_tags=frozenset({"allowed"}))
    report = report_plan(restricted, [base], examples=100)
    result = next(r for r in report.comparisons if r.case.label == fixture)
    assert result.exact.allowed and result.gated.allowed
    assert result.v3.outcome == "deny" and result.v3.reason == reason


@pytest.mark.parametrize("changes", [
    {"models": frozenset()}, {"workflows": frozenset()},
    {"allow": CapabilitySelector(intents=frozenset())},
    {"backends": frozenset()}, {"protocols": frozenset()},
    {"remote": RemoteProviderPolicy()},
])
def test_grants_retain_non_tag_denials(changes):
    profile = replace(AppProfile(id="p", remote=RemoteProviderPolicy(providers=frozenset({"paid"}))), **changes)
    request = Case("denied", PolicyRequest(protocol="openai", intent="chat", model="model",
                   workflow="workflow", backend="backend", remote_provider="paid"),
                   "service@report", "workflow@report")
    result = compare(profile, convert_plan(profile, [request]), request)
    assert not result.exact.allowed and not result.gated.allowed
    assert result.v3.reason == "NO_GRANT"


@pytest.mark.parametrize("selector,field", [
    ("exposure", "capability_exposure"), ("trust_tiers", "capability_trust_tier"),
    ("resource_tiers", "capability_resource_tier"),
])
def test_scalar_selectors_convert_to_categories(selector, field):
    profile = AppProfile(id="p", allow=CapabilitySelector(**{selector: frozenset({"allowed"})}))
    missing = case()
    allowed = replace(missing, request=replace(missing.request, **{field: "allowed"}))
    denied = replace(missing, request=replace(missing.request, **{field: "other"}))
    converted = convert_plan(profile, [missing, allowed, denied])
    assert compare(profile, converted, missing).v3.reason == "REQUIRE_MISSING"
    assert compare(profile, converted, missing).gated.allowed
    assert compare(profile, converted, allowed).v3.outcome == "allow"
    assert compare(profile, converted, denied).v3.reason == "REQUIRE_MISMATCH"


def test_empty_tag_allowlist_does_not_convert_to_unrestricted():
    profile = AppProfile(id="p", allow=CapabilitySelector(tags=frozenset()))
    missing = case()
    result = compare(profile, convert_plan(profile, [missing]), missing)
    assert result.exact.allowed and not result.gated.allowed
    assert result.v3.reason == "REQUIRE_MISSING"


def test_example_limit_and_counts(restricted):
    report = report_plan(restricted, [case()], examples=1)
    assert report.gate_changes == 3  # matrix, missing and explicit None
    assert report.v3_changes == 18
    assert len(report.gate_examples) == len(report.v3_examples) == 1


def test_all_allowed_multiple_assertions():
    profile = AppProfile(id="p", allow=CapabilitySelector(tags=frozenset({"a", "b"})))
    request = case(capability_tags=frozenset({"a", "b"}))
    result = compare(profile, convert_plan(profile, [request]), request)
    assert result.exact.allowed and result.gated.allowed and result.v3.outcome == "allow"


def test_projection_matches_full_plan(restricted):
    allowed = case(capability_tags=frozenset({"allowed"}))
    other = replace(allowed, service_id="other@report", root_id="other-model@report")
    converted = convert_plan(restricted, [allowed, other])
    assert len(converted.plan.grants) == 2
    assertion = category.Assertion("legacy-tags", "allowed", "owner", True,
                                   "report", "report", "report-owner", "report-v1")
    for request in (allowed, other, replace(other, service_id="ungranted@report")):
        service = category.Service(request.grant.service,
            (category.Subject(request.grant.release, "release", (assertion,)),))
        full = category.evaluate(converted.categories, [converted.plan], [converted.plan], service, "chat")
        assert compare(restricted, converted, request).v3 == full


def test_registry_provider_and_alias_contexts():
    registry = {"services": {"s": {"capabilities": ["chat"], "config": {
        "provider_id": "paid", "models": [{"id": "m", "model_group": "alias"}]}}},
        "protocol_frontends": {"f": {"protocol": "openai.chat", "intents": ["chat"]}}}
    matrix = registry_matrix(registry)
    assert len(matrix) == 10  # Two targets x five bounded permission modes.
    assert {c.request.model for c in matrix} == {"m", "alias"}
    assert {c.request.remote_provider for c in matrix} == {"paid"}
    assert len({c.service_id for c in matrix}) == 10
    denied = AppProfile(id="p")
    assert all(not compare(denied, convert_plan(denied, matrix), c).exact.allowed for c in matrix)
    permitted = replace(denied, remote=RemoteProviderPolicy(providers=frozenset({"paid"})))
    converted = convert_plan(permitted, matrix)
    for c in matrix:
        result = compare(permitted, converted, c)
        regular = not (c.request.requires_cloud_fallback or c.request.remote_paid_fallback
                       or c.request.native_passthrough)
        assert result.exact.allowed == regular
        assert (result.v3.outcome == "allow") == regular


def test_registry_conversion_has_one_active_revision_per_service_stem():
    registry = {"services": {"s": {"capabilities": ["chat", "image"], "config": {
        "models": [{"id": "m", "model_group": "alias", "model_metadata": {"tags": ["allowed"]}}]}}},
        "protocol_frontends": {
            "chat": {"protocol": "openai.chat", "intents": ["chat"]},
            "image": {"protocol": "openai.images", "intents": ["image"]}}}
    matrix = registry_matrix(registry)
    assert len(matrix) == 24  # Two capabilities x two targets x two protocols x three modes.
    profile = AppProfile(id="p", allow=CapabilitySelector(tags=frozenset({"allowed"})))
    converted = convert_plan(profile, matrix)
    assertion = category.Assertion("legacy-tags", "allowed", "owner", True,
                                   "report", "report", "report-owner", "report-v1")
    services = {c.grant.service: category.Service(c.grant.service,
                (category.Subject(c.grant.release, "release", (assertion,)),)) for c in matrix}
    assert len(services) == 12  # Capabilities share the same pinned permission context.
    version = category.PolicyVersion("report-v1", converted.categories,
        {"person": (converted.plan,)}, {"app": (converted.plan,)}, services)
    category.validate_snapshot(version)
    for request in matrix:
        expected = "deny" if (request.request.requires_cloud_fallback or request.request.native_passthrough) else "allow"
        assert category.decision_for(version, {"person": "person", "app": "app",
            "service": request.grant.service, "capability": request.request.intent}).outcome == expected


def test_normalized_workflow_bindings_enforce_backend_lists():
    registry = {"workflows": {"w": {"id": "w", "manifest": {
        "intent": "image", "bindings": {"s": {}, "other": {}}},
        "summary": {"intent": "image", "tags": ["allowed"],
                    "supported_backends": ["other", "s"],
                    "bindings": [{"backend": "other"}, {"backend": "s"}]}}}}
    matrix = registry_matrix(registry)
    assert {c.request.backend for c in matrix} == {"s", "other"}
    profile = AppProfile(id="p", backends=frozenset())
    converted = convert_plan(profile, matrix)
    assert converted.plan.grants == ()
    assert all(not compare(profile, converted, c).exact.allowed for c in matrix)
    permitted = replace(profile, backends=frozenset({"s"}))
    converted = convert_plan(permitted, matrix)
    for request in matrix:
        result = compare(permitted, converted, request)
        assert result.exact.allowed == (request.request.backend == "s"
            and not request.request.requires_cloud_fallback and not request.request.native_passthrough)
        assert (result.v3.outcome == "allow") == result.exact.allowed


def test_scope_family_and_none_vs_empty_grants():
    request = case(capability_tags=frozenset({"allowed"}))
    request = replace(request, request=replace(request.request, intent="chat.complete"))
    profile = AppProfile(id="p", allow=CapabilitySelector(intents=frozenset({"chat"})))
    assert compare(profile, convert_plan(profile, [request]), request).v3.outcome == "allow"
    denied = replace(profile, models=frozenset())
    assert convert_plan(denied, [request]).plan.grants == ()


def database(tmp_path):
    path = tmp_path / "admin ?#.db"
    registry = {
        "services": {"s": {"id": "s", "capabilities": ["chat", "image"],
            "config": {"models": [{"id": "m", "model_metadata": {"tags": ["allowed"]}}]}}},
        "workflows": {"w": {"manifest": {"id": "w", "intent": "image", "bindings": {"s": {}}}}},
        "capabilities": {}, "protocol_frontends": {},
    }
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE profiles(id TEXT, name TEXT, profile_json TEXT);"
                           "CREATE TABLE operator_settings(key TEXT, value_json TEXT);")
        conn.execute("INSERT INTO profiles VALUES (?, ?, ?)",
                     ("p", "PRIVATE PERSON", json.dumps({"allow": {"tags": ["allowed"]}})))
        conn.execute("INSERT INTO operator_settings VALUES (?, ?)",
                     ("service_registry", json.dumps(registry)))
    return path, registry


def test_database_is_read_only_and_queries_only_policy_fields(tmp_path):
    path, _ = database(tmp_path)
    before = path.read_bytes()
    statements = []
    with open_database(path) as conn:
        conn.set_trace_callback(statements.append)
        profiles, registry = load_database(conn)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM profiles")
    assert profiles[0].id == "p"
    assert len(registry_matrix(registry)) == 12
    assert not any("name" in s.lower() for s in statements)
    assert path.read_bytes() == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        open_database(path)
    assert not path.exists()


def test_cli_private_output_and_deterministic_counts(tmp_path):
    path, _ = database(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts/categories-migration-report"
    cmd = [sys.executable, str(script), str(path), "--examples", "0"]
    before = path.read_bytes()
    first = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    assert first == subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    assert "PRIVATE PERSON" not in first
    assert "plan_id" in first and "1->2" in first and "1->3" in first
    assert "proxy.py:" in first and "static" in first.lower()
    assert path.read_bytes() == before


def test_cli_connection_rejects_writes(tmp_path, monkeypatch, capsys):
    path, _ = database(tmp_path)
    connect = sqlite3.connect
    connections = []

    def checked_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM profiles")
        connections.append(conn)
        return conn

    script = Path(__file__).resolve().parents[1] / "scripts/categories-migration-report"
    monkeypatch.setattr(sqlite3, "connect", checked_connect)
    monkeypatch.setattr(sys, "argv", [str(script), str(path), "--examples", "0"])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(script), run_name="__main__")
    assert result.value.code == 0
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    assert "plans=1" in capsys.readouterr().out


def test_static_scan_preserves_missing_none_empty_and_dynamic(tmp_path):
    source = tmp_path / "proxy.py"
    source.write_text('''
@app.post("/v1/chat/completions")
def chat():
    make_policy_request(protocol="openai")
    PolicyRequest(capability_tags=None)
    PolicyRequest(capability_tags=[])
    PolicyRequest(capability_tags=tags)
    make_policy_request(**kwargs)
''')
    calls = scan_callsites(tmp_path)
    assert [c.metadata for c in calls] == ["omitted", "none", "empty", "dynamic", "dynamic"]
    assert calls[0].function == "chat" and calls[0].routes == ("/v1/chat/completions",)
