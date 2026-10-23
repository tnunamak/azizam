"""Review probes for retention slice 5a (feat/retention-s5a at fc613b8).

Each test states the behaviour the design (REQUEST_RETENTION.md 3.4-3.6, 4, 12) or DECISIONS.md D50-D52 asks for.
A test marked ``xfail(strict=True)`` is a review finding: it fails today, and the strict mark makes it pass loudly
once the finding is fixed, so the fixer removes the mark. Unmarked tests pin a claim the review proved holds.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import admin_store, media_jobs, receipt_store, request_history, retention
from gateway.request_history_capture import IDENTITY_STATE_KEY, RequestHistoryMiddleware
from tests.support.gateway import use_admin_db
from tests.test_request_history_routes import _call, _member, _record, _store  # noqa: F401
from tests.test_request_history_routes import gateway  # noqa: F401 - fixture
from tests.test_prompt_memory_responses import _post_response, responses_gateway  # noqa: F401 - fixture

CANARY = "sk-live-review-canary-0123456789"
SENTENCE = "a sentence the member typed for the review probe"
BOUNDARY = "reviewboundary"


# ------------------------------------------------------------------ helpers


def _multipart(fields: list[tuple[str, str | bytes, str | None]]) -> bytes:
    """A multipart body; a field with a filename is a file part."""
    out = bytearray()
    for name, value, filename in fields:
        out += f"--{BOUNDARY}\r\n".encode()
        if filename is None:
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        else:
            out += (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    "Content-Type: audio/wav\r\n\r\n").encode()
        out += value if isinstance(value, bytes) else value.encode()
        out += b"\r\n"
    out += f"--{BOUNDARY}--\r\n".encode()
    return bytes(out)


def _capture(content_type: str, raw: bytes) -> request_history.BodyCapture:
    body = request_history.BodyCapture(content_type, limit=1 << 20)
    body.feed(raw)
    return body


@pytest.fixture
def member_db(monkeypatch, tmp_path):
    """An admin DB with one new member (request history ON, D50) and an empty media DB."""
    db = use_admin_db(monkeypatch, tmp_path)
    person = admin_store.upsert_oidc_user("https://fixture.test", "probe-person", path=db)["id"]
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    receipt_store.init_db(receipt_store.DB_PATH)
    return db, media, person


def _identity(person: str):
    return SimpleNamespace(identity=SimpleNamespace(person_id=person, app_id="app_probe", auth_kind="app_key",
                                                    credential_id="cred_probe", connection_id=None))


def _drive(db: Path, media: Path, person: str, *, chunks: list[bytes], content_type: str,
           route_reads: int | None = None) -> list[dict]:
    """Run the capture around a route that authenticates, reads ``route_reads`` body chunks (all when None) and
    answers. After the response is complete, ``receive`` answers ``http.disconnect`` at once: uvicorn 0.33's
    h11/httptools ``receive`` does exactly that (``if self.disconnected or self.response_complete``)."""
    sent: list[dict] = []
    state = {"response_complete": False, "index": 0}

    async def route(scope, receive, send):
        scope["state"][IDENTITY_STATE_KEY] = _identity(person)
        reads = 0
        while route_reads is None or reads < route_reads:
            message = await receive()
            reads += 1
            if not message.get("more_body", False):
                break
        from starlette.responses import JSONResponse
        await JSONResponse({"error": {"message": "refused early"}}, status_code=413)(scope, receive, send)

    async def receive():
        if state["response_complete"] or state["index"] >= len(chunks):
            return {"type": "http.disconnect"}
        index = state["index"]
        state["index"] += 1
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            state["response_complete"] = True

    middleware = RequestHistoryMiddleware(route, admin_path=lambda: db, media_path=lambda: media)
    scope = {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
             "headers": [(b"content-type", content_type.encode())], "query_string": b"", "state": {}}
    asyncio.run(middleware(scope, receive, send))
    return sent


def _kept(media: Path) -> list[str]:
    conn = sqlite3.connect(media)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='kept_requests'").fetchone() is None:
            return []
        return [row[0] for row in conn.execute("SELECT parts_json FROM kept_requests")]
    finally:
        conn.close()


# ------------------------------------------------------------------ 1. the secret floor


@pytest.mark.parametrize("name", ["headers[Authorization]", "provider[api_key]", "auth[password]",
                                  "extensions[com.example][api_key]"])
def test_a_bracketed_multipart_credential_field_is_removed(name):
    """The OpenAI SDK flattens a nested form value to ``a[b]`` (the gateway itself reads
    ``extensions[ns][name]``). Rule 8 splits names on ``-_ .`` and camel case only, so ``[`` hides the segment."""
    raw = _multipart([("model", "whisper-1", None), (name, CANARY, None)])
    kept, _ = _capture(f"multipart/form-data; boundary={BOUNDARY}", raw).result()
    assert CANARY not in json.dumps(kept)


def test_a_signed_url_inside_message_text_loses_its_query_values():
    """Rule 4 is 'URLs inside a body'. Only a string that is a whole URL is scrubbed; a pasted signed URL in a
    message keeps its signature."""
    text = f"describe https://bucket.s3.amazonaws.com/a.png?X-Amz-Signature={CANARY} please"
    kept, _ = request_history.redact_json({"messages": [{"role": "user", "content": text}]})
    assert CANARY not in json.dumps(kept)


def test_a_credential_field_inside_tool_call_arguments_is_removed():
    args = json.dumps({"url": "https://api.example.test/v1", "api_key": CANARY})
    body = {"messages": [{"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "fetch", "arguments": args}}]}]}
    kept, _ = request_history.redact_json(body)
    assert CANARY not in json.dumps(kept)


@pytest.mark.parametrize("name", ["session_token", "aws_session_token", "private_key", "bearer", "cookie",
                                  "hf_token", "github_token", "token", "key"])
def test_names_rule_8_does_not_cover_today(name):
    """Documents the edge of rule 8 as specified (names only, no plain ``token``). Not a defect of the build; listed
    so a decision to widen the list is visible."""
    kept, _ = request_history.redact_json({name: CANARY})
    assert kept[name] == CANARY


def test_whole_url_and_header_and_query_rules_hold():
    """Positive controls for the floor: a whole signed URL, headers, data URLs and a nested key are scrubbed."""
    kept, found = request_history.redact_json({
        "image_url": {"url": f"https://u:p@h.test/p?sig={CANARY}#f"},
        "a": {"b": {"c": {"apiKey": CANARY, "X-Api-Key": CANARY, "client_secret": CANARY}}},
        "img": "data:image/png;base64," + "A" * 100,
    })
    assert CANARY not in json.dumps(kept)
    assert {"credential_field", "url_parts_removed", "media"} <= set(found)
    assert request_history.allowed_request_headers(
        {"Authorization": CANARY, "Cookie": CANARY, "X-Api-Key": CANARY, "Content-Type": "application/json"}
    ) == {"content-type": "application/json"}


# ------------------------------------------------------------------ 3. forget


@pytest.mark.xfail(strict=True, reason="review finding: the JSON-string extensions form field is not read")
def test_forget_in_the_json_string_extensions_form_field_keeps_nothing(member_db):
    """The workflow and identity routes accept the forget control as a JSON-string ``extensions`` form field
    (proxy.py:24085 ``multipart_extensions(form.get("extensions"))``; gateway/identity_api.py:393). The capture
    reads only ``extensions[com.azizam.prompts][mode]``, so it keeps a request the route itself forgot."""
    db, media, person = member_db
    raw = _multipart([("prompt", SENTENCE, None),
                      ("extensions", json.dumps({"com.azizam.prompts": {"mode": "forget"}}), None)])
    _drive(db, media, person, chunks=[raw], content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert _kept(media) == []


def test_positive_control_the_bracket_form_forget_keeps_nothing(member_db):
    db, media, person = member_db
    raw = _multipart([("prompt", SENTENCE, None), ("extensions[com.azizam.prompts][mode]", "forget", None)])
    _drive(db, media, person, chunks=[raw], content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert _kept(media) == []
    raw = _multipart([("prompt", SENTENCE, None)])
    _drive(db, media, person, chunks=[raw], content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert len(_kept(media)) == 1


@pytest.mark.xfail(strict=True, reason="review finding: http.disconnect is counted as a complete body")
def test_an_unread_body_whose_forget_never_arrived_is_not_kept(member_db):
    """The route answers 413 after reading the first chunk. Under uvicorn the capture's 30 s drain then gets
    ``http.disconnect`` at once, and ``receive_wrapper`` marks the body complete. The fields seen so far are kept,
    although the forget field was in the part that never arrived (the case the 'incomplete' branch exists for)."""
    db, media, person = member_db
    raw = _multipart([("prompt", SENTENCE, None), ("file", b"RIFF" + b"\0" * 4096, "a.wav"),
                      ("extensions[com.azizam.prompts][mode]", "forget", None)])
    split = raw.index(b"\0" * 64)
    _drive(db, media, person, chunks=[raw[:split], raw[split:]],
           content_type=f"multipart/form-data; boundary={BOUNDARY}", route_reads=1)
    assert _kept(media) == []


@pytest.mark.xfail(strict=True, reason="review finding: the header forget is stripped before the other kinds see it")
def test_the_forget_header_also_forgets_the_conversation(responses_gateway):
    """3.4: the one control covers all four kinds. The header is removed before the route runs, so the Responses
    route never learns of it and stores the conversation that the header asked to forget."""
    response = responses_gateway["client"].post(
        "/v1/responses", headers={**responses_gateway["headers"], "X-Azizam-Prompts": "forget"},
        json={"model": "default", "input": SENTENCE})
    assert response.status_code == 200, response.text
    import proxy
    assert proxy.responses_store.get(response.json()["id"], path=responses_gateway["responses_db"]) is None


# ------------------------------------------------------------------ 2. the reader floor


def test_the_owner_and_an_admin_with_personal_keys_see_none_of_a_members_rows(gateway):
    """The existing 'owner' test uses a second member. Here the owner and an admin hold real roles."""
    db = gateway["db"]
    owner, admin = _member(db, "the-owner"), _member(db, "an-admin")
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=owner.person, path=db)
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=admin.person, path=db)
    record = _record(gateway["maker"])
    _store(gateway, record)
    for viewer in (owner, admin):
        assert _call("GET", "/gateway/v1/requests", viewer.key).json()["requests"] == []
        assert _call("GET", f"/gateway/v1/requests/{record['record_id']}", viewer.key).status_code == 404
        assert _call("DELETE", f"/gateway/v1/requests/{record['record_id']}", viewer.key).status_code == 404
    assert _call("GET", f"/gateway/v1/requests/{record['record_id']}", gateway["maker"].key).status_code == 200


def test_the_makers_other_app_cannot_read_and_the_same_apps_second_key_can(gateway):
    """D52: a delegated key reads what its own App made. A second App of the same person is 'elsewhere'. The
    check compares the App, not the credential: a second key of the same App reads the row."""
    from gateway.runtime_auth import hash_api_key

    db, maker = gateway["db"], gateway["maker"]
    record = _record(maker)
    _store(gateway, record)
    other_app = admin_store.create_app("second app", kind="personal", owner_user_id=maker.person,
                                       access_plan="person", path=db)
    other_key = f"opk_second_{other_app['id']}"
    admin_store.create_credential_hash(subject_kind="app", subject_id=other_app["id"], app_id=other_app["id"],
                                       hash=hash_api_key(other_key), prefix=other_key[:16],
                                       created_by=maker.person, path=db)
    same_app_key = f"opk_again_{maker.app}"
    admin_store.create_credential_hash(subject_kind="app", subject_id=maker.app, app_id=maker.app,
                                       hash=hash_api_key(same_app_key), prefix=same_app_key[:16],
                                       created_by=maker.person, path=db)
    assert _call("GET", f"/gateway/v1/requests/{record['record_id']}", other_key).status_code == 404
    assert _call("GET", "/gateway/v1/requests", other_key).json()["requests"] == []
    assert _call("GET", f"/gateway/v1/requests/{record['record_id']}", same_app_key).status_code == 200


def test_a_receipts_request_history_entry_is_masked_for_anyone_but_its_maker(tmp_path):
    path = tmp_path / "receipts.db"
    receipt_store.init_db(path)
    from tests.test_receipt_detail_context import CHAT_RECEIPT
    receipt_store.record({**CHAT_RECEIPT, "request_id": "req_probe", "maker_person_id": "person-a"}, path=path)
    entry = {"kind": "request_history", "decision": "kept", "reason": "kept", "kept_until_ms": 1792300000000,
             "parts": ["accepted"], "limited_by": "member_choice"}
    assert receipt_store.update_retention("req_probe", entry, path=path)
    for audience in ("client", "admin"):
        other = receipt_store.get("req_probe", audience=audience, viewer_person_id="person-b", path=path)
        assert other["retention"] == [{"kind": "request_history", "decision": "kept"}]
        maker = receipt_store.get("req_probe", audience=audience, viewer_person_id="person-a", path=path)
        assert maker["retention"] == [entry]


# ------------------------------------------------------------------ 4. cost


@pytest.mark.xfail(strict=True, reason="review finding: every request of an OFF member takes the admin.db write lock")
def test_a_member_with_history_off_costs_no_admin_write_transaction(member_db, monkeypatch):
    """Every existing member starts OFF (12.3), so this is all of today's traffic."""
    db, media, person = member_db
    request_history.set_member_choice(person, keep=False, retention_id="7_days", parts=["accepted"],
                                      admin_path=db, path=media, now=int(time.time()) - 5)
    calls = []
    real = retention.guard

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(retention, "guard", counting)
    _drive(db, media, person, chunks=[b'{"model":"m","input":"x"}'], content_type="application/json")
    assert _kept(media) == []
    assert calls == []


def test_existing_members_start_off_and_new_members_on(tmp_path, monkeypatch):
    """12.3 and D50, against the real upgrade: build a schema-16 layout with one person, upgrade, add a person."""
    from gateway import retention_migration

    db = use_admin_db(monkeypatch, tmp_path)
    old = admin_store.upsert_oidc_user("https://fixture.test", "old-person", path=db)["id"]
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE prompt_memory_preferences")
    conn.execute("CREATE TABLE prompt_memory_preferences (user_id TEXT PRIMARY KEY, remember INTEGER NOT NULL, "
                 "retention TEXT NOT NULL, updated_at INTEGER NOT NULL, updated_by TEXT)")
    conn.execute("INSERT INTO prompt_memory_preferences VALUES (?, 1, 'never', 1, NULL)", (old,))
    retention_migration.migrate_group_keys(conn)
    conn.commit()
    conn.close()
    new = admin_store.upsert_oidc_user("https://fixture.test", "new-person", path=db)["id"]
    assert retention.member_choice(db, old, retention.GROUP_REQUEST_HISTORY)["remember"] is False
    choice = retention.member_choice(db, new, retention.GROUP_REQUEST_HISTORY)
    assert choice is None or choice["remember"] is True


@pytest.mark.xfail(strict=True, reason="review finding: an over-limit body's forget is never read, so a row is kept")
def test_a_json_body_over_the_record_limit_that_forgets_leaves_no_row(member_db):
    """The body is omitted as record_size, so no words are kept, but the control in it is never parsed: the request
    the caller sent incognito still gets a kept row (route, status, time) and a receipt that says kept."""
    db, media, person = member_db
    body = json.dumps({"model": "m", "input": "x" * (1 << 20),
                       "extensions": {"com.azizam.prompts": {"mode": "forget"}}}).encode()
    _drive(db, media, person, chunks=[body], content_type="application/json")
    assert _kept(media) == []
