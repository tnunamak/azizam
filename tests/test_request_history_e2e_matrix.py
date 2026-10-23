"""Request-history privacy through proxy.app, with real auth, routes and stores.

Inference HTTP, hardware availability and media metadata encoding use test doubles;
auth, protocol loopbacks, history capture, cleaning and storage run proxy.app.
Accepted slice-5a limits in REQUEST_RETENTION.md stay accepted:
non-JSON replies may be omitted, unreadable/oversize input may keep no row, and
App keys/grants cannot delete. Each negative assertion has a positive control.
Credential canaries are assembled at runtime and failures print labels only.
"""
from __future__ import annotations

import asyncio
import copy
import gzip
import io
import json
import sqlite3
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import proxy
import responses_store
from gateway import (
    admin_store, browser_sessions, media_jobs, prompt_memory, receipt_store,
    request_history, request_history_capture, retention,
)
from gateway.admin_oidc import encode_session_cookie
from gateway.runtime_auth import hash_api_key
from gateway.service_registry import service_from_backend_config
from tests.registry_test_helpers import write_service_registry_for_test
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401
from tests.support.gateway import gateway_client, register_litellm, use_admin_db
from tests.test_engine_dispatch_wiring import _registry
from tests.test_media_provenance import comfy_png
from tests.test_request_history_capture import _kept_rows, _mint_member_key

NAMESPACE = "com.azizam.prompts"
FORGET = {NAMESPACE: {"mode": "forget"}}
MARKER = "matrix harmless request text"
MODEL = "garage-chat"
LIMIT = int(retention.bundled().data["kinds"][retention.REQUEST_HISTORY]["max_record_bytes"])


@dataclass(frozen=True)
class Shape:
    name: str
    path: str = "/v1/chat/completions"
    wire: str = "json"
    stream: bool = False


SHAPES = [
    Shape("chat_sse", stream=True),
    Shape("responses_sse", "/v1/responses", stream=True),
    Shape("anthropic", "/v1/messages"),
    Shape("anthropic_sse", "/anthropic/v1/messages", stream=True),
    Shape("ollama_chat", "/api/chat", stream=True),
    Shape("ollama_generate", "/ollama/api/generate", stream=True),
    Shape("audio_upload", "/v1/audio/transcriptions", "multipart"),
    Shape("image_edit_upload", "/v1/images/edits", "multipart"),
    Shape("embeddings", "/v1/embeddings"),
    Shape("workflow_upload", "/gateway/v1/workflows/video.edit.rife-interpolate/run", "multipart"),
    Shape("tool_arguments"),
    Shape("upstream_error"),
    Shape("chunked_json", wire="chunked"),
    Shape("gzip_json", wire="gzip"),
    Shape("duplicate_keys", wire="duplicate"),
    Shape("near_limit", wire="near_limit"),
    Shape("over_limit", wire="over_limit"),
]
SHAPE_IDS = [shape.name for shape in SHAPES]


def _canaries() -> dict[str, str]:
    # No complete credential-shaped value is present in the source tree.
    return {name: "".join(parts) for name, parts in {
        "api_key": ("sk", "-", "matrix", "A1b2C3d4E5f6G7h8J9k0"),
        "password": ("matrix", "-", "private", "-", "P4ss"),
        "cookie": ("session", "=", "matrix", "C8d9E0f1G2h3J4k5"),
        "url_user": ("matrix", "-", "user", "-", "U9"),
        "url_password": ("matrix", "-", "url", "-", "P8"),
        "url_query": ("matrix", "Query", "Q7s8"),
        "url_fragment": ("matrix", "Fragment", "F6x5"),
        "url_path": ("matrix", "Path", "A1b2C3d4E5f6G7h8J9k0"),
    }.items()}


def _payload(canaries: dict[str, str], *, cookies: bool = False) -> dict:
    nested = {"apiKeys": [canaries["api_key"]], "password1": {"value": canaries["password"]},
              "headers[Authorization]": "Bearer " + canaries["api_key"],
              "provider": [{"refresh_token": canaries["api_key"]}]}
    if cookies:
        nested.update({"Cookie": canaries["cookie"], "Set-Cookie": canaries["cookie"]})
    urls = [
        f"https://{canaries['url_user']}:{canaries['url_password']}@files.example.test/a",
        f"https://files.example.test/a?signature={canaries['url_query']}#{canaries['url_fragment']}",
        f"https://files.example.test/hooks/{canaries['url_path']}/run",
    ]
    return {"safe": MARKER, "nested": [nested], "urls": urls,
            "json_string": json.dumps(nested),
            "wrapped_json_string": json.dumps(json.dumps(nested))}


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
            await asyncio.sleep(0)


@dataclass
class World:
    db: Path
    media: Path
    receipts: Path
    person: str
    key: str
    app: str
    canaries: dict[str, str]
    shape: Shape | None = None
    calls: list[str] = field(default_factory=list)
    reply_payload: dict = field(default_factory=dict)


@pytest.fixture
def world(fake_upstreams, monkeypatch, tmp_path):
    db = use_admin_db(monkeypatch, tmp_path)
    person = admin_store.upsert_oidc_user("https://fixture.test", "fixture-person", path=db)["id"]
    key = _mint_member_key(db, person)
    with sqlite3.connect(db) as conn:
        app = conn.execute("SELECT id FROM apps WHERE kind='personal' AND owner_user_id=?", (person,)).fetchone()[0]
        conn.execute("UPDATE prompt_memory_preferences SET parts_json=? WHERE user_id=? AND grp='request_history'",
                     (json.dumps(["accepted", "received"]), person))
    # These stores bind their default path at import. Use the suite-owned
    # sandbox paths, as chat_gateway does, rather than patching only DB_PATH.
    media, receipts = tmp_path / "media.db", receipt_store.DB_PATH
    media_jobs.init_db(media)
    prompt_memory.init_db(path=media)
    receipt_store.init_db(receipts)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    with sqlite3.connect(receipts) as conn:
        conn.execute("DELETE FROM receipts")
    # Responses' conversation store is also owned by this hermetic pytest run.
    responses_store.init_db(responses_store.DB_PATH)
    responses_store.delete_all()
    monkeypatch.setattr(request_history_capture, "_PENDING", threading.BoundedSemaphore(32))
    signing_key = (("matrix" + "-" + "session-signing") * 2).encode()
    monkeypatch.setattr(proxy, "ADMIN_SESSION_SIGNING_KEY", signing_key)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", signing_key)
    w = World(db, media, receipts, person, key, app, _canaries())
    w.reply_payload = _payload(w.canaries)

    async def engine(request):
        await request.aread()
        w.calls.append(request.url.path)
        path = request.url.path
        headers = {"set-cookie": w.canaries["cookie"], "x-api-key": w.canaries["api_key"]}
        if path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"text": json.dumps(w.reply_payload)}, headers=headers)
        if path == "/v1/embeddings":
            return httpx.Response(200, json={"object": "list", "model": MODEL,
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 2, "total_tokens": 2}, "metadata": w.reply_payload}, headers=headers)
        assert path == "/v1/chat/completions", "unexpected inference path"
        body = json.loads(request.content)
        if w.shape and w.shape.name == "upstream_error":
            return httpx.Response(400, json={"error": {"type": "invalid_request_error",
                "message": "Rejected request: " + json.dumps(body)}}, headers=headers)
        message = {"role": "assistant", "content": json.dumps(w.reply_payload)}
        if w.shape and w.shape.name == "tool_arguments":
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_matrix_reply",
                "type": "function", "function": {"name": "lookup", "arguments": json.dumps(w.reply_payload)}}]}
        base = {"id": "chatcmpl-matrix", "created": 1767225600, "model": MODEL}
        if body.get("stream"):
            chunks = []
            # Deliberately split within a credential value and a URL, as real SSE deltas do.
            content = json.dumps(w.reply_payload)
            key_cut = content.index(w.canaries["api_key"]) + 8
            url_cut = content.index(w.canaries["url_path"]) + 8
            for delta in ({"role": "assistant"}, {"content": content[:key_cut]},
                          {"content": content[key_cut:url_cut]}, {"content": content[url_cut:]}):
                chunks.append(b"data: " + json.dumps({**base, "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": delta, "finish_reason": None}]}).encode() + b"\n\n")
            chunks.append(b"data: " + json.dumps({**base, "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n")
            chunks.append(b"data: [DONE]\n\n")
            return httpx.Response(200, stream=_Chunks(chunks), headers={**headers, "content-type": "text/event-stream"})
        return httpx.Response(200, json={**base, "object": "chat.completion", "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}, headers=headers)

    async def graph(request):
        await request.aread()
        w.calls.append(request.url.path)
        video = w.shape and w.shape.name == "workflow_upload"
        filename = "out.mp4" if video else "out.png"
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "matrix-upload.mp4" if video else "matrix-upload.png"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "matrix-job"})
        if request.url.path == "/history/matrix-job":
            return httpx.Response(200, json={"matrix-job": {"status": {"completed": True}, "outputs": {
                "6": {"videos" if video else "images": [{"filename": filename, "type": "output"}]}}}})
        if request.url.path == "/view":
            return httpx.Response(200, content=b"rendered-video" if video else comfy_png(),
                                  headers={"content-type": "video/mp4" if video else "image/png"})
        pytest.fail("unexpected graph HTTP path")

    fake_upstreams.router.add_transport("matrix-engine.fk.test", HandlerTransport(engine))
    fake_upstreams.router.add_transport("matrix-graph.fk.test", HandlerTransport(graph))
    # A real protocol loopback through the same app, without opening any TCP port.
    monkeypatch.setattr(proxy, "PROXY_PORT", 49171)
    fake_upstreams.router.add("127.0.0.1:49171", proxy.app)
    register_litellm(monkeypatch, db, "http://matrix-engine.fk.test")
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"].update(_registry("http://matrix-engine.fk.test/v1")["services"])
    speech = {"name": "Matrix speech", "host": "matrix-engine.fk.test", "port": 80, "enabled": True,
              "capabilities": ["audio", "transcriptions"], "default_model": "matrix-speech",
              "chunking": {"enabled": False, "spool_dir": str(tmp_path / "spool")}}
    registry["services"]["matrix-speech"] = service_from_backend_config("matrix-speech", speech, source="ui")
    write_service_registry_for_test(registry, path=db)
    cfg = copy.deepcopy(proxy.config)
    cfg["backends"] = {"matrix-speech": speech}
    cfg["model_aliases"]["transcriptions"] = {
        "engines": {"matrix-speech": {"service": "matrix-speech", "model": "matrix-speech"}},
        "stable": {},
    }
    monkeypatch.setattr(proxy, "config", cfg)
    for name, value in {
        "COMFY_ENABLED": True, "COMFY_HOST": "http://matrix-graph.fk.test",
        "IMAGE_CACHE_DIR": tmp_path / "assets", "_image_url_signing_secret": b"matrix-signing",
        "get_vram_estimate": lambda *_a, **_kw: 1024,
        "graph_free_vram_bytes": AsyncMock(return_value=10**10),
        "_service_availability_response": lambda *_a, **_kw: None,
        "resolve_image_background_decision": AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False)),
    }.items():
        monkeypatch.setattr(proxy, name, value)
    # Metadata encoding is outside retention; keep this video fixture independent of ffmpeg.
    monkeypatch.setattr(proxy.media_delivery, "deliver_bytes", lambda data, *_a, **_kw: data)
    monkeypatch.setattr(proxy.media_delivery, "attach_provenance", lambda data, *_a, **_kw: data)
    return w


def _body(shape: Shape, payload: dict) -> dict:
    text = json.dumps(payload)
    body = {"model": MODEL, "messages": [{"role": "user", "content": text}],
            "stream": shape.stream, "metadata": payload}
    if shape.name == "responses_sse":
        body = {"model": MODEL, "input": text, "stream": True, "store": True, "metadata": payload}
    elif shape.name.startswith("anthropic"):
        body["max_tokens"] = 32
    elif shape.name == "ollama_generate":
        body = {"model": MODEL, "prompt": text, "stream": True}
    elif shape.name == "embeddings":
        body = {"model": MODEL, "input": [text], "metadata": payload}
    elif shape.name == "tool_arguments":
        body["messages"] = [{"role": "user", "content": MARKER},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_matrix",
             "type": "function", "function": {"name": "lookup", "arguments": text}}]},
            {"role": "tool", "tool_call_id": "call_matrix", "content": text}]
        body["tools"] = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    return body


def _wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 1600)
    return out.getvalue()


async def _send(w: World, shape: Shape, *, forget: str | None = None,
                payload: dict | None = None, key: str | None = None,
                auth_headers: dict | None = None, extra_files: dict | None = None) -> httpx.Response:
    w.shape = shape
    payload = _payload(w.canaries) if payload is None else payload
    headers = {"authorization": "Bearer " + (key or w.key), "cookie": w.canaries["cookie"],
               "x-api-key": key or w.key, "x-provider-api-key": w.canaries["api_key"]}
    if forget == "header":
        headers["x-azizam-prompts"] = "forget"
    headers.update(auth_headers or {})
    async with gateway_client() as client:
        if shape.wire == "multipart":
            controls = {"com.azizam.matrix": payload}
            data = {"extensions": json.dumps(controls)}
            if shape.name == "audio_upload":
                data.update(model="matrix-speech", prompt=json.dumps(payload))
                files = {"file": ("clip.wav", _wav(), "audio/wav")}
            elif shape.name == "image_edit_upload":
                data.update(prompt=json.dumps(payload), response_format="b64_json", background="opaque")
                files = {"image": ("input.png", comfy_png(), "image/png")}
            else:
                data.update(multiplier="2")
                files = {"source_video": ("sample.mp4", b"input-video-bytes", "video/mp4")}
            files.update(extra_files or {})
            if forget == "body":
                controls.update(FORGET)
                data["extensions"] = json.dumps(controls)
            elif forget == "bracket":
                data[f"extensions[{NAMESPACE}][mode]"] = "forget"
            return await client.post(shape.path, data=data, files=files, headers=headers)
        body = _body(shape, payload)
        if forget == "body":
            body["extensions"] = FORGET
        raw = json.dumps(body, ensure_ascii=False).encode()
        if shape.wire == "duplicate":
            # Both duplicated values contain credentials; the route's JSON parser chooses the last.
            raw = b'{"metadata":' + json.dumps(payload).encode() + b',' + raw[1:]
        if shape.wire in {"near_limit", "over_limit"}:
            body["padding"] = ""
            raw = json.dumps(body).encode()
            target = LIMIT - 1 if shape.wire == "near_limit" else LIMIT + 1
            body["padding"] = "p" * (target - len(raw))
            raw = json.dumps(body).encode()
            assert len(raw) == target
        headers["content-type"] = "application/json"
        if shape.wire == "gzip":
            raw = gzip.compress(raw)
            headers["content-encoding"] = "gzip"
        if shape.wire == "chunked":
            async def chunks():
                for index in range(0, len(raw), 17):
                    yield raw[index:index + 17]
                    await asyncio.sleep(0)
            return await client.post(shape.path, content=chunks(), headers=headers)
        return await client.post(shape.path, content=raw, headers=headers)


def _run(w, shape, **kwargs):
    response = asyncio.run(_send(w, shape, **kwargs))
    refused_form = shape.name == "image_edit_upload" and kwargs.get("forget") == "bracket"
    expected = 400 if shape.name in {"gzip_json", "upstream_error"} or refused_form else 200
    assert response.status_code == expected, f"{shape.name}: unexpected status {response.status_code}"
    if shape.name != "gzip_json" and not refused_form:
        assert w.calls, "positive control: no inference HTTP call"
    if shape.stream:
        assert response.content, "stream was not consumed"
        expected_type = "application/x-ndjson" if shape.name.startswith("ollama") else "text/event-stream"
        assert expected_type in response.headers.get("content-type", "")
    return response


def _dump_records(w: World) -> str:
    # Include summary/metadata columns too; checking parts alone misses secondary sinks.
    return json.dumps([dict(row) for row in _kept_rows(w.media)], ensure_ascii=False)


def _assert_absent(text: str, values: dict[str, str]):
    leaked = [label for label, value in values.items() if value in text]
    if leaked:
        pytest.fail("persisted credential canaries: " + ", ".join(leaked), pytrace=False)


def _receipts(w: World) -> list[dict]:
    with sqlite3.connect(w.receipts) as conn:
        return [json.loads(row[0]) for row in conn.execute("SELECT receipt_json FROM receipts")]


@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
def test_credentials_are_absent_from_saved_request_reply_and_metadata(world, shape):
    _run(world, shape)
    rows = _kept_rows(world.media)
    if shape.name not in {"gzip_json", "over_limit"}:
        assert len(rows) == 1, "positive control: no saved request"
        parts = json.loads(rows[0]["parts_json"])
        assert set(parts) == {"accepted", "received"}
        assert MARKER in json.dumps(parts["accepted"]), "harmless content was not saved"
        if not shape.stream and shape.name not in {"image_edit_upload", "workflow_upload", "upstream_error"}:
            assert MARKER in json.dumps(parts["received"]), "positive control: the reply content was not saved"
    if shape.name == "responses_sse":
        assert responses_store.count() == 1, "positive control: conversation retention was not exercised"
    _assert_absent(_dump_records(world), {**world.canaries, "caller_key": world.key})
    _assert_absent(json.dumps(_receipts(world)), {**world.canaries, "caller_key": world.key})


@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
@pytest.mark.parametrize("control", ["body", "header"])
def test_forget_keeps_no_history_and_only_structural_receipts(world, shape, control):
    _run(world, shape, forget=control)
    assert _kept_rows(world.media) == []
    receipts = _receipts(world)
    if shape.name != "gzip_json":
        assert receipts, "forget must still have its receipt"
    for receipt in receipts:
        _assert_absent(json.dumps(receipt), {**world.canaries, "request_text": MARKER})
    assert not any(entry.get("kind") == "request_history" and entry.get("decision") == "kept"
                   for receipt in receipts for entry in receipt.get("retention", []))
    # No conversation or Library prompt may be a second copy of this request.
    with sqlite3.connect(world.media) as conn:
        assert conn.execute("SELECT COUNT(*) FROM asset_prompts").fetchone()[0] == 0
    with sqlite3.connect(responses_store.DB_PATH) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "responses" in tables:
            assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0


@pytest.mark.parametrize("shape", [s for s in SHAPES if s.wire == "multipart"], ids=lambda s: s.name)
def test_bracket_form_forget_keeps_no_history(world, shape):
    _run(world, shape, forget="bracket")
    assert _kept_rows(world.media) == []
    assert any({"kind": "request_history", "decision": "not_kept", "reason": "request_forget"}
               in receipt.get("retention", []) for receipt in _receipts(world))


def _session_headers(w: World, person: str, *, role: str = "none") -> dict[str, str]:
    cookie = proxy._browser_admin_cookie_config()
    session = browser_sessions.issue({"sub": person, "email": "matrix@example.test"}, role=role,
                                     cookie=cookie, path=w.db)
    token = encode_session_cookie(session, signing_key=proxy._browser_admin_session_signing_key())
    return {"cookie": f"{cookie.name}={token}", "x-azizam-user-action": "1",
            "x-azizam-csrf": proxy._csrf_token_for_oidc_session(session)}


def _other_app(w: World) -> str:
    app = admin_store.create_app("Second app", kind="personal", owner_user_id=w.person, access_plan="person", path=w.db)
    key = "".join(("opk", "_", "matrix_other_", app["id"]))
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"],
        hash=hash_api_key(key), prefix=key[:16], created_by=w.person, path=w.db)
    return key


def _second_key(w: World) -> str:
    key = "".join(("opk", "_", "matrix_second_", w.app))
    admin_store.create_credential_hash(subject_kind="app", subject_id=w.app, app_id=w.app,
        hash=hash_api_key(key), prefix=key[:16], created_by=w.person, path=w.db)
    return key


@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
def test_only_maker_can_read_and_delete_and_app_keys_stay_in_their_app(world, shape):
    _run(world, shape)
    # An omitted body still has no row to expose; these cases use the same route's readable control.
    if shape.name in {"gzip_json", "over_limit"}:
        assert _kept_rows(world.media) == []
        _run(world, Shape("readable_control"))
    [row] = _kept_rows(world.media)
    record_id = row["record_id"]
    other_key = _other_app(world)
    same_app_key = _second_key(world)
    readable_shape = Shape("readable_control") if shape.name in {"gzip_json", "over_limit"} else shape
    _run(world, readable_shape, key=other_key)
    [other_row] = [item for item in _kept_rows(world.media) if item["record_id"] != record_id]
    principals = []
    for role in ("member", "owner", "admin"):
        user = admin_store.upsert_oidc_user("https://fixture.test", f"matrix-{role}", path=world.db)
        if role != "member":
            admin_store.assign_admin_role(role=role, subject_kind="user", subject_id=user["id"], path=world.db)
        principals.append(_session_headers(world, user["id"], role="none" if role == "member" else role))

    async def check():
        async with gateway_client() as client:
            client.headers.clear()  # no default App key on browser requests
            path = f"/gateway/v1/requests/{record_id}"
            own_key = {"authorization": "Bearer " + world.key}
            reply = await client.get(path, headers=own_key)
            assert reply.status_code == 200
            assert reply.json()["record_id"] == record_id
            assert (await client.get(path, headers={"authorization": "Bearer " + same_app_key})).status_code == 200
            own_list = await client.get("/gateway/v1/requests", headers=own_key)
            assert [item["record_id"] for item in own_list.json()["requests"]] == [record_id]
            refused = await client.delete(path, headers=own_key)
            assert refused.status_code == 403  # accepted: privacy permission is not built
            assert refused.json()["error"]["code"] == "privacy_permission_required"
            for headers in [*principals, {"authorization": "Bearer " + other_key}]:
                listed = await client.get("/gateway/v1/requests", headers=headers)
                assert listed.status_code == 200
                assert record_id not in {item["record_id"] for item in listed.json()["requests"]}
                assert (await client.get(path, headers=headers)).status_code == 404
                assert (await client.delete(path, headers=headers)).status_code == 404
                # A bulk-delete attempt cannot erase the maker's record either.
                await client.delete("/gateway/v1/requests", headers=headers)
                assert (await client.get(path, headers=own_key)).status_code == 200
            maker = _session_headers(world, world.person)
            assert (await client.get(path, headers=maker)).status_code == 200
            assert (await client.delete(path, headers=maker)).status_code == 204
            assert (await client.get(path, headers=own_key)).status_code == 404
            other_path = f"/gateway/v1/requests/{other_row['record_id']}"
            assert (await client.get(other_path, headers={"authorization": "Bearer " + other_key})).status_code == 200
            assert (await client.delete(other_path, headers=maker)).status_code == 204
    asyncio.run(check())
    assert _kept_rows(world.media) == []


@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
def test_capture_work_and_buffers_stay_bounded_through_the_app(world, shape, monkeypatch):
    """Observe real capture work; do not replace the writer or cleaning functions.

    Buffer and budget bounds are deterministic. CPU is a generous backstop for
    accidental quadratic work, measured only inside history cleaning, not routing.
    """
    observations = []
    captures = []
    cpu = []
    original_feed = request_history.BodyCapture.feed
    original_result = request_history.BodyCapture.result
    original_init = request_history_capture._Capture.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captures.append(self)

    def feed(self, chunk):
        original_feed(self, chunk)
        observations.append(len(self._buffer) + self._field_bytes)

    def result(self):
        started = time.thread_time()
        try:
            return original_result(self)
        finally:
            cpu.append(time.thread_time() - started)

    monkeypatch.setattr(request_history_capture._Capture, "__init__", init)
    monkeypatch.setattr(request_history.BodyCapture, "feed", feed)
    monkeypatch.setattr(request_history.BodyCapture, "result", result)
    _run(world, shape)
    assert captures and observations
    assert max(observations) <= LIMIT
    assert sum(cpu) < 5.0, "history cleaning exceeded five CPU seconds for one bounded request"
    for capture in captures:
        assert capture.budget.decoded_bytes <= LIMIT
        assert capture.budget.nodes <= request_history.JSON_NODE_LIMIT
        assert capture.admitted is False, "writer admission slot leaked"
    for row in _kept_rows(world.media):
        assert row["byte_size"] <= 4 * LIMIT + 16384


def _grant(w: World, label: str, *, grant_id: str | None = None) -> tuple[str, str]:
    """Real stored grants/tokens, used at the core HTTP boundary MCP dispatch calls."""
    store = proxy.MCP_OAUTH.deps.store
    admin_store.ensure_session_app_for_user(w.person, name="Matrix signed-in work", path=w.db)
    client = store.put_client(client_id="matrix-client", kind="dynamic", name="Matrix client",
        redirect_uris=["https://client.example.test/callback"], grant_types=["authorization_code", "refresh_token"],
        auth_method="none", secret_hash=None, metadata={})
    if grant_id is None:
        grant = store.create_grant(person_id=w.person, client=client,
            redirect_uri=client.redirect_uris[0], resource="http://gateway.test/mcp", scope="mcp", session=None)
        grant_id = grant.id
    token = "".join(("azm", "_at_", label, "_", "M1a2T3r4I5x6T7o8K9e0N1"))
    store.save_token(token, kind="access", grant_id=grant_id, client_id=client.client_id, scope="mcp", ttl=3600)
    return token, grant_id


@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
def test_mcp_grants_read_only_their_connection_and_cannot_delete(world, shape):
    token, grant_id = _grant(world, "first")
    rotated, _ = _grant(world, "rotated", grant_id=grant_id)
    other_token, _ = _grant(world, "other")
    dispatch = {proxy._MCP_DISPATCH_HEADER: proxy._MCP_DISPATCH_SECRET}
    # Exactly the real MCP adapter's internal authentication; no identity stubs.
    _run(world, shape, key=token, auth_headers=dispatch)
    if shape.name in {"gzip_json", "over_limit"}:
        assert _kept_rows(world.media) == []
        _run(world, Shape("readable_control"), key=token, auth_headers=dispatch)
    [row] = _kept_rows(world.media)
    assert row["origin_kind"] == "mcp_grant" and row["origin_id"] == grant_id
    _assert_absent(_dump_records(world), {"mcp_access_token": token,
                                         "mcp_dispatch_credential": proxy._MCP_DISPATCH_SECRET})
    readable_shape = Shape("readable_control") if shape.name in {"gzip_json", "over_limit"} else shape
    _run(world, readable_shape, key=other_token, auth_headers=dispatch)
    [other_row] = [item for item in _kept_rows(world.media) if item["record_id"] != row["record_id"]]

    async def check():
        async with gateway_client() as client:
            client.headers.clear()
            path = f"/gateway/v1/requests/{row['record_id']}"
            # The public route must not accept an MCP-audience token by itself.
            public = await client.get(path, headers={"authorization": "Bearer " + token})
            assert public.status_code == 401
            own = {**dispatch, "authorization": "Bearer " + token}
            same_connection = {**dispatch, "authorization": "Bearer " + rotated}
            other_connection = {**dispatch, "authorization": "Bearer " + other_token}
            for headers in (own, same_connection):
                assert (await client.get(path, headers=headers)).status_code == 200
                listed = await client.get("/gateway/v1/requests", headers=headers)
                assert [item["record_id"] for item in listed.json()["requests"]] == [row["record_id"]]
                refused = await client.delete(path, headers=headers)
                assert refused.status_code == 403
                assert refused.json()["error"]["code"] == "privacy_permission_required"
            for headers in (other_connection, {"authorization": "Bearer " + world.key}):
                assert (await client.get(path, headers=headers)).status_code == 404
                listed = await client.get("/gateway/v1/requests", headers=headers)
                assert row["record_id"] not in {item["record_id"] for item in listed.json()["requests"]}
                assert (await client.delete(path, headers=headers)).status_code == 404
                await client.delete("/gateway/v1/requests", headers=headers)
                assert (await client.get(path, headers=own)).status_code == 200
            maker = _session_headers(world, world.person)
            assert (await client.delete(path, headers=maker)).status_code == 204
            other_path = f"/gateway/v1/requests/{other_row['record_id']}"
            assert (await client.get(other_path, headers=other_connection)).status_code == 200
            assert (await client.delete(other_path, headers=maker)).status_code == 204
    asyncio.run(check())


@pytest.mark.parametrize("shape", [
    pytest.param(shape, id=shape.name, marks=(
        pytest.mark.xfail(strict=True, raises=pytest.fail.Exception,
                          reason="R1: Cookie/Set-Cookie names are not scrubbed (gateway/request_history.py:50)")
        if shape.name not in {"gzip_json", "over_limit"} else []))
    for shape in SHAPES
])
def test_cookie_fields_inside_json_are_removed_from_saved_content(world, shape):
    """Cookies in parsed header objects/JSON strings are credentials too (requirement 1)."""
    _run(world, shape, payload=_payload(world.canaries, cookies=True))
    if shape.name not in {"gzip_json", "over_limit"}:
        assert len(_kept_rows(world.media)) == 1
    _assert_absent(_dump_records(world), {"nested_cookie": world.canaries["cookie"]})


@pytest.mark.parametrize("chunked", [False, True], ids=["declared_length", "chunked"])
@pytest.mark.xfail(strict=True, raises=pytest.fail.Exception,
                   reason="R2: last-key-wins JSON discards an earlier forget (gateway/request_history.py:514)")
def test_a_duplicate_extensions_key_cannot_override_an_earlier_forget(world, chunked):
    body = _body(Shape("duplicate_control"), _payload(world.canaries))
    # Last-key-wins must not turn a request containing forget into a kept request.
    raw = b'{"extensions":' + json.dumps(FORGET).encode() + b',"extensions":{},' + json.dumps(body).encode()[1:]

    async def call():
        async with gateway_client() as client:
            headers = {"authorization": "Bearer " + world.key, "content-type": "application/json"}
            if chunked:
                async def chunks():
                    for index in range(0, len(raw), 23):
                        yield raw[index:index + 23]
                return await client.post("/v1/chat/completions", content=chunks(), headers=headers)
            return await client.post("/v1/chat/completions", content=raw, headers=headers)
    world.shape = Shape("duplicate_control")
    response = asyncio.run(call())
    assert response.status_code in {200, 400}
    if _kept_rows(world.media):
        pytest.fail("an earlier forget was overridden by a duplicate extensions key", pytrace=False)


@pytest.mark.parametrize("shape", [s for s in SHAPES if s.wire == "multipart"], ids=lambda s: s.name)
@pytest.mark.parametrize("header_bytes", [LIMIT - 1, 4 * LIMIT], ids=["near_limit", "over_limit"])
def test_upload_part_headers_are_included_in_the_capture_memory_bound(world, shape, header_bytes, monkeypatch):
    """A file's field name/header is metadata; it must not bypass the byte budget."""
    captures = []
    metadata_sizes = []
    original_init = request_history_capture._Capture.__init__
    original_feed = request_history.BodyCapture.feed

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captures.append(self)
    def feed(self, chunk):
        original_feed(self, chunk)
        part_bytes = sum(len(value) for value in self._part.values() if isinstance(value, (str, bytes, bytearray)))
        files_bytes = sum(len(json.dumps(item).encode()) for item in self._files)
        metadata_sizes.append(part_bytes + files_bytes)
    monkeypatch.setattr(request_history_capture._Capture, "__init__", init)
    monkeypatch.setattr(request_history.BodyCapture, "feed", feed)
    extra = {"x" * header_bytes: ("extra.bin", b"x", "application/octet-stream")}
    response = asyncio.run(_send(world, shape, extra_files=extra))
    assert response.status_code in {200, 400, 413, 502}
    assert captures
    assert max(metadata_sizes) <= LIMIT, f"capture metadata bytes={max(metadata_sizes)}, limit={LIMIT}"
    # The limit is on captured content and metadata, independent of upload size.
    for capture in captures:
        metadata_bytes = sum(len(json.dumps(item).encode()) for item in capture.body._files)
        assert metadata_bytes + len(capture.body._buffer) <= LIMIT, "multipart metadata bypassed max_record_bytes"
        assert capture.admitted is False
    for row in _kept_rows(world.media):
        assert row["byte_size"] <= 2 * LIMIT + 16384


@pytest.mark.parametrize("shape", [s for s in SHAPES if s.wire == "multipart"], ids=lambda s: s.name)
@pytest.mark.parametrize("part_count", [
    pytest.param(8, id="within_metadata_budget"),
    pytest.param(320, id="over_metadata_budget", marks=pytest.mark.xfail(
        strict=True, raises=pytest.fail.Exception,
        reason="R4: multipart file-list metadata has no aggregate cap (gateway/request_history.py:588)")),
])
def test_upload_part_list_has_an_aggregate_capture_budget(world, shape, part_count, monkeypatch):
    """Every header fits python-multipart's limit; their combined metadata must fit too."""
    captures = []
    original_init = request_history_capture._Capture.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captures.append(self)
    monkeypatch.setattr(request_history_capture._Capture, "__init__", init)
    # Below Starlette's 1000-file ceiling and the parser's per-header ceiling.
    # The file bytes are tiny; field names alone exceed the history record cap.
    extra = {f"extra-{index}-" + "x" * 4000: ("part.bin", b"x", "application/octet-stream")
             for index in range(part_count)}
    response = asyncio.run(_send(world, shape, extra_files=extra))
    assert response.status_code in {200, 400, 413, 502}
    assert captures and any(capture.body.total > 0 for capture in captures)
    metadata_bytes = max(sum(len(json.dumps(item).encode()) for item in capture.body._files)
                         for capture in captures)
    if metadata_bytes > LIMIT:
        pytest.fail(f"aggregate capture metadata bytes={metadata_bytes}, limit={LIMIT}", pytrace=False)
    for row in _kept_rows(world.media):
        assert row["byte_size"] <= 2 * LIMIT + 16384


@pytest.mark.parametrize("stress", ["nested_nodes", "deep_json_string", "long_digit_name", "long_url_scheme"])
def test_adversarial_json_stays_within_cpu_memory_and_decode_budgets(world, stress, monkeypatch):
    """Exercise costly JSON inputs through routing and the real writer, below the input cap."""
    payload = _payload(world.canaries)
    if stress == "nested_nodes":
        payload["stress"] = ["[" + ",".join("0" for _ in range(200)) + "]"] * 1000
    elif stress == "deep_json_string":
        payload["stress"] = "[" * 5000 + json.dumps({"password": world.canaries["password"]}) + "]" * 5000
    elif stress == "long_digit_name":
        payload["stress"] = {"password" + "9" * 200_000: world.canaries["password"]}
    else:
        payload["stress"] = "a" * 120_000 + "://host.example.test/path"
    shape = Shape("adversarial_json")
    assert len(json.dumps(_body(shape, payload)).encode()) < LIMIT
    captures, cleaning_cpu = [], []
    original_init = request_history_capture._Capture.__init__
    original_result = request_history.BodyCapture.result

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captures.append(self)

    def result(self):
        started = time.thread_time()
        try:
            return original_result(self)
        finally:
            cleaning_cpu.append(time.thread_time() - started)

    monkeypatch.setattr(request_history_capture._Capture, "__init__", init)
    monkeypatch.setattr(request_history.BodyCapture, "result", result)
    _run(world, shape, payload=payload)
    assert len(_kept_rows(world.media)) == 1
    _assert_absent(_dump_records(world), world.canaries)
    assert sum(cleaning_cpu) < 5.0, "one bounded JSON input exceeded five CPU seconds of history cleaning"
    assert captures
    for capture in captures:
        assert len(capture.body._buffer) <= LIMIT
        assert capture.budget.decoded_bytes <= LIMIT
        assert capture.budget.nodes <= request_history.JSON_NODE_LIMIT
        assert capture.admitted is False
    if stress == "nested_nodes":
        assert "json_budget" in _dump_records(world), "the many-small-JSON-strings budget was not exercised"
    elif stress == "deep_json_string":
        assert "json_depth" in _dump_records(world)
