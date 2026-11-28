"""Add engine: a key the owner stored never comes back in an answer (security review r1, findings 3 and 4).

The fakes below answer with a synthetic sentinel key, as an engine that
echoes what it was sent would. Nothing a check, a test, a saved check summary
or the engine view returns may hold it, for the owner or an administrator.
An administrator whose request carried the owner's stored key also gets no
raw server text at all: a scrub catches the key's usual spellings, but a
hostile server could split or re-encode it.

Header rows whose name carries a credential are the owner's alone on every
route that takes values, whatever the row's ``secret`` flag says.
"""

from __future__ import annotations

import base64
import json

import pytest

from gateway import engine_check, secret_store, service_registry
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture
from tests.test_engine_admin_routes import TOKEN, _create, _edit, _put_registry, _registry, _silent
from tests.test_engine_admin_routes import fake_engine_dns, fixed_baseline  # noqa: F401 - fixtures
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

SENTINEL = "sk-SENTINEL-7f3a91c2d4"
SPELLINGS = [SENTINEL, base64.b64encode(f"Bearer {SENTINEL}".encode()).decode(), "".join(f"%{b:02X}" for b in SENTINEL.encode())]
PROVENANCE = {"source": "hand-written", "reference": "add-engine-security-review-r1.md finding 3"}
WITHHELD = "only the owner sees"


def _echo(where: str) -> str:
    return f"You sent Authorization: Bearer {SENTINEL} (b64 {base64.b64encode(f'Bearer {SENTINEL}'.encode()).decode()}) from {where}"


def _baselines() -> list[dict]:
    return [{"id": f"baseline-{method.lower()}", "match": {"method": method, "path": f"/v1/zz-not-a-route-{TOKEN}"},
             "response": {"status": 404, "json": {"detail": "Not Found"}}, "repeat": True, "required": False}
            for method in ("GET", "POST")]


def _exchange(ident: str, method: str, path: str, status: int, *, body=None, headers=None) -> dict:
    response: dict = {"status": status, "json": body if body is not None else {"detail": "Not Found"}}
    if headers:
        response["headers"] = headers
    return {"id": ident, "match": {"method": method, "path": path}, "response": response, "repeat": True, "required": False}


def _engine(fakes, gateway, host: str, exchanges: list[dict]):
    """A fake engine at ``host`` and a saved engine record holding the owner's key for it."""
    fake = fakes.asgi(scenario_from_data({"protocol": "engine-probe", "name": host.split(".")[0], "provenance": PROVENANCE,
                                          "exchanges": _baselines() + exchanges}), host=host)
    origin = engine_check.origin_of(fake.base_url)
    secret_id = f"engine:{host.split('.')[0]}:api_key:test"
    secret_store.put_provider_secret(secret_id, SENTINEL, db_path=gateway.path, approved_origin=origin)
    registry = _registry(gateway)
    service_id = host.split(".")[0]
    registry.setdefault("services", {})[service_id] = service_registry.service_from_backend_config(service_id, {
        "name": "Echo engine", "host": host, "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
        "definition": "openai-compatible", "definition_version": 1, "address": f"{fake.base_url}/v1",
        "capabilities": ["chat", "completions"], "api_key_secret_id": secret_id, "credential_origin": origin,
        "models": [{"model_group": "echo-chat", "upstream_model": "echo-chat"}]}, source="ui")
    _put_registry(gateway, registry)
    return fake, service_id


def _assert_hidden(text: str) -> None:
    for spelling in SPELLINGS:
        assert spelling not in text, f"{spelling!r} came back"


def _check(client, service_id, fake):
    return client.post("/admin/api/engines/check", headers=csrf_for(client), json={
        "definition": "openai-compatible", "service_id": service_id, "values": {"address": f"{fake.base_url}/v1"}})


def _test(client, service_id, fake):
    return client.post("/admin/api/engines/test", headers=csrf_for(client), json={
        "definition": "openai-compatible", "service_id": service_id, "values": {"address": f"{fake.base_url}/v1"},
        "capability": "chat", "model": "echo-chat"})


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_a_refusal_that_echoes_the_stored_key_does_not_return_it(gateway, fake_upstreams, who):
    owner, _, _, admin = owner_and_admin(gateway)
    fake, service_id = _engine(fake_upstreams, gateway, "echo.fk.test", [
        _exchange("models", "GET", "/v1/models", 401, body={"error": {"message": _echo("models")}},
                  headers={"content-type": f"application/json; echo={SENTINEL}", "location": f"http://x.fk.test/?k={SENTINEL}"}),
    ])
    answered = _check(owner if who == "owner" else admin, service_id, fake)
    assert answered.status_code == 200, answered.text
    assert fake.requests[0].headers["authorization"] == f"Bearer {SENTINEL}", "the stored key was sent"
    _assert_hidden(answered.text)
    finding = answered.json()["check"]["findings"][0]
    assert finding["code"] == "auth_rejected"
    if who == "admin":
        assert WITHHELD in finding["details"]["raw"]
        assert "You sent" not in answered.text, "an administrator gets no raw server text after the owner's key was sent"
    else:
        assert "You sent Authorization: [hidden]" in finding["details"]["raw"]


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_a_redirect_that_echoes_the_stored_key_does_not_return_it(gateway, fake_upstreams, who):
    owner, _, _, admin = owner_and_admin(gateway)
    fake, service_id = _engine(fake_upstreams, gateway, "bounce.fk.test", [
        _exchange("models", "GET", "/v1/models", 302, body={},
                  headers={"location": f"http://x.fk.test/login?key={SENTINEL}", "content-type": f"text/html; k={SENTINEL}"}),
    ])
    answered = _check(owner if who == "owner" else admin, service_id, fake)
    assert answered.json()["check"]["findings"][0]["code"] == "redirect"
    _assert_hidden(answered.text)


@pytest.mark.parametrize("who", ["owner", "admin"])
@pytest.mark.parametrize("status", [200, 400])
def test_a_real_test_reply_that_echoes_the_stored_key_does_not_return_it(gateway, fake_upstreams, who, status):
    owner, _, _, admin = owner_and_admin(gateway)
    reply = ({"id": "x", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": _echo("chat")}}]}
             if status == 200 else {"error": {"message": _echo("chat")}})
    fake, service_id = _engine(fake_upstreams, gateway, f"reply{status}.fk.test", [
        _exchange("chat", "POST", "/v1/chat/completions", status, body=reply),
    ])
    answered = _test(owner if who == "owner" else admin, service_id, fake)
    assert answered.status_code == 200, answered.text
    assert answered.json()["test"]["ok"] is (status == 200)
    _assert_hidden(answered.text)
    if who == "admin":
        assert "You sent" not in answered.text


def test_a_saved_check_summary_and_the_engine_view_never_hold_the_echoed_key(gateway, fake_upstreams):
    owner, _, _, admin = owner_and_admin(gateway)
    fake, service_id = _engine(fake_upstreams, gateway, "summary.fk.test", [
        _exchange("models", "GET", "/v1/models", 200, body={"data": [{"id": f"model-{SENTINEL}"}, {"id": "echo-chat"}]}),
        _exchange("chat", "POST", "/v1/chat/completions", 400, body={"error": {"message": _echo("chat route")}}),
        _exchange("embeddings", "POST", "/v1/embeddings", 404),
        _exchange("openapi", "GET", "/openapi.json", 404),
        _exchange("capabilities", "GET", "/v1/models/capabilities", 404),
    ])
    saved = _edit(owner, service_id, {"definition": "openai-compatible", "recheck": True, "acknowledge_warnings": True,
                                      "values": {}})
    assert saved.status_code == 200, saved.text
    _assert_hidden(saved.text)
    _assert_hidden(json.dumps(_registry(gateway)))
    for client in (owner, admin):
        view = client.get(f"/admin/api/engines/{service_id}")
        assert view.status_code == 200
        _assert_hidden(view.text)


# ---------------------------------------------------------------------------
# Finding 4: one credential rule on every route that takes values
# ---------------------------------------------------------------------------

CREDENTIAL_ROWS = [
    {"name": "Authorization", "value": "Bearer sk-header-sentinel", "secret": True},
    {"name": "Authorization", "value": "Bearer sk-header-sentinel", "secret": False},
    {"name": "X-API-Key", "value": "sk-header-sentinel", "secret": True},
    {"name": "X-API-Key", "value": "sk-header-sentinel", "secret": False},
    {"name": "x-session-cookie", "value": "sk-header-sentinel"},
]


def _send(client, route: str, address: str, row: dict, service_id: str = ""):
    values = {"address": address, "headers": [row]}
    body = {"definition": "openai-compatible", "values": values}
    if route == "check":
        return client.post("/admin/api/engines/check", headers=csrf_for(client), json=body)
    if route == "test":
        return client.post("/admin/api/engines/test", headers=csrf_for(client), json={**body, "capability": "chat", "model": "m"})
    if route == "create":
        return _create(client, {**body, "name": "carrier", "acknowledge_warnings": True})
    return _edit(client, service_id, {**body, "acknowledge_warnings": True})


@pytest.mark.parametrize("route", ["check", "test", "create", "update"])
@pytest.mark.parametrize("row", CREDENTIAL_ROWS, ids=lambda row: f"{row['name']}-{row.get('secret')}")
def test_an_administrator_cannot_send_a_credential_in_a_header(gateway, fake_upstreams, route, row):
    owner, _, _, admin = owner_and_admin(gateway)
    silent = _silent(fake_upstreams, "carrier.fk.test")
    address = "http://carrier.fk.test/v1"
    service_id = ""
    if route == "update":
        assert _create(owner, {"definition": "openai-compatible", "name": "existing", "disabled": True,
                               "values": {"address": address}}).status_code == 201
        service_id = "existing"
    refused = _send(admin, route, address, row, service_id)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert silent.requests == []
    assert "sk-header-sentinel" not in json.dumps(_registry(gateway))


@pytest.mark.parametrize("route", ["check", "test", "create"])
def test_the_owner_is_told_to_use_the_key_field_and_nothing_is_sent(gateway, fake_upstreams, route):
    """Header values are plain text in the router's config, so a key there would not be a secret."""
    owner, *_ = owner_and_admin(gateway)
    silent = _silent(fake_upstreams, "carrier.fk.test")
    refused = _send(owner, route, "http://carrier.fk.test/v1", {"name": "X-API-Key", "value": "sk-header-sentinel"})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["field"] == "headers"
    assert "API key field" in refused.json()["error"]["message"]
    assert silent.requests == []
