"""Per-engine native pass-through (contract section 5): /native/{engine_id}/... forwards the engine's own API.

An engine whose record sets ``native_passthrough`` exposes its API under ``/native/{engine_id}``. The
gateway authenticates the caller, applies the access plan, and writes a receipt. It forwards the
request and the reply untouched. The lane is off by default and never serves a remote provider.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import receipt_store
from tests.support.access import plan_for
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
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


def _engine(engine_url: str, **record) -> dict:
    return {"protocol": "openai-compatible", "url": engine_url, **record}


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
