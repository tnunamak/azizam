"""What Add engine saves reaches the engine on the real request path.

No control may save a setting the running gateway ignores (add-engine-spec.md
section 8). Each test builds the record the Add engine routes write, compiles
it the way the gateway hands it to its model router (the LiteLLM sidecar), and
sends a chat request through a real LiteLLM Router to a fake engine on a
loopback port. The fake's scenario matches the path and the headers, so a
setting that does not arrive fails the test.

The hop from the gateway to the sidecar is not in these tests; the gateway
forwards the request body and LiteLLM owns this call. The same headers on the
direct engine route are proven in test_runtime_route_policy.
"""

from __future__ import annotations

import asyncio
import json
import time

import litellm
import pytest
from litellm import Router

import proxy
from gateway import admin_store, litellm_runtime, record_carriers, runtime_secrets, secret_refs
from gateway.engine_admin import build_raw, clean_values
from gateway.engine_definitions import load_engine_catalog
from gateway.litellm_substrate import compile_litellm_config_from_registry, engine_headers, router_reply_timeout
from gateway.service_registry import service_from_backend_config
from tests.registry_test_helpers import write_service_registry_for_test
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.support.gateway import gateway_client, register_litellm, use_admin_db

MESSAGES = [{"role": "user", "content": "hi"}]
# The gateway sends these with every routed request (LlmExecutionRequest trusted retries).
NO_RETRIES = {"num_retries": 0, "max_retries": 0}


def _registry(address: str, **values) -> dict:
    catalog = load_engine_catalog()
    definition = catalog.definition("openai-compatible")
    clean = clean_values(definition, {"address": address, **values})
    raw = build_raw(catalog, definition, clean, name="Garage GPU", capabilities=["chat"],
                    models=[{"id": "demo-chat", "public_name": "garage-chat"}], enabled=True,
                    check={}, tests=None, secret_id=None)
    return {"schema_version": 1, "services": {"garage-gpu": service_from_backend_config("garage-gpu", raw, source="ui")}}


@pytest.fixture(autouse=True)
def secret_database(tmp_path):
    """The records below are written the way a write stores them: header values go to the store, the router config
    resolves them (decision D26), so the request that reaches the engine carries the value the owner typed."""
    path = tmp_path / "secrets" / "admin.db"
    path.parent.mkdir()
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    yield path
    runtime_secrets.configure(None)


def _entry(registry: dict) -> dict:
    put = secret_refs.putter(secret_refs.database())
    registry = {**registry, "services": {sid: record_carriers.externalize_service(record, put)[0]
                                         for sid, record in registry["services"].items()}}
    compiled = litellm_runtime.compile_executable_config(registry)
    return next(row for row in compiled["model_list"] if row["model_name"] == "garage-chat")


def test_the_headers_and_the_extra_path_an_owner_typed_reach_the_engine(fake_upstreams):
    fake = fake_upstreams.loopback(load_scenario(
        "openai-compatible/engine-record-chat", tenant="family", model="demo-chat"))
    registry = _registry(f"{fake.base_url}/provider", path_prefix="/v1",
                         headers=[{"name": "X-Tenant", "value": "family"}])
    entry = _entry(registry)
    router = Router(model_list=[entry], num_retries=0)
    reply = asyncio.run(router.acompletion(model="garage-chat", messages=MESSAGES, **NO_RETRIES))
    assert reply.choices[0].message.content == "reached"
    [sent] = fake.calls("chat")
    assert sent.path == "/provider/v1/chat/completions"
    assert sent.headers["x-tenant"] == "family"


def test_the_reply_time_an_owner_set_ends_the_wait(fake_upstreams):
    fake = fake_upstreams.loopback(load_scenario(
        "openai-compatible/engine-record-slow", model="demo-chat"))
    entry = _entry(_registry(f"{fake.base_url}/v1", reply_timeout_s=1))
    assert entry["litellm_params"]["timeout"] == 1.0
    router = Router(model_list=[entry], num_retries=0)
    started = time.monotonic()
    with pytest.raises(litellm.Timeout):
        asyncio.run(router.acompletion(model="garage-chat", messages=MESSAGES, **NO_RETRIES))
    assert time.monotonic() - started < 10, "the engine never answers; only the saved reply time ends the wait"
    assert len(fake.calls("chat")) == 1


def test_the_headers_reach_an_embedding_engine_too(fake_upstreams):
    fake = fake_upstreams.loopback(load_scenario("openai-compatible/engine-record-embeddings", tenant="family", model="demo-chat"))
    entry = _entry(_registry(f"{fake.base_url}/v1", headers=[{"name": "X-Tenant", "value": "family"}]))
    router = Router(model_list=[entry], num_retries=0)
    reply = asyncio.run(router.aembedding(model="garage-chat", input="hello", **NO_RETRIES))
    assert len(reply.data) == 1
    assert fake.calls("embeddings")[0].headers["x-tenant"] == "family"


def test_the_gateway_waits_for_the_router_at_least_as_long_as_the_engine_reply_time(fake_upstreams, monkeypatch, tmp_path):
    """The hop in front of the router must not cut the owner's reply time short (600 s chat, 120 s embeddings)."""
    fake = fake_upstreams.asgi(load_scenario("openai-compatible/chat-text", model="garage-chat", content="ok"),
                               host="litellm.fk.test")
    db = use_admin_db(monkeypatch, tmp_path)
    register_litellm(monkeypatch, db, fake.base_url)
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"].update(_registry("http://garage.fk.test/v1", reply_timeout_s=1800)["services"])
    write_service_registry_for_test(registry, path=db)
    sent = []
    real = proxy.send_llm_execution_request

    async def spy(request, *args, **kwargs):
        sent.append(request)
        return await real(request, *args, **kwargs)

    monkeypatch.setattr(proxy, "send_llm_execution_request", spy)

    async def call():
        async with gateway_client() as client:
            return await client.post("/v1/chat/completions", json={"model": "garage-chat", "messages": MESSAGES})

    response = asyncio.run(call())
    assert response.status_code == 200, response.text
    assert sent[0].read_timeout == 1805.0


def test_the_hop_keeps_its_own_limit_when_the_engine_sets_none_or_a_shorter_one():
    registry = _registry("http://garage.fk.test/v1", reply_timeout_s=30)
    assert router_reply_timeout(registry, "garage-chat") == 30.0
    assert router_reply_timeout(registry, "another-model") is None
    hopped = {"schema_version": 1, "services": {"hopped": service_from_backend_config("hopped", {
        "name": "Hopped", "host": "h.fk.test", "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
        "capabilities": ["chat"], "gateway_lifecycle": True, "timeout": 900,
        "models": [{"model_group": "hopped-chat", "upstream_model": "hopped"}]}, source="ui")}}
    assert router_reply_timeout(hopped, "hopped-chat") is None, "the lifecycle hop applies its own time"


def test_a_header_stored_by_a_raw_edit_is_sent_only_when_it_is_safe():
    rows = [{"name": "X-Tenant", "value": "family", "public": True}, {"name": "X-Api-Key", "value": "sk-raw", "public": True},
            {"name": "Host", "value": "elsewhere", "public": True}, {"name": "X-Bad", "value": "a\r\nX-Injected: 1", "public": True},
            {"name": "bad name", "value": "x", "public": True}, {"name": "X-Empty", "value": "", "public": True},
            {"name": "X-Inline", "value": "written-by-hand"}]  # a value no migration has moved is never sent
    assert engine_headers({"headers": rows}) == {"X-Tenant": "family"}


def test_a_record_without_headers_or_a_reply_time_compiles_as_before():
    entry = _entry(_registry("http://plain.fk.test/v1"))
    # The form's default reply time is the gateway's own read limit, so a default changes nothing.
    assert entry["litellm_params"]["timeout"] == 600.0
    assert "extra_headers" not in entry["litellm_params"]
    legacy = {"schema_version": 1, "services": {"older": service_from_backend_config("older", {
        "name": "Older", "host": "older.fk.test", "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
        "capabilities": ["chat"], "models": [{"model_group": "older-chat", "upstream_model": "older"}]}, source="ui")}}
    params = compile_litellm_config_from_registry(legacy)["model_list"][0]["litellm_params"]
    assert "timeout" not in params and "extra_headers" not in params


@pytest.mark.parametrize("header", [
    {"name": "Authorization", "value": "Bearer sk-typed-here"},
    {"name": "X-Client", "value": "kept-secret", "secret": True},
    {"name": "X-API-Key", "value": "sk-typed-here"},
    {"name": "X-Auth-Token", "value": "t0k3n"},
])
def test_a_key_cannot_go_in_a_plain_header(header):
    """Header values are saved as plain text, so a key goes in the API key field instead."""
    definition = load_engine_catalog().definition("openai-compatible")
    from gateway.engine_check import DraftError

    with pytest.raises(DraftError) as refused:
        clean_values(definition, {"address": "http://plain.fk.test/v1", "headers": [header]})
    assert refused.value.field == "headers"
    assert "API key field" in str(refused.value)


def test_the_draft_real_test_sends_the_request_with_the_adjustments_applied():
    """The dialog's real test is the request the gateway would send: adjustments included."""
    from gateway.engine_check import KeyInput, run_real_test

    sent = []

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        from gateway.engine_check import Answer

        sent.append(json.loads(content))
        reply = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}]}
        return Answer(method=method, url=url, status=200, body=json.dumps(reply).encode())

    catalog = load_engine_catalog()
    definition = catalog.definition("openai-compatible")
    values = clean_values(definition, {"address": "http://garage.fk.test/v1", "transforms": [
        {"kind": "default-params", "set": {"temperature": 0.2}},
        {"kind": "drop-params", "drop": ["max_tokens"], "phase": "request"},
    ]})
    result = asyncio.run(run_real_test(catalog, definition, values, "chat", key=KeyInput(), fetch=fetch, model="demo-chat"))
    assert result["ok"] is True
    assert sent[0]["temperature"] == 0.2 and "max_tokens" not in sent[0]
    assert result["adjusted"] is True
    assert "with your request adjustments" in result["sent"]


def test_a_draft_real_test_whose_adjustment_refuses_the_request_sends_nothing():
    from gateway.engine_check import KeyInput, run_real_test

    async def fetch(*args, **kwargs):
        raise AssertionError("nothing may be sent")

    catalog = load_engine_catalog()
    definition = catalog.definition("openai-compatible")
    values = clean_values(definition, {"address": "http://garage.fk.test/v1", "transforms": ["no-such-adjustment"]})
    result = asyncio.run(run_real_test(catalog, definition, values, "chat", key=KeyInput(), fetch=fetch, model="demo-chat"))
    assert result["ok"] is False and result["adjusted"] is False
    assert "no-such-adjustment" in result["finding"]["what"]
