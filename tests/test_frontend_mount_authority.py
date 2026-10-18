"""A frontend row cannot reach a handler that the mount does not authenticate (contract section 5).

Two rules hold together. Every mounted handler is authenticated inside the mount wrapper, whatever
its path prefix, so a row at ``/x/{provider}`` never reaches the provider-native code without a key.
And only builtin rows may name a privileged translator (provider-native, native, gateway, realtime):
a package, UI or imported row may name only translators marked package-mountable, checked when the
package is reviewed and again when the table mounts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import proxy
from gateway import frontend_mount, route_registry
from gateway.extension_packages import review_local_directory_package
from tests.support.access import plan_for
from tests.test_runtime_route_policy import AUTH_HEADERS, authenticated_profile

PRIVILEGED_ROW = {
    "methods": ["GET"],
    "canonical": "/x/{provider}",
    "intent": "provider.detail",
    "translator": "provider-native",
}


def _frontend(source: str, *routes: dict, frontend_id: str = "vendor") -> dict:
    return {"id": frontend_id, "enabled": True, "source": source, "registered_routes": list(routes)}


def _package(tmp_path: Path, row_yaml: str) -> Path:
    package = tmp_path / "package"
    (package / "objects").mkdir(parents=True)
    (package / "extension.yml").write_text(
        "schema_version: 1\nkind: extension_package\nid: demo\nversion: 0.1.0\n"
        "objects:\n  protocol_frontends:\n    - path: objects/frontend.yml\n",
        encoding="utf-8",
    )
    (package / "objects" / "frontend.yml").write_text(
        "id: demo-api\nname: Demo API\nprotocol: demo\nsource: builtin\nroutes:\n  - /demo\n"
        "registered_routes:\n" + row_yaml,
        encoding="utf-8",
    )
    return package


def test_a_package_row_naming_provider_native_is_refused_at_import(tmp_path):
    review = review_local_directory_package(_package(tmp_path, (
        "  - methods: [GET]\n    canonical: /x/{provider}\n    intent: provider.detail\n"
        "    translator: provider-native\n"
    )))

    assert review["ok"] is False
    assert any("provider-native" in error and "package" in error for error in review["errors"]), review["errors"]


def test_a_package_row_naming_a_package_mountable_translator_is_accepted_at_import(tmp_path):
    review = review_local_directory_package(_package(tmp_path, (
        "  - methods: [POST]\n    canonical: /demo/v1/chat/completions\n    intent: chat.completions\n"
        "    translator: identity\n"
    )))

    assert review["errors"] == []


def test_a_package_row_whose_path_opens_with_a_parameter_is_refused_at_import(tmp_path):
    review = review_local_directory_package(_package(tmp_path, (
        "  - methods: [GET]\n    canonical: /{anything:path}\n    intent: models.list\n    translator: identity\n"
    )))

    assert any("literal" in error for error in review["errors"]), review["errors"]


@pytest.mark.parametrize("translator", ["provider-native", "native", "gateway", "realtime"])
def test_a_row_that_is_not_builtin_cannot_mount_a_privileged_translator(translator):
    intent = {
        "provider-native": "provider.detail", "native": "engine.native",
        "gateway": "media.job.content", "realtime": "realtime.session",
    }[translator]
    methods = ["WEBSOCKET"] if translator == "realtime" else ["GET"]
    for source in ("imported", "ui", "provisioned", ""):
        rows = frontend_mount.rows_from_frontends([_frontend(
            source, {"methods": methods, "canonical": "/x/{provider}", "intent": intent, "translator": translator},
        )])
        with pytest.raises(frontend_mount.FrontendMountError, match="package"):
            frontend_mount.mount(FastAPI(), rows, proxy.FRONTEND_TRANSLATORS)


def test_a_builtin_row_may_mount_a_privileged_translator():
    rows = frontend_mount.rows_from_frontends([_frontend("builtin", PRIVILEGED_ROW)])
    routes = frontend_mount.mount(
        FastAPI(), rows, proxy.FRONTEND_TRANSLATORS, authenticate=proxy._authenticate_mounted_route,
    )
    assert [route.path for route in routes] == ["/x/{provider}"]


def test_a_stored_row_that_claims_builtin_without_a_seed_row_is_not_builtin():
    forged = _frontend("builtin", PRIVILEGED_ROW, frontend_id="forged")
    stored = {"protocol_frontends": {"forged": forged}}

    records = route_registry.mount_frontend_records(registry=stored, proxy_config=proxy.config)
    [record] = [record for record in records if record["id"] == "forged"]

    assert record["source"] != "builtin"
    rows = frontend_mount.rows_from_frontends([record])
    with pytest.raises(frontend_mount.FrontendMountError, match="package"):
        frontend_mount.mount(FastAPI(), rows, proxy.FRONTEND_TRANSLATORS)


def test_a_stored_builtin_frontend_takes_its_routes_from_the_seed_even_when_the_seed_names_none():
    seed = route_registry.protocol_frontend_records(proxy_config=proxy.config)
    [openai] = [record for record in seed if record["id"] == "openai"]
    stored_openai = {**openai, "registered_routes": [PRIVILEGED_ROW]}
    stored = {"protocol_frontends": {"openai": stored_openai}}

    records = route_registry.mount_frontend_records(registry=stored, proxy_config=proxy.config)
    [record] = [record for record in records if record["id"] == "openai"]

    assert PRIVILEGED_ROW not in record["registered_routes"]


def test_a_privileged_row_forced_into_the_live_table_refuses_a_caller_without_a_key():
    rows = frontend_mount.rows_from_frontends([_frontend("builtin", PRIVILEGED_ROW)])
    routes = frontend_mount.mount(
        proxy.app, rows, proxy.FRONTEND_TRANSLATORS,
        index=proxy.app.router.routes.index(proxy.WEB_UI_ROUTE),
        authenticate=proxy._authenticate_mounted_route, refuse=proxy._frontend_refusal,
    )
    try:
        client = TestClient(proxy.app)
        anonymous = client.get("/x/garage")
        wrong_key = client.get("/x/garage", headers={"Authorization": "Bearer not-a-key"})
        with authenticated_profile(plan_for("mounted-auth", caps=())):
            keyed = client.get("/x/garage", headers=AUTH_HEADERS)
    finally:
        for route in routes:
            proxy.app.router.routes.remove(route)

    assert anonymous.status_code == 401, anonymous.text
    assert wrong_key.status_code == 401, wrong_key.text
    assert keyed.status_code != 401, keyed.text


def test_a_mount_without_an_authenticator_refuses_an_operation_that_does_not_authenticate_itself():
    async def endpoint(request: Request) -> dict:
        return {"ok": True}

    translators = frontend_mount.Translators({
        "identity": {"models.list": frontend_mount.Operation(endpoint, frozenset({"GET"}))},
    })
    rows = frontend_mount.rows_from_frontends([_frontend("builtin", {
        "methods": ["GET"], "canonical": "/x/models", "intent": "models.list", "translator": "identity",
    })])

    with pytest.raises(frontend_mount.FrontendMountError, match="authenticat"):
        frontend_mount.mount(FastAPI(), rows, translators)


def test_the_mount_authenticates_before_the_handler_on_any_prefix():
    calls: list[str] = []

    async def endpoint(request: Request) -> dict:
        calls.append("handler")
        return {"ok": True}

    async def authenticate(request: Request):
        calls.append("authenticate")
        from fastapi.responses import JSONResponse
        if request.headers.get("authorization") != "Bearer k":
            return JSONResponse({"error": "key_required"}, status_code=401)
        return None

    translators = frontend_mount.Translators({
        "identity": {"models.list": frontend_mount.Operation(endpoint, frozenset({"GET"}))},
    })
    rows = frontend_mount.rows_from_frontends([_frontend("builtin", {
        "methods": ["GET"], "canonical": "/anywhere/models", "intent": "models.list", "translator": "identity",
    })])
    app = FastAPI()
    frontend_mount.mount(app, rows, translators, authenticate=authenticate)
    client = TestClient(app)

    assert client.get("/anywhere/models").status_code == 401
    assert calls == ["authenticate"]
    assert client.get("/anywhere/models", headers={"authorization": "Bearer k"}).status_code == 200
    assert calls == ["authenticate", "authenticate", "handler"]


# Operations that check the caller inside the handler (with their own error shape, or by signature).
SELF_AUTHENTICATING = {"identity", "anthropic", "ollama", "realtime", "gateway"}
PACKAGE_MOUNTABLE = {"identity", "anthropic", "ollama"}


def test_only_the_reviewed_translators_authenticate_themselves_and_only_three_are_package_mountable():
    translators = proxy.FRONTEND_TRANSLATORS
    ids = set(translators.ids())
    assert {"provider-native", "native"} <= ids
    for translator_id in ids:
        operations = translators.operations(translator_id)
        assert operations
        for intent, operation in operations.items():
            assert operation.self_authenticates is (translator_id in SELF_AUTHENTICATING), (translator_id, intent)
        assert translators.package_mountable(translator_id) is (translator_id in PACKAGE_MOUNTABLE), translator_id
    assert frontend_mount.PACKAGE_MOUNTABLE_TRANSLATORS == frozenset(PACKAGE_MOUNTABLE)
