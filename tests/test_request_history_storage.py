"""Request history storage: member rows and erasure cutoffs are keyed by group, and the one writer checks them.

docs/design/REQUEST_RETENTION.md sections 3.2, 3.6, 12 ("Migration: no silent change", 16.1.2) and 3.3 step 5.
The old table layouts below are the ones schema 16 creates on main; the test rebuilds them so the upgrade path runs
exactly as it would for an installed database.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from gateway import admin_store, retention

OLD_LAYOUT = """
CREATE TABLE prompt_memory_preferences (
  user_id TEXT PRIMARY KEY,
  remember INTEGER NOT NULL,
  retention TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  updated_by TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE prompt_memory_erasure_cutoffs (person_id TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL);
CREATE TABLE prompt_memory_erasure_intents (person_id TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL);
"""


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _downgrade_to_schema_16(db: Path, rows: dict) -> None:
    """Put a fresh database back at schema 16, with the old layouts and the given rows."""
    conn = _connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    for table in ("prompt_memory_preferences", "prompt_memory_erasure_cutoffs", "prompt_memory_erasure_intents"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(OLD_LAYOUT)
    for user_id, remember, period in rows["members"]:
        conn.execute("INSERT INTO prompt_memory_preferences VALUES (?,?,?,?,NULL)", (user_id, remember, period, 1000))
    for person_id, cutoff in rows["cutoffs"]:
        conn.execute("INSERT INTO prompt_memory_erasure_cutoffs VALUES (?,?)", (person_id, cutoff))
    for person_id, cutoff in rows["intents"]:
        conn.execute("INSERT INTO prompt_memory_erasure_intents VALUES (?,?)", (person_id, cutoff))
    conn.execute("UPDATE schema_version SET version = 16")
    conn.commit()
    conn.close()


@pytest.fixture
def admin_db(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    return db


def _person(db: Path, subject: str) -> str:
    return admin_store.upsert_oidc_user("https://idp.example", subject, email=f"{subject}@example.test", path=db)["id"]


def test_upgrade_moves_member_rows_and_cutoffs_to_the_prompts_group_and_adds_request_history_off(admin_db):
    alice, bob = _person(admin_db, "alice"), _person(admin_db, "bob")
    _downgrade_to_schema_16(admin_db, {
        "members": [(alice, 1, "never"), (bob, 0, "3_months")],
        "cutoffs": [(alice, 100), ("*", 200)],
        "intents": [(bob, 150)],
    })
    admin_store.init_db(admin_db)  # the upgrade runs here

    conn = _connect(admin_db)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 17
    prompts = conn.execute(
        "SELECT user_id, remember, retention FROM prompt_memory_preferences WHERE grp='prompts' ORDER BY user_id"
    ).fetchall()
    assert [tuple(row) for row in prompts] == sorted([(alice, 1, "never"), (bob, 0, "3_months")])
    history = conn.execute(
        "SELECT user_id, remember, retention FROM prompt_memory_preferences WHERE grp='request_history' ORDER BY user_id"
    ).fetchall()
    # 12.3: existing members see nothing new kept, whatever the household default is.
    assert [tuple(row) for row in history] == sorted([(alice, 0, "7_days"), (bob, 0, "7_days")])
    cutoffs = conn.execute("SELECT person_id, grp, cutoff_at FROM prompt_memory_erasure_cutoffs").fetchall()
    assert sorted(tuple(row) for row in cutoffs) == sorted([(alice, "prompts", 100), ("*", "prompts", 200)])
    intents = conn.execute("SELECT person_id, grp, cutoff_at FROM prompt_memory_erasure_intents").fetchall()
    assert [tuple(row) for row in intents] == [(bob, "prompts", 150)]
    conn.close()


def test_running_the_upgrade_twice_changes_nothing(admin_db):
    alice = _person(admin_db, "alice")
    _downgrade_to_schema_16(admin_db, {"members": [(alice, 1, "never")], "cutoffs": [], "intents": []})
    admin_store.init_db(admin_db)
    first = _snapshot(admin_db)
    admin_store.init_db(admin_db)
    assert _snapshot(admin_db) == first


def _snapshot(db: Path) -> list[tuple]:
    conn = _connect(db)
    rows = [tuple(row) for row in conn.execute(
        "SELECT user_id, grp, remember, retention FROM prompt_memory_preferences ORDER BY user_id, grp")]
    conn.close()
    return rows


def test_a_person_created_after_the_upgrade_gets_request_history_on_by_default(admin_db):
    bob = _person(admin_db, "bob-new")
    conn = _connect(admin_db)
    row = conn.execute(
        "SELECT remember, retention FROM prompt_memory_preferences WHERE user_id=? AND grp='request_history'", (bob,)
    ).fetchone()
    conn.close()
    assert tuple(row) == (1, "7_days")


def _set_household_kill_switch(db: Path) -> None:
    conn = _connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO operator_settings(key, value_json, updated_at, updated_by) VALUES (?, ?, 0, NULL)",
        (retention.SETTING_KEY, '{"never_keep": true}'),
    )
    conn.commit()
    conn.close()


def _write(db: Path, person_id: str, *, now: int) -> tuple[bool, list, list]:
    decision = retention.decide(retention.REQUEST_HISTORY, person_id=person_id, member=None,
                                household_policy=None, request_body=None, now=now)
    calls: list = []
    outcomes: list = []
    kept = retention.write(retention.REQUEST_HISTORY, admin_path=db, person_id=person_id, created_at=now,
                           decision=decision, commit=lambda deadline: calls.append(deadline),
                           on_result=outcomes.append)
    return kept, calls, outcomes


def test_the_one_writer_refuses_request_history_under_the_household_kill_switch(admin_db):
    bob = _person(admin_db, "bob-kill")
    _set_household_kill_switch(admin_db)
    kept, calls, outcomes = _write(admin_db, bob, now=int(time.time()))
    assert kept is False
    assert calls == []
    assert outcomes[0].reason == "server_disabled"


def test_the_one_writer_refuses_request_history_when_the_member_has_switched_it_off(admin_db):
    bob = _person(admin_db, "bob-off")
    conn = _connect(admin_db)
    conn.execute("UPDATE prompt_memory_preferences SET remember=0 WHERE user_id=? AND grp='request_history'", (bob,))
    conn.commit()
    conn.close()
    kept, calls, outcomes = _write(admin_db, bob, now=int(time.time()))
    assert kept is False and calls == []
    assert outcomes[0].reason == "member_disabled"


def test_the_one_writer_keeps_request_history_for_the_default_period_and_runs_the_commit_once(admin_db):
    bob = _person(admin_db, "bob-on")
    now = int(time.time())
    kept, calls, outcomes = _write(admin_db, bob, now=now)
    assert kept is True
    assert calls == [now + 7 * 86400]
    assert outcomes[0].reason == "kept"
    assert outcomes[0].kept_until == now + 7 * 86400
    assert outcomes[0].limited_by == "member_choice"
