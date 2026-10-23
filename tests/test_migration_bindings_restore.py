"""Owner visibility step 2: database guards on App keys and trusted addresses (design §3.5, §8.1).

A fresh database gets the guards after every migration. A database that already had bindings keeps
them unguarded until the ownership cut-over is applied, so the upgrade test pins that wait. The
tests here use only ``init_db`` and raw SQL, so they describe the database itself, not this module's API.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import accounts, admin_store  # noqa: E402
from tests.test_access_plan_migration import build_v6  # noqa: E402

GUARD_NAMES = {
    "apps_kind_fixed", "apps_personal_owner_fixed", "apps_owner_moves_only_unbound", "apps_never_deleted",
    "credentials_app_key_added_by_its_person", "app_trusted_address_added_by_its_person",
    "credentials_app_key_identity_fixed", "credentials_no_control_plane_key_becomes_app_key",
    "credentials_app_key_revocation_final", "app_trusted_address_fixed",
}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def _sql(path: Path, statement: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _person(path: Path, email: str) -> dict:
    return admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email=email,
                                        display_name=email, path=path)


def _app(path: Path, owner: dict, kind: str = "personal") -> dict:
    return admin_store.create_app("Desk", kind=kind, owner_user_id=owner["id"], access_plan="person", path=path)


def _key(path: Path, app_id: str, *, created_by: str | None, expires_at: int | None = None) -> str:
    key_id = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app_id, app_id=app_id, hash=f"sha256:{uuid.uuid4().hex}",
        prefix="opk_test", created_by=created_by, expires_at=expires_at, path=path,
    )["id"]
    return key_id


def _address(path: Path, app_id: str, *, created_by: str | None) -> None:
    _sql(path, "INSERT INTO app_trusted_addresses(id, app_id, address, created_at, created_by) "
               "VALUES (?, ?, ?, 1, ?)", (f"tad_{uuid.uuid4().hex[:8]}", app_id, "10.0.0.7", created_by))


def _refused(path: Path, statement: str, params: tuple = ()) -> bool:
    try:
        _sql(path, statement, params)
    except sqlite3.IntegrityError:
        return True
    return False


# --- a fresh database is guarded after every migration -----------------------------------------

def test_a_fresh_database_has_every_binding_guard(db):
    conn = sqlite3.connect(db)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    conn.close()
    assert GUARD_NAMES <= names, sorted(GUARD_NAMES - names)


def test_a_fresh_database_records_the_ownership_version_once(db):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT name FROM schema_migration_steps WHERE name LIKE 'ownership-version-%'").fetchall()
    conn.close()
    assert rows == [("ownership-version-1",)]


def test_restarting_a_fresh_database_keeps_the_guards_and_the_version_row(db):
    admin_store.init_db(db)
    conn = sqlite3.connect(db)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    rows = conn.execute("SELECT COUNT(*) FROM schema_migration_steps WHERE name='ownership-version-1'").fetchone()[0]
    conn.close()
    assert GUARD_NAMES <= names
    assert rows == 1


# --- O2: kind and personal ownership are fixed --------------------------------------------------

def test_an_app_keeps_its_kind(db):
    owner = _person(db, "o@example.test")
    app = _app(db, owner, kind="service")
    assert _refused(db, "UPDATE apps SET kind='personal' WHERE id=?", (app["id"],))


def test_a_personal_app_keeps_its_person(db):
    owner, other = _person(db, "o@example.test"), _person(db, "x@example.test")
    app = _app(db, owner, kind="personal")
    assert _refused(db, "UPDATE apps SET owner_user_id=? WHERE id=?", (other["id"], app["id"]))


def test_an_app_is_disabled_not_deleted(db):
    owner = _person(db, "o@example.test")
    app = _app(db, owner)
    assert _refused(db, "DELETE FROM apps WHERE id=?", (app["id"],))


# --- O3: a service App changes person only with zero bindings ----------------------------------

def test_a_service_app_keeps_its_person_while_a_live_key_exists(db):
    owner, other = _person(db, "o@example.test"), _person(db, "x@example.test")
    app = _app(db, owner, kind="service")
    _key(db, app["id"], created_by=owner["id"])
    assert _refused(db, "UPDATE apps SET owner_user_id=? WHERE id=?", (other["id"], app["id"]))


def test_a_service_app_keeps_its_person_while_an_expired_but_unrevoked_key_exists(db):
    owner, other = _person(db, "o@example.test"), _person(db, "x@example.test")
    app = _app(db, owner, kind="service")
    _key(db, app["id"], created_by=owner["id"], expires_at=2)
    assert _refused(db, "UPDATE apps SET owner_user_id=? WHERE id=?", (other["id"], app["id"]))


def test_a_service_app_keeps_its_person_while_a_trusted_address_exists(db):
    owner, other = _person(db, "o@example.test"), _person(db, "x@example.test")
    app = _app(db, owner, kind="service")
    _address(db, app["id"], created_by=owner["id"])
    assert _refused(db, "UPDATE apps SET owner_user_id=? WHERE id=?", (other["id"], app["id"]))


def test_a_service_app_with_zero_bindings_satisfies_the_transfer_precondition(db):
    owner, other = _person(db, "o@example.test"), _person(db, "x@example.test")
    app = _app(db, owner, kind="service")
    key = _key(db, app["id"], created_by=owner["id"])
    _sql(db, "UPDATE credentials SET revoked_at=1 WHERE id=?", (key,))
    _sql(db, "UPDATE apps SET owner_user_id=? WHERE id=?", (other["id"], app["id"]))
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT owner_user_id FROM apps WHERE id=?", (app["id"],)).fetchone()[0] == other["id"]
    conn.close()


# --- O4: a key or trusted address is added by its accountable person, with provenance -----------

@pytest.mark.parametrize("creator", [None, "", "usr_someone_else"])
def test_an_app_key_without_its_persons_provenance_is_refused_at_insert(db, creator):
    owner = _person(db, "o@example.test")
    app = _app(db, owner)
    if creator == "usr_someone_else":
        creator = _person(db, "x@example.test")["id"]
    with pytest.raises(sqlite3.IntegrityError):
        _key(db, app["id"], created_by=creator)


def test_an_app_key_added_by_its_person_is_accepted(db):
    owner = _person(db, "o@example.test")
    app = _app(db, owner)
    assert _key(db, app["id"], created_by=owner["id"])


@pytest.mark.parametrize("creator", [None, ""])
def test_a_trusted_address_without_provenance_is_refused_at_insert(db, creator):
    owner = _person(db, "o@example.test")
    app = _app(db, owner)
    assert _refused(db, "INSERT INTO app_trusted_addresses(id, app_id, address, created_at, created_by) "
                        "VALUES ('tad_x', ?, '10.0.0.7', 1, ?)", (app["id"], creator))


def test_a_trusted_address_added_by_someone_else_is_refused_at_insert(db):
    owner, other = _person(db, "o@example.test"), _person(db, "x@example.test")
    app = _app(db, owner)
    assert _refused(db, "INSERT INTO app_trusted_addresses(id, app_id, address, created_at, created_by) "
                        "VALUES ('tad_x', ?, '10.0.0.7', 1, ?)", (app["id"], other["id"]))


def test_an_app_keys_app_and_creator_never_change(db):
    owner, other = _person(db, "o@example.test"), _person(db, "x@example.test")
    app, second = _app(db, owner), _app(db, owner)
    key = _key(db, app["id"], created_by=owner["id"])
    assert _refused(db, "UPDATE credentials SET app_id=?, subject_id=? WHERE id=?", (second["id"], second["id"], key))
    assert _refused(db, "UPDATE credentials SET created_by=? WHERE id=?", (other["id"], key))


def test_a_revoked_app_key_is_never_unrevoked_but_its_own_value_is_no_change(db):
    owner = _person(db, "o@example.test")
    app = _app(db, owner)
    key = _key(db, app["id"], created_by=owner["id"])
    _sql(db, "UPDATE credentials SET revoked_at=5 WHERE id=?", (key,))
    assert _refused(db, "UPDATE credentials SET revoked_at=NULL WHERE id=?", (key,))
    assert not _refused(db, "UPDATE credentials SET revoked_at=5 WHERE id=?", (key,))


def test_a_trusted_address_is_replaced_not_edited(db):
    owner = _person(db, "o@example.test")
    app = _app(db, owner)
    _address(db, app["id"], created_by=owner["id"])
    assert _refused(db, "UPDATE app_trusted_addresses SET address='10.9.9.9' WHERE app_id=?", (app["id"],))


def test_a_control_plane_key_never_becomes_an_app_key(db):
    _sql(db, "INSERT INTO service_accounts(id, name, description, created_at) VALUES ('svc_x', 'svc', NULL, 1)")
    _sql(db, "INSERT INTO credentials(id, subject_kind, subject_id, hash, prefix, created_at) "
             "VALUES ('cred_cp', 'service_account', 'svc_x', 'sha256:cp', 'oak_x', 1)")
    assert _refused(db, "UPDATE credentials SET subject_kind='app', subject_id='svc_x', app_id='svc_x' WHERE id='cred_cp'")


# --- control-plane adoption is not an App key write --------------------------------------------

def test_control_plane_adoption_still_sets_the_creator_of_a_control_plane_key(db):
    _sql(db, "INSERT INTO service_accounts(id, name, description, created_at) VALUES ('svc_y', 'svc', NULL, 1)")
    _sql(db, "INSERT INTO credentials(id, subject_kind, subject_id, hash, prefix, created_at) "
             "VALUES ('cred_adopt', 'service_account', 'svc_y', 'sha256:adopt', 'oak_y', 1)")
    _sql(db, "UPDATE credentials SET created_by='user:usr_owner' WHERE id='cred_adopt'")
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT created_by FROM credentials WHERE id='cred_adopt'").fetchone()[0] == "user:usr_owner"
    conn.close()


def test_a_same_value_update_is_not_a_change(db):
    owner = _person(db, "o@example.test")
    app = _app(db, owner, kind="service")
    _sql(db, "UPDATE apps SET kind=kind, owner_user_id=owner_user_id WHERE id=?", (app["id"],))


# --- race: a key insert and a person change serialize against the committed state -------------

def test_a_transfer_racing_a_key_insert_is_refused_once_the_key_commits(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    owner = _person(path, "o@example.test")
    other = _person(path, "x@example.test")
    app = _app(path, owner, kind="service")

    writer = sqlite3.connect(path, timeout=0, isolation_level=None)
    mover = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at, created_by) "
                       "VALUES ('cred_race', 'app', ?, ?, 'sha256:race', 'opk_race', 1, ?)", (app["id"], app["id"], owner["id"]))
        with pytest.raises(sqlite3.OperationalError):
            mover.execute("BEGIN IMMEDIATE")  # the transfer cannot start while the key is being written
        writer.execute("COMMIT")
        mover.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            mover.execute("UPDATE apps SET owner_user_id=? WHERE id=?", (other["id"], app["id"]))
        mover.execute("ROLLBACK")
    finally:
        writer.close()
        mover.close()


def test_a_key_insert_after_a_transfer_is_refused_for_the_former_person(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    owner = _person(path, "o@example.test")
    other = _person(path, "x@example.test")
    app = _app(path, owner, kind="service")
    _sql(path, "UPDATE apps SET owner_user_id=? WHERE id=?", (other["id"], app["id"]))

    assert _refused(path, "INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at, created_by) "
                          "VALUES ('cred_late', 'app', ?, ?, 'sha256:late', 'opk_late', 1, ?)",
                    (app["id"], app["id"], owner["id"]))


# --- upgrade, current and restart paths ---------------------------------------------------------

def test_an_upgraded_database_keeps_its_bindings_until_the_cut_over_is_applied(tmp_path):
    path = tmp_path / "admin.db"
    build_v6(path)
    admin_store.init_db(path)
    conn = sqlite3.connect(path)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    receipts = conn.execute("SELECT COUNT(*) FROM schema_migration_steps WHERE name LIKE 'ownership-version-%'").fetchone()[0]
    conn.close()
    assert not (GUARD_NAMES & names)
    assert receipts == 0


def test_an_upgraded_database_fills_only_the_creators_its_audit_log_proves_before_the_cut_over(tmp_path):
    path = tmp_path / "admin.db"
    build_v6(path)
    admin_store.init_db(path)
    # Unproven App keys: no creator recorded. The audit log names each App's owner as the creator of
    # every key but one, which has no audit row at all.
    _sql(path, "UPDATE credentials SET created_by=NULL WHERE subject_kind='app'")
    _sql(path, "INSERT INTO audit_events(id, created_at, actor_kind, actor_id, action, target_kind, target_id) "
               "SELECT 'aud_' || c.id, 1, 'user', a.owner_user_id, 'credential.create', 'credential', c.id "
               "FROM credentials c JOIN apps a ON a.id = COALESCE(NULLIF(c.app_id, ''), c.subject_id) "
               "WHERE c.subject_kind = 'app'")
    conn = sqlite3.connect(path)
    unaudited = conn.execute("SELECT id FROM credentials WHERE subject_kind='app' ORDER BY id LIMIT 1").fetchone()
    expected = dict(conn.execute(
        "SELECT c.id, a.owner_user_id FROM credentials c JOIN apps a ON a.id = COALESCE(NULLIF(c.app_id, ''), c.subject_id) "
        "WHERE c.subject_kind = 'app'").fetchall())
    conn.close()
    if unaudited is not None:
        _sql(path, "DELETE FROM audit_events WHERE target_id = ?", (unaudited[0],))
        expected[unaudited[0]] = None
    admin_store.init_db(path)
    after = dict(sqlite3.connect(path).execute(
        "SELECT id, created_by FROM credentials WHERE subject_kind='app'").fetchall())
    assert after == expected
