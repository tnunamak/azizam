"""An edited engine's headers and reply time reach it on a real inference request (round 4).

The request goes into the gateway's chat route with an App key, out to the
model router, and on to a fake engine on a loopback port. The router is a
stand-in that routes with the real LiteLLM Router over the config the gateway
compiles from its registry (tests/support/router_sidecar.py): the sidecar
process itself is not started in tests, so its HTTP server is the one part not
exercised. The engine record is written by the same build step the Add
engine routes use, then edited.
"""

from __future__ import annotations

import asyncio
import time

import proxy
from gateway.engine_admin import build_raw, clean_values
from gateway.engine_definitions import load_engine_catalog
from gateway.service_registry import service_from_backend_config
from tests.registry_test_helpers import write_service_registry_for_test
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.support.gateway import gateway_client, register_litellm, use_admin_db
from tests.support.router_sidecar import router_sidecar

MESSAGES = [{"role": "user", "content": "hi"}]


def _save_engine(db, address: str, **values) -> None:
    catalog = load_engine_catalog()
    definition = catalog.definition("openai-compatible")
    clean = clean_values(definition, {"address": address, **values})
    raw = build_raw(catalog, definition, clean, name="Garage GPU", capabilities=["chat"],
                    models=[{"id": "demo-chat", "public_name": "garage-chat"}], enabled=True,
                    check={}, tests=None, secret_id=None)
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"]["garage"] = service_from_backend_config("garage", raw, source="ui")
    write_service_registry_for_test(registry, path=db)


def _gateway(fake_upstreams, monkeypatch, tmp_path):
    db = use_admin_db(monkeypatch, tmp_path)
    fake_upstreams.router.add("router.fk.test", router_sidecar(proxy.ADMIN_STORE.get_service_registry))
    register_litellm(monkeypatch, db, "http://router.fk.test")
    return db


def _chat():
    async def call():
        async with gateway_client() as client:
            return await client.post("/v1/chat/completions", json={"model": "garage-chat", "messages": MESSAGES})

    return asyncio.run(call())


def test_an_edited_header_and_extra_path_arrive_on_an_inference_request(fake_upstreams, monkeypatch, tmp_path):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/engine-record-chat", tenant="family", model="demo-chat"))
    db = _gateway(fake_upstreams, monkeypatch, tmp_path)
    _save_engine(db, f"{engine.base_url}/provider", path_prefix="/v1", headers=[{"name": "X-Tenant", "value": "before-edit"}])
    # The owner edits the header; the scenario answers only the edited value.
    _save_engine(db, f"{engine.base_url}/provider", path_prefix="/v1", headers=[{"name": "X-Tenant", "value": "family"}])
    response = _chat()
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "reached"
    [sent] = engine.calls("chat")
    assert sent.path == "/provider/v1/chat/completions"
    assert sent.headers["x-tenant"] == "family"


def test_an_edited_reply_time_ends_an_inference_request(fake_upstreams, monkeypatch, tmp_path):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/engine-record-slow", model="demo-chat"))
    db = _gateway(fake_upstreams, monkeypatch, tmp_path)
    _save_engine(db, f"{engine.base_url}/v1")  # the default: the gateway's own limit
    _save_engine(db, f"{engine.base_url}/v1", reply_timeout_s=1)  # edited
    started = time.monotonic()
    response = _chat()
    elapsed = time.monotonic() - started
    assert response.status_code >= 400, response.text
    assert elapsed < 10, f"the request waited {elapsed:.1f} s; the engine's 1 s reply time did not apply"
    assert len(engine.calls("chat")) == 1
