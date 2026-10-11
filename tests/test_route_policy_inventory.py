"""Runtime denial inventory for every model-facing route.

Run with pytest -s to retain the complete inventory on successful runs.
All backend I/O is a recording tripwire; this module never contacts a model.
"""

from __future__ import annotations

import json
import re
import socket
import time
from pathlib import Path
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
from gateway import admin_store, category_enforcement, category_policy, media_jobs, receipt_store
from gateway.admin_oidc import (
    AdminSession, CookieConfig, OIDCConfig, encode_session_cookie, session_from_claims,
)
from gateway.category_api import snapshot_to_json
from gateway.category_store import CategoryStore
from gateway.policy import ALLOWED, DENIED as POLICY_DENIED, PolicyRequest
from gateway import runtime_auth
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
def gateway(monkeypatch, tmp_path, request):
    # Copy the isolated construction, not the other test module's fixtures.
    paths = {}
    for module, name in (
        (admin_store, "admin"), (receipt_store, "receipts"), (media_jobs, "media_jobs"),
    ):
        path = tmp_path / f"{name}.db"
        module.init_db(path)
        connect = module._conn
        monkeypatch.setattr(module, "_conn", lambda _path, fn=connect, db=path: fn(db))
        monkeypatch.setattr(module, "DB_PATH", path)
        paths[name] = path

    monkeypatch.setattr(proxy.ADMIN_STORE, "path", paths["admin"])
    cache = tmp_path / "image-cache"
    cache.mkdir()
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    monkeypatch.setattr(proxy, "IMAGE_SIGNED_URL_SECRET_CFG", "inventory-test-only")
    monkeypatch.setattr(proxy, "_image_url_signing_secret", None)
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
    # Translators (A1111, MCP) call the gateway's own routes in process through
    # httpx.ASGITransport; those calls meet the gateway's policy again and are
    # not backend calls. Any other transport is a backend call.
    real_send = httpx.AsyncClient.send
    backend_send = tripwire("HTTP backend", RuntimeError("backend reached"))

    async def send(self, *args, **kwargs):
        if isinstance(self._transport, httpx.ASGITransport) and self._transport.app is proxy.app:
            return await real_send(self, *args, **kwargs)
        return await backend_send(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)

    def no_network(*_args, **_kwargs):
        calls.append("network connection")
        raise RuntimeError("backend connection reached")

    # Last-resort transport guards also catch websocket/backend client variants.
    # TestClient's ASGI transport uses no TCP connection.
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)

    mode = getattr(request, "param", "ordinary")
    category_mode = mode == "categories"
    restricted_workflow = mode == "restricted-workflow"
    # Restore the prior process-wide gate even if fixture setup fails.
    monkeypatch.setattr(category_enforcement, "_GATE", category_enforcement._GATE)
    category_enforcement.configure(None)
    profile_id = (
        "restricted-workflow" if restricted_workflow
        else "grant-everything" if category_mode or mode == "discovery" else "grant-nothing"
    )
    # Missing allow/protocol/workflow/model/backend restrictions means
    # unrestricted access; a wildcard allow-list is not the same profile.
    profile_body = {"id": profile_id, "native_passthrough": {"enabled": category_mode or mode == "discovery"}}
    if restricted_workflow:
        profile_body["allow"] = {"intents": ["image.edit", "image.variation"]}
        profile_body["workflows"] = ["no-inventory-request-uses-this-workflow"]
    elif not category_mode and mode != "discovery":
        profile_body["allow"] = {"intents": []}
    profile = admin_store.create_profile(
        profile_id, profile_body, path=paths["admin"],
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
    assert auth.identity.profile.id == profile_id
    assert auth.identity.person_id == user["id"]
    if restricted_workflow:
        assert category_enforcement._GATE is None
        early = PolicyRequest(protocol="openai.images", intent="image.edit")
        assert runtime_auth.evaluate_and_apply(auth.identity, early).decision == ALLOWED
        resolved = PolicyRequest(
            protocol="openai.images", intent="image.edit", workflow="flux2-klein-4b",
        )
        decision = runtime_auth.evaluate_and_apply(auth.identity, resolved)
        assert decision.decision == POLICY_DENIED
        assert any("workflow" in reason.lower() for reason in decision.reasons)
        assert category_enforcement.SOURCE not in decision.sources
    if category_mode:
        # Prove that a valid active deny-all snapshot, rather than the ordinary
        # access plan or a broken snapshot, is responsible for the denial.
        probe = PolicyRequest(
            protocol="openai.images", intent="image.generate", workflow="inventory-placeholder",
        )
        assert runtime_auth.evaluate_and_apply(auth.identity, probe).decision == ALLOWED
        # This valid plan grants only an unreachable target. No inventory
        # request has that service stem, so the category grant set is empty
        # for every exercised model while both person and app are governed.
        fact = category_policy.Assertion(
            "audience", "general", "owner", True, "2026-01-01", "2026-01-01",
            "owner-1", "inventory-edit",
        )
        root = category_policy.Subject("no-such-service@1", "workflow", (fact,))
        service = category_policy.Service("no-such-service@r1", (root,))
        plan = category_policy.Plan(
            "grant-no-inventory-targets",
            (category_policy.Grant("image.generate", "no-such-service@r1"),),
            (category_policy.Row("aud", "audience", "in", ("general",)),),
        )
        snapshot = category_policy.PolicyVersion(
            "inventory-rules",
            {"audience": category_policy.Category("audience", "Audience", values=("general",))},
            {user["id"]: (plan,)}, {app["id"]: (plan,)}, {"no-such-service@r1": service},
        )
        category_path = paths["admin"].parent / "categories.db"
        store = CategoryStore(category_path)
        candidate = store.create_candidate(
            snapshot_to_json(snapshot), None, "owner-1", "inventory",
        )
        store.activate(candidate.revision, None)
        assert store.get_active().revision == candidate.revision
        category_enforcement.configure(category_path)
        decision = runtime_auth.evaluate_and_apply(auth.identity, probe)
        assert decision.decision == POLICY_DENIED
        assert category_enforcement.SOURCE in decision.sources
        assert any("category rules: NO_GRANT" in reason for reason in decision.reasons)

    cfg = OIDCConfig(
        enabled=True, scopes=("openid", "email"), allowed_email_domains=(), admin_emails=(),
        issuer="https://inventory.test", client_id="inventory", client_secret="test-only",
        redirect_uri="http://testserver/callback", signing_key=b"s" * 32,
        cookie=CookieConfig(secure=False), authorization_endpoint="http://idp/auth",
        token_endpoint="http://idp/token", jwks_uri="http://idp/jwks",
    )
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", cfg)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", cfg.signing_key)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", cfg.cookie)
    member_session = session_from_claims(
        {"sub": user["id"], "email": user["email"]}, role="none", cookie=cfg.cookie,
    )
    session_headers = {
        "x-azizam-user-action": "1",
        "x-azizam-csrf": proxy._csrf_token_for_oidc_session(member_session),
        "Idempotency-Key": "route-policy-inventory",
    }
    client = TestClient(_BoundedApp(proxy.app), raise_server_exceptions=False)
    member_client = TestClient(_BoundedApp(proxy.app), raise_server_exceptions=False)
    member_client.cookies.set(
        cfg.cookie.name, encode_session_cookie(member_session, signing_key=cfg.signing_key),
    )
    calls.clear()
    try:
        yield SimpleNamespace(
            client=client, member_client=member_client, calls=calls, paths=paths,
            headers=headers, session_headers=session_headers, category_mode=category_mode,
            person_id=user["id"], app_id=app["id"],
            category_path=paths["admin"].parent / "categories.db",
        )
    finally:
        category_enforcement.configure(None)
        member_client.close()
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


# A valid 1x1 PNG, so image routes get past decoding to policy.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000"
    "907753de0000000c49444154789c63606060000000040001f61738550000000049454e44ae426082"
)


def _request_kwargs(route, path, method):
    dependant = getattr(route, "dependant", None)
    params = {}
    for field in getattr(dependant, "query_params", ()):
        if field.required:
            typ = getattr(field, "type_", str)
            params[field.alias] = 1 if typ in (int, float) else "inventory-placeholder"
    # These routes parse expiry themselves or require it even when OpenAPI
    # does not expose the query parameters. A future integer reaches signature
    # authorization; the placeholder signature is deliberately not valid.
    if "/images/" in path or "/assets/" in path:
        params.update(exp=int(time.time()) + 300, sig="inventory-placeholder")
    for name in ("exp", "expires", "expiry", "expires_at"):
        if name in params:
            params[name] = int(time.time()) + 300
    kwargs = {"params": params}
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return kwargs

    body = {"model": "inventory-placeholder"}
    if path.endswith("/images/generations"):
        # A real workflow, so the request reaches the check after resolution.
        body["model"] = "flux2-klein-4b"
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

    # Required-only schema sampling can erase optional prompts or produce {}.
    if path.endswith("/sdapi/v1/txt2img"):
        body = {"prompt": "hi", "width": 512, "height": 512, "steps": 1}
    elif "systemone" in path.split("/"):
        body = {**(body if isinstance(body, dict) else {}), "prompt": "hi"}
    elif re.search(r"(?:^|/)me/api/make/jobs$", path):
        body = {"intent": "image.generate", "model": "image", "prompt": "hi"}

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
        # Real workflows, so the request reaches the check after resolution.
        # Variations take no prompt.
        data = ({"model": "flux2-klein-4b", "prompt": "hi"} if path.endswith("/edits")
                else {"model": "flux2-klein-4b"})
        kwargs.update(data=data, files={"image": ("inventory.png", _PNG, "image/png")})
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
        _category_reasons(gateway, receipt, row, "receipt")
        outcomes.append(f"{status}/{decision}")
        # A denial may name a backend. The policy decision is essential.
        if receipt.get("backend") is not None and decision != "denied":
            dispatched.append(request_id)
    row["receipts"] = ", ".join(outcomes) if outcomes else "none"
    row["dispatched"] = "yes" if dispatched else "no"
    if dispatched:
        row["outcome"] = "VIOLATION"
        row["rule"] = "dispatched receipt overrides response classification"
        row["detail"] += f" dispatched receipt(s): {dispatched!r}"


def _scoped_empty(response):
    """Allow only empty member collections, never an arbitrary successful body."""
    if response.status_code != 200:
        return False
    try:
        value = response.json()
    except ValueError:
        return False
    if isinstance(value, list):
        return value == []
    if not isinstance(value, dict):
        return False
    lists = [item for item in value.values() if isinstance(item, list)]
    return (
        len(lists) == 1 and lists[0] == []
        and all(not isinstance(item, dict) for item in value.values())
    )


def _category_reasons(gateway, value, row, source):
    """Record category evidence without mistaking another refusal for coverage."""
    if not gateway.category_mode:
        return
    reasons = []

    def walk(item, reason_field=False):
        if isinstance(item, dict):
            for key, child in item.items():
                walk(child, reason_field or key in {"reasons", "policy_reasons", "reason"})
        elif isinstance(item, list):
            for child in item:
                walk(child, reason_field)
        elif isinstance(item, str):
            category_reason = item.strip().startswith("category rules:")
            if category_reason:
                row["category evidence"] = True
            if reason_field or category_reason:
                reasons.append(item)

    walk(value)
    if reasons:
        row["category reasons"] = "; ".join(
            part for part in (
                row.get("category reasons", ""), f"{source}: {'; '.join(reasons)}",
            ) if part
        )


def _category_oracle(gateway, row):
    if not gateway.category_mode or row["outcome"] == "VIOLATION":
        return
    if row.get("category evidence") and row["outcome"] in {"denied", "refused", "closed"}:
        row["rule"] = "category-rules refusal with no backend call"
        return
    previous = row["rule"]
    row["outcome"] = "not exercised"
    row["rule"] = "no demonstrated category-rules refusal"
    row["detail"] += f" prior classification: {previous}"


@pytest.mark.parametrize("payload,expected", [
    ({"policy_reasons": ["profile allowed", "category rules: NO_GRANT"]}, "denied"),
    ({"error": {"reasons": ["category rules: NO_GRANT"]}}, "denied"),
    ({"error": {"reasons": ["ordinary plan denied"]}}, "not exercised"),
    ({"error": {"message": "not found"}}, "not exercised"),
])
def test_category_oracle_requires_category_evidence(payload, expected):
    env = SimpleNamespace(category_mode=True)
    row = {"outcome": "denied", "rule": "HTTP status in DENIED", "detail": ""}
    _category_reasons(env, payload, row, "test")
    _category_oracle(env, row)
    assert row["outcome"] == expected


def test_category_evidence_never_overrides_backend_violation():
    env = SimpleNamespace(category_mode=True)
    row = {"outcome": "VIOLATION", "rule": "backend call", "detail": ""}
    _category_reasons(env, {"reasons": ["category rules: NO_GRANT"]}, row, "test")
    _category_oracle(env, row)
    assert row["outcome"] == "VIOLATION"


@pytest.mark.parametrize("body", [
    [], {"receipts": []}, {"resources": [], "schema_version": 1},
    {"capabilities": [], "audience": "member"}, {"models": [], "object": "list"},
])
def test_scoped_empty_accepts_empty_collections_with_scalar_metadata(body):
    assert _scoped_empty(httpx.Response(200, json=body))


@pytest.mark.parametrize("body", [
    {}, {"data": [{"output": "generated"}]}, {"images": ["generated"]},
    {"models": [], "capabilities": []}, {"models": [], "result": {"text": "generated"}},
    {"output": "generated"},
])
def test_scoped_empty_never_hides_generation_or_nested_data(body):
    assert not _scoped_empty(httpx.Response(200, json=body))


def _body_summary(response):
    """Show shape and collection sizes without dumping successful payloads."""
    try:
        value = response.json()
    except ValueError:
        return f"non-JSON bytes[{len(response.content)}]"

    def shape(item, depth=0):
        if isinstance(item, list):
            return f"list[{len(item)}]"
        if isinstance(item, dict):
            summary = f"dict[{len(item)}] keys " + ",".join(sorted(item))
            if depth < 4:
                nested = [
                    f"{key}={shape(child, depth + 1)}"
                    for key, child in sorted(item.items())
                    if isinstance(child, (list, dict))
                ]
                if nested:
                    summary += " (" + "; ".join(nested) + ")"
            return summary
        if isinstance(item, str):
            return f"str[{len(item)}]"
        return "null" if item is None else type(item).__name__

    return shape(value)


def _validation_message(response):
    try:
        return json.dumps(response.json(), ensure_ascii=True, sort_keys=True)
    except ValueError:
        return response.text


def _table(rows):
    columns = (
        "method", "path", "status", "body", "backend called", "receipts",
        "dispatched", "category reasons", "outcome", "rule", "detail",
    )
    values = [
        [str(row.get(key, "")).replace("\r", "\\r").replace("\n", "\\n")
         for key in columns]
        for row in rows
    ]
    widths = [
        max([len(key)] + [len(row[index]) for row in values])
        for index, key in enumerate(columns)
    ]
    return "\n".join(
        " | ".join(value.ljust(width) for value, width in zip(row, widths))
        for row in [columns, *values]
    )


# 200 responses that are allowed by the access contract: they list metadata or
# the caller's own records and never run a model. Each needs a reason; a backend
# call still makes the row a violation.
_DISCOVERY = "audience-scoped discovery metadata; running an entry is policy-checked"
_A1111_METADATA = "A1111 translator metadata derived from the same discovery catalog"
_OWN_RECORDS = "the caller's own records, scoped by owner"
_METADATA_CONTRACT = {
    ("GET", "/gateway/v1/capabilities"): "discovery annotated with this caller's decision",
    ("GET", "/gateway/v1/workflows"): _DISCOVERY,
    ("GET", "/gateway/v1/extensions"): _DISCOVERY,
    ("GET", "/gateway/v1/aliases"): _DISCOVERY,
    ("GET", "/gateway/v1/aliases/resolve"): _DISCOVERY,
    ("GET", "/sdapi/v1/options"): _A1111_METADATA,
    ("GET", "/sdapi/v1/sd-models"): _A1111_METADATA,
    ("GET", "/sdapi/v1/samplers"): _A1111_METADATA,
    ("GET", "/sdapi/v1/schedulers"): _A1111_METADATA,
    ("GET", "/sdapi/v1/upscalers"): _A1111_METADATA,
    ("GET", "/sdapi/v1/progress"): "the caller's own A1111 session state",
    ("POST", "/sdapi/v1/interrupt"): "cancels only the caller's own A1111 session",
    ("GET", "/me/api/make/jobs"): _OWN_RECORDS,
    ("GET", "/me/api/media"): _OWN_RECORDS,
    ("GET", "/me/api/usage/summary"): _OWN_RECORDS,
}


def _metadata_contract(method, template, response, row):
    reason = _METADATA_CONTRACT.get((method, template))
    if reason is None or response.status_code != 200:
        return
    if template == "/gateway/v1/capabilities":
        # The client audience reports "available"; other audiences "policy_available".
        items = response.json()["capabilities"]
        assert all("policy_available" in item or "available" in item for item in items), items[:1]
        available = [item.get("id") or item.get("intent") for item in items
                     if item.get("policy_available", item.get("available"))]
        if available:
            row["detail"] = f"capabilities shown as available to a caller who may use none: {available}"
            return
    row["outcome"] = "metadata (contract)"
    row["rule"] = reason
    row["detail"] = ""


def _probe_request(headers):
    return Request({
        "type": "http", "method": "GET", "path": "/v1/models", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("203.0.113.9", 1), "server": ("testserver", 80), "scheme": "http",
    })


# Running-model status: the member holds grants, so reading engine state is allowed.
_STATUS_PROBES = {("GET", "/api/ps"), ("GET", "/ollama/api/ps")}


_CATEGORY_LISTINGS = {
    "/v1/models", "/openai/v1/models", "/anthropic/v1/models",
    "/v1/audio/models", "/openai/v1/audio/models", "/v1/audio/voices",
    "/openai/v1/audio/voices", "/v1/voices", "/openai/v1/voices",
    "/api/tags", "/ollama/api/tags", "/gateway/v1/workflows",
    "/gateway/v1/aliases", "/sdapi/v1/sd-models",
}


def _category_listing_empty(response):
    body = response.json()
    if isinstance(body, list):
        return body == []
    collections = [body[key] for key in ("data", "models", "voices", "engines", "workflows", "operations")
                   if key in body]
    return bool(collections) and all(value in ([], {}) for value in collections)


def _decoy_oracle(method, template, row, gateway, response):
    """Classify the decoy pass, where the member holds grants on a decoy service.

    Restricted discovery must be empty. Anything that runs a model must still
    be refused, and a backend call from any other route stays a violation.
    """
    status = row["status"]
    if row.get("dispatched") == "yes":
        return  # A dispatched receipt stays a violation.
    if method == "GET" and template in _CATEGORY_LISTINGS and status == 200 and not gateway.calls:
        if _category_listing_empty(response):
            row["outcome"], row["rule"] = "scoped-empty", "category discovery hides ungranted services"
            row["detail"] = ""
        else:
            row["outcome"], row["rule"] = "VIOLATION", "category discovery exposes ungranted services"
            row["detail"] = row.get("body", "")
    elif ((method, template) in _STATUS_PROBES and status == 200 and gateway.calls
            and set(gateway.calls) <= {"health", "model-state", "HTTP backend"}):
        row["outcome"], row["rule"] = "metadata (contract)", "running-model status probe"
        row["detail"] = ""
    elif method == "GET" and status == 200 and not gateway.calls:
        row["outcome"], row["rule"] = "metadata (contract)", "discovery for a member who holds grants"
        row["detail"] = ""
    elif (method, template) == ("POST", "/me/api/make/jobs") and status == 200 and not gateway.calls:
        # Queued only; test_http_category_resolved_check_inventory checks the job never ran.
        row["outcome"], row["rule"] = "metadata (contract)", "queued; the worker checks the resolved service"
        row["detail"] = ""


def _report(rows, title):
    gaps = [row for row in rows if row["outcome"] == "coverage gap"]
    not_exercised = [row for row in rows if row["outcome"] == "not exercised"]
    classified = [row for row in rows if row["outcome"] not in {"coverage gap", "not exercised"}]
    print(f"\n{title} (passes and violations)\n{_table(classified)}")
    print(f"\n{title}: coverage gaps (not policy proof)\n{_table(gaps)}")
    print(f"\n{title}: not exercised (not category proof)\n{_table(not_exercised)}")
    violations = [row for row in rows if row["outcome"] == "VIOLATION"]
    assert not violations, f"{title}: {len(violations)} violation(s)\n{_table(violations)}"


def _http_inventory(gateway, title=None, report=True):
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
                member = _member_model_path(template, route)
                client = gateway.member_client if member else gateway.client
                headers = gateway.session_headers if member else gateway.headers
                response = client.request(
                    method, path, headers=headers, follow_redirects=False,
                    **_request_kwargs(route, path, method),
                )
                row["status"] = response.status_code
                if response.status_code == 200:
                    row["body"] = _body_summary(response)
                    row["rule"] = "200 is a violation unless GET is _scoped_empty"
                if response.status_code in DENIED:
                    row["outcome"] = "denied"
                    row["rule"] = "HTTP status in DENIED"
                elif response.status_code in INVALID_BODY:
                    row["outcome"] = "coverage gap"
                    row["rule"] = "400/422 validation response is not policy proof"
                    row["detail"] = _validation_message(response)
                elif (
                    method == "GET"
                    and response.status_code == 200
                    and _scoped_empty(response)
                ):
                    row["outcome"] = "scoped-empty"
                    row["rule"] = "GET 200 satisfies _scoped_empty"
                else:
                    row.setdefault("rule", "unexpected HTTP status")
                    row["detail"] = "response is not an allowed denial or scoped-empty collection"
                _metadata_contract(method, template, response, row)
                if response.status_code >= 500 and not gateway.calls:
                    row["outcome"] = "coverage gap"
                    row["rule"] = "server/harness failure is not policy proof"
                    row["detail"] = _validation_message(response)
                if member and response.status_code == 401:
                    row["outcome"] = "coverage gap"
                    row["rule"] = "member session did not reach policy"
                if response.status_code >= 400:
                    try:
                        _category_reasons(gateway, response.json(), row, "error")
                    except ValueError:
                        pass
                _record_receipts(gateway, before, row)
            except Exception as exc:
                row["outcome"] = "VIOLATION"
                row["rule"] = "unexpected exception"
                row["detail"] = f"{type(exc).__name__}: {exc}"
            if gateway.calls:
                row["backend called"] = "yes"
                row["outcome"] = "VIOLATION"
                row["rule"] = "backend call overrides response classification"
                row["detail"] += f" backend calls={gateway.calls!r}"
            if getattr(gateway, "decoy", False):
                _decoy_oracle(method, template, row, gateway, response)
            _category_oracle(gateway, row)
            rows.append(row)
    assert rows, "No model-facing HTTP routes discovered; inventory would be vacuous"
    if gateway.category_mode:
        for target in ("paid chat models", "paid decision paths"):
            rows.append({
                "method": "NOT RUN", "path": target, "status": "not requested",
                "backend called": "no", "outcome": "not exercised",
                "rule": "covered by owner unit tests",
                "detail": "Owner-reported coverage in 10a24fc1; outside this base and fixture.",
            })
    if not report:
        return rows
    prefix = title or ("Category enforcement" if gateway.category_mode else "Ordinary access plan")
    _report(rows, f"{prefix}: model-facing HTTP route inventory")
    if gateway.category_mode:
        assert any(row.get("rule") == "category-rules refusal with no backend call" for row in rows), (
            "No HTTP route demonstrated category enforcement; see the not-exercised table"
        )


@pytest.mark.parametrize("gateway", ["restricted-workflow"], indirect=True)
@pytest.mark.parametrize("prefix", ["", "/openai"])
@pytest.mark.parametrize("operation", ["edits", "variations"])
def test_image_workflow_refusal_precedes_upload(gateway, prefix, operation):
    """A valid image must never be uploaded before an ordinary workflow refusal."""
    path = f"{prefix}/v1/images/{operation}"
    data = {"model": "flux2-klein-4b"}
    if operation == "edits":
        data["prompt"] = "hi"
    gateway.calls.clear()
    response = gateway.client.post(
        path, headers=gateway.headers, data=data,
        files={"image": ("inventory.png", _PNG, "image/png")},
    )
    # Check this first: even a subsequent policy refusal cannot undo an upload.
    assert gateway.calls == [], f"{path}: backend calls before refusal: {gateway.calls!r}"
    assert response.status_code == 403, f"{path}: {_validation_message(response)}"
    reasons = []

    def collect_reasons(value, reason_field=False):
        if isinstance(value, dict):
            for key, child in value.items():
                collect_reasons(child, reason_field or key in {"reason", "reasons", "policy_reasons", "message"})
        elif isinstance(value, list):
            for child in value:
                collect_reasons(child, reason_field)
        elif isinstance(value, str) and reason_field:
            reasons.append(value)

    collect_reasons(response.json())
    assert any("workflow" in reason.lower() for reason in reasons), (
        f"{path}: no workflow restriction in error reasons: {response.json()!r}"
    )
    assert not any("category rules:" in reason for reason in reasons), reasons


def test_http_route_policy_inventory(gateway):
    _http_inventory(gateway)


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
def test_http_category_enforcement_inventory(gateway):
    _http_inventory(gateway)


def _activate_decoy_snapshot(gateway, capabilities):
    """Grant observed capabilities only on a service no inventory request targets."""
    fact = category_policy.Assertion(
        "audience", "general", "owner", True, "2026-01-01", "2026-01-01", "owner-1", "decoy-edit",
    )
    decoy = category_policy.Service(
        "decoy-service@r1", (category_policy.Subject("decoy-service@1", "workflow", (fact,)),),
    )
    plan = category_policy.Plan(
        "decoy-grants",
        tuple(category_policy.Grant(capability, decoy.id) for capability in sorted(capabilities)),
        (category_policy.Row("aud", "audience", "in", ("general",)),),
    )
    snapshot = category_policy.PolicyVersion(
        "inventory-decoy-rules",
        {"audience": category_policy.Category("audience", "Audience", values=("general",))},
        {gateway.person_id: (plan,)}, {gateway.app_id: (plan,)}, {decoy.id: decoy},
    )
    store = CategoryStore(gateway.category_path)
    active = store.get_active()
    candidate = store.create_candidate(snapshot_to_json(snapshot), active.revision, "owner-1", "decoy")
    store.activate(candidate.revision, active.revision)


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
def test_http_category_resolved_check_inventory(gateway, monkeypatch):
    """Every dispatch path checks the resolved service, not only the early route check.

    An early check runs before the request names a workflow or model, so the
    category gate passes it when any service is allowed for the capability.
    The first pass records every capability the gateway asks about. The second
    pass grants all of them on a decoy service that no request targets: every
    early check now passes, so only a check after resolution can refuse, and a
    path without one reaches the backend tripwire as a violation.
    """
    capabilities = set()
    real_check = category_enforcement.CategoryGate.check

    def recording_check(self, identity, request):
        capabilities.add(request.intent or "")
        return real_check(self, identity, request)

    monkeypatch.setattr(category_enforcement.CategoryGate, "check", recording_check)
    _http_inventory(gateway, report=False)  # test_http_category_enforcement_inventory judges this pass
    capabilities.discard("")
    assert capabilities, "the first pass reached no category check"

    _activate_decoy_snapshot(gateway, capabilities)

    identity = proxy.authenticate_request(_probe_request(gateway.headers), "/v1/models").identity
    early = PolicyRequest(protocol="openai.images", intent="image.generate")
    assert runtime_auth.evaluate_and_apply(identity, early).decision == ALLOWED
    resolved = PolicyRequest(protocol="openai.images", intent="image.generate", workflow="flux")
    assert runtime_auth.evaluate_and_apply(identity, resolved).decision == POLICY_DENIED

    gateway.decoy = True
    _http_inventory(gateway, title="Category resolved-service check")
    jobs = gateway.member_client.get("/me/api/make/jobs", headers=gateway.session_headers).json()["jobs"]
    assert all(job["status"] != "succeeded" and not job.get("assets") for job in jobs), jobs


def _websocket_inventory(gateway, title=None):
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
                member = _member_model_path(template, route)
                client = gateway.member_client if member else gateway.client
                headers = gateway.session_headers if member else gateway.headers
                with client.websocket_connect(path, headers=headers) as ws:
                    message = ws.receive()
                    row["status"] = message["type"]
                    _category_reasons(gateway, message, row, "websocket")
                    if message["type"] == "websocket.close":
                        row["status"] = f"closed:{message.get('code', 1000)}"
                        row["outcome"] = "closed"
                        row["rule"] = "websocket.close without member data"
                    else:
                        row["rule"] = "websocket delivered member data"
                        row["detail"] = "member received websocket data"
            except Exception as exc:
                row["outcome"] = "VIOLATION"
                # WebSocketDenialResponse can inherit WebSocketDisconnect but
                # carries status_code rather than code. Check HTTP status first.
                status = getattr(exc, "status_code", None)
                if status is not None:
                    row["status"] = status
                    row["detail"] = f"{type(exc).__name__}: {exc}"
                    if status == 200:
                        row["body"] = _body_summary(exc)
                    if status >= 400:
                        try:
                            _category_reasons(gateway, exc.json(), row, "handshake")
                        except (AttributeError, ValueError):
                            pass
                    if status in DENIED:
                        row["outcome"] = "refused"
                        row["rule"] = "HTTP handshake refusal status in DENIED"
                    elif status in INVALID_BODY:
                        row["outcome"] = "coverage gap"
                        row["rule"] = "400/422 handshake validation is not policy proof"
                        row["detail"] += " " + _validation_message(exc)
                    else:
                        row["rule"] = "unexpected HTTP handshake status"
                elif isinstance(exc, WebSocketDisconnect) and hasattr(exc, "code"):
                    _category_reasons(gateway, {"reason": getattr(exc, "reason", "")}, row, "websocket")
                    row["status"] = f"refused:{exc.code}"
                    row["outcome"] = "refused"
                    row["rule"] = "WebSocketDisconnect refusal code"
                else:
                    row["rule"] = "unexpected websocket exception"
                    row["detail"] = f"{type(exc).__name__}: {exc}"
            _record_receipts(gateway, before, row)
        except Exception as exc:
            row["outcome"] = "VIOLATION"
            row["rule"] = "unexpected exception"
            row["detail"] = f"{type(exc).__name__}: {exc}"
        if gateway.calls:
            row["backend called"] = "yes"
            row["outcome"] = "VIOLATION"
            row["rule"] = "backend call overrides response classification"
            row["detail"] += f" backend calls={gateway.calls!r}"
        _category_oracle(gateway, row)
        rows.append(row)
    prefix = title or ("Category enforcement" if gateway.category_mode else "Ordinary access plan")
    _report(rows, f"{prefix}: registered websocket route inventory")
    assert rows, "No websocket routes registered; inventory would be vacuous"
    return rows


@pytest.mark.parametrize("gateway", ["ordinary", "categories"], indirect=True)
def test_websocket_route_policy_inventory(gateway, monkeypatch):
    capabilities = set()
    probes = set()
    if gateway.category_mode:
        real_check = category_enforcement.CategoryGate.check

        def recording_check(self, identity, request):
            capabilities.add(request.intent or "")
            probes.add((request.protocol, request.intent))
            return real_check(self, identity, request)

        monkeypatch.setattr(category_enforcement.CategoryGate, "check", recording_check)

    _websocket_inventory(gateway)
    if not gateway.category_mode:
        return

    capabilities.discard("")
    assert capabilities, "the first websocket pass reached no category check"
    _activate_decoy_snapshot(gateway, capabilities)
    identity = proxy.authenticate_request(_probe_request(gateway.headers), "/v1/models").identity
    # Prove the decoy lets each observed early check through, while a request
    # naming an actual (non-decoy) target remains category-denied.
    for protocol, intent in sorted(probes):
        if not intent:
            continue
        early = PolicyRequest(protocol=protocol, intent=intent)
        assert runtime_auth.evaluate_and_apply(identity, early).decision == ALLOWED
        resolved = PolicyRequest(
            protocol=protocol, intent=intent, workflow="inventory-placeholder",
        )
        decision = runtime_auth.evaluate_and_apply(identity, resolved)
        assert decision.decision == POLICY_DENIED
        assert category_enforcement.SOURCE in decision.sources
        assert any(reason.startswith("category rules:") for reason in decision.reasons)

    rows = _websocket_inventory(gateway, title="Category resolved-service check")
    unproved = [
        row for row in rows
        if row["outcome"] not in {"refused", "closed"}
        or not row.get("category evidence")
        or row["backend called"] != "no"
        or row.get("dispatched") != "no"
    ]
    assert not unproved, (
        "Every websocket route must demonstrate a category-rules refusal after "
        f"decoy grants allow early checks; missing resolved check(s):\n{_table(unproved)}"
    )


@pytest.mark.parametrize("gateway", ["discovery"], indirect=True)
def test_ungoverned_discovery_snapshot(gateway, monkeypatch):
    """Full JSON bytes captured on the parent implementation remain unchanged."""
    monkeypatch.setattr(gateway.client._transport, "raise_server_exceptions", True)
    monkeypatch.setattr(proxy.time, "time", lambda: 123)
    monkeypatch.setattr(proxy.ollama_protocol, "_iso_utc_now", lambda: "1970-01-01T00:02:03Z")
    monkeypatch.setattr(proxy, "_workflow_model_entry_created", lambda _path: 123)
    monkeypatch.setattr(proxy, "_entry_created_ts", lambda _path: 123)
    responses = {}
    for path in sorted(_CATEGORY_LISTINGS):
        gateway.calls.clear()
        response = gateway.client.get(path, headers=gateway.headers)
        assert response.status_code == 200, (path, response.status_code)
        assert not gateway.calls, (path, gateway.calls)
        responses[path] = response.content.decode()
    expected = json.loads((Path(__file__).parent / "fixtures/category_discovery_ungoverned.json").read_text())
    assert responses == expected
    # An active snapshot governing other identities must preserve the same bytes.
    from tests.test_category_enforcement import _activate, _snapshot
    _activate(gateway.category_path, _snapshot())
    category_enforcement.configure(gateway.category_path)
    for path, body in expected.items():
        response = gateway.client.get(path, headers=gateway.headers)
        assert response.status_code == 200, path
        assert response.content.decode() == body, path


def _activate_discovery_grants(gateway, grants):
    fact = category_policy.Assertion(
        "audience", "general", "owner", True, "2026-01-01", "2026-01-01", "owner-1", "discovery",
    )
    services = {target + "@r1": category_policy.Service(
        target + "@r1", (category_policy.Subject(target + "@1", "workflow", (fact,)),),
    ) for target in grants}
    plan = category_policy.Plan(
        "discovery-grants",
        tuple(category_policy.Grant(intent, target + "@r1")
              for target, intents in grants.items() for intent in intents),
        (category_policy.Row("aud", "audience", "in", ("general",)),),
    )
    snapshot = category_policy.PolicyVersion(
        "discovery-rules",
        {"audience": category_policy.Category("audience", "Audience", values=("general",))},
        {gateway.person_id: (plan,)}, {gateway.app_id: (plan,)}, services,
    )
    store = CategoryStore(gateway.category_path)
    active = store.get_active()
    candidate = store.create_candidate(snapshot_to_json(snapshot), active.revision, "owner-1", "discovery")
    store.activate(candidate.revision, active.revision)


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
def test_governed_discovery_keeps_only_permitted_resolved_services(gateway):
    """Mixed grants keep allowed handles and hide their restricted neighbors."""
    _activate_discovery_grants(gateway, {
        "current": ("chat.completions", "chat.messages", "models.list"),
        "audio.generate.ace-step-1.5-2b-base": ("audio.generate",),
        "image.generate.flux2-klein-4b": ("image.generate",),
        "supertonic": ("speech.synthesis",),
    })

    def get(path):
        gateway.calls.clear()
        response = gateway.client.get(path, headers=gateway.headers)
        assert response.status_code == 200, (path, response.status_code, response.text)
        assert not gateway.calls, (path, gateway.calls)
        return response.json()

    models = {item["id"] for item in get("/v1/models")["data"]}
    assert "current" in models and "current-fast" not in models
    assert "flux2-klein-4b" in models and "krea2-turbo" not in models
    assert "image-fast" in models and "image" not in models
    assert {item["id"] for item in get("/anthropic/v1/models")["data"]} == {"current"}
    assert {item["model"] for item in get("/api/tags")["models"]} == {"current"}
    assert {item["id"] for item in get("/v1/audio/models")["data"]} == {"ace-step-1.5-2b-base"}
    assert {item["id"] for item in get("/gateway/v1/workflows")["workflows"]} == {
        "image.generate.flux2-klein-4b", "audio.generate.ace-step-1.5-2b-base",
    }
    aliases = get("/gateway/v1/aliases")["operations"]
    assert {item["alias"] for item in aliases["images"]["stable"]} == {"image-fast"}
    assert "image_edits" not in aliases
    assert all(set(item.get("candidates", [])) <= {"supertonic"} for item in aliases["speech"]["stable"])
    speech_aliases = [item for item in get("/v1/models")["data"] if item["id"] in {"tts", "tts-fast"}]
    assert speech_aliases
    assert all(set(item["azizam_engine_candidates"]["speech"]) == {"supertonic"} for item in speech_aliases)
    assert {item["title"] for item in get("/sdapi/v1/sd-models")} == {"image-fast", "flux2-klein-4b"}
    voices = get("/v1/audio/voices")
    assert voices["voices"]
    assert set(voices["engines"]) == {"supertonic"}


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
@pytest.mark.parametrize("path", sorted(_CATEGORY_LISTINGS))
def test_category_discovery_hides_decoy_grants(gateway, path, monkeypatch):
    monkeypatch.setattr(gateway.client._transport, "raise_server_exceptions", True)
    _activate_decoy_snapshot(gateway, {
        "models.list", "chat.completions", "text.embeddings", "image.generate", "image.edit",
        "video.generate", "audio.generate", "speech.synthesis", "speech.transcription",
        "decision.systemone", "text.rerank",
    })
    gateway.calls.clear()
    response = gateway.client.get(path, headers=gateway.headers)
    assert response.status_code == 200, (path, response.status_code)
    assert not gateway.calls, (path, gateway.calls)
    assert _category_listing_empty(response), (path, _body_summary(response))


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
@pytest.mark.parametrize("intent,path", [
    ("chat.messages", "/anthropic/v1/models"),
    ("chat.completions", "/api/tags"),
    ("chat.generate", "/api/tags"),
])
def test_model_discovery_uses_the_frontends_runtime_intent(gateway, intent, path):
    _activate_discovery_grants(gateway, {"current": (intent, "models.list")})
    gateway.calls.clear()
    response = gateway.client.get(path, headers=gateway.headers)
    assert response.status_code == 200
    assert not gateway.calls
    body = response.json()
    entries = body.get("data", body.get("models"))
    assert {item.get("id", item.get("model")) for item in entries} == {"current"}
    other_path = "/api/tags" if path == "/anthropic/v1/models" else "/anthropic/v1/models"
    assert _category_listing_empty(gateway.client.get(other_path, headers=gateway.headers))


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
@pytest.mark.parametrize("entry", [{}, {"id": ""}, {"description": "unattributed model metadata"}])
def test_category_discovery_does_not_treat_nameless_rows_as_early_checks(gateway, entry):
    _activate_decoy_snapshot(gateway, {"chat.completions"})
    auth = proxy.authenticate_request(_probe_request(gateway.headers), "/v1/models")
    filtered, _ = anyio.run(proxy._filter_model_list_for_profile, [entry], auth)
    assert filtered == []
    ungoverned, _ = anyio.run(proxy._filter_model_list_for_profile, [entry], proxy.NO_AUTH_CONTEXT)
    assert ungoverned == [entry]


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
@pytest.mark.parametrize("path", ["/v1/models", "/api/tags"])
def test_category_discovery_uses_declared_model_capabilities(gateway, monkeypatch, path):
    _activate_discovery_grants(gateway, {"encoder": ("text.embeddings", "models.list")})
    registry = {"services": {"vectors": {
        "id": "vectors", "enabled": True, "capabilities": ["embeddings"],
        "config": {"models": [{"id": "encoder"}, {"id": "restricted-encoder"}]},
    }}}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    monkeypatch.setattr(proxy, "_litellm_runtime_config", lambda: SimpleNamespace(enabled=True, service_id="catalog"))

    async def catalog(*_args, **_kwargs):
        return {"object": "list", "data": [
            {"id": "encoder", "object": "model", "created": 0},
            {"id": "restricted-encoder", "object": "model", "created": 0},
        ]}

    monkeypatch.setattr(proxy, "_litellm_models_payload", catalog)
    gateway.calls.clear()
    response = gateway.client.get(path, headers=gateway.headers)
    assert response.status_code == 200
    assert not gateway.calls
    body = response.json()
    entries = body.get("data", body.get("models"))
    assert {item.get("id", item.get("model")) for item in entries} == {"encoder"}
