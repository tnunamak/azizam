"""Owner visibility step 2: an existing database runs in pre-cut-over mode until its cut-over is applied.

Before the cut-over, a restart does not refuse to start and says the cut-over is pending. The startup
back-fill still records the creator that the audit log proves, so live keys keep their person. After
the cut-over, the guards are installed, the ownership version is recorded, and no restart runs the
back-fill again. A fresh database starts after the cut-over (design §8.1, docs/ops/OWNER_BINDINGS.md).
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from gateway import accounts, admin_store, app_binding_guard
from tests.support.app_keys import before_the_cut_over

PENDING = "cut-over is pending"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def _person(path: Path, email: str) -> dict:
    return admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email=email,
                                        display_name=email, path=path)


def _service_app(path: Path, owner: dict) -> str:
    return admin_store.create_app("Desk", kind="service", owner_user_id=owner["id"],
                                  access_plan="person", path=path)["id"]


def _legacy_key(path: Path, app_id: str, *, created_by_audit: str | None) -> str:
    """A key written before provenance existed: no creator, and an audit row naming whoever made it."""
    key_id = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app_id, app_id=app_id, hash=f"sha256:{uuid.uuid4().hex}",
        prefix="opk_legacy", created_by=None, path=path,
    )["id"]
    if created_by_audit is not None:
        admin_store.record_audit("credential.create", actor_kind="user", actor_id=created_by_audit,
                                 target_kind="credential", target_id=key_id, path=path)
    return key_id


def _creators(path: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT id, created_by FROM credentials WHERE subject_kind = 'app'").fetchall())
    finally:
        conn.close()


def test_a_pending_cut_over_does_not_stop_a_restart_and_says_how_to_preview_it(db, capsys):
    before_the_cut_over(db)
    capsys.readouterr()

    admin_store.init_db(db)  # must start

    lines = [line for line in capsys.readouterr().out.splitlines() if PENDING in line]
    assert len(lines) == 1, lines
    assert "gateway.app_binding_preview" in lines[0]


def test_a_fresh_database_never_says_the_cut_over_is_pending(tmp_path, capsys):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.init_db(path)

    assert PENDING not in capsys.readouterr().out


def test_before_the_cut_over_a_restart_records_only_the_creators_the_audit_log_proves(db):
    owner = _person(db, "owner@example.test")
    member = _person(db, "member@example.test")
    app_id = _service_app(db, owner)
    before_the_cut_over(db)
    proven = _legacy_key(db, app_id, created_by_audit=owner["id"])
    foreign = _legacy_key(db, app_id, created_by_audit=member["id"])
    unknown = _legacy_key(db, app_id, created_by_audit=None)

    admin_store.init_db(db)

    creators = _creators(db)
    assert creators[proven] == owner["id"]
    assert creators[foreign] == ""  # an explicit, unproven creator; the cut-over revokes the key
    assert creators[unknown] is None


def test_after_the_cut_over_a_restart_never_runs_the_back_fill(db, monkeypatch):
    def back_fill_ran(*_args, **_kwargs):
        raise AssertionError("the creator back-fill ran after the cut-over")

    # A fresh database is recorded as cut over, so its restart must skip the back-fill.
    monkeypatch.setattr(admin_store, "_backfill_credential_creators", back_fill_ran, raising=False)

    admin_store.init_db(db)

    assert _unverified(db) == []


def test_a_restart_after_the_cut_over_puts_back_a_guard_that_was_dropped(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TRIGGER apps_never_deleted")
        conn.commit()
    finally:
        conn.close()

    admin_store.init_db(db)

    assert _unverified(db) == []


def _unverified(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return app_binding_guard.verify(conn)
    finally:
        conn.close()


def test_a_restart_after_the_cut_over_keeps_the_version_recorded_once(db):
    admin_store.init_db(db)
    admin_store.init_db(db)

    conn = sqlite3.connect(db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM schema_migration_steps WHERE name = ?",
                             (app_binding_guard.RECEIPT,)).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
