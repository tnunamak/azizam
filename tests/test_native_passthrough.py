"""Per-engine native pass-through (contract section 5): /native/{engine_id}/... forwards the engine's own API.

An engine whose record sets ``native_passthrough`` exposes its API under ``/native/{engine_id}``,
and only the calls its ``native.allow`` list names (method plus path template, class ``generate`` or
``control``). Default deny. The gateway authenticates the caller, applies the access plan (and, for a
``control`` call, the owner check), forwards with the engine's own credential and never the client's,
caps request and response bytes, refuses redirects, and writes a receipt that says the cost is unknown.
The lane never serves a remote provider.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import receipt_store, service_registry
from tests.support.access import plan_for
from tests.support.fake_upstream import fake_upstreams, load_scenario, scenario_from_data  # noqa: F401 - fixture
from tests.test_runtime_route_policy import AUTH_HEADERS, _migrated_model_policy_config, authenticated_profile

# The runtime policy tests run under a config whose services carry the model-policy schema.
pytestmark = pytest.mark.usefixtures("native_gateway")


@pytest.fixture
def native_gateway():
    receipt_store.init_db()
    with patch.object(proxy, "config", _migrated_model_policy_config()):
        yield

MESSAGES = [{"role": "user", "content": "hi"}]
BODY = {"model": "demo-chat", "messages": MESSAGES}
NATIVE_PATH = "/native/garage/v1/chat/completions"


ALLOW_CHAT = {"allow": [{"method": "POST", "path": "/v1/chat/completions", "class": "generate"}]}


def _engine(engine_url: str, **record) -> dict:
    record.setdefault("native", ALLOW_CHAT)
    return {"protocol": "openai-compatible", "url": engine_url, **record}


def _scenario(*exchanges: dict):
    return scenario_from_data({
        "protocol": "openai-compatible",
        "name": "native-fixture",
        "provenance": {"source": "hand-written", "reference": "docs/design/ENGINE_CONTRACT.md section 6"},
        "exchanges": list(exchanges),
    })


def _receipts_for(path: str) -> list[dict]:
    return [row for row in receipt_store.recent(limit=50, audience="internal") if row.get("path") == path]


def _post(path: str, body: dict | None = None):
    return TestClient(proxy.app).post(path, headers=AUTH_HEADERS, json=body or BODY)


def _new_receipt(before: list[dict], path: str) -> dict:
    seen = {row["request_id"] for row in before}
    fresh = [row for row in _receipts_for(path) if row["request_id"] not in seen]
    assert len(fresh) == 1, f"expected one new receipt for {path}, found {len(fresh)}"
    return fresh[0]


def test_an_allowed_engine_call_is_forwarded_untouched_and_receipted(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/chat-text", model="demo-chat", content="native"))
    profile = plan_for("native-allowed", caps=(), engines=["garage"], operations=["v1/chat/completions"])
    before = _receipts_for(NATIVE_PATH)

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True)},
    ):
        response = _post(NATIVE_PATH)

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "native"
    [sent] = engine.calls("chat")
    assert sent.path == "/v1/chat/completions"
    assert sent.json == BODY
    assert "authorization" not in {name.lower() for name in sent.headers}

    receipt = _new_receipt(before, NATIVE_PATH)
    assert receipt["protocol"] == "provider-native"
    assert receipt["intent"] == "provider.native"
    assert receipt["backend"] == "garage"
    assert receipt["app_id"] == "app_runtime_policy"
    assert receipt["metadata"]["lane"] == "native"
    assert receipt["metadata"]["provider"] == "garage"
    assert receipt["metadata"]["native_path"] == "/v1/chat/completions"
    assert receipt["metadata"]["status_code"] == 200
    assert receipt["metadata"]["native_operation"] == "POST /v1/chat/completions"
    assert receipt["metadata"]["native_class"] == "generate"
    assert receipt["metadata"]["cost"] == "unknown"
    assert receipt["cost"]["usd"] is None
    assert receipt["cost"]["reconciliation_status"] == "unknown"


def test_a_streamed_reply_reaches_the_caller_as_the_engine_sent_it(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/chat-stream", model="demo-chat"))
    profile = plan_for("native-stream", caps=(), engines=["garage"], operations=["v1/chat/completions"])

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True)},
    ):
        response = _post(NATIVE_PATH, {**BODY, "stream": True})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [line for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(events) >= 2, response.text
    assert "chatcmpl-fk-stream" in response.text
    [sent] = engine.calls("chat")
    assert sent.json["stream"] is True


def test_a_plan_without_native_access_is_refused_before_the_engine_is_called(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    profile = plan_for("native-denied", caps=())
    before = _receipts_for(NATIVE_PATH)

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True)},
    ):
        response = _post(NATIVE_PATH)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "not_in_access_plan"
    assert engine.requests == []
    assert _new_receipt(before, NATIVE_PATH)["status"] == "denied"


def test_a_remote_provider_is_never_served_by_the_native_lane(fake_upstreams, monkeypatch):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    profile = plan_for("native-remote", caps=(), engines=["hosted"], operations=["v1/chat/completions"])

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"],
        {"hosted": _engine(engine.base_url, native_passthrough=True, remote_provider=True)},
    ):
        response = _post("/native/hosted/v1/chat/completions")

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "native_remote_forbidden"
    assert engine.requests == []


def test_the_native_lane_is_off_unless_the_engine_record_enables_it(fake_upstreams, monkeypatch):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    profile = plan_for("native-off", caps=(), engines=["garage"], operations=["v1/chat/completions"])

    for record in (_engine(engine.base_url), _engine(engine.base_url, native_passthrough=False)):
        with authenticated_profile(profile), patch.dict(proxy.config["backends"], {"garage": record}):
            response = _post(NATIVE_PATH)
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "native_not_enabled"

    assert engine.requests == []


def test_a_call_without_a_key_is_refused_before_the_engine_is_called(fake_upstreams, monkeypatch):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))

    with patch.dict(proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True)}):
        response = TestClient(proxy.app).post(NATIVE_PATH, json=BODY)

    assert response.status_code == 401, response.text
    assert engine.requests == []


# --- contract v2.1: default deny per method plus path template, two classes -----------------

def _denied(response, code: str) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == code


def test_a_lane_with_no_allow_list_refuses_every_call(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    profile = plan_for("native-no-list", caps=(), engines="all", operations=["*"])
    before = _receipts_for(NATIVE_PATH)

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native={})},
    ):
        response = _post(NATIVE_PATH)

    _denied(response, "native_not_allowed")
    assert engine.requests == []
    receipt = _new_receipt(before, NATIVE_PATH)
    assert receipt["status"] == "denied"
    assert receipt["metadata"]["refusal"] == "native_not_allowed"


def test_a_call_the_allow_list_does_not_name_is_refused_by_method_and_by_path(fake_upstreams, monkeypatch):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    profile = plan_for("native-unlisted", caps=(), engines="all", operations=["*"])
    client = TestClient(proxy.app)

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True)},
    ):
        wrong_method = client.get(NATIVE_PATH, headers=AUTH_HEADERS)
        wrong_path = client.post("/native/garage/v1/embeddings", headers=AUTH_HEADERS, json=BODY)

    _denied(wrong_method, "native_not_allowed")
    _denied(wrong_path, "native_not_allowed")
    assert engine.requests == []


def test_a_path_template_stands_for_one_segment(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(_scenario({
        "id": "model", "match": {"method": "GET", "path": "/v1/models/demo-chat"},
        "response": {"status": 200, "json": {"id": "demo-chat", "object": "model"}},
    }))
    profile = plan_for("native-template", caps=(), engines="all", operations=["*"])
    allow = {"allow": [{"method": "GET", "path": "/v1/models/{model}", "class": "generate"}]}
    client = TestClient(proxy.app)

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=allow)},
    ):
        listed = client.get("/native/garage/v1/models/demo-chat", headers=AUTH_HEADERS)
        deeper = client.get("/native/garage/v1/models/demo-chat/extra", headers=AUTH_HEADERS)
        shorter = client.get("/native/garage/v1/models", headers=AUTH_HEADERS)

    assert listed.status_code == 200, listed.text
    assert listed.json()["id"] == "demo-chat"
    _denied(deeper, "native_not_allowed")
    _denied(shorter, "native_not_allowed")


CONTROL_ALLOW = {"allow": [
    *ALLOW_CHAT["allow"],
    {"method": "DELETE", "path": "/v1/models/{model}", "class": "control"},
]}
UNLOAD_PATH = "/native/garage/v1/models/demo-chat"


def _unload_scenario():
    return _scenario({
        "id": "unload", "match": {"method": "DELETE", "path": "/v1/models/demo-chat"},
        "response": {"status": 200, "json": {"unloaded": True}},
    })


def test_a_control_call_is_owner_only_whatever_the_plan_grants(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    # The widest plan there is: every engine, every operation. It still does not reach a control entry.
    profile = plan_for("native-control-denied", engines="all", operations=["*"])
    before = _receipts_for(UNLOAD_PATH)

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=CONTROL_ALLOW)},
    ):
        response = TestClient(proxy.app).delete(UNLOAD_PATH, headers=AUTH_HEADERS)

    _denied(response, "native_owner_only")
    assert engine.requests == []
    receipt = _new_receipt(before, UNLOAD_PATH)
    assert receipt["status"] == "denied"
    assert receipt["metadata"]["native_class"] == "control"


def test_an_owner_can_make_a_control_call_and_the_receipt_names_its_class(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(_unload_scenario())
    profile = plan_for("native-control-owner", caps=(), engines=["garage"], operations=["DELETE /v1/models/*"])
    before = _receipts_for(UNLOAD_PATH)

    with authenticated_profile(profile), patch.object(proxy, "_native_caller_is_owner", return_value=True), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=CONTROL_ALLOW)},
    ):
        response = TestClient(proxy.app).delete(UNLOAD_PATH, headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    assert response.json() == {"unloaded": True}
    receipt = _new_receipt(before, UNLOAD_PATH)
    assert receipt["metadata"]["native_class"] == "control"
    assert receipt["metadata"]["native_operation"] == "DELETE /v1/models/demo-chat"


def test_an_owner_still_needs_the_plan_to_name_the_engine_for_a_control_call(fake_upstreams, monkeypatch):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    profile = plan_for("native-control-no-engine", caps=())

    with authenticated_profile(profile), patch.object(proxy, "_native_caller_is_owner", return_value=True), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=CONTROL_ALLOW)},
    ):
        response = TestClient(proxy.app).delete(UNLOAD_PATH, headers=AUTH_HEADERS)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "not_in_access_plan"
    assert engine.requests == []


def test_the_engines_credential_goes_out_and_the_clients_never_does(fake_upstreams, monkeypatch):
    monkeypatch.setenv("NATIVE_ENGINE_KEY", "engine-secret")
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/chat-text", model="demo-chat", content="ok"))
    profile = plan_for("native-credential", caps=(), engines=["garage"], operations=["v1/chat/completions"])
    connection = {"credentials": {"inference": {
        "secret_env": "NATIVE_ENGINE_KEY", "header": "x-api-key", "origin": service_registry.origin_of(engine.base_url),
    }}}

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, connection=connection)},
    ):
        response = TestClient(proxy.app).post(
            NATIVE_PATH, json=BODY,
            headers={**AUTH_HEADERS, "Cookie": "session=client-cookie", "X-Admin-Key": "client-admin"},
        )

    assert response.status_code == 200, response.text
    [sent] = engine.calls("chat")
    headers = {name.lower(): value for name, value in sent.headers.items()}
    assert headers["x-api-key"] == "engine-secret"
    assert not {"authorization", "cookie", "x-admin-key"} & set(headers)


def test_a_request_over_the_byte_cap_is_refused_before_the_engine_is_called(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    profile = plan_for("native-request-cap", caps=(), engines=["garage"], operations=["v1/chat/completions"])
    capped = {**ALLOW_CHAT, "max_request_bytes": 64}
    before = _receipts_for(NATIVE_PATH)

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=capped)},
    ):
        response = _post(NATIVE_PATH, {**BODY, "pad": "x" * 200})

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "native_request_too_large"
    assert engine.requests == []
    assert _new_receipt(before, NATIVE_PATH)["status"] == "denied"


def test_a_streamed_reply_that_runs_past_the_cap_is_cut_off_and_receipted_as_failed(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(_scenario({
        "id": "long", "match": {"method": "POST", "path": "/v1/chat/completions"},
        "response": {"status": 200, "headers": {"content-type": "text/event-stream"},
                     "body": [{"text": "data: " + "z" * 100 + "\n\n"} for _ in range(20)]},
    }))
    profile = plan_for("native-response-stream-cap", caps=(), engines=["garage"], operations=["v1/chat/completions"])
    capped = {**ALLOW_CHAT, "max_response_bytes": 512}
    before = _receipts_for(NATIVE_PATH)

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=capped)},
    ):
        try:
            response = TestClient(proxy.app, raise_server_exceptions=False).post(NATIVE_PATH, headers=AUTH_HEADERS, json=BODY)
            relayed = len(response.content)
        except Exception:  # the connection is cut mid-reply
            relayed = 0

    assert relayed <= 512
    receipt = _new_receipt(before, NATIVE_PATH)
    assert receipt["status"] == "failed"
    assert receipt["metadata"]["refusal"] == "native_response_too_large"


def test_a_redirect_from_the_engine_is_refused_not_followed_or_relayed(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    target = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    engine = fake_upstreams.loopback(_scenario({
        "id": "moved", "match": {"method": "POST", "path": "/v1/chat/completions"},
        "response": {"status": 302, "headers": {"location": f"{target.base_url}/v1/models"}},
    }))
    profile = plan_for("native-redirect", caps=(), engines=["garage"], operations=["v1/chat/completions"])

    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True)},
    ):
        response = _post(NATIVE_PATH)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "native_redirect_refused"
    assert "location" not in {name.lower() for name in response.headers}
    assert target.requests == []
