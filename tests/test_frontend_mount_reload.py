"""The frontend table mounts at boot and on reload, and a bad row never stops the gateway (contract section 5).

A row that collides or names an unknown translator is skipped and logged; its frontend is reported as
broken, to the admin API and to the Overview's attention list. A reload adds the rows that are new,
removes the rows that are gone, and a reload that fails leaves the table, and the config, as they were.
Builtin rows win every collision, whatever the order of the records.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import proxy
from gateway import frontend_mount, route_registry
from gateway.admin_api import derive_admin_attention
from tests.registry_test_helpers import write_service_registry_for_test
from tests.support.gateway import gateway_client, use_admin_db


async def _served(request: Request) -> dict:
    return {"served": request.url.path}


def _translators() -> frontend_mount.Translators:
    return frontend_mount.Translators({
        "identity": {
            "models.list": frontend_mount.Operation(_served, frozenset({"GET"}), self_authenticates=True),
            "chat.completions": frontend_mount.Operation(_served, frozenset({"POST"}), self_authenticates=True),
        },
    })


def _frontend(frontend_id: str, *routes: dict, source: str = "imported") -> dict:
    return {"id": frontend_id, "name": frontend_id.title(), "enabled": True, "source": source,
            "registered_routes": list(routes)}


def _get(path: str) -> dict:
    return {"methods": ["GET"], "canonical": path, "intent": "models.list", "translator": "identity"}


def _post(path: str) -> dict:
    return {"methods": ["POST"], "canonical": path, "intent": "chat.completions", "translator": "identity"}


def _mounts(app: FastAPI | None = None) -> frontend_mount.FrontendMounts:
    return frontend_mount.FrontendMounts(app or FastAPI(), _translators(), authenticate=None)


def _paths(mounts: frontend_mount.FrontendMounts) -> list[str]:
    return [route.path for route in mounts.app.routes if getattr(route, "path", "").startswith("/x")]


def _broken(mounts: frontend_mount.FrontendMounts) -> dict[str, list[dict]]:
    return {item["id"]: item["errors"] for item in mounts.status() if item["status"] == "broken"}


# --- boot ----------------------------------------------------------------------------------

def test_a_row_that_collides_with_post_chat_completions_is_skipped_and_the_others_mount():
    seed = route_registry.protocol_frontend_records(proxy_config=proxy.config)
    clash = _frontend("acme", _post("/v1/chat/completions"), _get("/acme/api/version"))
    mounts = frontend_mount.FrontendMounts(FastAPI(), proxy.FRONTEND_TRANSLATORS,
                                           authenticate=proxy._authenticate_mounted_route)

    # The package record comes first: the builtin row still keeps the path.
    mounts.apply([clash, *seed])

    chat = [route for route in mounts.app.routes
            if getattr(route, "path", "") == "/v1/chat/completions" and "POST" in route.methods]
    assert [route.endpoint for route in chat] == [proxy.chat_completions]
    assert any(getattr(route, "path", "") == "/acme/api/version" for route in mounts.app.routes)
    [error] = _broken(mounts)["acme"]
    assert "POST /v1/chat/completions" in error["message"]
    assert error["route"] == "/v1/chat/completions"
    statuses = {item["id"]: item["status"] for item in mounts.status()}
    assert statuses["acme"] == "broken"
    assert statuses["openai"] == "mounted"


def test_a_row_naming_an_unknown_translator_is_skipped_and_reported():
    mounts = _mounts()
    mounts.apply([
        _frontend("good", _get("/x/good")),
        _frontend("bad", {"methods": ["GET"], "canonical": "/x/bad", "intent": "models.list", "translator": "nope"}),
    ])

    assert _paths(mounts) == ["/x/good"]
    assert "nope" in _broken(mounts)["bad"][0]["message"]


def test_a_frontend_with_an_unreadable_strictness_is_broken_and_the_rest_mount():
    mounts = _mounts()
    mounts.apply([
        {**_frontend("sloppy", _get("/x/sloppy")), "strictness": "sloppy"},
        _frontend("good", _get("/x/good")),
    ])

    assert _paths(mounts) == ["/x/good"]
    assert "strictness" in _broken(mounts)["sloppy"][0]["message"]


def test_a_skipped_row_is_logged(capsys):
    mounts = _mounts()
    mounts.apply([_frontend("bad", {"methods": ["GET"], "canonical": "/x/bad", "intent": "models.list",
                                    "translator": "nope"})])

    assert "bad" in capsys.readouterr().out


def test_the_live_gateway_boots_with_every_builtin_row_mounted_and_none_broken():
    assert proxy.FRONTEND_MOUNTS.broken() == []
    assert {item["id"] for item in proxy.FRONTEND_MOUNTS.status()} >= {"openai", "anthropic", "ollama", "native"}


# --- reload --------------------------------------------------------------------------------

def test_a_reload_adds_a_new_row_and_removes_a_row_that_is_gone():
    mounts = _mounts()
    mounts.apply([_frontend("a", _get("/x/a"))])
    client = TestClient(mounts.app)
    assert client.get("/x/a").json() == {"served": "/x/a"}
    assert client.get("/x/b").status_code == 404

    mounts.apply([_frontend("a", _get("/x/a")), _frontend("b", _get("/x/b"))])
    assert client.get("/x/b").json() == {"served": "/x/b"}

    mounts.apply([_frontend("b", _get("/x/b"))])
    assert client.get("/x/a").status_code == 404
    assert client.get("/x/b").json() == {"served": "/x/b"}


def test_a_reload_keeps_the_rows_where_they_were_among_the_other_routes():
    app = FastAPI()
    mounts = _mounts(app)
    mounts.apply([_frontend("a", _get("/x/a"))])

    @app.get("/after")
    async def after() -> dict:
        return {}

    before = [getattr(route, "path", "") for route in app.routes]
    mounts.apply([_frontend("a", _get("/x/a")), _frontend("b", _get("/x/b"))])
    paths = [getattr(route, "path", "") for route in app.routes]

    assert paths.index("/x/a") < paths.index("/after")
    assert paths.index("/x/b") < paths.index("/after")
    assert [path for path in paths if path not in {"/x/b"}] == before


def test_a_reload_clears_the_broken_report_of_a_frontend_that_was_fixed():
    mounts = _mounts()
    mounts.apply([_frontend("a", {"methods": ["GET"], "canonical": "/x/a", "intent": "models.list",
                                  "translator": "nope"})])
    assert "a" in _broken(mounts)

    mounts.apply([_frontend("a", _get("/x/a"))])

    assert _broken(mounts) == {}
    assert _paths(mounts) == ["/x/a"]


def test_a_reload_that_fails_leaves_the_table_and_the_report_as_they_were():
    mounts = _mounts()
    mounts.apply([
        _frontend("a", _get("/x/a")),
        _frontend("bad", {"methods": ["GET"], "canonical": "/x/bad", "intent": "models.list", "translator": "nope"}),
    ])
    client = TestClient(mounts.app)
    routes_before = list(mounts.app.routes)
    report_before = mounts.status()

    class Boom(RuntimeError):
        pass

    original = mounts.translators.operation

    def failing(translator_id: str, intent: str):
        if intent == "chat.completions":
            raise Boom("translator lookup failed")
        return original(translator_id, intent)

    mounts.translators.operation = failing  # type: ignore[method-assign]
    with pytest.raises(Boom):
        mounts.apply([_frontend("a", _get("/x/a")), _frontend("b", _get("/x/b"), _post("/x/b/chat"))])

    assert list(mounts.app.routes) == routes_before
    assert mounts.status() == report_before
    assert client.get("/x/a").json() == {"served": "/x/a"}
    assert client.get("/x/b").status_code == 404


def test_a_commit_that_fails_midway_restores_the_routes(monkeypatch):
    mounts = _mounts()
    mounts.apply([_frontend("a", _get("/x/a"))])
    routes_before = list(mounts.app.routes)
    prepared = mounts.prepare([_frontend("b", _get("/x/b"))])

    def broken_swap(*args, **kwargs):
        raise RuntimeError("swap failed")

    monkeypatch.setattr(frontend_mount.FrontendMounts, "_swap", broken_swap)
    with pytest.raises(RuntimeError):
        prepared.commit()

    assert list(mounts.app.routes) == routes_before
    assert _paths(mounts) == ["/x/a"]


# --- the live gateway ----------------------------------------------------------------------

def _publish_registry(db, *frontends: dict) -> None:
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["protocol_frontends"] = {item["id"]: item for item in frontends}
    write_service_registry_for_test(registry, path=db)


@pytest.fixture
def live(monkeypatch, tmp_path):
    """The live gateway on a fresh admin store; its config and runtime constants are put back, and the table remounted."""
    db = use_admin_db(monkeypatch, tmp_path)
    for name in ("config", "KOBOLD_HOST", "TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "COMFY_HOST", "TTS_ENABLED",
                 "STT_ENABLED", "STT_TIMEOUT", "EMBEDDINGS_ENABLED", "COMFY_ENABLED", "LLM_BACKEND_HOSTS"):
        monkeypatch.setattr(proxy, name, getattr(proxy, name))
    boot = route_registry.mount_frontend_records(registry=proxy._PROTOCOL_ROUTE_REGISTRY, proxy_config=proxy.config)
    yield db
    proxy.FRONTEND_MOUNTS.apply(boot)


def _get_status(path: str) -> int:
    async def call() -> int:
        async with gateway_client() as client:
            return (await client.get(path)).status_code

    return asyncio.run(call())


def test_reloading_the_runtime_config_mounts_a_new_package_row_and_unmounts_a_removed_one(live):
    acme = _frontend("acme", {"methods": ["GET"], "canonical": "/acme/api/version", "intent": "version",
                              "translator": "ollama"})
    assert _get_status("/acme/api/version") == 404

    _publish_registry(live, acme)
    proxy.reload_runtime_config()
    assert _get_status("/acme/api/version") == 200

    _publish_registry(live)
    proxy.reload_runtime_config()
    assert _get_status("/acme/api/version") == 404


def test_a_reload_that_cannot_mount_leaves_the_old_config_and_routes_serving(live, monkeypatch):
    acme = _frontend("acme", {"methods": ["GET"], "canonical": "/acme/api/version", "intent": "version",
                              "translator": "ollama"})
    _publish_registry(live, acme)
    proxy.reload_runtime_config()
    config_before = proxy.config
    routes_before = list(proxy.app.router.routes)

    _publish_registry(live, acme, _frontend("beta", {"methods": ["GET"], "canonical": "/beta/api/version",
                                                     "intent": "version", "translator": "ollama"}))

    def failing(*args, **kwargs):
        raise RuntimeError("mount failed")

    with monkeypatch.context() as patched:
        patched.setattr(proxy.FRONTEND_MOUNTS, "prepare", failing)
        with pytest.raises(RuntimeError, match="mount failed"):
            proxy.reload_runtime_config()

    assert proxy.config is config_before
    assert proxy.app.router.routes == routes_before
    assert _get_status("/acme/api/version") == 200
    assert _get_status("/beta/api/version") == 404


def test_a_package_row_forced_into_the_store_does_not_mount_a_privileged_translator_on_reload(live):
    forced = _frontend("forced", {"methods": ["GET"], "canonical": "/x/{provider}", "intent": "provider.detail",
                                  "translator": "provider-native"}, source="builtin")
    _publish_registry(live, forced)

    proxy.reload_runtime_config()

    assert _get_status("/x/garage") == 404
    broken = {item["id"]: item for item in proxy.FRONTEND_MOUNTS.status() if item["status"] == "broken"}
    assert "package" in broken["forced"]["errors"][0]["message"]


# --- the admin surfaces --------------------------------------------------------------------

def test_a_broken_frontend_is_an_attention_item_with_its_reason():
    items = derive_admin_attention({"frontends": [
        {"id": "ok", "name": "Fine", "status": "mounted", "errors": []},
        {"id": "acme", "name": "Acme", "status": "broken",
         "errors": [{"route": "/v1/chat/completions", "message": "POST /v1/chat/completions is registered twice"}]},
    ]})

    [item] = [item for item in items if item["kind"] == "frontend"]
    assert item["severity"] == "problem"
    assert "Acme" in item["title"]
    assert "registered twice" in item["detail"]


def test_the_admin_overview_lists_every_frontend_with_its_mount_status_and_flags_a_broken_one():
    from gateway.admin_api import create_admin_router
    from tests.test_admin_api import FakeAdminStore, _admin_auth, _auth

    status = [
        {"id": "openai", "name": "OpenAI-compatible", "status": "mounted", "errors": []},
        {"id": "acme", "name": "Acme", "status": "broken",
         "errors": [{"route": "/v1/chat/completions", "message": "POST /v1/chat/completions is registered twice"}]},
    ]
    app = FastAPI()
    app.include_router(create_admin_router(FakeAdminStore(), auth=_admin_auth, frontend_status=lambda: status))

    response = TestClient(app).get("/admin/api/overview", headers=_auth())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["frontends"] == status
    [item] = [item for item in body["attention"] if item["kind"] == "frontend"]
    assert "Acme" in item["title"] and "registered twice" in item["detail"]


def test_the_live_overview_reports_the_frontend_table():
    from gateway.admin_api import create_admin_router
    from tests.test_admin_api import FakeAdminStore, _admin_auth, _auth

    app = FastAPI()
    app.include_router(create_admin_router(
        FakeAdminStore(), auth=_admin_auth, frontend_status=lambda: proxy.FRONTEND_MOUNTS.status()))

    body = TestClient(app).get("/admin/api/overview", headers=_auth()).json()

    assert {item["id"] for item in body["frontends"]} >= {"openai", "anthropic", "ollama", "native"}
    assert [item for item in body["attention"] if item["kind"] == "frontend"] == []
