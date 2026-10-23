"""The per-request control: forget keeps nothing but the receipt's structural entry, on every data-plane route.

docs/design/REQUEST_RETENTION.md section 3.4 (the one control: extensions["com.azizam.prompts"] and the new header
X-Azizam-Prompts), 16.1.6 (the control on every route, invalid values refused, nothing reaches the engine) and 16.1.9
(the receipt's retention entry is set from the writer's result). Each route is sent once with forget and once without:
the second is the positive control, so a route that is not captured fails here too.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import proxy
from gateway import receipt_store, request_history
from gateway.receipts import validate_retention
from tests.support.gateway import gateway_client
from tests.support.fake_upstream import fake_upstreams  # noqa: F401 - fixture
from tests.support.fake_upstream.scenario import SCENARIO_DIR, scenario_from_data
from tests.registry_test_helpers import write_service_registry_for_test
from tests.support.gateway import register_litellm, use_admin_db
from tests.test_engine_dispatch_wiring import _registry
from gateway import admin_store, media_jobs
from tests.test_request_history_capture import SENTENCE, _kept_rows, _mint_member_key, chat_gateway  # noqa: F401

FORGET = {"extensions": {"com.azizam.prompts": {"mode": "forget"}}}

# (path, body) for each data-plane route that the design lists (3.4). The body is valid for the route, so the route
# parses it and the capture sees it. The reply does not matter: capture happens whatever the route returns.
JSON_ROUTES = [
    ("/v1/chat/completions", {"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}]}),
    ("/v1/completions", {"model": "garage-chat", "prompt": SENTENCE}),
    ("/v1/embeddings", {"model": "garage-chat", "input": SENTENCE}),
    ("/v1/rerank", {"model": "garage-chat", "query": SENTENCE, "documents": ["a", "b"]}),
    ("/v1/responses", {"model": "garage-chat", "input": SENTENCE}),
    ("/v1/messages", {"model": "garage-chat", "max_tokens": 8, "messages": [{"role": "user", "content": SENTENCE}]}),
    ("/api/chat", {"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}]}),
    ("/v1/images/generations", {"model": "garage-chat", "prompt": SENTENCE}),
    ("/v1/audio/speech", {"model": "garage-chat", "input": SENTENCE, "voice": "alloy"}),
]


def _send(path: str, body: dict, headers: dict | None = None):
    async def call():
        async with gateway_client() as client:
            return await client.post(path, json=body, headers=headers or {})
    return asyncio.run(call())


def _kept_count(media) -> int:
    return len(_kept_rows(media))


@pytest.fixture
def answering_gateway(fake_upstreams, monkeypatch, tmp_path):
    """A gateway whose scripted engine answers 200 whenever it is called, and is not required to be called.

    A forget never reaches the engine, and that is what these tests check, so the scripted exchange is optional.
    """
    fake = fake_upstreams.asgi(_optional_chat(model="garage-chat", content="ok"), host="litellm.fk.test")
    db = use_admin_db(monkeypatch, tmp_path)
    person_id = admin_store.upsert_oidc_user("https://fixture.test", "fixture-person", path=db)["id"]
    _mint_member_key(db, person_id)
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    receipt_store.init_db(receipt_store.DB_PATH)
    register_litellm(monkeypatch, db, fake.base_url)
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"].update(_registry("http://garage.fk.test/v1")["services"])
    write_service_registry_for_test(registry, path=db)
    return tmp_path, media, db


ENGINE_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/rerank",
                "/v1/images/generations", "/v1/audio/speech")


def _optional_chat(**values):
    """The chat-text scenario, with one optional exchange that answers any POST. A forget still dispatches the request,
    so the engine is called on every route, and the test does not care which engine path it was."""
    data = json.loads((SCENARIO_DIR / "openai-compatible" / "chat-text.json").read_text(encoding="utf-8"))
    answer = data["exchanges"][0]["response"]
    # One optional exchange per direct engine path. The frontends (Anthropic, Responses, Ollama) reach the chat route
    # through a stub forwarder in these tests, so they need none.
    data["exchanges"] = [{"id": f"any-{index}", "match": {"method": "POST", "path": path}, "response": answer,
                          "required": False, "repeat": True}
                         for index, path in enumerate(ENGINE_PATHS)]
    return scenario_from_data(data, source="chat-text-optional", **values)

@pytest.fixture
def frontends(monkeypatch):
    """The Anthropic, Responses and Ollama frontends reach the chat route through an in-process forwarder: stub it."""
    async def fake_forward(request, auth_context, cc_body, *, stream):
        import httpx
        from tests.test_anthropic_schema_conformance import chat_completion
        return httpx.Response(200, json=chat_completion({"role": "assistant", "content": "ok"}),
                              request=httpx.Request("POST", "http://gateway/v1/chat/completions"))
    monkeypatch.setattr(proxy, "_forward_chat_completions", fake_forward)


@pytest.mark.parametrize("path, body", JSON_ROUTES, ids=[path for path, _ in JSON_ROUTES])
def test_forget_in_the_body_keeps_nothing_on_every_json_route(path, body, answering_gateway, frontends):
    tmp_path, media, _ = answering_gateway
    _send(path, {**body, **FORGET})
    assert _kept_count(media) == 0


@pytest.mark.parametrize("path, body", JSON_ROUTES, ids=[path for path, _ in JSON_ROUTES])
def test_without_forget_the_same_route_keeps_its_request(path, body, answering_gateway, frontends):
    """Positive control: the route is captured, so the forget case above proves something."""
    tmp_path, media, _ = answering_gateway
    _send(path, body)
    assert _kept_count(media) == 1


def test_forget_in_a_multipart_form_field_keeps_nothing(answering_gateway, frontends):
    tmp_path, media, _ = answering_gateway
    files = {"file": ("clip.wav", b"RIFF" + b"\x00" * 64, "audio/wav")}
    data = {"model": "garage-chat", "extensions[com.azizam.prompts][mode]": "forget"}
    async def call():
        async with gateway_client() as client:
            return await client.post("/v1/audio/transcriptions", data=data, files=files)
    asyncio.run(call())
    assert _kept_count(media) == 0


def test_the_header_forgets_a_body_the_gateway_does_not_parse_and_is_stripped_before_dispatch(answering_gateway):
    """The middleware removes X-Azizam-Prompts before the route runs, and the forget keeps nothing (3.4)."""
    from gateway.request_history_capture import RequestHistoryMiddleware

    tmp_path, media, db = answering_gateway
    received: list[dict] = []

    async def route(scope, receive, send):
        received.append(scope)
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        from starlette.responses import PlainTextResponse
        await PlainTextResponse("ok")(scope, receive, send)

    middleware = RequestHistoryMiddleware(route, admin_path=lambda: db, media_path=lambda: media)
    raw = b"model=garage-chat&message=" + SENTENCE.encode()
    headers = [(b"content-type", b"text/plain"), (b"x-azizam-prompts", b"forget"), (b"content-length", str(len(raw)).encode())]
    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": headers,
             "query_string": b"", "state": {}}
    bodies = iter([{"type": "http.request", "body": raw, "more_body": False}])
    sent: list[dict] = []

    async def receive():
        return next(bodies, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    assert received, "the route did not run"
    assert all(key.lower() != b"x-azizam-prompts" for key, _ in received[0]["headers"])
    assert _kept_count(media) == 0


def test_without_the_header_a_non_json_body_is_recorded_as_unsupported_not_copied(answering_gateway):
    tmp_path, media, _ = answering_gateway
    raw = b"model=garage-chat&message=" + SENTENCE.encode()
    _send_raw("/v1/chat/completions", raw, {"content-type": "text/plain"})
    rows = _kept_rows(media)
    assert len(rows) == 1
    parts = json.loads(rows[0]["parts_json"])
    assert parts["accepted"]["body"]["omitted"] == "unsupported_container"
    assert SENTENCE not in rows[0]["parts_json"]


def _send_raw(path: str, raw: bytes, headers: dict):
    async def call():
        async with gateway_client() as client:
            return await client.post(path, content=raw, headers=headers)
    return asyncio.run(call())


@pytest.mark.parametrize("value", ["keep", "FORGET", "forget "])
def test_an_invalid_header_value_is_refused_with_400_and_keeps_nothing(answering_gateway, value):
    tmp_path, media, _ = answering_gateway
    response = _send_raw("/v1/chat/completions", b"{}", {"content-type": "application/json", "x-azizam-prompts": value})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_prompt_mode"
    assert _kept_count(media) == 0


@pytest.mark.parametrize("extension", [
    {"mode": "keep"}, {"mode": "forget", "extra": 1}, "forget",
])
def test_an_invalid_body_extension_is_refused_and_keeps_nothing(answering_gateway, extension):
    tmp_path, media, _ = answering_gateway
    body = {"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}],
            "extensions": {"com.azizam.prompts": extension}}
    response = _send("/v1/chat/completions", body)
    assert response.status_code == 400
    assert _kept_count(media) == 0


def test_the_extension_and_the_header_never_reach_the_engine(chat_gateway, monkeypatch):
    tmp_path, media, _ = chat_gateway
    sent = []
    real = proxy.send_llm_execution_request

    async def spy(request, *args, **kwargs):
        sent.append(request)
        return await real(request, *args, **kwargs)

    monkeypatch.setattr(proxy, "send_llm_execution_request", spy)
    _send("/v1/chat/completions", {"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}],
                                   **FORGET})
    assert sent, "the chat route did not reach the engine hop"
    for request in sent:
        assert b"com.azizam.prompts" not in request.body
        assert "x-azizam-prompts" not in {key.lower() for key in request.headers}


def _receipt_retention(request_id: str) -> list:
    conn = sqlite3.connect(receipt_store.DB_PATH)
    try:
        row = conn.execute("SELECT receipt_json FROM receipts WHERE request_id=?", (request_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]).get("retention", []) if row else []


def _latest_receipt_id() -> str:
    conn = sqlite3.connect(receipt_store.DB_PATH)
    try:
        row = conn.execute("SELECT request_id FROM receipts WHERE path=? ORDER BY created_at DESC LIMIT 1",
                           ("/v1/chat/completions",)).fetchone()
    finally:
        conn.close()
    return row[0]


def test_a_forget_writes_the_receipt_entry_not_kept_because_of_the_request(answering_gateway):
    _send("/v1/chat/completions", {"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}],
                                   **FORGET})
    entries = _receipt_retention(_latest_receipt_id())
    assert {"kind": "request_history", "decision": "not_kept", "reason": "request_forget"} in entries


def test_a_kept_request_writes_the_receipt_entry_with_its_deadline_and_parts(answering_gateway):
    _send("/v1/chat/completions", {"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}]})
    entry = next(item for item in _receipt_retention(_latest_receipt_id()) if item["kind"] == "request_history")
    assert entry["decision"] == "kept" and entry["reason"] == "kept"
    assert entry["parts"] == ["accepted"]
    assert isinstance(entry["kept_until_ms"], int) and entry["kept_until_ms"] > 0
    assert entry["limited_by"] == "member_choice"


def test_a_forced_writer_failure_is_recorded_as_failed_never_kept(answering_gateway, monkeypatch):
    def broken(record, *, path=None):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(request_history, "insert", broken)
    _send("/v1/chat/completions", {"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}]})
    entries = _receipt_retention(_latest_receipt_id())
    assert {"kind": "request_history", "decision": "failed", "reason": "write_failed"} in entries


def test_the_receipt_refuses_free_text_in_the_request_history_entry():
    with pytest.raises(ValueError):
        validate_retention([{"kind": "request_history", "decision": "not_kept", "reason": "a sentence the user typed"}])
    with pytest.raises(ValueError):
        validate_retention([{"kind": "request_history", "decision": "kept", "reason": "kept", "kept_until_ms": 1,
                             "parts": ["sent"], "limited_by": "member_choice"}])
