"""The plan's own usage windows: read from each plan answer's headers, kept per connection, shown to the owner only."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import receipt_store, subscription_api, subscription_kinds
from gateway.admin_api import AdminAuthError, AdminContext
from tests.support.subscription_fakes import KIND, Upstream, connect, make_service
from tests.test_subscription_plan_access import COMPLETED, RESPONSES_PATH, _receipts_table, call, granted, sse  # noqa: F401

START = 1_000_000.0
PRIMARY = KIND.usage_windows[0]
SECONDARY = KIND.usage_windows[1]


def run(coro):
    return asyncio.run(coro)


def plan_headers(**values: str) -> dict[str, str]:
    """Usage headers by the kind's own names: primary_used_percent="42" -> the primary window's percent header."""
    out = {}
    for key, value in values.items():
        which, field = key.split("_", 1)
        out[(PRIMARY if which == "primary" else SECONDARY)[field]] = value
    return out


def answering(upstream, headers: dict[str, str], status: int = 200) -> None:
    if status == 200:
        upstream.routes[("POST", RESPONSES_PATH)] = lambda r: httpx.Response(
            200, content=sse({"type": "response.created", "response": {}}, COMPLETED),
            headers={"content-type": "text/event-stream", **headers})
    else:
        upstream.routes[("POST", RESPONSES_PATH)] = lambda r: httpx.Response(
            status, json={"error": {"code": "subscription_sharing_usage_limit_exceeded"}}, headers=headers)


@pytest.fixture()
def plan(tmp_path):
    now = [START]
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream, now=lambda: now[0])
    run(connect(service, jwks, upstream))
    return service, store, upstream, now


def limits(service):
    return run(service.status_one("chatgpt")).get("usage_limits")


FRESH = plan_headers(primary_used_percent="42.5", primary_window_minutes="300", primary_resets_at=str(int(START) + 3600),
                     secondary_used_percent="18", secondary_window_minutes="10080",
                     secondary_resets_after_seconds="86400")


# ------------------------------------------------------------------ capture

def test_a_plan_answer_with_usage_headers_is_kept_as_numbers(plan):
    service, store, upstream, now = plan
    answering(upstream, FRESH)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    assert limits(service) == {"captured_at": int(START), "windows": [
        {"used_percent": 42.5, "window_minutes": 300, "resets_at": int(START) + 3600},
        {"used_percent": 18.0, "window_minutes": 10080, "resets_at": int(START) + 86400}]}


def test_an_answer_without_usage_headers_keeps_the_last_reading(plan):
    service, store, upstream, now = plan
    ident, access = granted()
    answering(upstream, FRESH)
    assert call(service, ident, access).status_code == 200
    kept = limits(service)
    now[0] += 600
    answering(upstream, {})
    assert call(service, ident, access).status_code == 200
    assert limits(service) == kept


def test_a_refusal_carries_usage_too(plan):
    service, store, upstream, now = plan
    answering(upstream, plan_headers(primary_used_percent="100", primary_window_minutes="300",
                                     primary_resets_at=str(int(START) + 900)), status=429)
    ident, access = granted()
    assert call(service, ident, access).status_code == 429
    assert limits(service)["windows"] == [{"used_percent": 100.0, "window_minutes": 300, "resets_at": int(START) + 900}]


@pytest.mark.parametrize("headers, windows", [
    (plan_headers(primary_used_percent="lots"), None),
    (plan_headers(primary_used_percent="nan", secondary_used_percent="inf"), None),
    (plan_headers(primary_used_percent="-3", primary_window_minutes="300"), None),
    (plan_headers(primary_used_percent="0"), None),  # no length, no reset: no data, as in Codex CLI
    (plan_headers(primary_used_percent="7", primary_window_minutes="five", primary_resets_at="soon",
                  primary_resets_after_seconds="-1"),
     [{"used_percent": 7.0, "window_minutes": None, "resets_at": None}]),
    (plan_headers(primary_used_percent="7", primary_window_minutes="0", primary_resets_at="99999999999999"),
     [{"used_percent": 7.0, "window_minutes": None, "resets_at": None}]),
])
def test_malformed_values_are_dropped_never_a_500(plan, headers, windows):
    service, store, upstream, now = plan
    answering(upstream, headers)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    shown = limits(service)
    assert (shown["windows"] if shown else None) == windows


def test_a_store_that_cannot_keep_usage_never_fails_the_request(plan, monkeypatch):
    service, store, upstream, now = plan

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "record_usage", broken, raising=False)
    answering(upstream, FRESH)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200


def test_a_new_sign_in_starts_without_the_old_reading(plan):
    service, store, upstream, now = plan
    answering(upstream, FRESH)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    assert limits(service)
    store.save_connection(kind="chatgpt", connected_by="owner_1", client_id="c", host_id="h", tokens=None,
                          origin=KIND.upstream_origin, scopes=[], email=None, subject=None, session=None)
    assert limits(service) is None


def test_a_store_without_the_usage_column_is_upgraded_in_place(tmp_path):
    path = tmp_path / "subscriptions.db"
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE subscription_connections (kind TEXT PRIMARY KEY, connected_by TEXT NOT NULL,
      client_id TEXT NOT NULL, host_id TEXT NOT NULL, status TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '',
      account_email TEXT, account_subject TEXT, access_expires_at INTEGER, earliest_refresh_at INTEGER,
      models_json TEXT NOT NULL DEFAULT '{}', models_at INTEGER, connected_at INTEGER, updated_at INTEGER NOT NULL);
      INSERT INTO subscription_connections(kind, connected_by, client_id, host_id, status, updated_at)
      VALUES ('chatgpt', 'o', 'c', 'h', 'connected', 5);""")
    conn.commit()
    conn.close()
    from gateway import subscription_store
    store = subscription_store.SubscriptionStore(lambda: path)
    assert store.connection("chatgpt")["usage"] is None
    store.record_usage("chatgpt", {"captured_at": 9, "windows": [{"used_percent": 1.0, "window_minutes": 300, "resets_at": None}]})
    assert store.connection("chatgpt")["usage"]["captured_at"] == 9


@pytest.mark.parametrize("rows", [
    "not a list", [{"window_minutes": "x-a"}], [{"used_percent": "x-a", "colour": "x-b"}], [{"used_percent": 3}]])
def test_a_kind_row_with_bad_usage_windows_is_refused(rows):
    import yaml
    raw = yaml.safe_load((subscription_kinds.KINDS_DIR / "chatgpt.yml").read_text())
    raw["usage_windows"] = rows
    with pytest.raises(ValueError, match="usage_windows"):
        subscription_kinds.normalize_kind(raw)


@pytest.mark.parametrize("value", ["http://chatgpt.com/settings/usage", "chatgpt.com/settings/usage", 7, ["https://x.test"]])
def test_a_kind_row_with_a_usage_page_that_is_not_an_https_address_is_refused(value):
    import yaml
    raw = yaml.safe_load((subscription_kinds.KINDS_DIR / "chatgpt.yml").read_text())
    raw["usage_page_url"] = value
    with pytest.raises(ValueError, match="usage_page_url"):
        subscription_kinds.normalize_kind(raw)


def test_each_shipped_plan_kind_names_its_providers_usage_page():
    pages = {kind_id: kind.usage_page_url for kind_id, kind in subscription_kinds.load_kinds().items()}
    assert pages == {"chatgpt": "https://chatgpt.com/settings/usage", "claude": "https://claude.ai/settings/usage"}


# ------------------------------------------------------------------ the owner's API

def _client(service, roles):
    def auth(request):
        if request.headers.get("x-test-who") is None:
            raise AdminAuthError("no", status_code=401, code="unauthorized")
        return AdminContext(subject="p1", actor_id="p1", roles=frozenset(roles), auth_method="key")

    app = FastAPI()
    app.include_router(subscription_api.create_subscription_router(service=service, auth=auth))
    return TestClient(app)


def test_the_owner_sees_the_reading_and_no_token_or_header_name(plan):
    service, store, upstream, now = plan
    answering(upstream, FRESH)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    response = _client(service, {"owner"}).get("/admin/api/subscriptions", headers={"x-test-who": "1"})
    assert response.status_code == 200
    shown = response.json()["subscriptions"][0]
    assert shown["usage_limits"]["windows"][0] == {"used_percent": 42.5, "window_minutes": 300, "resets_at": int(START) + 3600}
    tokens = store.load_tokens("chatgpt", origin=KIND.upstream_origin)
    text = response.text
    assert tokens["access_token"] not in text and tokens["refresh_token"] not in text
    assert not any(name in text for window in KIND.usage_windows for name in window.values())


def test_a_member_cannot_read_it(plan):
    service, store, upstream, now = plan
    answering(upstream, FRESH)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    response = _client(service, {"admin", "member"}).get("/admin/api/subscriptions", headers={"x-test-who": "1"})
    assert response.status_code == 403 and "usage_limits" not in response.text


def test_never_seen_means_absent_and_the_view_points_to_the_providers_usage_page(plan):
    service, *_ = plan
    response = _client(service, {"owner"}).get("/admin/api/subscriptions", headers={"x-test-who": "1"})
    shown = response.json()["subscriptions"][0]
    assert "usage_limits" not in shown
    assert shown["usage_page_url"] == "https://chatgpt.com/settings/usage"


# ------------------------------------------------------------------ tokens the gateway counted

def _receipt(conn, request_id, created_at, connection_id, usage):
    conn.execute("INSERT INTO receipts(request_id, created_at, receipt_json) VALUES (?, ?, ?)",
                 (request_id, created_at, json.dumps({"request_id": request_id, "usage": usage,
                                                      "metadata": {"connection_id": connection_id}})))


def test_token_totals_count_one_connection_over_a_day_and_a_week(tmp_path):
    path = tmp_path / "metrics.db"
    receipt_store.init_db(path)
    now = 2_000_000
    with sqlite3.connect(path) as conn:
        _receipt(conn, "r1", now - 60, "chatgpt-owner", {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2})
        _receipt(conn, "r2", now - 3 * 86400, "chatgpt-owner", {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20})
        _receipt(conn, "r3", now - 8 * 86400, "chatgpt-owner", {"input_tokens": 999, "output_tokens": 999})
        _receipt(conn, "r4", now - 60, "other-owner", {"input_tokens": 999, "output_tokens": 999})
        _receipt(conn, "r5", now - 120, "chatgpt-owner", None)
    totals = receipt_store.plan_token_totals("chatgpt-owner", now=now, path=path)
    assert totals == {
        "day": {"requests": 2, "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2,
                "legacy_requests": 2, "incomplete_attempts": 1, "covered_since": now - 86400},
        "week": {"requests": 3, "input_tokens": 110, "cached_input_tokens": 4, "output_tokens": 22,
                 "legacy_requests": 3, "incomplete_attempts": 1, "covered_since": now - 7 * 86400},
        "read_at": now}
    assert receipt_store.plan_token_totals("x", now=now, path=tmp_path / "absent.db")["week"]["requests"] == 0


def test_a_plan_request_is_counted_on_its_connection(plan):
    service, store, upstream, now = plan
    answering(upstream, {})
    ident, access = granted()
    before = receipt_store.plan_token_totals("chatgpt-owner")["day"]
    assert call(service, ident, access).status_code == 200
    after = receipt_store.plan_token_totals("chatgpt-owner")["day"]
    assert after["requests"] == before["requests"] + 1
    assert (after["input_tokens"] - before["input_tokens"], after["output_tokens"] - before["output_tokens"]) == (3, 2)


def test_the_owner_view_carries_token_totals_when_the_gateway_counts_them(plan):
    service, _, _, now = plan
    service.token_totals = lambda connection_id, **since: {"day": {"requests": 1}, "week": {"requests": 2}, "for": connection_id}
    assert run(service.status_one("chatgpt"))["token_totals"]["for"] == "chatgpt-owner"

    def broken(connection_id, **since):
        raise sqlite3.OperationalError("locked")

    service.token_totals = broken
    now[0] += 61
    assert "token_totals" not in run(service.status_one("chatgpt"))


def test_token_totals_are_reused_for_a_minute_then_read_again(plan):
    service, _, _, now = plan
    reads = []
    service.token_totals = lambda connection_id, **since: reads.append(connection_id) or {"day": {"requests": len(reads)}}
    for _ in range(3):
        assert run(service.status_one("chatgpt"))["token_totals"] == {"day": {"requests": 1}}
    now[0] += 61
    assert run(service.status_one("chatgpt"))["token_totals"] == {"day": {"requests": 2}}
    assert reads == ["chatgpt-owner", "chatgpt-owner"]


def test_an_unchanged_reading_is_not_written_again_within_a_minute(plan, monkeypatch):
    service, store, upstream, now = plan
    writes = []
    keep = store.record_usage
    monkeypatch.setattr(store, "record_usage", lambda kind, snapshot: writes.append(snapshot) or keep(kind, snapshot))
    ident, access = granted()
    answering(upstream, FRESH)
    for _ in range(3):
        assert call(service, ident, access).status_code == 200
    assert len(writes) == 1
    answering(upstream, plan_headers(primary_used_percent="50", primary_window_minutes="300"))
    assert call(service, ident, access).status_code == 200
    answering(upstream, FRESH)
    now[0] += 61
    assert call(service, ident, access).status_code == 200
    assert len(writes) == 3


def test_a_reading_that_cannot_be_parsed_never_fails_the_request(plan, monkeypatch):
    service, store, upstream, now = plan

    def broken(self, headers, *, now):
        raise RuntimeError("bad reading")

    monkeypatch.setattr(type(KIND), "usage_snapshot", broken)
    answering(upstream, FRESH)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    assert limits(service) is None


@pytest.mark.parametrize("models, shown", [("Opus", {"models": "Opus"}), ("", {}), ("  ", {}), (7, {}), ("x" * 41, {})])
def test_the_owner_view_names_a_windows_models_only_when_they_are_a_short_name(models, shown):
    from gateway.subscription_oauth import usage_limits_view

    stored = {"captured_at": 5, "windows": [{"used_percent": 1.0, "window_minutes": 10080, "resets_at": 9, "models": models}]}
    assert usage_limits_view(stored)["windows"] == [{"used_percent": 1.0, "window_minutes": 10080, "resets_at": 9, **shown}]


# ------------------------------------------------------------------ one count definition (finish plan A1)

# Input 150 = 100 fresh + 20 written + 30 cached; output 40 includes 10 reasoning.
RECONCILE = {"input_tokens": 150, "input_tokens_details": {"cached_tokens": 30, "cache_creation_tokens": 20},
             "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 10}, "total_tokens": 190}


def test_one_plan_usage_reconciles_on_the_receipt_chat_and_messages():
    from gateway.protocols import anthropic, responses_plan
    facts = responses_plan.usage_facts(RECONCILE)
    assert facts == {"input_tokens": 150, "cached_input_tokens": 30, "cache_write_tokens": 20,
                     "output_tokens": 40, "reasoning_tokens": 10}
    chat = responses_plan._chat_usage(RECONCILE)
    assert (chat["prompt_tokens"], chat["completion_tokens"], chat["total_tokens"]) == (150, 40, 190)
    assert chat["prompt_tokens_details"] == {"cached_tokens": 30, "cache_creation_tokens": 20}
    assert chat["completion_tokens_details"] == {"reasoning_tokens": 10}
    messages = anthropic._messages_usage(chat, include_missing_prompt=True)
    assert messages == {"input_tokens": 100, "output_tokens": 40,
                        "cache_read_input_tokens": 30, "cache_creation_input_tokens": 20}


@pytest.mark.parametrize("usage, facts", [
    ({"input_tokens": 150, "input_tokens_details": {"cached_tokens": 30}, "output_tokens": 40},
     {"input_tokens": 150, "cached_input_tokens": 30, "output_tokens": 40}),
    ({"output_tokens": 40}, {"output_tokens": 40}),
    ({"input_tokens": -1, "output_tokens": "40", "input_tokens_details": {"cache_creation_tokens": None}}, {}),
])
def test_a_count_the_upstream_did_not_report_stays_absent_never_zero(usage, facts):
    from gateway.protocols import responses_plan
    assert responses_plan.usage_facts(usage) == facts


def test_the_owner_route_attempt_keeps_cache_writes():
    from gateway.receipts import sanitize_route_metadata
    usage = {"input_tokens": 150, "cached_input_tokens": 30, "cache_write_tokens": 20, "output_tokens": 40,
             "reasoning_tokens": 10, "made_up": 5}
    out = sanitize_route_metadata({"route_attempts": [{"route_id": "plan", "outcome": "refused", "usage": usage}]},
                                  owner=True)
    assert out["route_attempts"][0]["usage"] == {key: usage[key] for key in usage if key != "made_up"}


# ------------------------------------------------------------------ A3: every sent attempt counted once, by its own connection

DAY, WEEK = 86_400, 7 * 86_400
NOW = 2_000_000


def _routed(conn, request_id, created_at, attempts, *, connection_id=None, usage=None):
    """A receipt as dispatch writes it: request usage is the winner's, attempts carry their own."""
    metadata = {"route_attempts": attempts, **({"connection_id": connection_id} if connection_id else {})}
    conn.execute("INSERT INTO receipts(request_id, created_at, receipt_json) VALUES (?, ?, ?)",
                 (request_id, created_at, json.dumps({"request_id": request_id, "usage": usage, "metadata": metadata})))


def _sent(connection_id, usage=None, outcome="sent", route_id="plan/a"):
    return {"route_id": route_id, "outcome": outcome, "connection_id": connection_id, **({"usage": usage} if usage is not None else {})}


PAID = {"route_id": "paid/a", "outcome": "committed", "usage": {"input_tokens": 500, "output_tokens": 500}}


def _db(tmp_path):
    path = tmp_path / "metrics.db"
    receipt_store.init_db(path)
    return path


def test_a_refused_plan_attempt_before_a_paid_winner_counts_on_its_own_connection(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        _routed(conn, "r1", NOW - 60, [_sent("chatgpt-owner", {"input_tokens": 9, "output_tokens": 0}), PAID],
                usage={"input_tokens": 500, "output_tokens": 500})
    day = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=path)["day"]
    assert (day["requests"], day["input_tokens"], day["output_tokens"]) == (1, 9, 0)


def test_totals_equal_the_attempt_sums_and_count_the_winner_once(tmp_path):
    path = _db(tmp_path)
    a = {"input_tokens": 9, "output_tokens": 0}
    b = {"input_tokens": 50, "cached_input_tokens": 20, "cache_write_tokens": 5, "output_tokens": 7, "reasoning_tokens": 3}
    with sqlite3.connect(path) as conn:
        # Two connections in one request: the first refuses, the second serves and its usage is the request's.
        _routed(conn, "r1", NOW - 60, [_sent("chatgpt-owner", a), _sent("second-owner", b, outcome="committed", route_id="plan/b")],
                connection_id="second-owner", usage=b)
        # The plan serves alone: its attempt and the request carry the same usage, counted once.
        _routed(conn, "r2", NOW - 120, [_sent("second-owner", b, outcome="committed", route_id="plan/b")],
                connection_id="second-owner", usage=b)
        # A skipped plan step ran nothing: no connection, nothing counted.
        _routed(conn, "r3", NOW - 180, [{"route_id": "plan/a", "outcome": "skipped"}, PAID], usage=PAID["usage"])
    first = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=path)["day"]
    second = receipt_store.plan_token_totals("second-owner", now=NOW, path=path)["day"]
    assert first == {"requests": 1, "input_tokens": 9, "cached_input_tokens": 0, "output_tokens": 0,
                     "legacy_requests": 0, "incomplete_attempts": 0, "covered_since": NOW - 180}
    assert second == {"requests": 2, "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 14,
                      "cache_write_tokens": 10, "reasoning_tokens": 6,
                      "legacy_requests": 0, "incomplete_attempts": 0, "covered_since": NOW - 180}


def test_an_attempt_without_full_usage_is_counted_as_incomplete_never_as_zero(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        _routed(conn, "r1", NOW - 60, [_sent("chatgpt-owner")], connection_id="chatgpt-owner")
        _routed(conn, "r2", NOW - 70, [_sent("chatgpt-owner", {"input_tokens": 4}, outcome="cancelled")], connection_id="chatgpt-owner")
        _routed(conn, "r3", NOW - 80, [_sent("chatgpt-owner", {"input_tokens": 4, "output_tokens": 1}, outcome="committed")],
                connection_id="chatgpt-owner", usage={"input_tokens": 4, "output_tokens": 1})
    day = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=path)["day"]
    assert (day["requests"], day["input_tokens"], day["output_tokens"], day["incomplete_attempts"]) == (3, 8, 1, 2)
    assert "cache_write_tokens" not in day and "reasoning_tokens" not in day


def test_a_receipt_from_before_attempt_attribution_counts_its_request_usage_once_and_is_marked(tmp_path):
    path = _db(tmp_path)
    old = {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2}
    with sqlite3.connect(path) as conn:
        # Written by the previous release: the attempts carry no connection.
        _routed(conn, "r1", NOW - 60, [{"route_id": "plan/a", "outcome": "committed", "usage": old}], connection_id="chatgpt-owner", usage=old)
        # Older still: no attempts at all.
        _receipt(conn, "r2", NOW - 90, "chatgpt-owner", {"input_tokens": 1, "output_tokens": 1})
        # A new receipt whose last plan step was skipped sent nothing on the plan.
        _routed(conn, "r3", NOW - 120, [{"route_id": "paid/a", "outcome": "refused"}, {"route_id": "plan/a", "outcome": "skipped"}],
                connection_id="chatgpt-owner")
        _routed(conn, "r4", NOW - 150, [_sent("chatgpt-owner", {"input_tokens": 3, "output_tokens": 2}, outcome="committed")],
                connection_id="chatgpt-owner", usage={"input_tokens": 3, "output_tokens": 2})
    day = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=path)["day"]
    assert (day["requests"], day["legacy_requests"]) == (3, 2)
    assert (day["input_tokens"], day["cached_input_tokens"], day["output_tokens"]) == (14, 4, 5)


def test_the_windows_include_their_edges_and_exclude_the_future(tmp_path):
    path = _db(tmp_path)
    one = {"input_tokens": 1, "output_tokens": 1}
    with sqlite3.connect(path) as conn:
        for request_id, created_at in (("edge-day", NOW - DAY), ("edge-now", NOW), ("future", NOW + 60),
                                       ("edge-week", NOW - WEEK), ("before-week", NOW - WEEK - 1), ("past-day", NOW - DAY - 1)):
            _routed(conn, request_id, created_at, [_sent("chatgpt-owner", one, outcome="committed")], connection_id="chatgpt-owner", usage=one)
    totals = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=path)
    assert totals["day"]["requests"] == 2
    assert totals["week"]["requests"] == 4
    assert totals["read_at"] == NOW


def test_coverage_starts_at_the_oldest_kept_receipt_when_history_was_pruned(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        _routed(conn, "kept", NOW - 2 * DAY, [PAID], usage=PAID["usage"])
    totals = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=path)
    assert totals["day"]["covered_since"] == NOW - DAY
    assert totals["week"]["covered_since"] == NOW - 2 * DAY


def test_no_receipt_file_or_no_receipts_means_not_counted_not_zero(tmp_path):
    absent = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=tmp_path / "absent.db")
    empty = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=_db(tmp_path))
    for totals in (absent, empty):
        assert totals["day"]["covered_since"] is None and totals["week"]["covered_since"] is None
        assert totals["day"]["requests"] == 0


def test_the_owner_view_keeps_the_read_time_of_reused_totals(plan):
    service, _, _, now = plan
    service.token_totals = lambda connection_id, **since: {"day": {"requests": 1}, "week": {"requests": 1}, "read_at": int(now[0])}
    first = run(service.status_one("chatgpt"))["token_totals"]
    now[0] += 30
    assert run(service.status_one("chatgpt"))["token_totals"]["read_at"] == first["read_at"]


# ------------------------------------------------------------------ A5: a late reading never replaces a newer one

def _window(percent):
    return [{"used_percent": percent, "window_minutes": 300, "resets_at": None}]


def _stored(store):
    return store.connection("chatgpt")["usage"]


def _reconnect(store):
    store.save_connection(kind="chatgpt", connected_by="owner_1", client_id="c", host_id="h", tokens=None,
                          origin=KIND.upstream_origin, scopes=[], email=None, subject=None, session=None)


def test_a_reading_from_the_plan_call_names_the_sign_in_it_came_from(plan):
    service, store, upstream, now = plan
    answering(upstream, FRESH)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    assert _stored(store)["generation"] == store.connection("chatgpt")["generation"]


def test_a_delayed_reading_from_an_earlier_sign_in_cannot_overwrite_the_current_one(plan):
    service, store, upstream, now = plan
    first = store.connection("chatgpt")["generation"]
    _reconnect(store)
    second = store.connection("chatgpt")["generation"]
    assert second != first
    store.record_usage("chatgpt", {"captured_at": 20, "generation": second, "windows": _window(5.0)})
    store.record_usage("chatgpt", {"captured_at": 30, "generation": first, "windows": _window(99.0)})
    assert (_stored(store)["generation"], _stored(store)["windows"]) == (second, _window(5.0))


def test_an_earlier_observation_cannot_replace_a_later_one(plan):
    service, store, upstream, now = plan
    generation = store.connection("chatgpt")["generation"]
    store.record_usage("chatgpt", {"captured_at": 30, "generation": generation, "windows": _window(7.0)})
    store.record_usage("chatgpt", {"captured_at": 20, "generation": generation, "windows": _window(3.0)})
    assert _stored(store)["captured_at"] == 30
    store.record_usage("chatgpt", {"captured_at": 30, "generation": generation, "windows": _window(8.0)})
    assert _stored(store)["windows"] == _window(8.0)


def test_readings_finishing_out_of_order_keep_the_later_observation(plan):
    service, store, upstream, now = plan
    generation = store.connection("chatgpt")["generation"]
    late = {"captured_at": 40, "generation": generation, "windows": _window(9.0)}
    early = {"captured_at": 35, "generation": generation, "windows": _window(4.0)}
    run(service.note_usage_snapshot("chatgpt", late))
    run(service.note_usage_snapshot("chatgpt", early))
    assert _stored(store)["windows"] == _window(9.0)


def test_a_failed_write_is_tried_again_by_the_next_identical_reading(plan, monkeypatch):
    service, store, upstream, now = plan
    keep = store.record_usage
    failures = [sqlite3.OperationalError("database is locked")]

    def flaky(kind, snapshot):
        if failures:
            raise failures.pop()
        keep(kind, snapshot)

    monkeypatch.setattr(store, "record_usage", flaky)
    answering(upstream, FRESH)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    assert _stored(store) is None
    assert call(service, ident, access).status_code == 200
    assert _stored(store) is not None


def test_a_reading_kept_before_sign_in_stamps_is_replaced_and_still_read(plan):
    service, store, upstream, now = plan
    with sqlite3.connect(store._path()) as conn:
        conn.execute("UPDATE subscription_connections SET usage_json = ?",
                     (json.dumps({"captured_at": 50, "windows": _window(2.0)}),))
    assert _stored(store)["windows"] == _window(2.0)
    generation = store.connection("chatgpt")["generation"]
    store.record_usage("chatgpt", {"captured_at": 60, "generation": generation, "windows": _window(6.0)})
    assert _stored(store)["windows"] == _window(6.0)
    with sqlite3.connect(store._path()) as conn:
        conn.execute("UPDATE subscription_connections SET usage_json = 'not json'")
    store.record_usage("chatgpt", {"captured_at": 70, "generation": generation, "windows": _window(1.0)})
    assert _stored(store)["windows"] == _window(1.0)


# ------------------------------------------------------------------ A3b: totals since this sign-in

def _gen(connection_id, generation, usage, outcome="committed"):
    return {**_sent(connection_id, usage, outcome=outcome), "generation": generation}


def test_since_this_sign_in_counts_only_attempts_of_the_current_generation(tmp_path):
    path = _db(tmp_path)
    signed_in = NOW - 3600
    with sqlite3.connect(path) as conn:
        _routed(conn, "current", NOW - 60, [_gen("chatgpt-owner", 3, {"input_tokens": 5, "output_tokens": 1})],
                connection_id="chatgpt-owner", usage={"input_tokens": 5, "output_tokens": 1})
        # A request of the earlier sign-in that finished after the reconnect.
        _routed(conn, "late-earlier", NOW - 120, [_gen("chatgpt-owner", 1, {"input_tokens": 100, "output_tokens": 100})],
                connection_id="chatgpt-owner", usage={"input_tokens": 100, "output_tokens": 100})
        _routed(conn, "before", NOW - 7200, [_gen("chatgpt-owner", 1, {"input_tokens": 100, "output_tokens": 100})],
                connection_id="chatgpt-owner", usage={"input_tokens": 100, "output_tokens": 100})
        # Counts with no sign-in stamp cannot be given to the current sign-in.
        _routed(conn, "unstamped", NOW - 90, [_sent("chatgpt-owner", {"input_tokens": 7, "output_tokens": 7}, outcome="committed")],
                connection_id="chatgpt-owner", usage={"input_tokens": 7, "output_tokens": 7})
        _receipt(conn, "legacy", NOW - 100, "chatgpt-owner", {"input_tokens": 9, "output_tokens": 9})
    totals = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=path, generation=3, signed_in_at=signed_in)
    expected = {"requests": 1, "input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1,
                "incomplete_attempts": 0, "unattributed_requests": 2, "covered_since": signed_in}
    assert totals["sign_in"] == {"generation": 3, "since": signed_in, "day": expected, "week": expected}
    assert totals["day"]["requests"] == 5 and totals["day"]["input_tokens"] == 221
    assert "sign_in" not in receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=path)


def test_since_this_sign_in_coverage_starts_at_the_sign_in_or_the_kept_receipts(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        _routed(conn, "kept", NOW - 2 * DAY, [PAID], usage=PAID["usage"])
    long_ago = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=path, generation=1, signed_in_at=NOW - 30 * DAY)
    assert (long_ago["sign_in"]["day"]["covered_since"], long_ago["sign_in"]["week"]["covered_since"]) == (NOW - DAY, NOW - 2 * DAY)
    absent = receipt_store.plan_token_totals("chatgpt-owner", now=NOW, path=tmp_path / "absent.db", generation=1, signed_in_at=NOW - 60)
    assert absent["sign_in"]["day"]["covered_since"] is None and absent["sign_in"]["day"]["requests"] == 0


def test_a_plan_attempt_names_the_sign_in_it_ran_on_through_a_token_refresh(plan):
    service, store, upstream, now = plan
    generation = store.connection("chatgpt")["generation"]
    answers = [httpx.Response(401, json={"error": {"code": "token_expired"}})]

    def respond(request):
        if answers:
            return answers.pop()
        return httpx.Response(200, content=sse({"type": "response.created", "response": {}}, COMPLETED),
                              headers={"content-type": "text/event-stream"})

    upstream.routes[("POST", RESPONSES_PATH)] = respond
    ident, access = granted()

    def counted(gen):
        return receipt_store.plan_token_totals("chatgpt-owner", generation=gen, signed_in_at=0)["sign_in"]["day"]["requests"]

    before, other = counted(generation), counted(generation + 99)
    assert call(service, ident, access).status_code == 200
    assert not answers and store.connection("chatgpt")["generation"] == generation
    assert (counted(generation) - before, counted(generation + 99) - other) == (1, 0)


def test_the_owner_view_asks_for_the_current_sign_in_and_rereads_after_a_reconnect(plan):
    service, store, upstream, now = plan
    asked = []
    service.token_totals = lambda connection_id, **since: asked.append(since) or {"day": {}, "week": {}}
    run(service.status_one("chatgpt"))
    row = store.connection("chatgpt")
    assert asked == [{"generation": row["generation"], "signed_in_at": row["connected_at"]}]
    _reconnect(store)
    run(service.status_one("chatgpt"))
    assert len(asked) == 2 and asked[1]["generation"] == store.connection("chatgpt")["generation"] != row["generation"]


# ------------------------------------------------------------------ plan-case gaps (lead round n)

def test_a_failed_receipt_read_shows_no_totals_and_the_next_view_reads_again(plan):
    service, _, _, now = plan
    reads = []

    def flaky(connection_id, **since):
        reads.append(connection_id)
        if len(reads) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"day": {"requests": 4}, "week": {"requests": 4}}

    service.token_totals = flaky
    assert "token_totals" not in run(service.status_one("chatgpt"))
    # The clock has not moved: a failed read is not reused as if it were a result.
    assert run(service.status_one("chatgpt"))["token_totals"]["day"] == {"requests": 4}
    assert len(reads) == 2


def test_the_same_reading_delivered_twice_writes_once_and_leaves_the_row_unchanged(plan, monkeypatch):
    service, store, upstream, now = plan
    generation = store.connection("chatgpt")["generation"]
    writes = []
    keep = store.record_usage
    monkeypatch.setattr(store, "record_usage", lambda kind, snapshot: writes.append(snapshot) or keep(kind, snapshot))
    reading = {"captured_at": 45, "windows": _window(12.0)}
    run(service.note_usage_snapshot("chatgpt", reading, generation=generation))
    first = _stored(store)
    run(service.note_usage_snapshot("chatgpt", reading, generation=generation))
    assert len(writes) == 1
    assert _stored(store) == first == {**reading, "generation": generation}


def test_a_reading_from_the_previous_account_cannot_overwrite_the_new_accounts(plan):
    service, store, upstream, now = plan
    before = store.connection("chatgpt")
    store.save_connection(kind="chatgpt", connected_by="owner_1", client_id="c", host_id="h", tokens=None,
                          origin=KIND.upstream_origin, scopes=[], email="other@example.test", subject="acct-2", session=None)
    after = store.connection("chatgpt")
    assert after["account_subject"] == "acct-2" != before["account_subject"]
    run(service.note_usage_snapshot("chatgpt", {"captured_at": 20, "windows": _window(5.0)}, generation=after["generation"]))
    # A plan call of the previous account finished late: its reading is newer by its own clock.
    run(service.note_usage_snapshot("chatgpt", {"captured_at": 30, "windows": _window(99.0)}, generation=before["generation"]))
    assert _stored(store) == {"captured_at": 20, "windows": _window(5.0), "generation": after["generation"]}


def test_a_later_reading_whose_write_is_held_still_wins_over_an_earlier_one_written_first(plan, monkeypatch):
    import threading

    service, store, upstream, now = plan
    generation = store.connection("chatgpt")["generation"]
    late = {"captured_at": 40, "windows": _window(9.0)}
    early = {"captured_at": 35, "windows": _window(4.0)}
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    keep = store.record_usage

    def held(kind, snapshot):
        if snapshot["captured_at"] == late["captured_at"]:
            entered.set()
            assert release.wait(5)
            keep(kind, snapshot)
            finished.set()
            return
        keep(kind, snapshot)

    monkeypatch.setattr(store, "record_usage", held)

    async def interleave():
        first = asyncio.ensure_future(service.note_usage_snapshot("chatgpt", late, generation=generation))
        assert await asyncio.to_thread(entered.wait, 5)  # reading 1 is inside its write, held
        await service.note_usage_snapshot("chatgpt", early, generation=generation)
        written_first = _stored(store)
        release.set()
        await first
        assert await asyncio.to_thread(finished.wait, 5)
        return written_first

    assert run(interleave())["captured_at"] == early["captured_at"]
    assert _stored(store) == {**late, "generation": generation}


def test_an_attempt_that_finishes_after_a_reconnect_counts_for_the_sign_in_it_started_on(plan):
    service, store, upstream, now = plan
    started_on = store.connection("chatgpt")["generation"]

    def respond(request):
        _reconnect(store)  # the owner signs in again while this plan call is in flight
        return httpx.Response(200, content=sse({"type": "response.created", "response": {}}, COMPLETED),
                              headers={"content-type": "text/event-stream"})

    upstream.routes[("POST", RESPONSES_PATH)] = respond
    ident, access = granted()

    def counted(**since):
        return receipt_store.plan_token_totals("chatgpt-owner", **since)

    all_time = counted()["day"]["requests"]
    old = counted(generation=started_on, signed_in_at=0)["sign_in"]["day"]["requests"]
    assert call(service, ident, access).status_code == 200
    current = store.connection("chatgpt")["generation"]
    assert current != started_on
    assert counted()["day"]["requests"] - all_time == 1
    assert counted(generation=started_on, signed_in_at=0)["sign_in"]["day"]["requests"] - old == 1
    assert counted(generation=current, signed_in_at=0)["sign_in"]["day"]["requests"] == 0
