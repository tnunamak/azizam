"""Inbound frontends mount from data: a row is enough for an OpenAI-shaped vendor, and proxy.py is untouched.

The acme frontend below exists only as data, the way an imported or UI-authored frontend record
does. Its one route row binds POST /acme/v1/chat/completions to the canonical chat intent through
the identity translator. The test mounts it on the live app, serves a chat request through a fake
engine, reads the receipt, and removes the route again.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, Request

import proxy
from gateway import frontend_mount, receipt_store, route_registry
from gateway.service_registry import service_from_backend_config
from gateway.engine_admin import build_raw, clean_values
from gateway.engine_definitions import load_engine_catalog
from tests.registry_test_helpers import write_service_registry_for_test
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.support.gateway import gateway_client, register_litellm, use_admin_db
from tests.support.router_sidecar import router_sidecar

ACME_FRONTEND = {
    "id": "acme",
    "name": "Acme",
    "protocol": "acme",
    "source": "imported",
    "enabled": True,
    "registered_routes": [
        {
            "methods": ["POST"],
            "canonical": "/acme/v1/chat/completions",
            "intent": "chat.completions",
            "translator": "identity",
        },
    ],
}
MESSAGES = [{"role": "user", "content": "hi"}]


@contextmanager
def mounted(app, rows, translators, *, before):
    """Mount rows ahead of ``before`` on a live app, and take them off again afterwards."""
    routes = frontend_mount.mount(app, rows, translators, index=app.router.routes.index(before))
    try:
        yield routes
    finally:
        for route in routes:
            app.router.routes.remove(route)


def _save_engine(db, address: str) -> None:
    catalog = load_engine_catalog()
    definition = catalog.definition("openai-compatible")
    clean = clean_values(definition, {"address": address, "path_prefix": "/v1"})
    raw = build_raw(catalog, definition, clean, name="Garage GPU", capabilities=["chat"],
                    models=[{"id": "demo-chat", "public_name": "garage-chat"}], enabled=True,
                    check={}, tests=None, secret_id=None)
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"]["garage"] = service_from_backend_config("garage", raw, source="ui")
    write_service_registry_for_test(registry, path=db)


def test_a_data_only_frontend_serves_chat_through_a_fake_engine_and_writes_a_receipt(
    fake_upstreams, monkeypatch, tmp_path,
):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/chat-text", model="demo-chat", content="reached"))
    db = use_admin_db(monkeypatch, tmp_path)
    receipts = tmp_path / "receipts.db"
    receipt_store.init_db(receipts)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts)
    fake_upstreams.router.add("router.fk.test", router_sidecar(proxy.ADMIN_STORE.get_service_registry))
    register_litellm(monkeypatch, db, "http://router.fk.test")
    _save_engine(db, engine.base_url)
    rows = frontend_mount.rows_from_frontends([ACME_FRONTEND])

    async def call():
        async with gateway_client() as client:
            return await client.post("/acme/v1/chat/completions", json={"model": "garage-chat", "messages": MESSAGES})

    with mounted(proxy.app, rows, proxy.FRONTEND_TRANSLATORS, before=proxy.WEB_UI_ROUTE):
        response = asyncio.run(call())

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "reached"
    assert len(engine.calls("chat")) == 1
    receipt = receipt_store.get(response.headers["x-azizam-request-id"], audience="internal", path=receipts)
    assert receipt is not None
    assert receipt["path"] == "/v1/chat/completions"


def test_the_acme_route_is_gone_once_the_row_is_removed(monkeypatch):
    rows = frontend_mount.rows_from_frontends([ACME_FRONTEND])
    with mounted(proxy.app, rows, proxy.FRONTEND_TRANSLATORS, before=proxy.WEB_UI_ROUTE):
        assert any(getattr(route, "path", "") == "/acme/v1/chat/completions" for route in proxy.app.routes)
    assert not any(getattr(route, "path", "") == "/acme/v1/chat/completions" for route in proxy.app.routes)


def test_a_new_row_for_an_existing_translator_mounts_its_handler_without_a_proxy_change():
    app = FastAPI()
    rows = frontend_mount.rows_from_frontends([{
        "id": "acme-claude",
        "enabled": True,
        "registered_routes": [{
            "methods": ["POST"],
            "canonical": "/acme/anthropic/v1/messages",
            "intent": "chat.completions",
            "translator": "anthropic",
        }],
    }])

    [route] = frontend_mount.mount(app, rows, proxy.FRONTEND_TRANSLATORS)

    assert route.path == "/acme/anthropic/v1/messages"
    assert route.endpoint is proxy.anthropic_messages
    assert route.methods == {"POST"}


def test_every_row_in_the_seed_registry_names_an_operation_the_translators_serve():
    rows = frontend_mount.rows_from_frontends(
        route_registry.protocol_frontend_records(proxy_config=proxy.config),
    )
    assert rows
    for row in rows:
        proxy.FRONTEND_TRANSLATORS.operation(row.translator, row.intent)


# --- the mount table's own rules, on a bare app -------------------------------------------

def _echo(request: Request) -> None:
    return None


def _translators() -> frontend_mount.Translators:
    return frontend_mount.Translators({
        "identity": {
            "chat.completions": frontend_mount.Operation(_echo, methods=frozenset({"POST"})),
            "models.list": frontend_mount.Operation(_echo, methods=frozenset({"GET"})),
        },
    })


def _frontend(*routes) -> dict:
    return {"id": "fixture", "enabled": True, "registered_routes": list(routes)}


def test_a_method_and_path_can_be_registered_once():
    rows = frontend_mount.rows_from_frontends([_frontend(
        {"methods": ["POST"], "canonical": "/x/v1/chat", "intent": "chat.completions", "translator": "identity"},
        {"methods": ["POST"], "canonical": "/x/v1/chat", "intent": "chat.completions", "translator": "identity"},
    )])
    with pytest.raises(frontend_mount.FrontendMountError, match="/x/v1/chat"):
        frontend_mount.mount(FastAPI(), rows, _translators())


def test_the_same_path_with_another_method_is_a_different_key():
    rows = frontend_mount.rows_from_frontends([_frontend(
        {"methods": ["POST"], "canonical": "/x/v1/items", "intent": "chat.completions", "translator": "identity"},
        {"methods": ["GET"], "canonical": "/x/v1/items", "intent": "models.list", "translator": "identity"},
    )])
    created = frontend_mount.mount(FastAPI(), rows, _translators())
    assert sorted(sorted(route.methods) for route in created) == [["GET"], ["POST"]]


def test_an_unknown_translator_or_intent_is_refused():
    unknown_translator = frontend_mount.rows_from_frontends([_frontend(
        {"methods": ["POST"], "canonical": "/x/a", "intent": "chat.completions", "translator": "nope"},
    )])
    with pytest.raises(frontend_mount.FrontendMountError, match="nope"):
        frontend_mount.mount(FastAPI(), unknown_translator, _translators())

    unknown_intent = frontend_mount.rows_from_frontends([_frontend(
        {"methods": ["POST"], "canonical": "/x/b", "intent": "images.generate", "translator": "identity"},
    )])
    with pytest.raises(frontend_mount.FrontendMountError, match="images.generate"):
        frontend_mount.mount(FastAPI(), unknown_intent, _translators())


def test_a_method_the_handler_does_not_serve_is_refused():
    rows = frontend_mount.rows_from_frontends([_frontend(
        {"methods": ["GET"], "canonical": "/x/v1/chat", "intent": "chat.completions", "translator": "identity"},
    )])
    with pytest.raises(frontend_mount.FrontendMountError, match="GET"):
        frontend_mount.mount(FastAPI(), rows, _translators())


def test_disabled_frontends_and_rows_without_a_translator_are_not_mounted():
    disabled = dict(_frontend(
        {"methods": ["POST"], "canonical": "/x/v1/chat", "intent": "chat.completions", "translator": "identity"},
    ), enabled=False)
    owner_mounted = _frontend(
        {"methods": ["POST"], "canonical": "/x/v1/owned", "intent": "chat.completions"},
    )
    assert frontend_mount.rows_from_frontends([disabled, owner_mounted]) == []


def test_a_translator_id_is_registered_once():
    translators = _translators()
    with pytest.raises(frontend_mount.FrontendMountError, match="identity"):
        translators.register("identity", {})
