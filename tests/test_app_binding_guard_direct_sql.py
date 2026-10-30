"""Owner visibility step 2: direct SQL cannot rewrite an App key's secret or move its expiry (design §3.5).

The routes are not the only writers, so the guards in gateway/app_binding_guard.py hold an App key's identity for every
writer. Its secret and its expiry are part of that identity: a new secret has no provenance, and a later expiry revives a
key that ran out. Recording a use, renaming, and revoking stay ordinary, and so do control-plane keys.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from gateway import accounts, admin_store

DAY = 86_400


def _sql(path: Path, statement: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(statement, params)
        conn.commit()


def _refused(path: Path, statement: str, params: tuple = ()) -> bool:
    try:
        _sql(path, statement, params)
    except sqlite3.IntegrityError:
        return True
    return False


@pytest.fixture
def guarded(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    alice = admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email="alice@example.test",
                                         display_name="alice", path=path)
    alice_app = admin_store.create_app("Alice", kind="personal", owner_user_id=alice["id"], access_plan="person", path=path)["id"]
    now = int(time.time())
    live = admin_store.create_credential_hash(subject_kind="app", subject_id=alice_app, hash=f"sha256:{uuid.uuid4().hex}",
                                              prefix="opk_alice", created_by=alice["id"], expires_at=now + 30 * DAY, path=path)["id"]
    expired = admin_store.create_credential_hash(subject_kind="app", subject_id=alice_app, hash=f"sha256:{uuid.uuid4().hex}",
                                                 prefix="opk_old", created_by=alice["id"], expires_at=now - DAY, path=path)["id"]
    control = admin_store.create_credential_hash(subject_kind="service_account", subject_id="svc_control",
                                                 hash=f"sha256:{uuid.uuid4().hex}", prefix="oak_control", path=path)["id"]
    return path, live, expired, control


def test_an_app_keys_secret_cannot_be_rewritten_in_place(guarded):
    path, live, _expired, _control = guarded
    assert _refused(path, "UPDATE credentials SET hash = 'sha256:attacker' WHERE id = ?", (live,))


def test_an_app_keys_expiry_cannot_be_moved_later(guarded):
    path, live, _expired, _control = guarded
    assert _refused(path, "UPDATE credentials SET expires_at = ? WHERE id = ?", (int(time.time()) + 365 * DAY, live))


def test_an_app_keys_expiry_cannot_be_removed(guarded):
    path, live, _expired, _control = guarded
    assert _refused(path, "UPDATE credentials SET expires_at = NULL WHERE id = ?", (live,))


def test_an_expired_app_key_cannot_be_revived_by_clearing_or_moving_its_expiry(guarded):
    path, _live, expired, _control = guarded
    assert _refused(path, "UPDATE credentials SET expires_at = NULL WHERE id = ?", (expired,))
    assert _refused(path, "UPDATE credentials SET expires_at = ? WHERE id = ?", (int(time.time()) + DAY, expired))


def test_recording_a_use_renaming_and_revoking_an_app_key_still_work(guarded):
    path, live, _expired, _control = guarded
    _sql(path, "UPDATE credentials SET last_used_at = ? WHERE id = ?", (int(time.time()), live))
    _sql(path, "UPDATE credentials SET display_name = 'Laptop' WHERE id = ?", (live,))
    _sql(path, "UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (int(time.time()), live))
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT display_name, revoked_at IS NOT NULL FROM credentials WHERE id = ?", (live,)).fetchone() == ("Laptop", 1)


def test_a_control_plane_keys_secret_and_expiry_are_not_app_key_identity(guarded):
    path, _live, _expired, control = guarded
    _sql(path, "UPDATE credentials SET expires_at = ? WHERE id = ?", (int(time.time()) + DAY, control))
    _sql(path, "UPDATE credentials SET hash = 'sha256:rotated' WHERE id = ?", (control,))
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT hash FROM credentials WHERE id = ?", (control,)).fetchone()[0] == "sha256:rotated"
