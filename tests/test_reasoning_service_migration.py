"""D-1 byte oracle: actual dispatch with the pre-migration preparer vs v5 data.

The frozen preparer is verbatim from 5f7ddc4bf4ce38c0575999c8a486dbf347bf6fca.
Fixtures mirror both local chat service kinds in the private registry snapshot;
connection/model names are synthetic. Both kinds are exercised when enabled.
"""
import copy
import json
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
import pytest

import proxy
from tests.support.access import authenticated  # noqa: E402
from gateway import admin_store, service_registry
from gateway.transforms import REGISTRY, TransformContext, run_request_pipeline

FIXTURES = Path(__file__).parent / "fixtures" / "d1"
OLD_TRANSFORM_NAMESPACE = {}
exec(compile((FIXTURES / "reasoning_before.py").read_text(), "d1-transform-before-5f7ddc4b", "exec"), OLD_TRANSFORM_NAMESPACE)
BEFORE_NAMESPACE = dict(vars(proxy), _ReasoningEffortTransform=OLD_TRANSFORM_NAMESPACE["ReasoningEffortTransform"])
# Explicit attachments must use the old implementation too, not today's registry.
OLD_REGISTRY = copy.deepcopy(REGISTRY)
OLD_REGISTRY.register_builtin("reasoning-effort", OLD_TRANSFORM_NAMESPACE["ReasoningEffortTransform"]())
BEFORE_NAMESPACE["_TRANSFORM_REGISTRY"] = OLD_REGISTRY
exec(compile((FIXTURES / "prepare_before.py").read_text(), "d1-before-5f7ddc4b", "exec"), BEFORE_NAMESPACE)
BEFORE = BEFORE_NAMESPACE["_prepare_resolved_openai_request"]
AFTER = proxy._prepare_resolved_openai_request


def legacy_database(tmp_path, registry):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE schema_version SET version=4")
        conn.execute("INSERT INTO operator_settings VALUES ('service_registry', ?, 123, 'fixture')",
                     (json.dumps(registry),))
        conn.execute("INSERT INTO operator_settings_generations VALUES ('service_registry', 7)")
    return db


def snapshot(db):
    with sqlite3.connect(db) as conn:
        raw = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0]
        generation = conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='service_registry'").fetchone()[0]
    return raw, generation


def fixture_registry(service_id, existing):
    service = json.loads((FIXTURES / f"{service_id}.json").read_text())
    service["enabled"] = True
    service["config"].update(host="127.0.0.1", port=5111, model_policy_schema_version=1)
    service["config"]["models"][1]["id"] = "current"
    service["config"]["models"][1]["policy_overlay"] = []
    if existing:
        service["config"]["transforms"] = ["llamacpp-cache-prompt", {
            "kind": "default-params", "set": {"temperature": 0.3},
        }]
        service["config"]["models"][0]["transforms"] = [
            {"kind": "default-params", "set": {"max_tokens": 73}},
        ]
        service["config"]["models"][1]["policy_overlay"] = [
            {"kind": "default-params", "set": {"reasoning_effort": "none"}},
        ]
    return {"services": {service_id: service}}


@pytest.mark.parametrize("service_id", ["kobold"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort", ["absent", None, "none", "minimal", "low", "medium", "high", "xhigh", "invalid", 42])
def test_upstream_bytes_equal_before_and_after(tmp_path, service_id, existing, stream, effort):
    old_registry = fixture_registry(service_id, existing)
    parsed = assert_upstream_bytes_equal(tmp_path, old_registry, service_id, stream, effort)
    if effort == "absent":
        assert parsed["thinking_budget_tokens"] == (
            0 if existing else proxy.config["reasoning"]["effort_budgets"]["high"]
        )


def assert_upstream_bytes_equal(tmp_path, old_registry, service_id, stream, effort):
    db = legacy_database(tmp_path, old_registry)
    admin_store.init_db(db)
    first = snapshot(db)
    admin_store.init_db(db)
    assert snapshot(db) == first
    assert first[1] == 8
    new_registry = json.loads(first[0])
    body = {"model": "current", "messages": [{"role": "user", "content": "héllo"}], "stream": stream}
    if effort != "absent":
        body["reasoning_effort"] = effort
    captured = []
    responses = []
    original_body = copy.deepcopy(body)

    async def send(request, **_kwargs):
        captured.append(request.body)
        response = httpx.Response(200, content=(
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
            if stream else b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        ), headers={"content-type": "text/event-stream" if stream else "application/json"})
        return httpx.AsyncClient(), response

    for registry, prepare in [(old_registry, BEFORE), (new_registry, AFTER)]:
        runtime = service_registry.apply_service_registry_to_config(proxy.config, registry)
        BEFORE_NAMESPACE["config"] = runtime
        # The frozen function's helper was defined in proxy's module globals;
        # patching config makes it observe this same registry projection.
        with patch.object(proxy, "config", runtime), authenticated(), \
             patch.object(proxy, "_prepare_resolved_openai_request", prepare), \
             patch.object(proxy, "LLM_BACKEND_HOSTS", {service_id: "http://127.0.0.1:5111"}), \
             patch.object(proxy, "get_active_backend", AsyncMock(return_value=service_id)), \
             patch.object(proxy, "current_llm_model_for_backend", AsyncMock(return_value="fixture-model")), \
             patch.object(proxy, "_service_availability_response", return_value=None), \
             patch.object(proxy, "send_llm_execution_request", send):
            response = TestClient(proxy.app).post("/v1/chat/completions", json=body)
            expected_status = 400 if effort in ("invalid", 42) else 200
            assert response.status_code == expected_status, response.text
            responses.append(response.content)
    assert body == original_body
    if effort in ("invalid", 42):
        assert captured == []
        assert responses[0] == responses[1]
        return
    assert len(captured) == 2
    assert captured[0] == captured[1]
    parsed = json.loads(captured[1])
    assert "reasoning_effort" not in parsed
    return parsed


# Every distinct base/alias policy pair in the enabled local service from the
# private 2026-10-10 snapshot. Model/connection names are replaced by fixtures;
# the request-transform data is unchanged. Disabled services are not migrated.
CURRENT_POLICIES = json.loads((FIXTURES / "current_policies.json").read_text())


@pytest.mark.parametrize("service_id,policy", [
    (sid, policy)
    for sid, service in CURRENT_POLICIES.items() if service["enabled"]
    for policy in service["policies"]
])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort", ["absent", None, "none", "minimal", "low", "medium", "high", "xhigh", "invalid", 42])
def test_current_registry_policy_upstream_bytes(tmp_path, service_id, policy, stream, effort):
    registry = fixture_registry(service_id, False)
    cfg = registry["services"][service_id]["config"]
    cfg["transforms"] = CURRENT_POLICIES[service_id]["transforms"]
    cfg["models"][0]["transforms"] = policy["base"]
    cfg["models"][1]["policy_overlay"] = policy["alias"]
    assert_upstream_bytes_equal(tmp_path, registry, service_id, stream, effort)


@pytest.mark.parametrize("request_path,transforms", [
    ("/v1/chat/completions", ["reasoning-effort", {"kind": "drop-params", "drop": ["reasoning_effort"]}]),
    ("/v1/completions", ["reasoning-effort"]),
])
def test_explicit_attachment_pipeline_bytes_match_before(request_path, transforms):
    ctx = TransformContext({"request_path": request_path, "reasoning": {"default_effort": "high"}})
    before = run_request_pipeline(transforms, {"reasoning_effort": "low"}, ctx, OLD_REGISTRY)
    after = run_request_pipeline(transforms, {"reasoning_effort": "low"}, ctx)
    assert before == {"thinking_budget_tokens": 4096}
    assert json.dumps(after).encode() == json.dumps(before).encode()


@pytest.mark.parametrize("request_path", ["/v1/chat/completions", "/v1/completions"])
@pytest.mark.parametrize("following", [
    {"kind": "drop-params", "drop": ["reasoning_effort"]},
    {"kind": "drop-params", "drop": ["thinking_budget_tokens"]},
    {"kind": "default-params", "set": {"reasoning_effort": "none"}},
])
@pytest.mark.parametrize("effort", ["low", "absent", None, "invalid"])
def test_migrated_explicit_attachment_prepared_bytes(tmp_path, request_path, following, effort):
    old = fixture_registry("kobold", False)
    old["services"]["kobold"]["config"]["transforms"] = ["reasoning-effort", following]
    db = legacy_database(tmp_path, old)
    admin_store.init_db(db)
    new = json.loads(snapshot(db)[0])
    outcomes = []
    for registry, prepare in [(old, BEFORE), (new, AFTER)]:
        runtime = service_registry.apply_service_registry_to_config({"reasoning": {
            "default_effort": "high", "effort_budgets": {"high": 8192, "low": 1234, "none": 0},
        }}, registry)
        target = proxy._resolve_request_target("current", runtime_config=runtime)
        body = {"model": "current"}
        if effort != "absent":
            body["reasoning_effort"] = effort
        error, prepared, _ = prepare(body, request_path=request_path, target=target,
                                    execution_service_id="kobold", runtime_config=runtime)
        outcomes.append((error.status_code if error else None, error.body if error else None,
                         json.dumps(prepared).encode()))
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_ordering_upstream_bytes(tmp_path, stream):
    registry = fixture_registry("kobold", False)
    registry["services"]["kobold"]["config"]["transforms"] = [
        "reasoning-effort", {"kind": "drop-params", "drop": ["reasoning_effort"]},
    ]
    parsed = assert_upstream_bytes_equal(tmp_path, registry, "kobold", stream, "low")
    assert parsed["thinking_budget_tokens"] == 4096


def test_migration_scope_generation_and_opt_out(tmp_path):
    registry = fixture_registry("kobold", True)
    prototype = registry["services"].pop("kobold")
    changes = {
        "local": {}, "disabled": {"enabled": False},
        "remote": {"config": {"provider_type": "paid-remote", "api_key_env": "FIXTURE_REMOTE_KEY"}},
        "substrate": {"kind": "llm-substrate"},
        "audio": {"capabilities": ["tts"]},
        "no-endpoint": {"config": {"port": None}},
        "already": {"config": {"transforms": ["reasoning-effort"]}},
        "already-final": {"config": {"transforms": ["reasoning-effort-final-chat"]}},
        "completion-only": {"capabilities": ["completions"]},
    }
    for key, overrides in changes.items():
        service = copy.deepcopy(prototype)
        service["id"] = key
        service["source"] = "provisioned"
        service["config"].pop("models", None)
        service["config"].pop("model_policy_schema_version", None)
        config = overrides.pop("config", {})
        service.update(overrides)
        service["config"].update(config)
        registry["services"][key] = service
    original = copy.deepcopy(registry)
    db = legacy_database(tmp_path, registry)
    admin_store.init_db(db)
    raw, generation = snapshot(db)
    migrated = json.loads(raw)
    # One bump for the reasoning rows, one for the credential origin of the remote record (schema 8).
    assert generation == 9
    assert migrated["services"]["remote"]["config"].pop("credential_origin").startswith("http")
    for key in changes:
        expected = copy.deepcopy(original["services"][key])
        if key in {"local", "completion-only", "already"}:
            expected["config"]["transforms"].append("reasoning-effort-final-chat")
        assert migrated["services"][key] == expected
    migrated["services"]["local"]["config"]["transforms"] = []
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(migrated),))
    opted_out = snapshot(db)
    admin_store.init_db(db)
    assert snapshot(db) == opted_out
    # Normalization/backfill must not reattach after an operator removes it.
    runtime = {"backends": {"local": {**prototype["config"], "transforms": ["reasoning-effort-final-chat"]}}}
    assert service_registry.ensure_service_registry(runtime, path=db)["services"]["local"]["config"]["transforms"] == []
    with sqlite3.connect(db) as conn:
        value = json.loads(snapshot(db)[0])
        del value["services"]["local"]["config"]["transforms"]
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(value),))
    assert "transforms" not in service_registry.ensure_service_registry(runtime, path=db)["services"]["local"]["config"]


@pytest.mark.parametrize("request_path", ["/v1/chat/completions", "/v1/completions"])
@pytest.mark.parametrize("extra", [{"reasoning_effort": "invalid"}, {"reasoning_effort": 42},
                                  {"thinking_budget_tokens": -2}, {"thinking_budget_tokens": True},
                                  {"thinking_budget_tokens": 0}, {"thinking_budget_tokens": -1}])
def test_errors_and_nonchat_match_old_code(tmp_path, request_path, extra):
    old = fixture_registry("kobold", False)
    db = legacy_database(tmp_path, old)
    admin_store.init_db(db)
    new = json.loads(snapshot(db)[0])
    outcomes = []
    for registry, prepare in [(old, BEFORE), (new, AFTER)]:
        runtime = service_registry.apply_service_registry_to_config({"reasoning": {"default_effort": "high"}}, registry)
        target = proxy._resolve_request_target("current", runtime_config=runtime)
        error, prepared, _ = prepare({"model": "current", **extra}, request_path=request_path,
                                    target=target, execution_service_id="kobold", runtime_config=runtime)
        outcomes.append((error.status_code if error else None, error.body if error else None, json.dumps(prepared).encode()))
    assert outcomes[0] == outcomes[1]


def test_fresh_seed_declares_opt_in_as_data():
    import yaml
    config = yaml.safe_load((Path(__file__).parents[1] / "proxy_config.yml").read_text())
    seed = service_registry.build_service_registry_seed(config)
    for service_id in ("kobold",):
        assert "reasoning-effort-final-chat" in seed["services"][service_id]["config"]["transforms"]
    custom = service_registry.build_service_registry_seed({"backends": {"custom": {"capabilities": ["chat"], "port": 5111}}})
    assert "transforms" not in custom["services"]["custom"]["config"]


def _local_chat_definitions():
    from gateway.engine_definitions import load_engine_catalog

    catalog = load_engine_catalog()
    return [catalog.definition(i) for i in catalog.ids() if "chat" in catalog.definition(i)["capabilities"]]


def _new_engine_raw(definition, **values):
    from gateway.engine_admin import build_raw, clean_values
    from gateway.engine_definitions import load_engine_catalog

    clean = clean_values(definition, {"address": "http://127.0.0.1:5111/v1", **values})
    return build_raw(load_engine_catalog(), definition, clean, name="Local chat", capabilities=["chat"], models=[],
                     enabled=True, check={}, tests=None, secret_id=None)


@pytest.mark.parametrize("definition", _local_chat_definitions(), ids=lambda d: d["definition"])
def test_a_new_local_chat_engine_carries_the_overridable_reasoning_attachment(definition):
    """D-1: the reasoning transform is opt-in per service, and every local chat kind opts in by default (as data)."""
    assert definition["record"]["transforms"] == ["reasoning-effort-final-chat"]
    raw = _new_engine_raw(definition)
    assert raw["transforms"] == ["reasoning-effort-final-chat"]
    raw["transforms"].clear()
    assert definition["record"]["transforms"] == ["reasoning-effort-final-chat"], "the definition is not shared state"
    # The owner can remove it, and the removal is kept as an explicit empty list.
    assert _new_engine_raw(definition, transforms=[])["transforms"] == []
    record = service_registry.service_from_backend_config("local", _new_engine_raw(definition, transforms=[]), source="ui")
    assert record["config"]["transforms"] == []


def test_the_local_chat_kinds_cover_the_old_presets():
    """openai-compatible, vLLM, LM Studio (OpenAI-compatible) and Ollama (own model list) are all local chat kinds."""
    assert {d["definition"] for d in _local_chat_definitions()} == {"openai-compatible", "own-model-list"}


def test_a_raw_record_without_a_definition_attaches_no_reasoning():
    for form in ({"protocol": "comfyui"}, {}):
        assert "transforms" not in service_registry.service_form_to_raw(form)


def test_a_raw_form_that_names_a_preset_is_refused():
    with pytest.raises(ValueError, match="presets are gone"):
        service_registry.service_form_to_raw({"preset": "vllm", "base_url": "http://127.0.0.1:5111/v1"})


def test_migration_rolls_back_registry_generation_and_version_together(tmp_path):
    db = legacy_database(tmp_path, fixture_registry("kobold", False))
    before = snapshot(db)
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TRIGGER reject_migration BEFORE UPDATE ON operator_settings_generations
                     BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        admin_store.init_db(db)
    assert snapshot(db) == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        conn.execute("DROP TRIGGER reject_migration")
    admin_store.init_db(db)
    assert snapshot(db)[1] == before[1] + 1


def test_attached_translation_reads_updated_runtime_budgets():
    registry = fixture_registry("kobold", False)
    registry["services"]["kobold"]["config"]["transforms"] = ["reasoning-effort-final-chat"]
    runtime = service_registry.apply_service_registry_to_config({}, registry)
    target = proxy._resolve_request_target("current", runtime_config=runtime)
    for budget in (12345, 23456):
        runtime["reasoning"] = {"default_effort": "high", "effort_budgets": {"high": budget}}
        error, prepared, _ = AFTER({"model": "current"}, request_path="/v1/chat/completions",
                                   target=target, execution_service_id="kobold", runtime_config=runtime)
        assert error is None
        assert prepared["thinking_budget_tokens"] == budget
