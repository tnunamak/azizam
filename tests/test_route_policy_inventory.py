"""Runtime denial inventory for every model-facing route.

Run with pytest -s to retain the complete inventory on successful runs.
All backend I/O is a recording tripwire; this module never contacts a model.
"""

from __future__ import annotations

import json
import re
import socket
from types import SimpleNamespace
from uuid import UUID

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.routing import Mount, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

import proxy
from gateway import admin_store, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.user_runtime import mint_user_app_key


DENIED = {401, 403, 404, 405}
INVALID_BODY = {400, 422}
REQUEST_TIMEOUT = 3


class _BoundedApp:
    """Keep an accidentally accepted, idle websocket from hanging the suite."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        with anyio.fail_after(REQUEST_TIMEOUT):
            await self.app(scope, receive, send)


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    # Copy the isolated construction, not the other test module's fixtures.
    paths = {}
    for module, name in ((admin_store, "admin"), (receipt_store, "receipts")):
        path = tmp_path / f"{name}.db"
        module.init_db(path)
        connect = module._conn
        monkeypatch.setattr(module, "_conn", lambda _path, fn=connect, db=path: fn(db))
        monkeypatch.setattr(module, "DB_PATH", path)
        paths[name] = path

    monkeypatch.setattr(proxy.ADMIN_STORE, "path", paths["admin"])
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "AUTH_PUBLIC_PATHS", ["/health"])
    monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", False)
    monkeypatch.setattr(proxy, "_client_is_lan", lambda _request: False)
    monkeypatch.setattr(proxy, "PATH_TOKENS", {})
    monkeypatch.setattr(proxy, "_service_availability_response", lambda *_a, **_k: None)

    calls = []

    def tripwire(name, result=None):
        async def called(*_args, **_kwargs):
            calls.append(name)
            if isinstance(result, BaseException):
                raise result
            return result
        return called

    monkeypatch.setattr(proxy, "check_service_health", tripwire("health", True))
    monkeypatch.setattr(proxy, "is_model_loaded", tripwire("model-state", False))
    for name in ("send_llm_execution_request", "_forward_current_loaded_openai_request"):
        monkeypatch.setattr(proxy, name, tripwire(name, RuntimeError("backend reached")))
    monkeypatch.setattr(
        httpx.AsyncClient, "send", tripwire("HTTP backend", RuntimeError("backend reached")),
    )

    def no_network(*_args, **_kwargs):
        calls.append("network connection")
        raise RuntimeError("backend connection reached")

    # Last-resort transport guards also catch websocket/backend client variants.
    # TestClient's ASGI transport uses no TCP connection.
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)

    profile = admin_store.create_profile(
        "Grant nothing",
        {
            "id": "grant-nothing",
            "allow": {"intents": []},
            "native_passthrough": {"enabled": False},
        },
        path=paths["admin"],
    )
    user = admin_store.upsert_oidc_user(
        "https://inventory.test", "member", email="member@example.test", path=paths["admin"],
    )
    session = AdminSession(
        subject=user["id"], email=user["email"], role="none",
        issued_at=1, expires_at=4_000_000_000,
    )
    app = admin_store.create_app(
        "Inventory", kind="personal", owner_user_id=user["id"],
        default_profile_id=profile["id"], path=paths["admin"],
    )
    key = mint_user_app_key(session, app["id"], path=paths["admin"])["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    request = Request({
        "type": "http", "method": "GET", "path": "/v1/models", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("203.0.113.9", 1), "server": ("testserver", 80), "scheme": "http",
    })
    auth = proxy.authenticate_request(request, "/v1/models")
    assert isinstance(auth, proxy.AuthContext), "inventory key must authenticate"
    assert auth.identity.app_id == app["id"]
    assert auth.identity.profile.id == "grant-nothing"
    calls.clear()

    client = TestClient(_BoundedApp(proxy.app), raise_server_exceptions=False)
    yield SimpleNamespace(client=client, calls=calls, paths=paths, headers=headers)
    client.close()


def _member_model_path(path, route):
    if not re.search(r"(?:^|/)me/api(?:/|$)", path):
        return False
    parts = path.split("/")
    module = getattr(getattr(route, "endpoint", None), "__module__", "")
    return (
        "make" in parts or "media" in parts
        or module in {"gateway.me_make", "gateway.me_media"}
    )


def _model_path(path, route):
    parts = [part for part in path.split("/") if part]
    if not parts or parts[0] in {
        "health", "healthz", "admin", "account", "accounts", "auth", "ui", "static", "showcase",
    }:
        return False
    # Includes /v1, /openai/v1, /anthropic/v1 and any proxy namespace /.../v1.
    return (
        "v1" in parts or "ollama" in parts or parts[0] == "api"
        or _member_model_path(path, route)
    )


def _routes(routes, prefix="", converters=None):
    """Recurse into mounted apps/routers, preserving parent path converters."""
    for route in routes:
        path = prefix + getattr(route, "path", "")
        params = {**(converters or {}), **getattr(route, "param_convertors", {})}
        if isinstance(route, Mount):
            # A StaticFiles mount has no routes; never make a request to it.
            yield from _routes(getattr(route, "routes", ()), path, params)
        elif isinstance(route, WebSocketRoute) or _model_path(path, route):
            yield route, path, params


def _concrete_path(path, converters):
    def replace(match):
        name = match.group(1)
        converter = converters.get(name)
        kind = match.group(2) or type(converter).__name__.lower()
        if "int" in kind:
            value = 1
        elif "float" in kind:
            value = 1.0
        elif "uuid" in kind:
            value = UUID(int=1)
        else:
            value = "inventory-placeholder"
        return converter.to_string(value) if converter else str(value)

    return re.sub(r"\{([^}:]+)(?::([^}]+))?\}", replace, path)


def _sample(schema, definitions=None, depth=0):
    """Small required-only JSON values from declared request models."""
    definitions = definitions or {}
    if depth > 8:
        return None
    if "$ref" in schema:
        schema = definitions.get(schema["$ref"].rsplit("/", 1)[-1], {})
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for key in ("anyOf", "oneOf", "allOf"):
        if schema.get(key):
            choice = next(
                (part for part in schema[key] if part.get("type") != "null"),
                schema[key][0],
            )
            return _sample(choice, definitions, depth + 1)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {
            key: _sample(props.get(key, {}), definitions, depth + 1)
            for key in schema.get("required", [])
        }
    if kind == "array":
        return [_sample(schema.get("items", {}), definitions, depth + 1)]
    if kind in {"integer", "number"}:
        return max(1, schema.get("minimum", 1))
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return "inventory-placeholder"


def _request_kwargs(route, path, method):
    dependant = getattr(route, "dependant", None)
    params = {}
    for field in getattr(dependant, "query_params", ()):
        if field.required:
            typ = getattr(field, "type_", str)
            params[field.alias] = 1 if typ in (int, float) else "inventory-placeholder"
    kwargs = {"params": params}
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return kwargs

    body = {"model": "inventory-placeholder"}
    if path.endswith(("/chat/completions", "/chat", "/messages")):
        body.update(messages=[{"role": "user", "content": "hi"}], max_tokens=1)
    elif path.endswith(("/embeddings", "/embed", "/responses", "/audio/speech")):
        body.update(input="hi")
        if path.endswith("/audio/speech"):
            body["voice"] = "inventory-placeholder"
    else:
        body.update(prompt="hi")

    field = getattr(route, "body_field", None)
    typ = getattr(field, "type_", None)
    schema_fn = getattr(typ, "model_json_schema", None) or getattr(typ, "schema", None)
    schema = schema_fn() if callable(schema_fn) else {}
    sample = _sample(schema, schema.get("$defs", schema.get("definitions", {})))
    if isinstance(sample, dict):
        body = {**sample, **{key: value for key, value in body.items() if key in sample}}
    elif schema:
        body = sample

    media_type = getattr(getattr(field, "field_info", None), "media_type", "")
    if media_type in {"multipart/form-data", "application/x-www-form-urlencoded"}:
        props = schema.get("properties", {})
        files, data = {}, {}
        for key, value in body.items():
            if props.get(key, {}).get("format") == "binary":
                files[key] = ("inventory.txt", b"inventory", "application/octet-stream")
            else:
                data[key] = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        kwargs.update(data=data)
        if files:
            kwargs["files"] = files
    elif path.endswith(("/audio/transcriptions", "/audio/translations")):
        kwargs.update(
            data={"model": "inventory-placeholder"},
            files={"file": ("inventory.wav", b"RIFF\x00\x00\x00\x00WAVE", "audio/wav")},
        )
    elif path.endswith(("/images/edits", "/images/variations")):
        kwargs.update(
            data={"model": "inventory-placeholder", "prompt": "hi"},
            files={"image": ("inventory.png", b"inventory", "image/png")},
        )
    else:
        kwargs["json"] = body
    return kwargs



def _receipt_ids(gateway):
    with receipt_store._conn(gateway.paths["receipts"]) as conn:
        return {row[0] for row in conn.execute("SELECT request_id FROM receipts")}


def _record_receipts(gateway, before, row):
    with receipt_store._conn(gateway.paths["receipts"]) as conn:
        receipts = conn.execute(
            "SELECT request_id, path, status, receipt_json FROM receipts"
        ).fetchall()
    fresh = [receipt for receipt in receipts if receipt[0] not in before]
    dispatched = []
    outcomes = []
    for request_id, _path, status, raw in fresh:
        receipt = json.loads(raw)
        decision = receipt.get("policy_decision")
        outcomes.append(f"{status}/{decision}")
        # A denial may name a backend. The policy decision is essential.
        if receipt.get("backend") is not None and decision != "denied":
            dispatched.append(request_id)
    row["receipts"] = ", ".join(outcomes) if outcomes else "none"
    row["dispatched"] = "yes" if dispatched else "no"
    if dispatched:
        row["outcome"] = "VIOLATION"
        row["detail"] += f" dispatched receipt(s): {dispatched!r}"


def _scoped_empty(response):
    """Allow only empty member collections, never an arbitrary successful body."""
    if response.status_code != 200:
        return False
    value = response.json()
    if isinstance(value, list):
        return value == []
    if not isinstance(value, dict):
        return False
    collections = {"jobs", "items", "media", "images", "videos", "audio", "files",
                   "data", "results", "assets"}
    present = collections.intersection(value)
    if not present or any(value[key] != [] for key in present):
        return False
    metadata = {"total", "count", "next_cursor", "cursor", "has_more"}
    return all(
        key in present or (key in metadata and item in (None, 0, False, ""))
        for key, item in value.items()
    )

def _table(rows):
    columns = (
        "method", "path", "status", "backend called", "receipts", "dispatched", "outcome", "detail",
    )
    values = [[str(row.get(key, "")) for key in columns] for row in rows]
    widths = [
        max([len(key)] + [len(row[index]) for row in values])
        for index, key in enumerate(columns)
    ]
    return "\n".join(
        " | ".join(value.ljust(width) for value, width in zip(row, widths))
        for row in [columns, *values]
    )


def _report(rows, title):
    table = _table(rows)
    print(f"\n{title}\n{table}")
    violations = [row for row in rows if row["outcome"] == "VIOLATION"]
    assert not violations, f"{title}: {len(violations)} violation(s)\n{_table(violations)}"


def test_http_route_policy_inventory(gateway):
    rows = []
    for route, template, converters in _routes(proxy.app.routes):
        if isinstance(route, WebSocketRoute):
            continue
        for method in sorted(getattr(route, "methods", ()) or ()):
            row = {
                "method": method, "path": template, "status": "ERROR",
                "backend called": "no", "outcome": "VIOLATION", "detail": "",
            }
            gateway.calls.clear()
            try:
                before = _receipt_ids(gateway)
                path = _concrete_path(template, converters)
                response = gateway.client.request(
                    method, path, headers=gateway.headers, follow_redirects=False,
                    **_request_kwargs(route, path, method),
                )
                row["status"] = response.status_code
                if response.status_code in DENIED:
                    row["outcome"] = "denied"
                elif response.status_code in INVALID_BODY:
                    row["outcome"] = "invalid-placeholder (not policy proof)"
                elif (
                    method == "GET" and _member_model_path(template, route)
                    and _scoped_empty(response)
                ):
                    row["outcome"] = "scoped-empty"
                else:
                    row["detail"] = "unexpected response status"
                _record_receipts(gateway, before, row)
            except Exception as exc:
                row["outcome"] = "VIOLATION"
                row["detail"] = f"{type(exc).__name__}: {exc}"
            if gateway.calls:
                row["backend called"] = "yes"
                row["outcome"] = "VIOLATION"
                row["detail"] += f" backend calls={gateway.calls!r}"
            rows.append(row)
    assert rows, "No model-facing HTTP routes discovered; inventory would be vacuous"
    invalid = [row for row in rows if row["status"] in INVALID_BODY]
    print(f"\nInvalid-placeholder coverage (separate from policy denials):\n{_table(invalid)}")
    _report(rows, "Model-facing HTTP route inventory")


def test_websocket_route_policy_inventory(gateway):
    rows = []
    for route, template, converters in _routes(proxy.app.routes):
        if not isinstance(route, WebSocketRoute):
            continue
        row = {
            "method": "WEBSOCKET", "path": template, "status": "ERROR",
            "backend called": "no", "outcome": "VIOLATION", "detail": "",
        }
        gateway.calls.clear()
        try:
            before = _receipt_ids(gateway)
            path = _concrete_path(template, converters)
            try:
                with gateway.client.websocket_connect(path, headers=gateway.headers) as ws:
                    message = ws.receive()
                    row["status"] = message["type"]
                    if message["type"] == "websocket.close":
                        row["status"] = f"closed:{message.get('code', 1000)}"
                        row["outcome"] = "closed"
                    else:
                        row["detail"] = "member received websocket data"
            except WebSocketDisconnect as exc:
                row["status"] = f"refused:{exc.code}"
                row["outcome"] = "refused"
            except Exception as exc:
                # Newer Starlette versions expose HTTP handshake denials as
                # WebSocketDenialResponse; avoid requiring that newer class.
                status = getattr(exc, "status_code", None)
                if status in DENIED:
                    row["status"] = status
                    row["outcome"] = "refused"
                else:
                    raise
            _record_receipts(gateway, before, row)
        except Exception as exc:
            row["outcome"] = "VIOLATION"
            row["detail"] = f"{type(exc).__name__}: {exc}"
        if gateway.calls:
            row["backend called"] = "yes"
            row["outcome"] = "VIOLATION"
            row["detail"] += f" backend calls={gateway.calls!r}"
        rows.append(row)
    _report(rows, "Registered websocket route inventory")
    if not rows:
        pytest.skip("No websocket routes registered")
