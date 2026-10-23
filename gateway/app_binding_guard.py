"""Database guards for App keys and trusted addresses (owner visibility step 2, design §3.5).

The guards make the App rules hold for every writer, not only for the routes:

* an App keeps its kind, and a personal App keeps its person (O2);
* a service App changes person only with no key, expired keys included, and no trusted address (O3);
* a key or trusted address is added with its App's accountable person as creator (O4);
* an App key keeps its App and creator, a revoked key stays revoked, and a trusted address is
  replaced rather than edited;
* an App is disabled, never deleted; a control-plane key never becomes an App key.

``install`` runs after every migration on a fresh database. A database that already held bindings is
not guarded here: the ownership cut-over (design §8.1) applies ``install`` after its preview is
accepted, because that cut-over is what removes the unproven bindings the guards would otherwise keep.
"""

from __future__ import annotations

import sqlite3
import uuid

RECEIPT = "ownership-version-1"
PENDING_NOTICE = (
    "[admin_store] App-binding cut-over is pending: existing App keys and trusted addresses are not checked "
    "until it is applied. To preview it, copy admin.db and run: "
    "python -m gateway.app_binding_preview --admin-db <copy of admin.db> (docs/ops/OWNER_BINDINGS.md)"
)

GUARDS: tuple[tuple[str, str], ...] = (
    ("apps_kind_fixed", """CREATE TRIGGER apps_kind_fixed
BEFORE UPDATE OF kind ON apps
WHEN NEW.kind IS NOT OLD.kind
BEGIN SELECT RAISE(ABORT, 'an App keeps its kind'); END"""),
    ("apps_personal_owner_fixed", """CREATE TRIGGER apps_personal_owner_fixed
BEFORE UPDATE OF owner_user_id ON apps
WHEN OLD.kind = 'personal' AND NEW.owner_user_id IS NOT OLD.owner_user_id
BEGIN SELECT RAISE(ABORT, 'a personal App keeps its person'); END"""),
    ("apps_owner_moves_only_unbound", """CREATE TRIGGER apps_owner_moves_only_unbound
BEFORE UPDATE OF owner_user_id ON apps
WHEN NEW.owner_user_id IS NOT OLD.owner_user_id AND (
  EXISTS (SELECT 1 FROM credentials WHERE subject_kind = 'app'
    AND COALESCE(NULLIF(app_id, ''), subject_id) = OLD.id AND revoked_at IS NULL)
  OR EXISTS (SELECT 1 FROM app_trusted_addresses WHERE app_id = OLD.id))
BEGIN SELECT RAISE(ABORT, 'an App with keys or trusted addresses keeps its person'); END"""),
    ("apps_never_deleted", """CREATE TRIGGER apps_never_deleted
BEFORE DELETE ON apps
BEGIN SELECT RAISE(ABORT, 'an App is disabled, not deleted'); END"""),
    ("credentials_app_key_added_by_its_person", """CREATE TRIGGER credentials_app_key_added_by_its_person
BEFORE INSERT ON credentials
WHEN NEW.subject_kind = 'app' AND (NEW.created_by IS NULL OR NEW.created_by IS NOT (
  SELECT owner_user_id FROM apps WHERE id = COALESCE(NULLIF(NEW.app_id, ''), NEW.subject_id)))
BEGIN SELECT RAISE(ABORT, 'an App key is added by the person the App answers for'); END"""),
    ("app_trusted_address_added_by_its_person", """CREATE TRIGGER app_trusted_address_added_by_its_person
BEFORE INSERT ON app_trusted_addresses
WHEN NEW.created_by IS NULL OR NEW.created_by IS NOT (SELECT owner_user_id FROM apps WHERE id = NEW.app_id)
BEGIN SELECT RAISE(ABORT, 'a trusted address is added by the person the App answers for'); END"""),
    ("credentials_app_key_identity_fixed", """CREATE TRIGGER credentials_app_key_identity_fixed
BEFORE UPDATE OF subject_kind, subject_id, app_id, created_by ON credentials
WHEN OLD.subject_kind = 'app' AND (
  NEW.subject_kind IS NOT OLD.subject_kind OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.app_id IS NOT OLD.app_id OR NEW.created_by IS NOT OLD.created_by)
BEGIN SELECT RAISE(ABORT, 'an App key keeps its App and creator'); END"""),
    ("credentials_no_control_plane_key_becomes_app_key", """CREATE TRIGGER credentials_no_control_plane_key_becomes_app_key
BEFORE UPDATE OF subject_kind ON credentials
WHEN OLD.subject_kind IS NOT 'app' AND NEW.subject_kind = 'app'
BEGIN SELECT RAISE(ABORT, 'a control-plane key never becomes an App key'); END"""),
    ("credentials_app_key_revocation_final", """CREATE TRIGGER credentials_app_key_revocation_final
BEFORE UPDATE OF revoked_at ON credentials
WHEN OLD.subject_kind = 'app' AND OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at
BEGIN SELECT RAISE(ABORT, 'a revoked App key stays revoked'); END"""),
    ("app_trusted_address_fixed", """CREATE TRIGGER app_trusted_address_fixed
BEFORE UPDATE OF app_id, address, created_by ON app_trusted_addresses
WHEN NEW.app_id IS NOT OLD.app_id OR NEW.address IS NOT OLD.address OR NEW.created_by IS NOT OLD.created_by
BEGIN SELECT RAISE(ABORT, 'a trusted address is replaced, not edited'); END"""),
)


class GuardError(RuntimeError):
    """The installed guards differ from this release, or a guard fails its own self-test."""


def recorded(conn: sqlite3.Connection) -> bool:
    """True once the ownership cut-over is applied: its version row is in the database."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration_steps'").fetchone() is None:
        return False
    return conn.execute("SELECT 1 FROM schema_migration_steps WHERE name = ?", (RECEIPT,)).fetchone() is not None


def verify(conn: sqlite3.Connection) -> list[str]:
    """Names of the guards whose stored SQL is not this release's SQL (empty when all match)."""
    stored = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall())
    return [name for name, sql in GUARDS if stored.get(name) != sql]


def install(conn: sqlite3.Connection) -> None:
    """Replace the guards with this release's, record the version, and prove them before traffic.

    Runs in a savepoint, so it commits or leaves the database as it was, inside or outside a caller's transaction.
    """
    conn.execute("SAVEPOINT binding_guards")
    try:
        for name, sql in GUARDS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(sql)
        conn.execute("INSERT OR IGNORE INTO schema_migration_steps(name) VALUES (?)", (RECEIPT,))
        mismatched = verify(conn)
        if mismatched:
            raise GuardError(f"binding guards were not installed as released: {', '.join(mismatched)}")
        self_test(conn)
    except BaseException:
        conn.execute("ROLLBACK TO binding_guards")
        conn.execute("RELEASE binding_guards")
        raise
    conn.execute("RELEASE binding_guards")


def _refused(conn: sqlite3.Connection, statement: str, params: tuple = ()) -> bool:
    try:
        conn.execute(statement, params)
    except sqlite3.IntegrityError:
        return True
    return False


def self_test(conn: sqlite3.Connection) -> None:
    """Every forbidden write is refused and every permitted write goes through, then nothing is kept.

    The fixtures live in a savepoint that is rolled back, so the self-test leaves no row behind.
    """
    owner, other = f"usr_{uuid.uuid4().hex}", f"usr_{uuid.uuid4().hex}"
    svc, zero, personal = (f"app_{uuid.uuid4().hex}" for _ in range(3))
    live, revoked, control = (f"cred_{uuid.uuid4().hex}" for _ in range(3))
    tad = f"tad_{uuid.uuid4().hex}"
    conn.execute("SAVEPOINT binding_guard_self_test")
    try:
        for user in (owner, other):
            conn.execute("INSERT INTO users(id, issuer, subject, email, display_name, created_at) "
                         "VALUES (?, 'https://self-test.invalid', ?, NULL, 'self test', 0)", (user, user))
        for app, kind in ((svc, "service"), (zero, "service"), (personal, "personal")):
            conn.execute("INSERT INTO apps(id, kind, name, owner_user_id, access_plan, created_at) "
                         "VALUES (?, ?, 'self test', ?, 'person', 0)", (app, kind, owner))
        conn.execute("INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at, created_by) "
                     "VALUES (?, 'app', ?, ?, ?, 'opk_self', 0, ?)", (live, svc, svc, f"sha256:{live}", owner))
        conn.execute("INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at, created_by, revoked_at) "
                     "VALUES (?, 'app', ?, ?, ?, 'opk_self', 0, ?, 1)", (revoked, personal, personal, f"sha256:{revoked}", owner))
        conn.execute("INSERT INTO credentials(id, subject_kind, subject_id, hash, prefix, created_at) "
                     "VALUES (?, 'service_account', 'svc_self_test', ?, 'oak_self', 0)", (control, f"sha256:{control}"))
        conn.execute("INSERT INTO app_trusted_addresses(id, app_id, address, created_at, created_by) "
                     "VALUES (?, ?, '10.0.0.1', 0, ?)", (tad, svc, owner))

        forbidden = (
            ("kind change", "UPDATE apps SET kind='personal' WHERE id=?", (svc,)),
            ("person change on a personal App", "UPDATE apps SET owner_user_id=? WHERE id=?", (other, personal)),
            ("person change with a live key", "UPDATE apps SET owner_user_id=? WHERE id=?", (other, svc)),
            ("delete", "DELETE FROM apps WHERE id=?", (zero,)),
            ("key without creator", "INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at, created_by) "
                                    "VALUES ('cred_x1', 'app', ?, ?, 'sha256:x1', 'opk', 0, NULL)", (svc, svc)),
            ("key by someone else", "INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at, created_by) "
                                    "VALUES ('cred_x2', 'app', ?, ?, 'sha256:x2', 'opk', 0, ?)", (svc, svc, other)),
            ("address without creator", "INSERT INTO app_trusted_addresses(id, app_id, address, created_at, created_by) "
                                        "VALUES ('tad_x1', ?, '10.0.0.2', 0, '')", (svc,)),
            ("key re-pointed", "UPDATE credentials SET created_by=? WHERE id=?", (other, live)),
            ("revoked key unrevoked", "UPDATE credentials SET revoked_at=NULL WHERE id=?", (revoked,)),
            ("address edited", "UPDATE app_trusted_addresses SET address='10.9.9.9' WHERE id=?", (tad,)),
            ("control-plane key made an App key", "UPDATE credentials SET subject_kind='app', subject_id=?, app_id=? WHERE id=?",
             (svc, svc, control)),
        )
        for label, statement, params in forbidden:
            if not _refused(conn, statement, params):
                raise GuardError(f"binding guard self-test: {label} was not refused")

        permitted = (
            ("same-value kind", "UPDATE apps SET kind=kind WHERE id=?", (svc,)),
            ("control-plane adoption", "UPDATE credentials SET created_by=? WHERE id=?", (f"user:{owner}", control)),
            ("transfer with zero bindings", "UPDATE apps SET owner_user_id=? WHERE id=?", (other, zero)),
            ("revocation of a live key", "UPDATE credentials SET revoked_at=5 WHERE id=?", (live,)),
        )
        for label, statement, params in permitted:
            try:
                conn.execute(statement, params)
            except sqlite3.IntegrityError as exc:
                raise GuardError(f"binding guard self-test: {label} was refused: {exc}") from None
    finally:
        conn.execute("ROLLBACK TO binding_guard_self_test")
        conn.execute("RELEASE binding_guard_self_test")
