"""Owner visibility step 2: the ownership cut-over is accepted, then applied, and only then enforced.

``accept`` records the digest of a preview the owner reviewed. ``apply`` runs only when that digest is
the digest of the database now, when no App would stop while in use, when the gateway is not running,
and when no other process has the database open. It backs the database up, then changes it in one
transaction (design §8.1 steps 2 and 3; docs/ops/OWNER_BINDINGS.md).
"""

from __future__ import annotations

import fcntl
import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import accounts, admin_store, app_binding_cutover, app_binding_guard, app_binding_preview, sqlite_startup
from tests.support.app_keys import before_the_cut_over

DAY = 86_400


def _person(path: Path, email: str) -> dict:
    return admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email=email, display_name=email, path=path)


def _sql(path: Path, statement: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _key(path: Path, app_id: str, *, created_by: str | None, last_used_at: int | None = None) -> str:
    key_id = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app_id, app_id=app_id, hash=f"sha256:{uuid.uuid4().hex}",
        prefix="opk_test", created_by=created_by, path=path,
    )["id"]
    if last_used_at is not None:
        _sql(path, "UPDATE credentials SET last_used_at = ? WHERE id = ?", (last_used_at, key_id))
    return key_id


def _address(path: Path, app_id: str, address: str, *, created_by: str | None) -> str:
    entry_id = f"tad_{uuid.uuid4().hex[:10]}"
    _sql(path, "INSERT INTO app_trusted_addresses(id, app_id, address, created_at, created_by) VALUES (?, ?, ?, 1, ?)",
         (entry_id, app_id, address, created_by))
    return entry_id


def _digest(path: Path) -> str:
    conn = app_binding_preview.connect_read_only(path)
    try:
        return app_binding_preview.digest(app_binding_preview.build(conn, as_of=int(time.time())))
    finally:
        conn.close()


def _live_keys(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute(
            "SELECT id FROM credentials WHERE subject_kind = 'app' AND revoked_at IS NULL")}
    finally:
        conn.close()


def _addresses(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT id FROM app_trusted_addresses")}
    finally:
        conn.close()


def _receipts(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM schema_migration_steps WHERE name = ?",
                            (app_binding_guard.RECEIPT,)).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def default_private_directory(monkeypatch):
    monkeypatch.delenv("AZIZAM_PRIVATE_TMPDIR", raising=False)


@pytest.fixture
def legacy(tmp_path):
    """An existing database before its cut-over: one proven and one unproven key and address per App."""
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    before_the_cut_over(path)
    owner, member = _person(path, "owner@example.test"), _person(path, "member@example.test")
    desk = admin_store.create_app("Desk", kind="service", owner_user_id=owner["id"], access_plan="person", path=path)["id"]
    kept_key = _key(path, desk, created_by=owner["id"])
    unproven_key = _key(path, desk, created_by=None)
    kept_address = _address(path, desk, "10.0.0.1", created_by=owner["id"])
    unproven_address = _address(path, desk, "10.0.0.2", created_by=member["id"])
    return SimpleNamespace(path=path, owner=owner, member=member, desk=desk, kept_key=kept_key,
                           unproven_key=unproven_key, kept_address=kept_address, unproven_address=unproven_address)


def _accept(path: Path, digest: str | None = None, *, by: str = "tim") -> int:
    return app_binding_cutover.main(["accept", "--admin-db", str(path), "--digest", digest or _digest(path), "--by", by])


def _apply(path: Path) -> int:
    return app_binding_cutover.main(["apply", "--admin-db", str(path)])


def test_accept_records_the_previewed_digest_who_and_when(legacy):
    before = int(time.time())

    assert _accept(legacy.path, by="tim") == 0

    conn = sqlite3.connect(legacy.path)
    try:
        rows = conn.execute("SELECT digest, accepted_by, accepted_at FROM app_binding_cutover_acceptances").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    digest, accepted_by, accepted_at = rows[0]
    assert digest == _digest(legacy.path)
    assert accepted_by == "tim"
    assert before <= accepted_at <= int(time.time())


def test_accept_refuses_a_digest_that_is_not_the_current_state(legacy, capsys):
    code = _accept(legacy.path, "sha256:" + "0" * 64)

    assert code == 1
    assert "re-run the preview" in capsys.readouterr().err
    assert _receipts(legacy.path) == 0


def test_accept_refuses_a_preview_in_which_an_app_would_stop_while_in_use(legacy, capsys):
    used = admin_store.create_app("Ledger", kind="service", owner_user_id=legacy.owner["id"],
                                  access_plan="person", path=legacy.path)["id"]
    _key(legacy.path, used, created_by=None, last_used_at=int(time.time()) - 2 * DAY)

    code = _accept(legacy.path)

    assert code == 1
    assert "blocks apply" in capsys.readouterr().err


def test_accept_refuses_a_database_that_has_not_run_this_release_s_migrations(legacy, capsys):
    _sql(legacy.path, "UPDATE schema_version SET version = ?", (admin_store.SCHEMA_VERSION - 1,))

    code = _accept(legacy.path, digest="sha256:" + "0" * 64)

    assert code == 1
    assert "start the gateway" in capsys.readouterr().err


def test_apply_refuses_without_an_accepted_preview_and_changes_nothing(legacy, capsys):
    assert _apply(legacy.path) == 1

    assert "no accepted preview" in capsys.readouterr().err
    assert legacy.unproven_key in _live_keys(legacy.path)
    assert _receipts(legacy.path) == 0


def test_apply_refuses_when_the_database_changed_after_the_accepted_preview(legacy, capsys):
    assert _accept(legacy.path) == 0
    _key(legacy.path, legacy.desk, created_by=None)  # a change since the preview

    assert _apply(legacy.path) == 1

    assert "changed since" in capsys.readouterr().err
    assert _receipts(legacy.path) == 0


def test_apply_refuses_while_an_app_would_stop_while_in_use(legacy, capsys):
    used = admin_store.create_app("Ledger", kind="service", owner_user_id=legacy.owner["id"],
                                  access_plan="person", path=legacy.path)["id"]
    _key(legacy.path, used, created_by=None, last_used_at=int(time.time()) - DAY)
    # accept refuses this preview, so record its digest directly: apply must still refuse on the block itself.
    conn = sqlite3.connect(legacy.path)
    try:
        app_binding_cutover.record_acceptance(conn, _digest(legacy.path), "tim")
        conn.commit()
    finally:
        conn.close()

    assert _apply(legacy.path) == 1
    assert "blocks apply" in capsys.readouterr().err
    assert _receipts(legacy.path) == 0


def test_apply_refuses_while_the_gateway_holds_its_private_directory_lock(legacy, capsys):
    assert _accept(legacy.path) == 0
    lock_dir = legacy.path.parent / "private-tmp"
    lock_dir.mkdir()
    holder = open(lock_dir / ".owner.lock", "w")  # the running gateway's lifetime lock
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        assert _apply(legacy.path) == 1
    finally:
        holder.close()

    assert "gateway is running" in capsys.readouterr().err
    assert legacy.unproven_key in _live_keys(legacy.path)


def test_apply_refuses_while_another_process_has_the_database_open(legacy, capsys):
    assert _accept(legacy.path) == 0
    open_connection = sqlite_startup.connect(legacy.path)
    try:
        assert _apply(legacy.path) == 1
    finally:
        open_connection.close()

    assert "has the database open" in capsys.readouterr().err
    assert _receipts(legacy.path) == 0


def test_apply_revokes_the_unproven_keys_and_removes_the_unproven_addresses_and_keeps_the_proven_ones(legacy):
    assert _accept(legacy.path) == 0

    assert _apply(legacy.path) == 0

    live = _live_keys(legacy.path)
    assert legacy.kept_key in live
    assert legacy.unproven_key not in live
    assert legacy.kept_address in _addresses(legacy.path)
    assert legacy.unproven_address not in _addresses(legacy.path)


def test_apply_installs_every_guard_and_records_the_ownership_version_once(legacy):
    assert _accept(legacy.path) == 0

    assert _apply(legacy.path) == 0

    conn = sqlite3.connect(legacy.path)
    try:
        assert app_binding_guard.verify(conn) == []
    finally:
        conn.close()
    assert _receipts(legacy.path) == 1


def test_apply_takes_a_backup_beside_the_database_before_it_writes(legacy):
    assert _accept(legacy.path) == 0

    assert _apply(legacy.path) == 0

    backups = sorted(legacy.path.parent.glob("admin.db.pre-ownership-cutover-*"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])
    try:
        assert legacy.unproven_key in {row[0] for row in conn.execute(
            "SELECT id FROM credentials WHERE subject_kind = 'app' AND revoked_at IS NULL")}
        assert legacy.unproven_address in {row[0] for row in conn.execute("SELECT id FROM app_trusted_addresses")}
    finally:
        conn.close()
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_apply_prints_what_it_did(legacy, capsys):
    assert _accept(legacy.path) == 0
    capsys.readouterr()

    assert _apply(legacy.path) == 0

    out = capsys.readouterr().out
    assert "keys revoked: 1" in out
    assert "addresses removed: 1" in out
    assert f"guards installed: {len(app_binding_guard.GUARDS)}" in out
    assert "backup:" in out and "admin.db.pre-ownership-cutover-" in out


def test_apply_is_one_transaction_so_a_failure_leaves_the_database_as_it_was(legacy, monkeypatch, capsys):
    assert _accept(legacy.path) == 0

    def guard_self_test_fails(conn):
        raise app_binding_guard.GuardError("self test failed")

    monkeypatch.setattr(app_binding_guard, "self_test", guard_self_test_fails)

    assert _apply(legacy.path) == 1

    assert "self test failed" in capsys.readouterr().err
    assert legacy.unproven_key in _live_keys(legacy.path)
    assert legacy.unproven_address in _addresses(legacy.path)
    assert _receipts(legacy.path) == 0


def test_apply_refuses_a_database_whose_cut_over_is_already_applied(legacy, capsys):
    assert _accept(legacy.path) == 0
    assert _apply(legacy.path) == 0

    assert _apply(legacy.path) == 1
    assert "already" in capsys.readouterr().err


def test_after_apply_a_restart_never_runs_the_back_fill_and_keeps_the_guards(legacy, monkeypatch):
    assert _accept(legacy.path) == 0
    assert _apply(legacy.path) == 0

    def back_fill_ran(*_args, **_kwargs):
        raise AssertionError("the creator back-fill ran after apply")

    monkeypatch.setattr(admin_store, "_backfill_credential_creators", back_fill_ran, raising=False)
    admin_store.init_db(legacy.path)

    conn = sqlite3.connect(legacy.path)
    try:
        assert app_binding_guard.verify(conn) == []
    finally:
        conn.close()


def test_after_apply_a_control_plane_key_still_takes_its_creator(legacy):
    assert _accept(legacy.path) == 0
    assert _apply(legacy.path) == 0
    control = admin_store.create_credential_hash(
        subject_kind="service_account", subject_id="svc_control", hash=f"sha256:{uuid.uuid4().hex}",
        prefix="oak_test", created_by=None, path=legacy.path,
    )["id"]

    _sql(legacy.path, "UPDATE credentials SET created_by = ? WHERE id = ?", (f"user:{legacy.owner['id']}", control))

    conn = sqlite3.connect(legacy.path)
    try:
        assert conn.execute("SELECT created_by FROM credentials WHERE id = ?", (control,)).fetchone()[0] == f"user:{legacy.owner['id']}"
    finally:
        conn.close()
