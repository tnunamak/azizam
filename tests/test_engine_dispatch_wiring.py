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
import time

import litellm
import pytest
from litellm import Router

from gateway.engine_admin import build_raw, clean_values
from gateway.engine_definitions import load_engine_catalog
from gateway.litellm_substrate import compile_litellm_config_from_registry
from gateway.service_registry import service_from_backend_config
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture

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


def _entry(registry: dict) -> dict:
    compiled = compile_litellm_config_from_registry(registry)
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
    {"name": "X-Client-Token", "value": "kept-secret", "secret": True},
])
def test_a_key_cannot_go_in_a_plain_header(header):
    """Header values are saved as plain text, so a key goes in the API key field instead."""
    definition = load_engine_catalog().definition("openai-compatible")
    from gateway.engine_check import DraftError

    with pytest.raises(DraftError) as refused:
        clean_values(definition, {"address": "http://plain.fk.test/v1", "headers": [header]})
    assert refused.value.field == "headers"
    assert "API key field" in str(refused.value)
