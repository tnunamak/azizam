"""Home's recent chats and the wait and cost each capability row carries.

A chat's title is the first user message of its kept body, read when Home is built. Nothing stores it, so erasing the
kept request erases the title. Only the person's own kept chats are listed (docs/design/REQUEST_RETENTION.md 4).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gateway import request_history, request_history_api
from gateway.capabilities import Capability
from gateway.home_api import _cost_hint, _typical_seconds

MAKER = "usr_robin"
OTHER = "usr_other"


def _chat_parts(content):
    messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": content}]
    return {"accepted": {"headers": {}, "body": {"model": "fast", "messages": messages}}}


def _record(record_id, *, person=MAKER, created_at=1_000, route="/v1/chat/completions", parts=None, kept_until=None):
    return {
        "record_id": record_id, "request_id": f"req-{record_id}", "person_id": person, "app_id": "app_one",
        "origin_kind": "session", "origin_id": None, "created_at": created_at, "kept_until": kept_until,
        "parts": _chat_parts("Plan a two-day coastal trip") if parts is None else parts, "omissions": [],
        "policy_version": 1, "summary": {"route": route, "model": "fast", "status": 200, "latency_ms": 5},
    }


@pytest.fixture
def media_db(tmp_path):
    return tmp_path / "media-jobs.db"


def test_first_user_text_takes_the_first_user_message_and_nothing_else():
    body = json.dumps(_chat_parts("  Plan\n a   trip  "))
    assert request_history.first_user_text(body) == "Plan a trip"


def test_first_user_text_reads_text_parts_and_caps_the_length():
    parts = {"accepted": {"body": {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "x"}}, {"type": "text", "text": "Describe this"}, {"type": "text", "text": "in detail"},
    ]}]}}}
    assert request_history.first_user_text(json.dumps(parts)) == "Describe this in detail"
    long = {"accepted": {"body": {"messages": [{"role": "user", "content": "w" * 200}]}}}
    assert len(request_history.first_user_text(json.dumps(long))) == request_history.TITLE_CHARS


@pytest.mark.parametrize("parts", [None, "", "not json", json.dumps({"accepted": {"body": {"input": "x"}}}),
                                   json.dumps({"accepted": {"body": {"messages": [{"role": "system", "content": "s"}]}}}),
                                   json.dumps({"accepted": {"body": {"messages": [{"role": "user", "content": "   "}]}}})])
def test_first_user_text_is_none_when_the_kept_body_holds_no_user_words(parts):
    assert request_history.first_user_text(parts) is None


def test_recent_chats_lists_the_persons_own_kept_chats_newest_first_with_titles(media_db, tmp_path):
    admin = tmp_path / "admin.db"
    request_history.insert(_record("old", created_at=100, parts=_chat_parts("Earlier question")), path=media_db)
    request_history.insert(_record("new", created_at=300, parts=_chat_parts("Plan a trip")), path=media_db)
    request_history.insert(_record("theirs", person=OTHER, created_at=400), path=media_db)
    request_history.insert(_record("picture", created_at=350, route="/v1/images/generations"), path=media_db)
    chats = request_history_api.recent_chats(MAKER, admin_path=admin, media_path=media_db, limit=6, now=1_000)
    assert [chat["record_id"] for chat in chats] == ["new", "old"]
    assert chats[0] == {"request_id": "req-new", "record_id": "new", "created_at": 300, "title": "Plan a trip"}


def test_recent_chats_leaves_out_an_expired_chat_and_one_with_no_kept_words(media_db, tmp_path):
    admin = tmp_path / "admin.db"
    request_history.insert(_record("expired", created_at=100, kept_until=900), path=media_db)
    request_history.insert(_record("no-body", created_at=200, parts={"received": {"status": 200}}), path=media_db)
    request_history.insert(_record("kept", created_at=300, kept_until=5_000), path=media_db)
    chats = request_history_api.recent_chats(MAKER, admin_path=admin, media_path=media_db, limit=6, now=1_000)
    assert [chat["record_id"] for chat in chats] == ["kept"]


def test_recent_chats_reads_titles_without_storing_them(media_db, tmp_path):
    admin = tmp_path / "admin.db"
    request_history.insert(_record("one", created_at=100), path=media_db)
    before = request_history.get_row("one", path=media_db)
    request_history_api.recent_chats(MAKER, admin_path=admin, media_path=media_db, limit=6, now=1_000)
    after = request_history.get_row("one", path=media_db)
    assert before == after


def test_recent_chats_is_empty_for_a_person_with_no_history(media_db, tmp_path):
    assert request_history_api.recent_chats(MAKER, admin_path=tmp_path / "admin.db", media_path=media_db, limit=6, now=1_000) == []


def test_typical_seconds_comes_from_the_workflow_manifest_then_the_configured_timing():
    cap = Capability(intent="image.generate", backend="engine", workflow="krea2", tags=("resolves_to:krea",))
    records = (SimpleNamespace(id="krea2", manifest={"typical_seconds": 9}),)
    assert _typical_seconds(cap, None, records) == 9.0
    model_cap = Capability(intent="image.generate", backend="engine", model="pic", tags=("resolves_to:pic",))
    registry = {"services": {"engine": {"config": {"models": [{"id": "pic", "typical_seconds": 20}]}}}}
    assert _typical_seconds(model_cap, registry, ()) == 20.0


@pytest.mark.parametrize("value", [None, 0, -4, True, "9"])
def test_typical_seconds_is_missing_unless_a_positive_number_is_declared(value):
    cap = Capability(intent="image.generate", backend="engine", model="pic", tags=("resolves_to:pic",))
    registry = {"services": {"engine": {"config": {"models": [{"id": "pic", "typical_seconds": value}]}}}}
    assert _typical_seconds(cap, registry, ()) is None


def test_cost_hint_is_free_for_local_paid_for_remote_and_unknown_otherwise():
    cap = Capability(intent="chat.completions", backend="engine", model="fast")
    assert _cost_hint("local", cap, None) == {"kind": "free", "price_usd": None}
    assert _cost_hint(None, cap, None) == {"kind": "unknown", "price_usd": None}
    priced = {"services": {"engine": {"config": {"price": 0.04}}}}
    assert _cost_hint("remote", cap, priced) == {"kind": "paid", "price_usd": 0.04}
    assert _cost_hint("remote", cap, {"services": {"engine": {"config": {}}}}) == {"kind": "paid", "price_usd": None}
