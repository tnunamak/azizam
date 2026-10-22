"""Runtime denial inventory for every model-facing route.

Run with pytest -s to retain the complete inventory on successful runs.
All backend I/O is a recording tripwire; this module never contacts a model.
"""

from __future__ import annotations

import asyncio
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
from gateway import accounts, admin_store, category_enforcement, category_policy, me_make, media_jobs, receipt_store
from gateway.admin_oidc import (
    AdminSession, CookieConfig, OIDCConfig, encode_session_cookie, session_from_claims,
)
from gateway.category_api import snapshot_to_json
from gateway.category_store import CategoryStore
from gateway.policy import ALLOWED, DENIED as POLICY_DENIED, PolicyRequest
from gateway import runtime_auth
from gateway.user_runtime import mint_user_app_key
from tests.support.access import access_context, store_plan


DENIED = {401, 403, 404, 405}
INVALID_BODY = {400, 422}
REQUEST_TIMEOUT = 3  # websocket handshakes: an accepted idle socket must not hang the suite
HTTP_REQUEST_TIMEOUT = 60  # HTTP: generous, so host load never turns a slow answer into a 500

# A row passes only with one of these outcomes. Refusals prove a decision;
# non-executable contracts are route-specific answers that run nothing.
REFUSALS = {"denied", "refused", "closed", "refused (wrapped)", "refused (queued)"}
NON_EXECUTABLE = {"scoped-empty", "metadata (contract)", "unsupported (contract)"}
PROVED = REFUSALS | NON_EXECUTABLE
OUT_OF_FIXTURE = "not run (out of fixture)"


class _BoundedApp:
    """Keep an accidentally accepted, idle websocket from hanging the suite.

    The bound sits outside the app's error handling, so a request that runs
    past it comes back as an empty 500. HTTP gets a far larger bound: under
    load, POST /me/api/make/jobs (several SQLite commits plus the worker's
    refusal in the same event loop) took longer than 3 s.

    A queued make job runs as a task on this request's event loop, and
    TestClient closes that loop (cancelling the task) once the app returns.
    So wait here for the jobs this request scheduled to settle; otherwise the
    inventory reads a job that is still running.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        limit = REQUEST_TIMEOUT if scope["type"] == "websocket" else HTTP_REQUEST_TIMEOUT
        with anyio.fail_after(limit):
            await self.app(scope, receive, send)
            loop = asyncio.get_running_loop()
            jobs = [task for task in proxy._image_media_job_tasks if task.get_loop() is loop]
            if jobs:
                await asyncio.wait(jobs)


@pytest.fixture
def gateway(monkeypatch, tmp_path, request):
    # Copy the isolated construction, not the other test module's fixtures.
    paths = {}
    for module, name in (
        (admin_store, "admin"), (receipt_store, "receipts"), (media_jobs, "media_jobs"),
        (proxy.responses_store, "responses"),  # else stored-response routes answer 500
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
    # The image-job payload spool defaults to <checkout>/data. The sandbox
    # mounts the checkout read-only, so POST /me/api/make/jobs failed with a
    # 500 there and queued (then ran) a job everywhere else.
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "image_media_jobs")
    monkeypatch.setattr(proxy, "IMAGE_SIGNED_URL_SECRET_CFG", "inventory-test-only")
    monkeypatch.setattr(proxy, "_image_url_signing_secret", None)
    monkeypatch.setattr(proxy, "AUTH_PUBLIC_PATHS", ["/health"])
    monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", False)
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
    if restricted_workflow:
        plan_kwargs = {"caps": ("image",), "workflows": ["no-inventory-request-uses-this-workflow"]}
    elif category_mode or mode == "discovery":
        plan_kwargs = {"engines": "all", "operations": ["*"]}
    else:
        plan_kwargs = {"caps": ()}
    profile = store_plan(profile_id, path=paths["admin"], **plan_kwargs)
    user = admin_store.upsert_oidc_user(
        "https://inventory.test", "member", email="member@example.test", path=paths["admin"],
    )
    session = AdminSession(
        subject=user["id"], email=user["email"], role="none",
        issued_at=1, expires_at=4_000_000_000,
    )
    accounts.change_person(user["id"], "owner", path=paths["admin"], plan_id=profile["id"])
    app = admin_store.create_app(
        "Inventory", kind="personal", owner_user_id=user["id"],
        access_plan="person", path=paths["admin"],
    )
    key = mint_user_app_key(session, app["id"], path=paths["admin"])["api_key"]
    # Session routes (/me/api/make/jobs) act as the person's keyless session
    # App, which uses the person's plan: the same plan as this key's App.
    session_app = admin_store.ensure_session_app_for_user(user["id"], name=me_make.APP_NAME, path=paths["admin"])
    headers = {"Authorization": f"Bearer {key}"}
    request = Request({
        "type": "http", "method": "GET", "path": "/v1/models", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("203.0.113.9", 1), "server": ("testserver", 80), "scheme": "http",
    })
    auth = proxy.authenticate_request(request, "/v1/models")
    assert isinstance(auth, proxy.AuthContext), "inventory key must authenticate"
    assert auth.identity.app_id == app["id"]
    assert auth.identity.access.plan_ids == (profile["id"],)
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
        assert decision.code == "not_in_access_plan"
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
            {user["id"]: (plan,)}, {app["id"]: (plan,), session_app["id"]: (plan,)},
            {"no-such-service@r1": service},
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
        enabled=True, scopes=("openid", "email"), admin_emails=(),
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
            connection_test_body={"api_key": key},
            person_id=user["id"], app_id=app["id"], session_app_id=session_app["id"],
            category_path=paths["admin"].parent / "categories.db", passes=0,
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
        or path.rstrip("/").endswith("/me/api/connection-test")
        or module in {"gateway.me_make", "gateway.me_media"}
    )


def _model_path(path, route):
    parts = [part for part in path.split("/") if part]
    if not parts or parts[0] in {
        "health", "healthz", "admin", "account", "accounts", "auth", "ui", "static",
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


def _request_kwargs(route, path, method, gateway=None):
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
    if "/assets/" in path and path.endswith("/thumbnail"):
        # The thumbnail route reads "expires" and refuses a signature made for
        # another mode, so the future expiry reaches signature authorization.
        params["expires"] = 1
    for name in ("exp", "expires", "expiry", "expires_at"):
        if name in params:
            params[name] = int(time.time()) + 300
    kwargs = {"params": params}
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return kwargs

    if method == "POST" and path.rstrip("/").endswith("/me/api/connection-test"):
        # This is the isolated member's own freshly minted test credential.
        # Never send the generic model placeholder to the Home key validator.
        assert gateway is not None
        return {**kwargs, "json": dict(gateway.connection_test_body)}

    if method == "POST" and path == "/me/api/media/remove-missing":
        return {**kwargs, "json": {"expected_missing": 0}}

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


# Scalar fields that describe an empty collection; anything else could carry output.
_EMPTY_RESPONSE_METADATA_KEYS = frozenset({"object", "schema_version", "audience", "asset_url_ttl_s",
                                             "first_id", "last_id", "has_more"})


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
    scalars = {key for key, item in value.items() if not isinstance(item, (list, dict))}
    return (
        len(lists) == 1 and lists[0] == []
        and all(not isinstance(item, dict) for item in value.values())
        and scalars <= _EMPTY_RESPONSE_METADATA_KEYS
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
    if row["outcome"] in NON_EXECUTABLE:
        return  # Runs nothing, so no category refusal is expected.
    if row.get("category evidence") and row["outcome"] in REFUSALS:
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


@pytest.mark.parametrize(
    "body",
    [
        {"models": [], "output": "generated result"},
        {"receipts": [], "image": "data:image/png;base64,fixture"},
        {"capabilities": [], "response": "generated text"},
    ],
)
def test_scoped_empty_does_not_hide_scalar_generation_output(body):
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
    ("GET", "/api/version"): "a constant: the Ollama API version the adapter speaks",
    ("GET", "/ollama/api/version"): "a constant: the Ollama API version the adapter speaks",
    ("GET", "/sdapi/v1/options"): _A1111_METADATA,
    ("GET", "/sdapi/v1/sd-models"): _A1111_METADATA,
    ("GET", "/sdapi/v1/samplers"): _A1111_METADATA,
    ("GET", "/sdapi/v1/schedulers"): _A1111_METADATA,
    ("GET", "/sdapi/v1/upscalers"): _A1111_METADATA,
    ("GET", "/sdapi/v1/progress"): "the caller's own A1111 session state",
    ("POST", "/sdapi/v1/interrupt"): "cancels only the caller's own A1111 session",
    ("GET", "/me/api/make/jobs"): _OWN_RECORDS,
    ("GET", "/me/api/jobs"): _OWN_RECORDS,
    ("GET", "/me/api/jobs/{job_id}"): _OWN_RECORDS,
    ("GET", "/me/api/media"): _OWN_RECORDS,
    ("GET", "/me/api/usage/summary"): _OWN_RECORDS,
    ("POST", "/me/api/media/remove-missing"): "removes only the caller's own entries whose files are gone; runs no model",
    ("POST", "/me/api/media/confirm-folder"): "owner only; writes the media folder marker and removes nothing; runs no model",
}


def _connection_test_contract(method, template, response, row, gateway):
    """Judge only Home's explicit wrapper; receipts and backend calls still veto it."""
    if (method, template) != ("POST", "/me/api/connection-test"):
        return
    if response.status_code != 200 or gateway.calls or row.get("dispatched") != "no":
        return
    body = response.json()
    if not isinstance(body, dict):
        return
    reason = body.get("reason")
    if body.get("ok") is False and isinstance(reason, str) and reason.strip():
        row["outcome"] = "refused (wrapped)"
        row["rule"] = "connection test reported a refusal; no model ran"
        row["detail"] = reason
        if "category rules:" in reason:
            _category_reasons(
                gateway, {"reason": reason[reason.index("category rules:"):]}, row, "wrapper",
            )
    elif body.get("ok") is True and body.get("path") == "/v1/models":
        row["outcome"] = "metadata (contract)"
        row["rule"] = "connection test fell back to the catalog; no model ran"
        row["detail"] = ""


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


def _media_folder_refusal(method, template, response, row):
    """Remove missing items answers a typed 503 when the media folder is not seen or not confirmed; no model runs."""
    if (method, template) != ("POST", "/me/api/media/remove-missing") or response.status_code != 503:
        return
    code = response.json().get("error", {}).get("code")
    if code == "media_folder_unavailable":
        row["outcome"] = "metadata (contract)"
        row["rule"] = "no media folder was seen, so nothing was removed; runs no model"
        row["detail"] = ""
    elif code == "media_folder_unconfirmed":
        row["outcome"] = "metadata (contract)"
        row["rule"] = "the media folder is not the one recorded, so nothing was removed; runs no model"
        row["detail"] = ""


def _media_folder_owner_refusal(method, template, response, row):
    """Confirming the media folder is owner-only: other callers get a typed 403 before any marker is written."""
    if (method, template) != ("POST", "/me/api/media/confirm-folder") or response.status_code != 403:
        return
    if response.json().get("error", {}).get("code") == "media_folder_owner_required":
        row["outcome"] = "metadata (contract)"
        row["rule"] = "only the owner may confirm the media folder; refused before the marker is written; runs no model"
        row["detail"] = ""


# API families the gateway does not implement: every method answers before
# any model or policy with the named code. Route-specific, never a status wildcard.
_UNSUPPORTED_CONTRACT = {
    **{f"{prefix}/v1/{family}{{rest:path}}": (400, "unsupported_operation")
       for prefix in ("", "/openai")
       for family in ("assistants", "files", "batches", "fine_tuning", "realtime")},
    **{f"{prefix}/v1/responses/{operation}": (400, "unsupported_operation")
       for prefix in ("", "/openai") for operation in ("input_tokens", "{response_id}/cancel")},
    **{f"{prefix}/api/{operation}": (501, "unsupported operation")
       for prefix in ("", "/ollama")
       for operation in ("create", "copy", "pull", "push", "blobs/{digest}", "delete")},
    **{f"/sdapi/v1/{operation}": (501, "not supported by this adapter")
       for operation in ("img2img", "extra-single-image", "extra-batch-images", "interrogate", "png-info")},
}


def _unsupported_contract(method, template, response, row):
    expected = _UNSUPPORTED_CONTRACT.get(template)
    if expected is None or response.status_code != expected[0]:
        return
    if method == "HEAD":
        body = {"detail": expected[1]}  # HEAD carries no body; the status is the whole answer.
    else:
        try:
            body = response.json()
        except ValueError:
            return
    error = body.get("error") if isinstance(body, dict) else None
    texts = [error.get("code") if isinstance(error, dict) else error,
             body.get("detail") if isinstance(body, dict) else None]
    if any(isinstance(text, str) and expected[1] in text for text in texts):
        row["outcome"] = "unsupported (contract)"
        row["rule"] = f"unimplemented API answers {expected[0]} {expected[1]}; nothing runs"
        row["detail"] = ""


def _queued_job_contract(method, template, response, row, gateway):
    """Home's make queue answers 202 before policy; only the worker's refusal proves it."""
    if (method, template) != ("POST", "/me/api/make/jobs") or response.status_code != 202:
        return
    if gateway.calls or row.get("dispatched") != "no":
        return  # Backend calls and dispatched receipts stay violations.
    job_id = response.json()["job"]["job_id"]
    job = media_jobs.get(job_id, path=gateway.paths["media_jobs"]) or {}
    refused = "rejected/denied" in row.get("receipts", "")
    if job.get("status") == media_jobs.STATUS_FAILED and refused:
        row["outcome"] = "refused (queued)"
        row["rule"] = "the worker refused the queued job; no model ran"
        row["detail"] = ""
    else:
        row["outcome"] = "coverage gap"
        row["rule"] = "a queued job is no proof until the worker refuses it"
        row["detail"] = f"job status {job.get('status')!r}, receipts {row.get('receipts')!r}"


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
            and set(gateway.calls) <= {"health", "model-state", "HTTP backend", "network connection"}):
        row["outcome"], row["rule"] = "metadata (contract)", "running-model status probe"
        row["detail"] = ""
    elif method == "GET" and status == 200 and not gateway.calls and row["outcome"] not in PROVED:
        # A blanket GET rule is no route contract, so the row stays unproved.
        row["outcome"], row["rule"] = "metadata (no contract)", "discovery for a member who holds grants"
        row["detail"] = ""



@pytest.mark.parametrize("method,template,dispatched,calls", [
    ("GET", "/api/ps", "yes", ["health"]),
    ("GET", "/ollama/api/ps", "yes", ["HTTP backend"]),
    ("GET", "/v1/models", "yes", []),
    ("POST", "/me/api/make/jobs", "yes", []),
    ("GET", "/api/ps", "no", ["send_llm_execution_request"]),
    ("GET", "/ollama/api/ps", "no", ["health", "_forward_current_loaded_openai_request"]),
    ("POST", "/me/api/connection-test", "yes", []),
    ("POST", "/me/api/connection-test", "no", ["HTTP backend"]),
])
@pytest.mark.parametrize("oracle", ["decoy", "category"])
def test_inventory_oracles_preserve_dispatch_and_backend_violations(
    method, template, dispatched, calls, oracle,
):
    env = SimpleNamespace(category_mode=True, calls=calls)
    row = {
        "status": 200, "outcome": "VIOLATION", "dispatched": dispatched,
        "backend called": "yes" if calls else "no",
        "category evidence": True, "category reasons": "category rules: NO_GRANT",
        "rule": "dispatched receipt" if dispatched == "yes" else "backend call",
        "detail": "existing violation evidence",
    }
    original = dict(row)
    if oracle == "decoy":
        _decoy_oracle(method, template, row, env, httpx.Response(200, json={}))
    else:
        _category_oracle(env, row)
    assert row["outcome"] == "VIOLATION"
    assert row == original, "Oracles must retain the original violation evidence"


@pytest.mark.parametrize("available,expected", [
    (True, "VIOLATION"), (False, "metadata (contract)"),
])
def test_client_capabilities_metadata_contract_checks_available(available, expected):
    response = httpx.Response(200, json={
        "audience": "client",
        "capabilities": [{"id": "inventory-chat", "available": available}],
    })
    row = {"outcome": "VIOLATION", "rule": "unexpected success", "detail": ""}
    _metadata_contract("GET", "/gateway/v1/capabilities", response, row)
    assert row["outcome"] == expected
    if available:
        assert "inventory-chat" in row["detail"]


def test_capabilities_metadata_contract_requires_availability_field():
    response = httpx.Response(200, json={
        "audience": "client", "capabilities": [{"id": "inventory-chat"}],
    })
    row = {"outcome": "VIOLATION", "rule": "unexpected success", "detail": ""}
    with pytest.raises(AssertionError):
        _metadata_contract("GET", "/gateway/v1/capabilities", response, row)
    assert row["outcome"] == "VIOLATION"



@pytest.mark.parametrize("ok,path,reason,expected", [
    (False, "/v1/chat/completions", "The gateway returned HTTP 403.", "refused (wrapped)"),
    (True, "/v1/models", "Your key can reach the model catalog.", "metadata (contract)"),
    (True, "/v1/chat/completions", "A one-token local chat request succeeded.", "VIOLATION"),
    (False, "/v1/chat/completions", "", "VIOLATION"),
    (False, "/v1/chat/completions", "   ", "VIOLATION"),
    (None, "/v1/models", "Missing verdict", "VIOLATION"),
])
def test_connection_test_wrapper_contract(ok, path, reason, expected):
    env = SimpleNamespace(category_mode=False, calls=[])
    row = {"outcome": "VIOLATION", "dispatched": "no", "rule": "unexpected success", "detail": ""}
    response = httpx.Response(200, json={
        "ok": ok, "path": path, "model": "fast" if path.endswith("completions") else None,
        "reason": reason, "request_id": None,
    })
    _connection_test_contract("POST", "/me/api/connection-test", response, row, env)
    assert row["outcome"] == expected


@pytest.mark.parametrize("ok,path", [(False, "/v1/chat/completions"), (True, "/v1/models")])
@pytest.mark.parametrize("calls,dispatched", [(["HTTP backend"], "no"), ([], "yes")])
def test_connection_test_wrapper_never_overrides_backend_or_dispatch(ok, path, calls, dispatched):
    env = SimpleNamespace(category_mode=True, calls=calls)
    row = {
        "outcome": "VIOLATION", "dispatched": dispatched,
        "rule": "backend or receipt violation", "detail": "original evidence",
    }
    original = dict(row)
    response = httpx.Response(200, json={
        "ok": ok, "path": path, "model": None,
        "reason": "category rules: NO_GRANT", "request_id": "synthetic-receipt",
    })
    _connection_test_contract("POST", "/me/api/connection-test", response, row, env)
    _category_oracle(env, row)
    assert row == original


@pytest.mark.parametrize("reason,receipt_evidence,expected", [
    ("The gateway returned HTTP 403.", False, "not exercised"),
    ("Refused: category rules: NO_GRANT", False, "refused (wrapped)"),
    ("The gateway returned HTTP 403.", True, "refused (wrapped)"),
])
def test_connection_test_wrapper_requires_category_proof(reason, receipt_evidence, expected):
    env = SimpleNamespace(category_mode=True, calls=[])
    row = {
        "outcome": "VIOLATION", "dispatched": "no", "rule": "unexpected success",
        "detail": "", "category evidence": receipt_evidence,
    }
    response = httpx.Response(200, json={
        "ok": False, "path": "/v1/chat/completions", "model": "fast",
        "reason": reason, "request_id": "synthetic-receipt",
    })
    _connection_test_contract("POST", "/me/api/connection-test", response, row, env)
    _category_oracle(env, row)
    assert row["outcome"] == expected


def test_connection_test_is_selected_without_selecting_home_metadata():
    route = SimpleNamespace(endpoint=SimpleNamespace(__module__="gateway.home_api"))
    assert _member_model_path("/me/api/connection-test", route)
    assert _model_path("/me/api/connection-test", route)
    assert not _member_model_path("/me/api/home", route)
    assert not _model_path("/me/api/home", route)


def test_connection_test_request_uses_owned_fixture_credential(gateway):
    route = SimpleNamespace()
    kwargs = _request_kwargs(route, "/me/api/connection-test", "POST", gateway)
    assert kwargs["json"] == gateway.connection_test_body
    assert gateway.headers["Authorization"] == f"Bearer {kwargs['json']['api_key']}"
    assert kwargs["json"] is not gateway.connection_test_body


def _load_known_gaps(path=Path(__file__).parent / "fixtures" / "route_policy_known_gaps.txt"):
    """Parse "[pass title]" sections of "METHOD template STATUS  # note" lines."""
    gaps, title = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            title = line[1:-1]
            gaps[title] = set()
        elif line:
            method, template, status = line.split()
            gaps[title].add(f"{method} {template} {status}")
    return {title: frozenset(keys) for title, keys in gaps.items()}


# Rows that prove no policy decision yet, per pass. They stay visible and can
# only shrink: _report fails on an unproved row that is not listed, and on a
# listed row that now proves policy. See the file's header.
KNOWN_GAPS = _load_known_gaps()


def _gap_key(row):
    return f"{row['method']} {row['path']} {row['status']}"


def _report(rows, title, known_gaps=None):
    out_of_fixture = [row for row in rows if row["outcome"] == OUT_OF_FIXTURE]
    unproved = [row for row in rows if row["outcome"] not in PROVED | {OUT_OF_FIXTURE}]
    proved = [row for row in rows if row["outcome"] in PROVED]
    print(f"\n{title} (passes)\n{_table(proved)}")
    print(f"\n{title}: unproved (no policy proof)\n{_table(unproved)}")
    if out_of_fixture:
        print(f"\n{title}: out of this fixture\n{_table(out_of_fixture)}")
    violations = [row for row in rows if row["outcome"] == "VIOLATION"]
    assert not violations, f"{title}: {len(violations)} violation(s)\n{_table(violations)}"
    known = KNOWN_GAPS.get(title, frozenset()) if known_gaps is None else known_gaps
    new = [row for row in unproved if _gap_key(row) not in known]
    assert not new, (
        f"{title}: {len(new)} row(s) prove no policy decision (status, validation or missing "
        f"category proof); make the request reach policy or add a route contract\n{_table(new)}"
    )
    fixed = known - {_gap_key(row) for row in unproved}
    assert not fixed, f"{title}: listed gap(s) now prove policy; remove them from KNOWN_GAPS: {sorted(fixed)}"


def _row(outcome, method="POST", path="/v1/x", **extra):
    return {"method": method, "path": path, "status": 403, "outcome": outcome, "rule": "", "detail": "", **extra}


@pytest.mark.parametrize("outcome", [
    "coverage gap", "not exercised", "ERROR", "", "an outcome nobody defined",
])
def test_report_fails_on_every_unproved_outcome(outcome):
    with pytest.raises(AssertionError, match="prove no policy decision"):
        _report([_row("denied", path="/v1/ok"), _row(outcome)], "unit", known_gaps=frozenset())


@pytest.mark.parametrize("outcome", sorted(PROVED))
def test_report_accepts_each_proved_outcome(outcome):
    _report([_row(outcome), _row(OUT_OF_FIXTURE, method="NOT RUN", path="paid")], "unit", known_gaps=frozenset())


def test_report_fails_violations_first_with_their_evidence():
    row = _row("VIOLATION", **{"backend called": "yes", "detail": "backend calls=['HTTP backend']"})
    with pytest.raises(AssertionError, match=r"1 violation\(s\)[\s\S]*HTTP backend"):
        _report([row], "unit", known_gaps=frozenset({"POST /v1/x 403"}))


def test_report_tolerates_only_listed_gaps_and_only_while_they_remain():
    listed = frozenset({"POST /v1/x 403"})
    _report([_row("coverage gap")], "unit", known_gaps=listed)
    with pytest.raises(AssertionError, match="now prove policy"):
        _report([_row("denied")], "unit", known_gaps=listed)
    with pytest.raises(AssertionError, match="prove no policy decision"):
        _report([_row("coverage gap", path="/v1/new")], "unit", known_gaps=listed)
    with pytest.raises(AssertionError, match="prove no policy decision"):
        _report([_row("coverage gap", status=500)], "unit", known_gaps=listed)  # same route, new status


def test_known_gap_file_names_every_inventory_pass_once():
    assert set(KNOWN_GAPS) == {
        "Ordinary access plan: model-facing HTTP route inventory",
        "Category enforcement: model-facing HTTP route inventory",
        "Category resolved-service check: model-facing HTTP route inventory",
    }


def _http_inventory(gateway, title=None, report=True):
    rows = []
    # A fresh key per pass, so each pass submits its own make job rather than
    # replaying the first pass's job.
    gateway.passes += 1
    session_headers = {**gateway.session_headers, "Idempotency-Key": f"route-policy-inventory-{gateway.passes}"}
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
                headers = session_headers if member else gateway.headers
                response = client.request(
                    method, path, headers=headers, follow_redirects=False,
                    **_request_kwargs(route, path, method, gateway),
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
                _media_folder_refusal(method, template, response, row)
                _media_folder_owner_refusal(method, template, response, row)
                if member and response.status_code == 401:
                    row["outcome"] = "coverage gap"
                    row["rule"] = "member session did not reach policy"
                if response.status_code >= 400:
                    try:
                        _category_reasons(gateway, response.json(), row, "error")
                    except ValueError:
                        pass
                _unsupported_contract(method, template, response, row)
                _record_receipts(gateway, before, row)
                _connection_test_contract(method, template, response, row, gateway)
                _queued_job_contract(method, template, response, row, gateway)
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
    assert any(
        row["method"] == "POST" and row["path"] == "/me/api/connection-test"
        for row in rows
    ), "The session connection test must appear in ordinary, category and decoy inventories"
    if gateway.category_mode:
        for target in ("paid chat models", "paid decision paths"):
            rows.append({
                "method": "NOT RUN", "path": target, "status": "not requested",
                "backend called": "no", "outcome": OUT_OF_FIXTURE,
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
    error = response.json()["error"]
    assert error["code"] == "not_in_access_plan", f"{path}: {error}"
    assert any("flux2-klein-4b" in reason for reason in reasons), (
        f"{path}: the refusal does not name the workflow: {reasons!r}"
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
        {gateway.person_id: (plan,)}, {gateway.app_id: (plan,), gateway.session_app_id: (plan,)},
        {decoy.id: decoy},
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


def _after_loop_turns(turns):
    """A future that resolves once the event loop has run ``turns`` more times."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def turn(left):
        if left:
            loop.call_soon(turn, left - 1)
        else:
            done.set_result(None)

    loop.call_soon(turn, turns)
    return done


def test_queued_make_job_is_read_after_the_worker_settles_it(gateway, monkeypatch):
    # The worker refuses the job after the request is done (10k loop turns is
    # far more than the request awaits after it queues the job); the
    # inventory must judge the settled job, not one still running.
    execute = proxy._SessionMakeRuntime.execute

    async def execute_late(self, payload):
        await _after_loop_turns(10_000)
        return await execute(self, payload)

    monkeypatch.setattr(proxy._SessionMakeRuntime, "execute", execute_late)
    response = gateway.member_client.post(
        "/me/api/make/jobs", headers=gateway.session_headers,
        json={"intent": "image.generate", "model": "image", "prompt": "hi"},
    )
    assert response.status_code == 202, response.text
    job = media_jobs.get(response.json()["job"]["job_id"], path=gateway.paths["media_jobs"])
    assert job["status"] == media_jobs.STATUS_FAILED, job


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
    if responses != expected:
        print("CATEGORY_SNAPSHOT_DIFF=" + json.dumps(
            {path: body for path, body in responses.items() if expected.get(path) != body},
            sort_keys=True,
        ))
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
    # An App and person the snapshot does not list are not governed by it.
    ungoverned, _ = anyio.run(proxy._filter_model_list_for_profile, [entry], access_context())
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


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
@pytest.mark.parametrize("path", ["/v1/audio/voices", "/openai/v1/audio/voices", "/v1/voices", "/openai/v1/voices"])
@pytest.mark.parametrize("grants,compatible", [
    ({"supertonic": ("speech.synthesis",)}, []),
    ({"omnivoice": ("speech.synthesis",)}, ["omnivoice"]),
])
def test_custom_voice_discovery_requires_a_permitted_compatible_engine(gateway, monkeypatch, path, grants, compatible):
    _activate_discovery_grants(gateway, grants)
    voice = {"id": "voice_" + "1" * 24, "object": "audio.voice", "name": "Narrator"}
    store = SimpleNamespace(list_voices=lambda *_args, **_kwargs: ([voice], True))
    monkeypatch.setattr(proxy, "_voice_store", lambda: store)
    monkeypatch.setattr(gateway.client._transport, "raise_server_exceptions", True)
    gateway.calls.clear()
    response = gateway.client.get(path, headers=gateway.headers)
    assert response.status_code == 200
    assert not gateway.calls
    body = response.json()
    if compatible:
        assert [row["id"] for row in body["data"]] == [voice["id"]]
        assert body["data"][0]["compatible_engines"] == compatible
        assert body["has_more"] is True
    else:
        assert body["data"] == []
        assert body["first_id"] is None and body["last_id"] is None
        assert body["has_more"] is False


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
@pytest.mark.parametrize("operation,alias,intent", [
    ("speech", "tts", "speech.synthesis"),
    ("chat", "fast", "chat.completions"),
])
def test_category_discovery_checks_the_resolved_alias_instead_of_its_granted_name(gateway, operation, alias, intent):
    _activate_discovery_grants(gateway, {alias: (intent, "models.list")})
    response = gateway.client.get("/gateway/v1/aliases", params={"operation": operation}, headers=gateway.headers)
    assert response.status_code == 200
    assert response.json()["operations"] == {}
    response = gateway.client.get("/v1/models", headers=gateway.headers)
    assert response.status_code == 200
    assert alias not in {row["id"] for row in response.json()["data"]}


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
def test_category_discovery_hides_an_unresolved_alias_even_when_its_name_is_granted(gateway, monkeypatch):
    _activate_discovery_grants(gateway, {"tts": ("speech.synthesis", "models.list")})
    aliases = {"speech": {"stable": {"tts": ["absent"]}, "engines": {"absent": {
        "service": "missing-service", "model": "absent-model",
    }}}}
    monkeypatch.setitem(proxy.config, "model_aliases", aliases)
    response = gateway.client.get("/gateway/v1/aliases", headers=gateway.headers)
    assert response.status_code == 200
    assert response.json()["operations"] == {}
    response = gateway.client.get("/v1/models", headers=gateway.headers)
    assert response.status_code == 200
    assert "tts" not in {row["id"] for row in response.json()["data"]}


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
def test_category_alias_patterns_use_their_declared_runtime_target(gateway, monkeypatch):
    _activate_discovery_grants(gateway, {"current": ("chat.completions",)})
    aliases = {"chat": {"stable": {"fast": "current"}, "openai": [
        {"match": "model-[ab]", "to": "fast"},
    ]}}
    monkeypatch.setitem(proxy.config, "model_aliases", aliases)
    gateway.calls.clear()
    response = gateway.client.get("/gateway/v1/aliases", headers=gateway.headers)
    assert response.status_code == 200
    assert not gateway.calls
    view = response.json()["operations"]["chat"]
    assert view["stable"] == [{"alias": "fast", "resolves_to": "current"}]
    assert view["openai"] == proxy._gateway_aliases_provider("chat")["chat"]["openai"]
    assert [row["match"] for row in view["openai"]] == ["model-[ab]"]
    assert proxy.resolve_stable_alias("chat", "model-a").target == "current"


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
@pytest.mark.parametrize("path,expected", [
    ("/anthropic/v1/models", {("anthropic.messages", "chat.messages", "current")}),
    ("/api/tags", {("ollama.chat", "chat.completions", "current"),
                   ("ollama.generate", "chat.generate", "current"),
                   ("ollama.embeddings", "text.embeddings", "current")}),
])
def test_frontend_discovery_builds_the_runtime_policy_request(gateway, monkeypatch, path, expected):
    _activate_decoy_snapshot(gateway, {"models.list", "chat.messages", "chat.completions",
                                       "chat.generate", "text.embeddings"})
    real_check = category_enforcement.CategoryGate.check
    probes = set()

    def recording_check(self, identity, request):
        if request.model == "current":
            probes.add((request.protocol, request.intent, request.model))
        return real_check(self, identity, request)

    monkeypatch.setattr(category_enforcement.CategoryGate, "check", recording_check)
    response = gateway.client.get(path, headers=gateway.headers)
    assert response.status_code == 200
    assert _category_listing_empty(response)
    assert probes == expected


@pytest.mark.parametrize("gateway", ["discovery"], indirect=True)
def test_ungoverned_discovery_preserves_custom_voices_and_unresolved_aliases(gateway, monkeypatch):
    voice = {"id": "voice_" + "1" * 24, "object": "audio.voice", "name": "Narrator"}
    store = SimpleNamespace(list_voices=lambda *_args, **_kwargs: ([voice], True))
    monkeypatch.setattr(proxy, "_voice_store", lambda: store)
    aliases = {"speech": {"stable": {"tts": ["absent"]}, "engines": {"absent": {
        "service": "missing-service", "model": "absent-model",
    }}}}
    monkeypatch.setitem(proxy.config, "model_aliases", aliases)
    monkeypatch.setattr(gateway.client._transport, "raise_server_exceptions", True)
    paths = ["/v1/audio/voices", "/openai/v1/audio/voices", "/v1/voices", "/openai/v1/voices",
             "/gateway/v1/aliases"]
    baseline = {}
    for path in paths:
        response = gateway.client.get(path, headers=gateway.headers)
        assert response.status_code == 200
        body = response.json()
        if path.endswith("aliases"):
            assert body["operations"]["speech"]["stable"][0]["alias"] == "tts"
            assert body["operations"]["speech"]["stable"][0]["resolves_to"] is None
        else:
            assert [row["id"] for row in body["data"]] == [voice["id"]]
            assert body["data"][0]["compatible_engines"] == []
            assert body["has_more"] is True
        baseline[path] = response.content
    from tests.test_category_enforcement import _activate, _snapshot
    _activate(gateway.category_path, _snapshot())
    category_enforcement.configure(gateway.category_path)
    for path, expected in baseline.items():
        response = gateway.client.get(path, headers=gateway.headers)
        assert response.status_code == 200
        assert response.content == expected


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
@pytest.mark.parametrize("service,operation,intent,model,aliases", [
    ("video.generate.video-ltx-2.5-t2v", "videos", "video.generate",
     "video-ltx-2.5-t2v", {"video", "video-fast"}),
    ("image.edit.flux2-klein-4b", "image_edits", "image.edit",
     "flux2-klein-4b-edit", {"image-fast", "image-edit-fast"}),
])
def test_media_discovery_uses_the_execution_workflow_target(gateway, service, operation, intent, model, aliases):
    _activate_discovery_grants(gateway, {service: (intent, "models.list")})
    gateway.calls.clear()
    response = gateway.client.get("/v1/models", headers=gateway.headers)
    assert response.status_code == 200
    entries = {row["id"] for row in response.json()["data"]}
    assert {model, *aliases} <= entries
    response = gateway.client.get("/gateway/v1/aliases", params={"operation": operation}, headers=gateway.headers)
    assert response.status_code == 200
    assert {row["alias"] for row in response.json()["operations"][operation]["stable"]} == aliases
    assert not gateway.calls


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
def test_video_model_discovery_does_not_use_the_gateway_manifest_service(gateway):
    _activate_discovery_grants(gateway, {"video.generate.ltx-2.5-t2v": ("video.generate", "models.list")})
    response = gateway.client.get("/gateway/v1/workflows", headers=gateway.headers)
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["workflows"]} == {"video.generate.ltx-2.5-t2v"}
    response = gateway.client.get("/v1/models", headers=gateway.headers)
    assert response.status_code == 200
    assert not {"video", "video-fast", "video-ltx-2.5-t2v"} & {row["id"] for row in response.json()["data"]}


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
def test_rerank_discovery_uses_the_runtime_embedding_intent(gateway):
    target = proxy.resolve_stable_alias("rerank", "rerank").target
    _activate_discovery_grants(gateway, {target: ("text.embeddings", "models.list")})
    response = gateway.client.get("/gateway/v1/aliases", params={"operation": "rerank"}, headers=gateway.headers)
    assert response.status_code == 200
    assert {row["alias"] for row in response.json()["operations"]["rerank"]["stable"]} == {"rerank"}
    response = gateway.client.get("/v1/models", headers=gateway.headers)
    assert response.status_code == 200
    assert "rerank" in {row["id"] for row in response.json()["data"]}


@pytest.mark.parametrize('gateway', ['discovery'], indirect=True)
def test_resource_admission_route_inventory(gateway, monkeypatch):
    """A new executable route must refuse before any model transport is reached.

    Invalid/unsupported inputs are allowed to fail before admission. Successful
    mutation routes require an explicit non-dispatch contract below; a new route
    that silently returns generated output cannot inherit a blanket 2xx exemption.
    The successful dispatch cases are exercised by test_dispatch_admission.py.
    """
    monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', {
        'disabled_resource_classes': ['chat', 'embeddings', 'decision', 'image', 'audio', 'video', 'native'],
    })
    monkeypatch.setattr(proxy, '_operator_override_docs', lambda: {'service_availability': {}, 'media_job_admission': {}})
    failures = []
    for route, template, converters in _routes(proxy.app.routes):
        if isinstance(route, WebSocketRoute):
            continue  # The existing websocket inventory owns handshake coverage.
        methods = set(getattr(route, 'methods', ())) & {'POST', 'PUT', 'PATCH'}
        for method in sorted(methods):
            gateway.calls.clear()
            path = _concrete_path(template, converters)
            kwargs = _request_kwargs(route, path, method, gateway)
            try:
                response = gateway.client.request(method, path, headers=gateway.headers, **kwargs)
            except Exception as exc:
                failures.append(f'{method} {template}: {type(exc).__name__}; calls={gateway.calls}')
                continue
            backend_calls = [call for call in gateway.calls if call not in {'health', 'model-state'}]
            if backend_calls:
                failures.append(f'{method} {template}: backend reached {backend_calls}')
            if response.status_code < 400 and (method, template) not in _METADATA_CONTRACT:
                # These wrappers report an inner refusal rather than changing their wire status.
                body = response.json()
                wrapped = (template == '/me/api/connection-test' and body.get('ok') is False)
                if not wrapped:
                    failures.append(f'{method} {template}: unexplained success {response.status_code}')
    assert not failures, '\n'.join(failures)


@pytest.mark.parametrize('gateway', ['discovery'], indirect=True)
def test_resource_inventory_detects_a_new_route_without_admission(gateway, monkeypatch):
    from fastapi.routing import APIRoute
    async def unguarded():
        return {'generated': 'unguarded output'}
    route = APIRoute('/v1/inventory-unguarded-generation', unguarded, methods=['POST'])
    monkeypatch.setattr(proxy.app.router, 'routes', [route, *proxy.app.router.routes])
    with pytest.raises(AssertionError, match='inventory-unguarded-generation: unexplained success'):
        test_resource_admission_route_inventory(gateway, monkeypatch)
