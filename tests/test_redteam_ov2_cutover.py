"""Independent cut-over review of main 05ea15e05; only synthetic databases.

Known defects are strict xfails asserting the required safe outcome. Passing
controls exercise the real module CLIs, restore, WAL readers, and SIGKILL at
transaction boundaries. No server or network listener is started.
"""

from __future__ import annotations

import json
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import admin_runtime, admin_store, app_binding_guard
from tests.test_access_plan_migration import TIM, build_v6
from tests.test_signin_who_can_enter import OWNER, csrf_for, gateway, signed_in  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
ACT = {"x-azizam-user-action": "1"}


def _sql(path, sql, args=()):
    with closing(sqlite3.connect(path)) as conn:
        result = conn.execute(sql, args).fetchall()
        conn.commit()
        return result


def _cli(module, *args):
    return subprocess.run(
        [sys.executable, "-m", "gateway." + module, *map(str, args)],
        cwd=ROOT, text=True, capture_output=True, timeout=30,
    )


def _preview(path):
    result = _cli("app_binding_preview", "--admin-db", path, "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _accept(path):
    body = _preview(path)
    result = _cli("app_binding_cutover", "accept", "--admin-db", path,
                  "--digest", body["digest"], "--by", "scratch reviewer")
    assert result.returncode == 0, result.stderr
    return body


def _apply(path):
    return _cli("app_binding_cutover", "apply", "--admin-db", path)


def _snapshot(path):
    """All durable rows and schema, without dumping credential hashes on failure."""
    with closing(sqlite3.connect(path)) as conn:
        return "\n".join(conn.iterdump())


def _assert_same_database(path, expected):
    # Keep a failed assertion from printing even synthetic credential material.
    assert _snapshot(path) == expected, "durable database changed"
    assert _sql(path, "PRAGMA integrity_check") == [("ok",)]


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("AZIZAM_PRIVATE_TMPDIR", raising=False)
    path = tmp_path / "admin.db"
    build_v6(path)
    admin_store.init_db(path)
    # The old fixture's imported apps are deliberately idle in this rehearsal.
    _sql(path, "UPDATE credentials SET last_used_at = NULL")
    app = admin_store.create_app("Review desk", kind="service", owner_user_id=TIM,
                                 access_plan="person", path=path)["id"]

    def key(creator, *, expires=None, session=None):
        return admin_store.create_credential_hash(
            subject_kind="app", subject_id=app, hash="sha256:" + uuid.uuid4().hex,
            prefix="synthetic", created_by=creator, expires_at=expires,
            session_id=session, path=path,
        )["id"]

    kept, foreign = key(TIM), key(None)
    for entry_id, address, creator in (("review_kept", "192.0.2.1", TIM),
                                       ("review_foreign", "192.0.2.2", None)):
        _sql(path, "INSERT INTO app_trusted_addresses(id,app_id,address,created_at,created_by) "
                   "VALUES(?,?,?,1,?)", (entry_id, app, address, creator))
    return SimpleNamespace(path=path, app=app, kept=kept, foreign=foreign, key=key)


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="F1: an expired proven key suppresses BLOCKS APPLY")
def test_recent_app_with_only_expired_proven_survivor_blocks_apply(legacy):
    """P1: an active foreign key is revoked even though no usable binding survives.

    Fix: count only runtime-usable survivors, including expiry and session state;
    reevaluate those facts at apply time and cover them in the decision digest.
    """
    path = legacy.path
    _sql(path, "DELETE FROM app_trusted_addresses WHERE app_id=?", (legacy.app,))
    _sql(path, "UPDATE credentials SET revoked_at=1 WHERE id=?", (legacy.kept,))
    survivor = legacy.key(TIM, expires=int(time.time()) - 60)
    _sql(path, "UPDATE credentials SET last_used_at=? WHERE id=?", (int(time.time()), legacy.foreign))
    body = _preview(path)
    app = next(a for a in body["apps"] if a["app_id"] == legacy.app)
    # Confirm the purported survivor really cannot authenticate.
    key_hash = _sql(path, "SELECT hash FROM credentials WHERE id=?", (survivor,))[0][0]
    assert admin_runtime.runtime_identity_for_app_credential_hash(key_hash, path=path) is None
    result = _cli("app_binding_cutover", "accept", "--admin-db", path,
                  "--digest", body["digest"], "--by", "scratch reviewer")
    if result.returncode == 0:
        applied = _apply(path)
        assert applied.returncode == 0, applied.stderr
        assert _sql(path, "SELECT revoked_at IS NOT NULL FROM credentials WHERE id=?", (legacy.foreign,)) == [(1,)]
        assert admin_store.list_active_credentials(subject_kind="app", app_id=legacy.app, path=path) == []
    assert app["blocks_apply"], "recently used App was stopped with no usable survivor"
    assert result.returncode != 0, "accept approved stopping a recently used App"


# The subprocess enters the production module CLI through runpy. The only
# instrumentation is a SQLite connection that announces a completed statement
# and waits for stdin. The parent kills the actual apply process with SIGKILL.
PAUSED_APPLY = r'''
import os, runpy, sqlite3, sys
from pathlib import Path
point = sys.argv.pop(1)
real_connect = sqlite3.connect
paused = False
def pause(label):
    global paused
    if not paused and point == label:
        paused = True
        print("REDTEAM_PAUSED", flush=True)
        sys.stdin.readline()
class Connection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        result = super().execute(sql, *args, **kwargs)
        if sql == "BEGIN IMMEDIATE": pause("transaction_started")
        if sql.startswith("UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL"):
            pause("key_revoked")
        if sql.startswith("DELETE FROM app_trusted_addresses WHERE id = ?"):
            pause("address_removed")
        if sql.startswith("CREATE TRIGGER apps_kind_fixed"):
            pause("guard_created")
        if sql.startswith("INSERT OR IGNORE INTO schema_migration_steps(name)"):
            pause("version_recorded")
        if sql == "COMMIT": pause("committed")
        return result
def connect(database, *args, **kwargs):
    if ".pre-ownership-cutover-" in str(database): pause("backup_opened")
    kwargs.setdefault("factory", Connection)
    return real_connect(database, *args, **kwargs)
sqlite3.connect = connect
runpy.run_module("gateway.app_binding_cutover", run_name="__main__")
'''


def _kill_apply_at(path, point):
    proc = subprocess.Popen(
        [sys.executable, "-c", PAUSED_APPLY, point, "apply", "--admin-db", str(path)],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), "apply never reached the crash point"
            assert proc.stdout.readline().strip() == "REDTEAM_PAUSED", "apply exited before the crash point"
        proc.kill()
        proc.communicate(timeout=10)
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="F2: backup is published with its final restore filename before any data is copied")
def test_killed_backup_never_leaves_a_nonrestorable_final_backup(legacy):
    """P2: kill/ENOSPC while backing up leaves an empty official backup artifact.

    Restoring it and starting the gateway initializes a new empty database.
    Fix: write to a temporary sibling, validate and sync, then publish atomically;
    incomplete backups must not use the documented restore filename.
    """
    _accept(legacy.path)
    before = _snapshot(legacy.path)
    _kill_apply_at(legacy.path, "backup_opened")
    _assert_same_database(legacy.path, before)
    for backup in legacy.path.parent.glob("admin.db.pre-ownership-cutover-*"):
        with closing(sqlite3.connect(backup)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "apps" in tables and "credentials" in tables, "a final-name backup has no restorable schema"


@pytest.mark.parametrize("point", ["backup_opened", "transaction_started", "key_revoked",
                                   "address_removed", "guard_created", "version_recorded", "committed"])
def test_sigkill_during_apply_is_atomic_and_restartable(legacy, point):
    _accept(legacy.path)
    before = _snapshot(legacy.path)
    _kill_apply_at(legacy.path, point)
    assert _sql(legacy.path, "PRAGMA integrity_check") == [("ok",)]
    if point != "committed":
        _assert_same_database(legacy.path, before)
    else:
        assert _sql(legacy.path, "SELECT revoked_at IS NOT NULL FROM credentials WHERE id=?", (legacy.foreign,)) == [(1,)]
        assert _sql(legacy.path, "SELECT revoked_at FROM credentials WHERE id=?", (legacy.kept,)) == [(None,)]
        with closing(sqlite3.connect(legacy.path)) as conn:
            assert app_binding_guard.recorded(conn)
            assert app_binding_guard.verify(conn) == []
    admin_store.init_db(legacy.path)
    with closing(sqlite3.connect(legacy.path)) as conn:
        assert app_binding_guard.recorded(conn) == (point == "committed")


def test_full_module_cli_path_and_backup_restore_with_wal_reader(legacy, tmp_path):
    """A raw SQLite reader pins the old WAL snapshot; the backup must include it."""
    path = legacy.path
    assert _sql(path, "PRAGMA journal_mode=WAL") == [("wal",)]
    with closing(sqlite3.connect(path)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM apps").fetchone()
        _accept(path)
        before = _snapshot(path)
        result = _apply(path)
        assert result.returncode == 0, result.stderr
        backup, = path.parent.glob("admin.db.pre-ownership-cutover-*")
        _assert_same_database(backup, before)
        assert backup.stat().st_mode & 0o777 == 0o600
        assert _sql(path, "SELECT revoked_at FROM credentials WHERE id=?", (legacy.kept,)) == [(None,)]
        assert _sql(path, "SELECT COUNT(*) FROM app_trusted_addresses WHERE id='review_kept'") == [(1,)]
        assert _sql(path, "SELECT COUNT(*) FROM app_trusted_addresses WHERE id='review_foreign'") == [(0,)]
    admin_store.init_db(path)
    with closing(sqlite3.connect(path)) as conn:
        assert app_binding_guard.verify(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM credentials c LEFT JOIN apps a "
                            "ON a.id=COALESCE(NULLIF(c.app_id,''),c.subject_id) "
                            "WHERE c.subject_kind='app' AND c.revoked_at IS NULL "
                            "AND (a.id IS NULL OR c.created_by IS NOT a.owner_user_id)").fetchone()[0] == 0
    restored = tmp_path / "restored.db"
    restored.write_bytes(backup.read_bytes())
    admin_store.init_db(restored)
    assert _sql(restored, "SELECT revoked_at FROM credentials WHERE id=?", (legacy.foreign,)) == [(None,)]
    assert _sql(restored, "PRAGMA integrity_check") == [("ok",)]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="F3: admin removal routes check role rank rather than accountable person")
@pytest.mark.parametrize("route", ["delete_key", "revoke_key", "delete_address"])
def test_admin_cannot_remove_a_kept_binding_on_another_persons_app(gateway, route):
    """P2: the requested accountable-only removal rule is absent on admin routes.

    Fix: for App bindings apply _require_accountable_person to both key-revoke
    aliases and address deletion; keep control-plane-key revocation separate.
    """
    owner, response = signed_in(gateway, sub="owner", email=OWNER)
    assert response.status_code == 302
    gateway.set_rules([], fallback="open")
    member, response = signed_in(gateway, sub="member", email="member@example.test")
    assert response.status_code == 302
    headers = {**csrf_for(member), **ACT}
    app = member.post("/me/api/apps", headers=headers, json={"name": "Member desk"}).json()["app"]["id"]
    key_response = member.post(f"/me/api/apps/{app}/keys", headers=headers, json={})
    assert key_response.status_code == 201
    key_id = key_response.json()["credential"]["id"]
    address_response = member.post(f"/me/api/apps/{app}/trusted-addresses", headers=headers,
                                   json={"address": "192.0.2.18"})
    assert address_response.status_code == 201
    entry_id = address_response.json()["trusted_address"]["id"]
    admin_headers = {**csrf_for(owner), **ACT}
    if route == "delete_key":
        result = owner.delete(f"/admin/api/credentials/{key_id}", headers=admin_headers)
    elif route == "revoke_key":
        result = owner.post(f"/admin/api/credentials/{key_id}/revoke", headers=admin_headers)
    else:
        result = owner.delete(f"/admin/api/apps/{app}/trusted-addresses/{entry_id}", headers=admin_headers)
    assert result.status_code == 403, "another person's binding was removed through an admin route"
    assert result.json()["error"]["code"] == "not_your_app"
    assert admin_store.get_credential(key_id, path=gateway.path)["revoked_at"] is None
    assert admin_store.list_trusted_addresses(app, path=gateway.path)
