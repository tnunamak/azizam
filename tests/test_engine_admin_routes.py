"""Add engine through the real routes: check, gate, keys, roles and the typed record.

Each test runs the gateway app behind a real sign-in (see test_signin_who_can_enter)
and checks against fake servers of the engine-probe protocol.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from gateway import admin_store, engine_admin, engine_check, secret_store, service_registry, transforms
from gateway.litellm_substrate import compile_litellm_config_from_registry
from gateway.provider_sidecar import provider_secret_env_map_from_registry, provider_secret_origin_map_from_registry
from tests.support.engine_dns import use_fake_engine_dns
from tests.support.fake_upstream import fake_upstreams, load_scenario, scenario_from_data  # noqa: F401 - fixture
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

TOKEN = "t0k3n"
KEY = "sk-local-demo-8f2a91"


@pytest.fixture(autouse=True)
def fixed_baseline(monkeypatch):
    monkeypatch.setattr(engine_check, "_baseline_token", lambda: TOKEN)


@pytest.fixture(autouse=True)
def fake_engine_dns(monkeypatch):
    """The check looks each name up once and refuses one that does not resolve: answer the fake hosts."""
    use_fake_engine_dns(monkeypatch)


def _serve(fakes, name, host=None, **values):
    return fakes.asgi(load_scenario(f"engine-probe/{name}", token=TOKEN, **values), host=host or f"{name}.fk.test")


def _silent(fakes, host):
    """A fake that expects nothing: any request to it fails the test at teardown."""
    scenario = scenario_from_data({"protocol": "engine-probe", "name": "silent", "exchanges": [],
                                   "provenance": {"source": "hand-written", "reference": "spec section 2 step 3: Add but keep off skips the check"}})
    return fakes.asgi(scenario, host=host)


def _write_headers(client):
    # A fresh database has no registry row yet: its generation is 0.
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def _create(client, body):
    return client.post("/admin/api/operator/services", headers=_write_headers(client), json=body)


def _edit(client, service_id, body):
    return client.patch(f"/admin/api/operator/services/{service_id}", headers=_write_headers(client), json=body)


def _registry(gateway):
    record = admin_store.get_service_registry_with_generation(path=gateway.path)
    return record["value"] if record else {"schema_version": 1, "services": {}}


def _put_registry(gateway, registry):
    record = admin_store.get_service_registry_with_generation(path=gateway.path)
    admin_store.write_service_registry_with_audit(
        registry, expected_generation=record["operator_settings_generation"] if record else 0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None, audit_target_kind=None,
        audit_target_id=None, path=gateway.path)


def _record(gateway, service_id):
    return _registry(gateway)["services"][service_id]


def _chat_server(fakes, host="chat.fk.test"):
    """A server that lists one model and validates an empty chat request (chat: Possible)."""
    return _serve(fakes, "chat-answers-real-test", host=host, chat_status=400, chat_reply={"error": {"message": "'messages' is required"}})


def test_an_owner_adds_an_engine_with_a_pasted_key_and_the_key_is_write_only(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    fake = _chat_server(fake_upstreams)
    created = _create(owner, {"definition": "openai-compatible", "name": "garage-gpu",
                              "values": {"address": f"{fake.base_url}/v1", "api_key": KEY},
                              "capabilities": ["chat"], "acknowledge_warnings": True})
    assert created.status_code == 201, created.text
    record = _record(gateway, "garage-gpu")
    config = record["config"]
    assert config["definition"] == "openai-compatible" and config["definition_version"] == 1
    secret_id = config["api_key_secret_id"]
    assert secret_id.startswith("engine:garage-gpu:api_key:")
    assert config["credential_origin"] == engine_check.origin_of(fake.base_url)
    # Stored encrypted, bound to the address it was entered for.
    assert secret_store.get_provider_secret(secret_id, db_path=gateway.path, expected_origin=config["credential_origin"]) == KEY
    with pytest.raises(PermissionError):
        secret_store.get_provider_secret(secret_id, db_path=gateway.path, expected_origin="http://elsewhere.fk.test:80")
    # Never shown again: not in the create answer, the registry, the engine view or the audit log.
    assert KEY not in created.text
    assert KEY not in owner.get("/admin/api/operator/service-registry").text
    view = owner.get("/admin/api/engines/garage-gpu")
    assert view.status_code == 200
    assert KEY not in view.text
    engine = view.json()["engine"]
    assert engine["key"]["state"] == "configured"
    assert engine["record"]["config"]["api_key_secret_id"] == "configured"
    assert KEY not in json.dumps(gateway.rows("SELECT * FROM audit_events"), default=str)
    # The check sent the pasted key to the address it was typed for.
    models = [r for r in fake.requests if r.path == "/v1/models"]
    assert models[0].headers.get("authorization") == f"Bearer {KEY}"


def test_a_pasted_key_reaches_the_execution_layer_by_reference_only(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    fake = _chat_server(fake_upstreams)
    assert _create(owner, {"definition": "openai-compatible", "name": "keyed",
                           "values": {"address": f"{fake.base_url}/v1", "api_key": KEY},
                           "capabilities": ["chat"], "models": [{"id": "qwen3-32b-instruct-q4_k_m", "public_name": "qwen3-32b"}],
                           "acknowledge_warnings": True}).status_code == 201
    registry = _registry(gateway)
    compiled = compile_litellm_config_from_registry(registry)
    entry = next(row for row in compiled["model_list"] if row["model_name"] == "qwen3-32b")
    reference = entry["litellm_params"]["api_key"]
    assert reference.startswith("os.environ/AZIZAM_PROVIDER_SECRET_")
    env_name = reference.removeprefix("os.environ/")
    secret_id = registry["services"]["keyed"]["config"]["api_key_secret_id"]
    assert provider_secret_env_map_from_registry(registry)[env_name] == secret_id
    assert provider_secret_origin_map_from_registry(registry)[env_name] == engine_check.origin_of(fake.base_url)
    assert KEY not in json.dumps(compiled)


def test_an_error_finding_blocks_create_and_nothing_is_saved(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    fake = _serve(fake_upstreams, "key-gated-routes")
    before = set(_registry(gateway)["services"])
    refused = _create(owner, {"definition": "openai-compatible", "name": "locked", "values": {"address": fake.base_url}})
    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert body["error"]["code"] == "engine_check_failed" and body["error"]["field"] == "key"
    assert body["check"]["findings"][0]["code"] == "needkey"
    assert set(_registry(gateway)["services"]) == before


def test_a_warning_blocks_once_and_acknowledge_warnings_adds_it(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    fake = _serve(fake_upstreams, "chat-route-without-model-list")
    body = {"definition": "openai-compatible", "name": "voice-box", "values": {"address": f"{fake.base_url}/v1"}}
    first = _create(owner, body)
    assert first.status_code == 409, first.text
    assert first.json()["error"]["code"] == "engine_check_warnings"
    assert "voice-box" not in _registry(gateway)["services"]
    second = _create(owner, {**body, "acknowledge_warnings": True})
    assert second.status_code == 201, second.text
    record = _record(gateway, "voice-box")
    # Only what the check marked Likely is ticked by default; a chat route that is only Possible is not.
    assert record["capabilities"] == []
    assert record["config"]["engine_check"]["status"] == "ok"


def test_add_but_keep_off_skips_the_check_and_sends_nothing(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "kept-off.fk.test")
    created = _create(owner, {"definition": "openai-compatible", "name": "attic", "disabled": True,
                              "values": {"address": "http://kept-off.fk.test/v1"}})
    assert created.status_code == 201, created.text
    record = _record(gateway, "attic")
    assert record["enabled"] is False
    assert record["config"]["engine_check"]["status"] == "not_started"
    assert record["capabilities"] == []


def test_a_duplicate_address_is_refused_even_when_kept_off(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "twice.fk.test")
    first = _create(owner, {"definition": "openai-compatible", "name": "kitchen", "disabled": True,
                            "values": {"address": "http://twice.fk.test/v1"}})
    assert first.status_code == 201
    second = _create(owner, {"definition": "openai-compatible", "name": "kitchen again", "disabled": True,
                             "values": {"address": "http://TWICE.fk.test:80/v1/"}})
    assert second.status_code == 409, second.text
    finding = second.json()["check"]["findings"][0]
    assert finding["code"] == "dup" and finding["action"] == {"id": "openexisting", "label": "Open kitchen", "service_id": "kitchen"}
    checked = owner.post("/admin/api/engines/check", headers=csrf_for(owner),
                         json={"definition": "openai-compatible", "values": {"address": "http://twice.fk.test/v1"}})
    assert checked.json()["check"]["findings"][0]["code"] == "dup", "the check catches it on Continue, before any request"


def test_an_administrator_adds_a_keyless_engine_but_not_a_key_or_start_commands(gateway, fake_upstreams):
    owner, _, _, admin = owner_and_admin(gateway)
    fake = _chat_server(fake_upstreams)
    address = f"{fake.base_url}/v1"
    for values, says in (
        ({"address": address, "api_key": KEY}, "Only the owner can add a key."),
        ({"address": address, "api_key_env": "LAN_SERVER_KEY"}, "environment variable"),
        ({"address": address, "lifecycle_mode": "managed", "lifecycle_start": "systemctl --user start x"}, "starts and stops"),
    ):
        refused = _create(admin, {"definition": "openai-compatible", "name": "helper", "values": values, "acknowledge_warnings": True})
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "owner_required"
        assert says in refused.json()["error"]["message"]
    checked = admin.post("/admin/api/engines/check", headers=csrf_for(admin),
                         json={"definition": "openai-compatible", "values": {"address": address, "api_key": KEY}})
    assert checked.status_code == 403, "an administrator cannot even test a key"
    added = _create(admin, {"definition": "openai-compatible", "name": "helper", "values": {"address": address}, "acknowledge_warnings": True})
    assert added.status_code == 201, added.text
    assert "api_key_secret_id" not in _record(gateway, "helper")["config"]


def test_an_administrator_cannot_move_an_engine_that_holds_a_key(gateway, fake_upstreams):
    owner, _, _, admin = owner_and_admin(gateway)
    fake = _chat_server(fake_upstreams)
    _chat_server(fake_upstreams, host="moved.fk.test")
    assert _create(owner, {"definition": "openai-compatible", "name": "keyed", "acknowledge_warnings": True,
                           "values": {"address": f"{fake.base_url}/v1", "api_key": KEY}}).status_code == 201
    refused = _edit(admin, "keyed", {"definition": "openai-compatible", "values": {"address": "http://moved.fk.test/v1"},
                                     "acknowledge_warnings": True})
    assert refused.status_code == 403, refused.text
    assert _record(gateway, "keyed")["config"]["address"] == f"{fake.base_url}/v1"


def test_a_stored_key_is_never_sent_to_an_edited_address(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    home = _chat_server(fake_upstreams)
    elsewhere = _chat_server(fake_upstreams, host="elsewhere.fk.test")
    assert _create(owner, {"definition": "openai-compatible", "name": "keyed", "acknowledge_warnings": True,
                           "values": {"address": f"{home.base_url}/v1", "api_key": KEY}}).status_code == 201
    checked = owner.post("/admin/api/engines/check", headers=csrf_for(owner), json={
        "definition": "openai-compatible", "service_id": "keyed",
        "values": {"address": f"{elsewhere.base_url}/v1", "api_key": engine_check.SECRET_MASK}})
    assert checked.status_code == 200, checked.text
    result = checked.json()["check"]
    assert result["key_sent"] is False and result["key_withheld"] is True
    assert all("authorization" not in r.headers for r in elsewhere.requests)
    # The same address keeps using it.
    again = owner.post("/admin/api/engines/check", headers=csrf_for(owner), json={
        "definition": "openai-compatible", "service_id": "keyed", "values": {"address": f"{home.base_url}/v1"}})
    assert again.json()["check"]["key_sent"] is True


def test_saving_a_new_address_without_a_new_key_removes_the_old_key(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    home = _chat_server(fake_upstreams)
    _chat_server(fake_upstreams, host="new-home.fk.test")
    assert _create(owner, {"definition": "openai-compatible", "name": "keyed", "acknowledge_warnings": True,
                           "values": {"address": f"{home.base_url}/v1", "api_key": KEY}}).status_code == 201
    old_secret = _record(gateway, "keyed")["config"]["api_key_secret_id"]
    saved = _edit(owner, "keyed", {"definition": "openai-compatible", "acknowledge_warnings": True,
                                   "values": {"address": "http://new-home.fk.test/v1"}})
    assert saved.status_code == 200, saved.text
    config = _record(gateway, "keyed")["config"]
    assert "api_key_secret_id" not in config
    assert secret_store.provider_secret_status(old_secret, db_path=gateway.path).state == "missing"
    view = owner.get("/admin/api/engines/keyed").json()["engine"]
    assert view["key"]["state"] == "none"
    assert view["check"]["key_sent"] is False


def test_the_mask_keeps_the_stored_key_on_save(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    home = _chat_server(fake_upstreams)
    assert _create(owner, {"definition": "openai-compatible", "name": "keyed", "acknowledge_warnings": True,
                           "values": {"address": f"{home.base_url}/v1", "api_key": KEY}}).status_code == 201
    secret_id = _record(gateway, "keyed")["config"]["api_key_secret_id"]
    saved = _edit(owner, "keyed", {"definition": "openai-compatible", "name": "Garage GPU",
                                   "values": {"api_key": engine_check.SECRET_MASK, "reply_timeout_s": 300}})
    assert saved.status_code == 200, saved.text
    record = _record(gateway, "keyed")
    assert record["name"] == "Garage GPU" and record["config"]["timeout"] == 300
    assert record["config"]["api_key_secret_id"] == secret_id
    assert secret_store.get_provider_secret(secret_id, db_path=gateway.path, expected_origin=record["config"]["credential_origin"]) == KEY


SIX = [
    {"kind": "value-map", "param": "reasoning_effort", "map": {"xhigh": "high", "minimal": "low"}, "phase": "request"},
    {"kind": "param-map", "from": "max_completion_tokens", "to": "max_tokens", "phase": "request"},
    {"kind": "drop-params", "drop": ["logit_bias", "user"], "phase": "request"},
    {"kind": "default-params", "set": {"top_p": 0.9}},
    {"kind": "message-inject", "role": "system", "text": "Answer in plain language.", "mode": "prepend"},
    {"kind": "clamp", "param": "temperature", "min": 0, "max": 1, "phase": "request"},
    "reasoning-effort-final-chat",
]


def test_typed_request_adjustments_round_trip_unchanged(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    fake = _chat_server(fake_upstreams)
    created = _create(owner, {"definition": "openai-compatible", "name": "tuned", "acknowledge_warnings": True,
                              "values": {"address": f"{fake.base_url}/v1", "transforms": SIX}})
    assert created.status_code == 201, created.text
    assert _record(gateway, "tuned")["config"]["transforms"] == SIX
    view = owner.get("/admin/api/engines/tuned").json()["engine"]
    assert view["values"]["transforms"] == SIX
    # The saved rows run as the six primitives do.
    body = {"reasoning_effort": "xhigh", "max_completion_tokens": 5, "user": "u", "temperature": 3,
            "messages": [{"role": "user", "content": "hi"}]}
    out = transforms.run_request_pipeline(SIX[:-1], body)
    assert out["reasoning_effort"] == "high" and out["max_tokens"] == 5 and "user" not in out
    assert out["temperature"] == 1 and out["top_p"] == 0.9 and out["messages"][0]["role"] == "system"
    # Saving the page again without touching them changes nothing.
    assert _edit(owner, "tuned", {"definition": "openai-compatible", "values": {}}).status_code == 200
    assert _record(gateway, "tuned")["config"]["transforms"] == SIX


@pytest.mark.parametrize("bad", [
    {"kind": "clamp", "min": 0},
    {"kind": "teleport"},
    {"kind": "drop-params", "drop": ["x"], "phase": "sideways"},
])
def test_a_malformed_adjustment_is_refused_with_a_sentence(gateway, fake_upstreams, bad):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "never-called.fk.test")
    refused = _create(owner, {"definition": "openai-compatible", "name": "tuned", "disabled": True,
                              "values": {"address": "http://never-called.fk.test/v1", "transforms": [bad]}})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["field"] == "transforms"
    assert refused.json()["error"]["message"].startswith("Adjustment 1")


def test_other_settings_are_kept_and_listed(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "attic.fk.test")
    assert _create(owner, {"definition": "openai-compatible", "name": "attic", "disabled": True,
                           "values": {"address": "http://attic.fk.test/v1"}}).status_code == 201
    registry = _registry(gateway)
    registry["services"]["attic"]["config"].update(probe_interval_s=30, legacy_note="bought 2025")
    _put_registry(gateway, registry)
    view = owner.get("/admin/api/engines/attic").json()["engine"]
    assert view["other_settings"] == {"probe_interval_s": 30, "legacy_note": "bought 2025"}
    assert _edit(owner, "attic", {"definition": "openai-compatible", "name": "Attic server", "disabled": True,
                                  "values": {}}).status_code == 200
    config = _record(gateway, "attic")["config"]
    assert config["probe_interval_s"] == 30 and config["legacy_note"] == "bought 2025"


def test_the_check_route_persists_nothing(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    fake = _serve(fake_upstreams, "family-b-embedding-server")
    before = owner.get("/admin/api/operator/service-registry").headers.get("etag")
    checked = owner.post("/admin/api/engines/check", headers=csrf_for(owner),
                         json={"definition": "openai-compatible", "values": {"address": f"{fake.base_url}/v1"}})
    assert checked.status_code == 200, checked.text
    assert {row["id"]: row["level"] for row in checked.json()["check"]["capabilities"]}["chat"] == "possible"
    assert owner.get("/admin/api/operator/service-registry").headers.get("etag") == before


def test_a_draft_real_test_runs_from_the_gateway_against_the_draft_values(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    reply = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}]}
    fake = _serve(fake_upstreams, "chat-answers-real-test", host="tested.fk.test", chat_status=200, chat_reply=reply)
    tested = owner.post("/admin/api/engines/test", headers=csrf_for(owner), json={
        "definition": "openai-compatible", "capability": "chat", "model": "qwen3-32b-instruct-q4_k_m",
        "values": {"address": f"{fake.base_url}/v1", "api_key": KEY}})
    assert tested.status_code == 200, tested.text
    result = tested.json()["test"]
    assert result["ok"] is True and result["reply"] == "“OK”" and result["via"] == "draft"
    # A new engine runs with its kind's default adjustment, so the test does too.
    assert result["sent"] == "One chat message, “Say OK”, with a limit of 1 token, with your request adjustments applied"
    assert result["adjusted"] is True
    trace = result["request"]
    sent = next(row for row in fake.requests if row.path == "/v1/chat/completions" and row.json.get("messages"))
    assert trace["method"] == sent.method
    assert trace["url"] == f"{fake.base_url}{sent.path}"
    assert trace["body"] == sent.json
    assert trace["headers"]["Authorization"] == "Bearer [redacted]"
    assert trace["headers"]["content-type"] == sent.headers["content-type"]
    assert KEY not in tested.text
    assert "tested" not in _registry(gateway)["services"]


def test_the_engine_routes_need_an_administrator(gateway):
    anonymous = gateway.client()
    assert anonymous.get("/admin/api/engine-definitions").status_code == 401
    assert anonymous.post("/admin/api/engines/check", json={}).status_code == 401


def test_an_administrator_can_rename_an_engine_that_holds_a_key_and_start_commands(gateway, fake_upstreams):
    owner, _, _, admin = owner_and_admin(gateway)
    fake = _chat_server(fake_upstreams)
    assert _create(owner, {"definition": "openai-compatible", "name": "keyed", "acknowledge_warnings": True,
                           "values": {"address": f"{fake.base_url}/v1", "api_key": KEY, "lifecycle_mode": "managed",
                                      "lifecycle_start": "systemctl --user start garage-llm",
                                      "lifecycle_stop": "systemctl --user stop garage-llm"}}).status_code == 201
    before = _record(gateway, "keyed")
    renamed = _edit(admin, "keyed", {"definition": "openai-compatible", "name": "Garage GPU", "values": {}})
    assert renamed.status_code == 200, renamed.text
    after = _record(gateway, "keyed")
    assert after["name"] == "Garage GPU"
    assert after["config"]["api_key_secret_id"] == before["config"]["api_key_secret_id"]
    # Schema v2 stores the legacy driver payload under process and keeps it
    # unchanged when the owner edits only the name.
    assert after["process"] == before["process"]
    assert after["config"]["models"][0]["id"] == "offering"
    assert "lifecycle" not in after


def test_an_older_record_keeps_its_key_reference_and_its_start_driver_through_the_page(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "older.fk.test")
    registry = _registry(gateway)
    lifecycle = {"type": "systemd", "managed": True, "service_name": "older-llm", "driver": "systemd",
                 "driver_config": {"unit": "older-llm.service", "health_timeout_s": 300}}
    registry["services"]["older"] = service_registry.service_from_backend_config("older", {
        "name": "Older engine", "host": "older.fk.test", "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
        "api_key_env": "OLDER_KEY", "capabilities": ["chat", "completions"], "lifecycle": lifecycle,
        "credential_origin": "http://older.fk.test:80"}, source="ui")
    _put_registry(gateway, registry)
    view = owner.get("/admin/api/engines/older").json()["engine"]
    assert view["inferred"] is True and view["definition"] == "openai-compatible"
    assert view["capabilities"] == ["chat"]
    assert view["key"] == {"state": "configured", "source": "env", "env_name": "OLDER_KEY", "origin": "http://older.fk.test:80"}
    saved = _edit(owner, "older", {"definition": "openai-compatible", "name": "Older engine, renamed", "values": {}})
    assert saved.status_code == 200, saved.text
    after = _record(gateway, "older")
    assert after["config"]["api_key_env"] == "OLDER_KEY"
    assert after["lifecycle"]["driver"] == "systemd" and after["lifecycle"]["driver_config"] == lifecycle["driver_config"]
    assert after["config"]["definition"] == "openai-compatible"


def test_create_answers_with_the_new_engine_id(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "named.fk.test")
    created = _create(owner, {"definition": "openai-compatible", "name": "Garage GPU!", "disabled": True,
                              "values": {"address": "http://named.fk.test/v1"}})
    assert created.status_code == 201, created.text
    assert created.json()["service_id"] == "garage-gpu"
    assert "garage-gpu" in created.json()["service_registry"]["services"]


def test_a_key_with_a_character_a_request_cannot_carry_is_refused(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "accents.fk.test")
    refused = owner.post("/admin/api/engines/check", headers=csrf_for(owner), json={
        "definition": "openai-compatible", "values": {"address": "http://accents.fk.test/v1", "api_key": "sk-clé"}})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["field"] == "api_key"


def test_moving_an_engine_kept_off_onto_another_engines_address_is_refused(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "first.fk.test")
    _silent(fake_upstreams, "second.fk.test")
    for name, host in (("first", "first.fk.test"), ("second", "second.fk.test")):
        assert _create(owner, {"definition": "openai-compatible", "name": name, "disabled": True,
                               "values": {"address": f"http://{host}/v1"}}).status_code == 201
    moved = _edit(owner, "second", {"definition": "openai-compatible", "disabled": True,
                                    "values": {"address": "http://first.fk.test/v1"}})
    assert moved.status_code == 409, moved.text
    assert moved.json()["error"]["code"] == "engine_duplicate"


def test_an_edit_keeps_what_the_page_does_not_show(gateway, fake_upstreams):
    """Priority, role, extra capabilities, per-model keys and turned-off models survive a save."""
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "rich.fk.test")
    registry = _registry(gateway)
    registry.setdefault("services", {})["rich"] = service_registry.service_from_backend_config("rich", {
        "name": "Rich engine", "host": "rich.fk.test", "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
        "priority": 7, "role": "optional", "capabilities": ["chat", "completions", "vision"],
        "models": [{"model_group": "big", "upstream_model": "big-q4", "activation_model": "/models/big.gguf"},
                   {"model_group": "small", "upstream_model": "small-q4", "enabled": False}]}, source="ui")
    _put_registry(gateway, registry)
    view = owner.get("/admin/api/engines/rich").json()["engine"]
    saved = _edit(owner, "rich", {"definition": "openai-compatible", "name": "Rich engine, renamed",
                                  "models": view["models"], "values": {}})
    assert saved.status_code == 200, saved.text
    after = _record(gateway, "rich")
    assert after["priority"] == 7 and after["role"] == "optional"
    assert "vision" in after["capabilities"]
    rows = {row["model_group"]: row for row in after["config"]["models"]}
    assert rows["big"]["activation_model"] == "/models/big.gguf"
    assert rows["small"]["enabled"] is False


@pytest.mark.parametrize("service_id, config", [
    # A node-graph server: the model router does not route it at all.
    ("graph", {"name": "Image server", "host": "graph.fk.test", "port": 80, "protocol": "comfyui", "capabilities": ["images"],
               "transforms": ["a-named-adjustment"], "workflows_dir": "./workflows/images",
               "models": [{"model_group": f"workflow-{n}", "upstream_model": f"workflow-{n}"} for n in range(3)]}),
    # A chat server with no protocol: served by the gateway itself, not by the router.
    ("plain", {"name": "Plain", "host": "plain.fk.test", "port": 80, "base_path": "/v1", "capabilities": ["chat"],
               "native_passthrough": False}),
    # A chat server the router reaches through the gateway's lifecycle hop.
    ("hopped", {"name": "Hopped", "host": "hopped.fk.test", "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
                "capabilities": ["chat", "completions"], "gateway_lifecycle": True,
                "models": [{"model_group": "hopped-chat", "upstream_model": "hopped"}]}),
])
def test_engine_records_remain_editable_regardless_of_router_path(gateway, fake_upstreams, service_id, config):
    """Routing through the gateway no longer locks the whole engine page."""
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, f"{service_id}.fk.test")
    registry = _registry(gateway)
    registry.setdefault("services", {})[service_id] = service_registry.service_from_backend_config(service_id, config, source="ui")
    _put_registry(gateway, registry)
    before = _record(gateway, service_id)
    view = owner.get(f"/admin/api/engines/{service_id}").json()["engine"]
    assert view["editable"] is True
    assert "not_editable_reason" not in view
    assert view["can_edit_offerings"] is (config.get("protocol") in (None, "", "openai-compatible"))
    assert bool(view["unsupported_fields"]) is (config.get("protocol") not in (None, "", "openai-compatible"))
    # Renaming stays possible through the plain service edit.
    renamed = _edit(owner, service_id, {"name": "Renamed"})
    assert renamed.status_code == 200, renamed.text
    after = _record(gateway, service_id)
    assert after["name"] == "Renamed"
    # The plain edit changes the name; what the gateway uses to reach the engine stays.
    for key in ("host", "port", "protocol", "gateway_lifecycle", "workflows_dir", "transforms"):
        assert after["config"].get(key) == before["config"].get(key), key
    assert ([row["model_group"] for row in after["config"].get("models") or ()]
            == [row["model_group"] for row in before["config"].get("models") or ()])


def test_plain_request_headers_are_sent_by_the_check_and_saved_as_plain_rows(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    home = _chat_server(fake_upstreams)
    assert _create(owner, {"definition": "openai-compatible", "name": "headed", "acknowledge_warnings": True,
                           "values": {"address": f"{home.base_url}/v1",
                                      "headers": [{"name": "X-Client", "value": "azizam-home"}]}}).status_code == 201
    assert _record(gateway, "headed")["config"]["headers"] == [{"name": "X-Client", "value": "azizam-home"}]
    sent = [r for r in home.requests if r.path == "/v1/models"]
    assert sent[0].headers.get("x-client") == "azizam-home"
    view = owner.get("/admin/api/engines/headed").json()["engine"]
    assert view["values"]["headers"] == [{"name": "X-Client", "value": "azizam-home"}]


def test_a_header_marked_secret_is_refused_with_a_sentence(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "secret-header.fk.test")
    refused = _create(owner, {"definition": "openai-compatible", "name": "headed", "disabled": True,
                              "values": {"address": "http://secret-header.fk.test/v1",
                                         "headers": [{"name": "X-Client-Token", "value": "header-secret-1", "secret": True}]}})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["field"] == "headers"
    assert "header-secret-1" not in json.dumps(_registry(gateway))


def test_an_older_record_with_models_as_a_mapping_keeps_them_through_the_page(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "mapped.fk.test")
    registry = _registry(gateway)
    record = service_registry.service_from_backend_config("mapped", {
        "name": "Mapped", "host": "mapped.fk.test", "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
        "capabilities": ["chat"]}, source="ui")
    record["config"]["models"] = {"existing-chat": {"upstream_model": "existing-upstream", "routing_hint": "keep-me"},
                                  "slot": {"model_group": "public", "deployment": "real", "routing_hint": "keep-slot"}}
    registry.setdefault("services", {})["mapped"] = record
    _put_registry(gateway, registry)
    view = owner.get("/admin/api/engines/mapped").json()["engine"]
    assert [(m["id"], m["public_name"]) for m in view["models"]] == [("existing-upstream", "existing-chat"), ("real", "public")]
    saved = _edit(owner, "mapped", {"definition": "openai-compatible", "name": "Mapped, renamed",
                                    "models": view["models"], "values": {}})
    assert saved.status_code == 200, saved.text
    rows = {row["model_group"]: row for row in _record(gateway, "mapped")["config"]["models"]}
    assert rows["existing-chat"]["upstream_model"] == "existing-upstream" and rows["existing-chat"]["routing_hint"] == "keep-me"
    assert rows["public"]["upstream_model"] == "real" and rows["public"]["routing_hint"] == "keep-slot"


def test_checking_an_engine_where_it_already_is_ignores_others_at_that_address(gateway, fake_upstreams):
    """Two records may share a server; Check again on one must not say "Already added"."""
    owner, *_ = owner_and_admin(gateway)
    fake = _chat_server(fake_upstreams)
    registry = _registry(gateway)
    for sid in ("twin-a", "twin-b"):
        registry.setdefault("services", {})[sid] = service_registry.service_from_backend_config(sid, {
            "name": sid, "host": "chat.fk.test", "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
            "capabilities": ["chat"]}, source="ui")
    _put_registry(gateway, registry)
    again = owner.post("/admin/api/engines/check", headers=csrf_for(owner), json={
        "definition": "openai-compatible", "service_id": "twin-a", "values": {"address": f"{fake.base_url}/v1"}})
    assert again.status_code == 200, again.text
    assert all(f["code"] != "dup" for f in again.json()["check"]["findings"])
    assert again.json()["check"]["models"], "the check reached the server"
    moved = owner.post("/admin/api/engines/check", headers=csrf_for(owner), json={
        "definition": "openai-compatible", "values": {"address": f"{fake.base_url}/v1"}})
    assert moved.json()["check"]["findings"][0]["code"] == "dup", "a new engine at that address is still a duplicate"


def _probe_route_app(monkeypatch, *, action=None, concurrent=None, per_minute=None):
    """Real handlers, inert probe service and a fixed clock: no upstream or DNS calls."""
    for name, value in (("AZIZAM_ENGINE_CHECK_MAX_CONCURRENT", concurrent),
                        ("AZIZAM_ENGINE_CHECKS_PER_MINUTE", per_minute)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(value))
    limiter_type = engine_admin._EngineProbeLimiter
    monkeypatch.setattr(engine_admin, "_EngineProbeLimiter",
                        lambda **limits: limiter_type(**limits, clock=lambda: 1000.0))
    calls = []

    def guard(request):
        actor = request.headers.get("x-test-actor")
        if not actor:
            return JSONResponse(status_code=401, content={"error": {"code": "unauthorized"}})
        return SimpleNamespace(actor_kind=request.headers.get("x-test-kind", "user"), actor_id=actor)

    async def read_json(request):
        try:
            return await request.json()
        except ValueError:
            raise engine_check.DraftError("The request must be JSON.", field="values") from None

    async def probe(ctx, body):
        calls.append((ctx.actor_kind, ctx.actor_id, body))
        return await action(ctx, body) if action else {"ok": True}

    app = FastAPI()
    router = APIRouter(prefix="/admin/api")
    engine_admin.register_engine_routes(
        router, service=SimpleNamespace(check=probe, test=probe), guard=guard, read_json=read_json,
    )
    app.include_router(router)
    return app, calls


def _probe_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _assert_probe_limited(response, *, retry_after=None):
    assert response.status_code == 429, response.text
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "engine_probe_rate_limited"
    assert response.json()["error"]["message"]
    assert int(response.headers["Retry-After"]) >= 1
    if retry_after is not None:
        assert response.headers["Retry-After"] == str(retry_after)


def test_check_and_draft_test_share_thirty_starts_per_caller(monkeypatch):
    app, calls = _probe_route_app(monkeypatch)

    async def exercise():
        async with _probe_client(app) as client:
            # Guard refusals cannot consume the authenticated caller's budget.
            assert (await client.post("/admin/api/engines/check", json={})).status_code == 401
            for index in range(30):
                endpoint = ("check", "test")[index % 2]
                response = await client.post(f"/admin/api/engines/{endpoint}", json={"index": index},
                                             headers={"x-test-actor": "alice"})
                assert response.status_code == 200, response.text
            for endpoint in ("check", "test"):
                _assert_probe_limited(await client.post(f"/admin/api/engines/{endpoint}", json={},
                                                       headers={"x-test-actor": "alice"}), retry_after=60)
            # Neither another user nor another actor kind shares Alice's budget.
            for headers in ({"x-test-actor": "bob"}, {"x-test-actor": "alice", "x-test-kind": "service_account"}):
                assert (await client.post("/admin/api/engines/test", json={}, headers=headers)).status_code == 200
        assert len(calls) == 32, "refused requests must not invoke the probe"

    asyncio.run(exercise())


@pytest.mark.parametrize("first, second", [("check", "test"), ("test", "check")])
def test_probe_routes_share_two_slots_and_release_on_cancellation(monkeypatch, first, second):
    async def exercise():
        started, release = asyncio.Queue(), asyncio.Event()

        async def action(ctx, body):
            if body.get("hold"):
                await started.put(body["hold"])
                await release.wait()
            return {"ok": True}

        app, calls = _probe_route_app(monkeypatch, action=action)
        async with _probe_client(app) as client:
            headers = {"x-test-actor": "alice"}
            tasks = [asyncio.create_task(client.post(f"/admin/api/engines/{endpoint}",
                                                    json={"hold": index + 1}, headers=headers))
                     for index, endpoint in enumerate((first, second))]
            try:
                assert {await asyncio.wait_for(started.get(), 5) for _ in tasks} == {1, 2}
                for endpoint in ("check", "test"):
                    _assert_probe_limited(await client.post(f"/admin/api/engines/{endpoint}", json={},
                                                           headers=headers), retry_after=1)
                assert len(calls) == 2
                assert (await client.post("/admin/api/engines/check", json={},
                                          headers={"x-test-actor": "bob"})).status_code == 200
                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
                assert (await client.post(f"/admin/api/engines/{first}", json={}, headers=headers)).status_code == 200
                assert not tasks[1].done(), "cancelling one check must not cancel its sibling"
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
            assert tasks[1].result().status_code == 200
            assert (await client.post(f"/admin/api/engines/{second}", json={}, headers=headers)).status_code == 200

    asyncio.run(exercise())


def test_probe_route_limits_are_configurable_and_rejections_do_not_count(monkeypatch):
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def action(ctx, body):
            if body.get("hold"):
                started.set()
                await release.wait()
            return {"ok": True}

        app, calls = _probe_route_app(monkeypatch, action=action, concurrent=1, per_minute=2)
        async with _probe_client(app) as client:
            headers = {"x-test-actor": "alice"}
            first = asyncio.create_task(client.post("/admin/api/engines/check", json={"hold": True}, headers=headers))
            try:
                await asyncio.wait_for(started.wait(), 5)
                _assert_probe_limited(await client.post("/admin/api/engines/test", json={}, headers=headers),
                                      retry_after=1)
            finally:
                release.set()
                await first
            assert (await client.post("/admin/api/engines/test", json={}, headers=headers)).status_code == 200
            _assert_probe_limited(await client.post("/admin/api/engines/check", json={}, headers=headers),
                                  retry_after=60)
        assert len(calls) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["route", "draft", "permission", "unexpected", "body"])
def test_probe_slot_is_released_when_request_fails(monkeypatch, failure):
    async def action(ctx, body):
        if body.get("fail"):
            if failure == "route":
                raise engine_admin.EngineRouteError(422, "test_failure", "Probe refused.")
            if failure == "draft":
                raise engine_check.DraftError("Invalid draft.", field="values")
            if failure == "permission":
                raise PermissionError("Owner required.")
            raise RuntimeError("Unexpected probe failure.")
        return {"ok": True}

    app, _ = _probe_route_app(monkeypatch, action=action, concurrent=1)

    async def exercise():
        async with _probe_client(app) as client:
            headers = {"x-test-actor": "alice"}
            if failure == "unexpected":
                with pytest.raises(RuntimeError, match="Unexpected probe failure"):
                    await client.post("/admin/api/engines/test", json={"fail": True}, headers=headers)
            else:
                kwargs = {"content": "{"} if failure == "body" else {"json": {"fail": True}}
                response = await client.post("/admin/api/engines/test", headers=headers, **kwargs)
                assert response.status_code == {"route": 422, "draft": 400, "permission": 403, "body": 400}[failure]
            assert (await client.post("/admin/api/engines/check", json={}, headers=headers)).status_code == 200

    asyncio.run(exercise())


def test_probe_limit_uses_a_rolling_minute_and_cleans_expired_callers():
    now = [100.0]
    limiter = engine_admin._EngineProbeLimiter(max_concurrent=2, per_minute=2, clock=lambda: now[0])
    alice = SimpleNamespace(actor_kind="user", actor_id="alice")
    bob = SimpleNamespace(actor_kind="user", actor_id="bob")
    with limiter.slot(alice), limiter.slot(bob):
        pass
    now[0] = 110.0
    with limiter.slot(alice):
        pass
    now[0] = 159.1
    with pytest.raises(engine_admin.EngineRouteError) as refused:
        with limiter.slot(alice):
            pytest.fail("A rolling minute still includes both starts.")
    response = refused.value.as_response()
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    now[0] = 160.0
    with limiter.slot(alice):
        pass
    assert ("user", "bob") not in limiter._starts
    assert list(limiter._starts[("user", "alice")]) == [110.0, 160.0]
    assert not limiter._active
    now[0] = 169.1
    with pytest.raises(engine_admin.EngineRouteError):
        with limiter.slot(alice):
            pytest.fail("Expiry of one start must not reset the whole minute.")
    now[0] = 170.0
    with limiter.slot(alice):
        pass


@pytest.mark.parametrize("invalid", ["0", "-1", "not-a-number", "1.5"])
def test_invalid_probe_limit_configuration_keeps_safe_defaults(monkeypatch, invalid):
    for name, default in (("AZIZAM_ENGINE_CHECK_MAX_CONCURRENT", 2), ("AZIZAM_ENGINE_CHECKS_PER_MINUTE", 30)):
        monkeypatch.setenv(name, invalid)
        assert engine_admin._probe_limit(name, default) == default


@pytest.mark.parametrize("service_id", ["workshop", "second-engine"])
def test_admin_api_hop_connection_edit_reaches_fake_upstream(gateway, fake_upstreams, monkeypatch, service_id):
    """An owner edit drives the final HTTP hop, including its real client timeout."""
    from unittest.mock import AsyncMock
    import proxy

    owner, *_ = owner_and_admin(gateway)
    old = _silent(fake_upstreams, "old-hop.fk.test")
    upstream = fake_upstreams.asgi(scenario_from_data({
        "protocol": "openai-compatible", "name": "edited-hop",
        "provenance": {"source": "hand-written", "reference": "ENGINE_CONTRACT section 9: edits drive the effective request"},
        "exchanges": [{"id": "chat", "match": {
            "method": "POST", "path": "/provider/v1/chat/completions",
            "headers": {"x-workspace": "new-value", "authorization": {"$absent": True}, "cookie": {"$absent": True}},
            "json": {"model": "new-upstream"},
        }, "response": {"status": 200, "json": {"choices": [{"message": {"content": "edited"}}]}}}],
    }), host="new-hop.fk.test")
    registry = _registry(gateway)
    registry["services"][service_id] = service_registry.service_from_backend_config(service_id, {
        "name": "Hop engine", "host": "old-hop.fk.test", "port": 80, "base_path": "/v1",
        "address": f"{old.base_url}/v1", "protocol": "openai-compatible", "gateway_lifecycle": True,
        "capabilities": ["chat", "openai"], "timeout": 600,
        "models": [{"id": "offering", "model_group": "hop-chat", "upstream_model": "old-upstream"}],
        "lifecycle": {"driver": "process", "driver_config": {"start": ["server"], "port": {"fixed": 1234}}},
    }, source="ui")
    service = registry["services"][service_id]
    service["process"] = service_registry._v2_process(service.pop("lifecycle"))
    service["config"].pop("lifecycle", None)
    _put_registry(gateway, registry)
    before = _record(gateway, service_id)
    saved = _edit(owner, service_id, {
        "definition": "openai-compatible", "disabled": True,
        "values": {"address": f"{upstream.base_url}/provider", "path_prefix": "/v1", "reply_timeout_s": 37,
                   "headers": [{"name": "X-Workspace", "value": "new-value"}]},
        "models": [{"id": "new-upstream", "public_name": "hop-chat"}],
    })
    assert saved.status_code == 200, saved.text
    after = _record(gateway, service_id)
    assert after["process"] == before["process"]
    assert _edit(owner, service_id, {"enabled": True}).status_code == 200
    view = owner.get(f"/admin/api/engines/{service_id}").json()["engine"]
    assert view["request_path"]["mode"] == "gateway_lifecycle"
    assert view["request_path"]["url"] == f"{upstream.base_url}/provider/v1/chat/completions"
    assert view["v2"]["offerings"][0]["id"] == "offering"
    assert view["lifecycle_editable"] is False
    monkeypatch.setenv("AZIZAM_INTERNAL_LITELLM_KEY", "internal-test-key")
    monkeypatch.setattr(proxy, "_service_availability_response", lambda *a, **kw: None)
    monkeypatch.setattr(proxy, "_ensure_target_leased", AsyncMock(return_value=(True, None)))
    clients = fake_upstreams.track_clients(proxy)
    response = owner.post(f"/internal/litellm/local/{service_id}/v1/chat/completions",
                          headers={"Authorization": "Bearer internal-test-key", "X-Workspace": "untrusted"},
                          json={"model": "new-upstream", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "edited"
    assert clients[-1].timeout.read == 37
    assert len(upstream.requests) == 1
    assert old.requests == []


@pytest.mark.parametrize("body", [
    {"values": {"reply_timeout_s": 12}},
    {"values": {"headers": [{"name": "X-Workspace", "value": "changed"}]}},
    {"models": [{"id": "new", "public_name": "new"}], "values": {}},
])
def test_administrator_cannot_change_existing_owner_engine_fields(gateway, fake_upstreams, body):
    owner, _, _, admin = owner_and_admin(gateway)
    fake = _silent(fake_upstreams, "owner-fields.fk.test")
    created = _create(owner, {"definition": "openai-compatible", "name": "owned", "disabled": True,
                              "values": {"address": f"{fake.base_url}/v1"}})
    assert created.status_code == 201, created.text
    before = _record(gateway, "owned")
    changed = _edit(admin, "owned", {"definition": "openai-compatible", "disabled": True, **body})
    assert changed.status_code == 403, changed.text
    assert _record(gateway, "owned") == before
