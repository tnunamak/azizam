"""Request history for a shared (service) App: the App's own setting, kept under its manager.

The App's setting (``history_keep``, ``history_period``) is off by default. When it is on, a request the App sends is
kept with ``person_id`` = the App's manager, so the manager's Request history page, erase, byte cap and maintenance
apply unchanged. The manager's own member switch does not gate it. The order is the household kill switch, the
request's own forget, then the App setting. Library prompts and conversations for a service App are not changed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from gateway import accounts, admin_store, request_history, retention
from gateway.request_history_capture import RequestHistoryMiddleware, _kept_record
from tests.support.access import service_app, store_plan

NOW = 1_700_000_000
DAY = 86_400


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    plan = store_plan("Manager plan", path=path, models=["test-model"])
    manager = admin_store.upsert_oidc_user("https://fixture.test", "manager-sub", email="manager@example.test", path=path)
    accounts.change_person(manager["id"], "owner", plan_id=plan["id"], path=path)
    app = service_app("Home voice", path=path, owner_user_id=manager["id"], plan_id=plan["id"])
    return SimpleNamespace(path=path, manager=manager["id"], app=app["id"], plan=plan["id"])


def _choice(db):
    return retention.app_history_choice(db.path, db.app)


def _turn_on(db, period="7_days"):
    admin_store.update_app(db.app, history_keep=True, history_period=period, path=db.path)


def _decide(db, **overrides):
    options = {"app_history": _choice(db), "now": NOW, "household_policy": None, "request_body": None}
    options.update(overrides)
    return retention.decide(retention.REQUEST_HISTORY, person_id=None, member=None, **options)


# ------------------------------------------------------------------ storage


def test_a_new_service_app_keeps_nothing_until_its_setting_is_on(db):
    row = admin_store.get_app(db.app, path=db.path)
    assert (row["history_keep"], row["history_period"]) == (0, "7_days")
    decision = _decide(db)
    assert (decision.keep, decision.reason, decision.person_id) == (False, "app_off", None)


def test_a_database_at_schema_20_gets_the_columns_with_history_off(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE apps DROP COLUMN history_keep")
    conn.execute("ALTER TABLE apps DROP COLUMN history_period")
    conn.execute("UPDATE schema_version SET version = 20")
    conn.commit()
    conn.close()

    admin_store.init_db(path)

    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(apps)")}
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    assert {"history_keep", "history_period"} <= columns
    assert version == admin_store.SCHEMA_VERSION
    admin_store.create_app("Old app", kind="service", owner_user_id=admin_store.upsert_oidc_user(
        "https://fixture.test", "later", path=path)["id"], access_plan="person", path=path)
    assert all(row["history_keep"] == 0 for row in admin_store.list_apps(path=path))


def test_the_store_refuses_a_period_the_request_history_group_does_not_offer(db):
    with pytest.raises(ValueError):
        admin_store.update_app(db.app, history_keep=True, history_period="1_year", path=db.path)
    assert admin_store.get_app(db.app, path=db.path)["history_keep"] == 0


# ------------------------------------------------------------- the decision


def test_a_service_app_with_its_setting_on_keeps_under_its_manager(db):
    _turn_on(db)
    decision = _decide(db)
    assert (decision.keep, decision.reason, decision.person_id) == (True, "remembered", db.manager)
    assert decision.kept_until == NOW + 7 * DAY
    assert decision.app_id == db.app


def test_the_household_cap_shortens_the_apps_period(db):
    _turn_on(db, period="30_days")
    decision = _decide(db, household_policy={"retention_cap": "1_day"})
    assert decision.kept_until == NOW + DAY


def test_the_app_period_shortens_a_longer_household_cap(db):
    _turn_on(db, period="1_day")
    decision = _decide(db, household_policy={"retention_cap": "30_days"})
    assert decision.kept_until == NOW + DAY


def test_the_household_kill_switch_wins_over_the_app_setting(db):
    _turn_on(db)
    decision = _decide(db, household_policy={"never_keep": True})
    assert (decision.keep, decision.reason) == (False, "server_disabled")


@pytest.mark.parametrize("request_forget, body, store", [
    (True, None, None),
    (False, {"extensions": {retention.NAMESPACE: {"mode": "forget"}}}, None),
    (False, None, False),
])
def test_a_request_forget_wins_over_the_app_setting(db, request_forget, body, store):
    _turn_on(db)
    decision = _decide(db, request_forget=request_forget, request_body=body, responses_store=store)
    assert (decision.keep, decision.reason) == (False, "request_forget")


def test_a_disabled_app_keeps_nothing(db):
    _turn_on(db)
    admin_store.disable_app(db.app, path=db.path)
    decision = _decide(db)
    assert (decision.keep, decision.reason, decision.person_id) == (False, "app_disabled", None)


def test_a_disabled_manager_keeps_nothing_for_the_app(db):
    _turn_on(db)
    accounts.change_person(db.manager, "owner", remove=True, path=db.path)
    choice = _choice(db)
    assert choice["active"] is False
    decision = _decide(db, app_history=choice)
    assert (decision.keep, decision.reason) == (False, "app_disabled")


def test_library_prompts_and_conversations_for_a_service_app_are_unchanged(db):
    _turn_on(db)
    for kind in (retention.LIBRARY_PROMPT, retention.CONVERSATION):
        decision = retention.decide(kind, person_id=None, member=None, household_policy=None, request_body=None,
                                    now=NOW, app_history=_choice(db))
        assert (decision.keep, decision.reason, decision.person_id) == (False, "no_person", None)


def test_a_personal_app_has_no_app_setting_to_read(db):
    personal = admin_store.create_app("Mine", kind="personal", owner_user_id=db.manager, access_plan="person",
                                      path=db.path)
    assert retention.app_history_choice(db.path, personal["id"]) is None
    assert retention.app_history_choice(db.path, "missing") is None


def test_the_capture_decision_reads_the_app_only_for_a_service_key(db):
    middleware = RequestHistoryMiddleware(None, admin_path=lambda: db.path, media_path=lambda: None)
    service = SimpleNamespace(person_id=None, app_id=db.app, auth_kind="service_key")
    assert middleware.decide(service, forget=False, now=NOW).reason == "app_off"
    _turn_on(db)
    decision = middleware.decide(service, forget=False, now=NOW)
    assert (decision.keep, decision.person_id) == (True, db.manager)

    personal = admin_store.create_app("Mine", kind="personal", owner_user_id=db.manager, access_plan="person",
                                      path=db.path)
    keyed = SimpleNamespace(person_id=None, app_id=personal["id"], auth_kind="app_key")
    assert middleware.decide(keyed, forget=False, now=NOW).reason == "no_person"


# -------------------------------------------------------- the write fence


def _write(db, decision):
    committed: list = []

    def commit(values, prompts):
        committed.append(values)

    history, _ = retention.write_media_capture(
        admin_path=db.path, person_id=decision.person_id, created_at=decision.created_at or NOW,
        decision=decision, prompts=[],
        commit=commit,
    )
    return history, committed


def test_the_managers_own_switch_does_not_gate_the_apps_history(db):
    _turn_on(db)
    # Turning the switch off erases the manager's history up to now, so the switch is set before the request.
    request_history.set_member_choice(db.manager, keep=False, retention_id="7_days",
                                      parts=["accepted", "received"], admin_path=db.path, now=NOW - 100)
    member = retention.member_choice(db.path, db.manager, retention.GROUP_REQUEST_HISTORY)
    personal = retention.decide(retention.REQUEST_HISTORY, person_id=db.manager, member=member,
                                household_policy=None, request_body=None, now=NOW)
    assert personal.reason == "member_disabled"

    decision = _decide(db, now=int(time.time()))
    outcome, committed = _write(db, decision)
    assert (outcome.kept, outcome.reason) == (True, "kept")
    assert len(committed) == 1 and committed[0][0] == decision.kept_until


def test_turning_the_app_setting_off_after_the_decision_refuses_the_write(db):
    _turn_on(db)
    decision = _decide(db, now=int(time.time()))
    admin_store.update_app(db.app, history_keep=False, path=db.path)
    outcome, committed = _write(db, decision)
    assert (outcome.kept, outcome.reason) == (False, "app_off")
    assert committed == []


def test_disabling_the_app_after_the_decision_refuses_the_write(db):
    _turn_on(db)
    decision = _decide(db, now=int(time.time()))
    admin_store.disable_app(db.app, path=db.path)
    outcome, committed = _write(db, decision)
    assert (outcome.kept, outcome.reason) == (False, "app_disabled")
    assert committed == []


def test_removing_the_manager_after_the_decision_refuses_the_write(db):
    _turn_on(db)
    decision = _decide(db, now=int(time.time()))
    accounts.change_person(db.manager, "owner", remove=True, path=db.path)
    outcome, committed = _write(db, decision)
    assert (outcome.kept, outcome.reason) == (False, "app_disabled")
    assert committed == []


def test_a_receipt_can_say_why_a_shared_apps_history_was_not_kept():
    from gateway import receipts

    for reason in ("app_off", "app_disabled"):
        entry = {"kind": "request_history", "decision": "not_kept", "reason": reason}
        assert receipts.validate_retention([entry]) == [entry]


# ------------------------------------------- the manager, re-read under the lock


def test_an_app_whose_manager_changed_after_the_decision_refuses_and_commits_nothing(db):
    _turn_on(db)
    decision = _decide(db, now=int(time.time()))
    successor = admin_store.upsert_oidc_user("https://fixture.test", "successor-sub", email="successor@example.test",
                                             path=db.path)
    conn = sqlite3.connect(db.path)
    conn.execute("UPDATE apps SET owner_user_id=? WHERE id=?", (successor["id"], db.app))
    conn.commit()
    conn.close()

    outcome, committed = _write(db, decision)

    assert (outcome.kept, outcome.reason) == (False, "app_disabled")
    assert committed == []


# ---------------------------- what changes between the decision and the write


def _household(db, document):
    admin_store.put_operator_setting("retention", document, path=db.path)


def _set_cutoff(db, person_id, cutoff_at):
    conn = sqlite3.connect(db.path)
    retention.ensure_erasure_cutoff_table(conn)
    conn.execute("INSERT INTO prompt_memory_erasure_cutoffs(person_id, grp, cutoff_at) VALUES (?, ?, ?)",
                 (person_id, "request_history", cutoff_at))
    conn.commit()
    conn.close()


def _write_direct(db, decision):
    committed: list = []
    kept = retention.write(
        retention.REQUEST_HISTORY, admin_path=db.path, person_id=decision.person_id,
        created_at=decision.created_at or NOW, decision=decision,
        commit=lambda deadline, parts: committed.append((deadline, parts)),
    )
    return kept, committed


def test_the_managers_erasure_cutoff_after_the_decision_refuses_the_app_history(db):
    _turn_on(db)
    now = int(time.time())
    decision = _decide(db, now=now)
    _set_cutoff(db, db.manager, now + 1)

    outcome, committed = _write(db, decision)

    assert (outcome.kept, outcome.reason) == (False, "erasure_cutoff")
    assert committed == []


def test_the_kill_switch_turned_on_after_the_decision_refuses_the_write(db):
    _turn_on(db)
    decision = _decide(db, now=int(time.time()))
    _household(db, {"never_keep": True})

    outcome, committed = _write(db, decision)

    assert (outcome.kept, outcome.reason) == (False, "server_disabled")
    assert committed == []


def test_the_household_cap_shortened_after_the_decision_shortens_the_deadline(db):
    _turn_on(db, period="30_days")
    now = int(time.time())
    decision = _decide(db, now=now)
    assert decision.kept_until == now + 30 * DAY
    _household(db, {"groups": {"request_history": {"retention_cap": "1_day"}}})

    kept, committed = _write_direct(db, decision)

    assert kept is True
    assert committed == [(now + DAY, ("accepted", "received"))]


def test_the_app_period_shortened_after_the_decision_shortens_the_deadline(db):
    _turn_on(db, period="30_days")
    now = int(time.time())
    decision = _decide(db, now=now)
    admin_store.update_app(db.app, history_period="1_day", path=db.path)

    kept, committed = _write_direct(db, decision)

    assert kept is True
    assert committed == [(now + DAY, ("accepted", "received"))]


def test_the_manager_disabled_after_the_decision_refuses_the_app_history(db):
    _turn_on(db)
    decision = _decide(db, now=int(time.time()))
    conn = sqlite3.connect(db.path)
    changed = conn.execute("UPDATE account_access SET disabled_at=? WHERE user_id=?",
                           (int(time.time()), db.manager)).rowcount
    conn.commit()
    conn.close()
    assert changed == 1

    outcome, committed = _write(db, decision)

    assert (outcome.kept, outcome.reason) == (False, "app_disabled")
    assert committed == []


def test_the_managers_own_parts_choice_does_not_narrow_the_apps_history(db):
    _turn_on(db)
    request_history.set_member_choice(db.manager, keep=True, retention_id="7_days", parts=["accepted"],
                                      admin_path=db.path, now=NOW - 100)
    decision = _decide(db, now=int(time.time()))

    kept, committed = _write_direct(db, decision)

    assert kept is True
    assert committed[0][1] == ("accepted", "received")


def test_a_direct_write_keeps_the_app_history_under_the_manager_for_the_deadline(db):
    _turn_on(db, period="7_days")
    now = int(time.time())
    decision = _decide(db, now=now)
    media_path = db.path.parent / "media.db"
    record_id = "ahr-direct-write"
    committed: list = []

    def persist(deadline, parts):
        # The middleware's own path: the record is shaped by _kept_record and inserted in the write transaction.
        committed.append((deadline, parts))
        record = _kept_record({
            "record_id": record_id, "request_id": None, "person_id": decision.person_id, "app_id": db.app,
            "origin_kind": "service_key", "origin_id": db.app, "created_at": now, "policy_version": "test",
            "parts": {"accepted": {"body": {"model": "test-model"}}, "received": {"body": {"ok": True}}},
            "omissions_by_part": {},
        }, parts, deadline)
        with request_history.write_transaction(media_path) as conn:
            request_history.insert_record(conn, record)

    kept = retention.write(
        retention.REQUEST_HISTORY, admin_path=db.path, person_id=decision.person_id,
        created_at=decision.created_at or NOW, decision=decision, commit=persist,
    )

    assert decision.person_id == db.manager
    assert kept is True
    assert committed == [(now + 7 * DAY, ("accepted", "received"))]
    stored = request_history.get_row(record_id, path=media_path)
    assert stored["person_id"] == db.manager
    assert json.loads(stored["parts_json"]).keys() == {"accepted", "received"}
    assert stored["kept_until"] == now + 7 * DAY
