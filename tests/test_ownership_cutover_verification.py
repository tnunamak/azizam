"""Independent ownership cut-over verification, starting with the schema-6 fixtures.

No operator data is used. Child processes use this file's small command dispatcher;
all database paths come from pytest scratch directories. Crash tests use host disk.
"""

from __future__ import annotations

import json
import select
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import accounts, admin_store, app_binding_cutover as cutover, app_binding_guard as guard
from gateway import app_binding_preview as preview
from tests.test_access_plan_migration import ODL, SIMON, TIM, build_v6


def rows(path, sql, params=()):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql, params).fetchall()


def write(path, sql, params=()):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(sql, params)


def snapshot(path):
    """Compare all durable data and schema, including receipts, audit and triggers."""
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        schema = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        data = {}
        for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = name.replace('"', '""')
            data[name] = sorted(conn.execute(f'SELECT * FROM "{quoted}"').fetchall(), key=repr)
        return schema, data


def copy_db(source, target):
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def body(path):
    with closing(preview.connect_read_only(path)) as conn:
        return preview.build(conn, as_of=int(time.time()))


def accept(path):
    cutover.accept(path, preview.digest(body(path)), "scratch verifier")


def make_legacy(path):
    build_v6(path)
    admin_store.init_db(path)
    accounts.initialize(path)
    # The synthetic fixture's dates can enter/leave the recent-use window as time
    # passes. These base Apps are idle; blocking Apps are tested separately.
    write(path, "UPDATE credentials SET last_used_at=NULL")
    for key_id, app_id, maker in (
        ("verify_unknown", "app_museum", None),
        ("verify_foreign", "app_museum", SIMON),
        ("verify_empty", "app_museum", ""),
        ("verify_orphan", "app_missing", TIM),
        ("verify_subject_fallback", "app_museum", TIM),
        ("verify_member_unknown", "app_odl_first", None),
    ):
        write(path, "INSERT INTO credentials(id,subject_kind,subject_id,app_id,hash,prefix,created_at,created_by) "
                    "VALUES (?,'app',?,?,?,?,1,?)",
              (key_id, app_id, None if key_id == "verify_subject_fallback" else app_id,
               "sha256:" + key_id, "opk_scratch", maker))
    for entry_id, app_id, maker, address in (
        ("verify_kept_address", "app_museum", TIM, "192.0.2.1"),
        ("verify_unknown_address", "app_museum", None, "192.0.2.2"),
        ("verify_foreign_address", "app_museum", SIMON, "192.0.2.3"),
        ("verify_orphan_address", "app_missing", TIM, "192.0.2.4"),
    ):
        write(path, "INSERT INTO app_trusted_addresses(id,app_id,address,created_at,created_by) VALUES (?,?,?,1,?)",
              (entry_id, app_id, address, maker))
    assert not body(path)["summary"]["apps_blocking_apply"]
    return path


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("AZIZAM_PRIVATE_TMPDIR", raising=False)
    return make_legacy(tmp_path / "admin.db")


@pytest.fixture
def disk_legacy(host_disk_dir, monkeypatch):
    monkeypatch.delenv("AZIZAM_PRIVATE_TMPDIR", raising=False)
    return make_legacy(host_disk_dir / "admin.db")


def assert_applied(path, before):
    """Use an independent LEFT JOIN, never the migration's own post-check."""
    old_keys = {r[0]: r for r in before[1]["credentials"]}
    assert rows(path, "SELECT c.id FROM credentials c LEFT JOIN apps a "
                     "ON a.id=COALESCE(NULLIF(c.app_id,''),c.subject_id) "
                     "WHERE c.subject_kind='app' AND c.revoked_at IS NULL "
                     "AND (a.id IS NULL OR c.created_by IS NULL OR c.created_by IS NOT a.owner_user_id)") == []
    assert rows(path, "SELECT t.id FROM app_trusted_addresses t LEFT JOIN apps a ON a.id=t.app_id "
                     "WHERE a.id IS NULL OR t.created_by IS NULL OR t.created_by IS NOT a.owner_user_id") == []
    with closing(sqlite3.connect(path)) as conn:
        assert guard.recorded(conn)
        assert guard.verify(conn) == []
        columns = [r[1] for r in conn.execute("PRAGMA table_info(credentials)")]
        # Proven keys keep every field, including secret hash and expiry.
        app_columns = [r[1] for r in conn.execute("PRAGMA table_info(apps)")]
        apps = {r[app_columns.index("id")]: r[app_columns.index("owner_user_id")] for r in before[1]["apps"]}
        current_keys = {r[0]: tuple(r) for r in conn.execute("SELECT * FROM credentials")}
        for key_id, old in old_keys.items():
            item = dict(zip(columns, old))
            app_id = item["app_id"] or item["subject_id"]
            if (item["subject_kind"] == "app" and item["revoked_at"] is None
                    and item["created_by"] is not None and item["created_by"] == apps.get(app_id)):
                assert current_keys.get(key_id) == old
            if item["subject_kind"] != "app":
                assert current_keys.get(key_id) == old
        address_columns = [r[1] for r in conn.execute("PRAGMA table_info(app_trusted_addresses)")]
        current_addresses = {r[0]: tuple(r) for r in conn.execute("SELECT * FROM app_trusted_addresses")}
        for old in before[1]["app_trusted_addresses"]:
            entry = dict(zip(address_columns, old))
            if entry["created_by"] is not None and entry["created_by"] == apps.get(entry["app_id"]):
                assert current_addresses.get(entry["id"]) == old


def assert_backup(path, before):
    backups = published_backups(path)
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert snapshot(backups[0]) == before


def published_backups(path):
    prefix = path.name + ".pre-ownership-cutover-"
    # A killed SQLite backup may leave its own -journal. Only the documented
    # restore filename (epoch suffix) promises to be a complete database.
    return [p for p in path.parent.glob(prefix + "*") if p.name.removeprefix(prefix).isdecimal()]


def test_real_cli_preview_accept_apply_on_legacy_copy(legacy, tmp_path):
    copy = tmp_path / "preview.db"
    copy_db(legacy, copy)
    unchanged = snapshot(copy)
    result = subprocess.run([sys.executable, "-m", "gateway.app_binding_preview", "--admin-db", str(copy), "--json"],
                            cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)
    digest = json.loads(result.stdout)["digest"]
    assert snapshot(copy) == unchanged
    subprocess.run([sys.executable, "-m", "gateway.app_binding_cutover", "accept", "--admin-db", str(legacy),
                    "--digest", digest, "--by", "scratch verifier"], cwd=ROOT, capture_output=True, timeout=30, check=True)
    before = snapshot(legacy)
    subprocess.run([sys.executable, "-m", "gateway.app_binding_cutover", "apply", "--admin-db", str(legacy)],
                   cwd=ROOT, capture_output=True, timeout=30, check=True)
    assert_applied(legacy, before)
    assert_backup(legacy, before)


def test_no_live_foreign_or_orphan_binding_survives(legacy):
    accept(legacy)
    before = snapshot(legacy)
    cutover.apply(legacy)
    assert_applied(legacy, before)
    assert rows(legacy, "SELECT revoked_at IS NOT NULL FROM credentials WHERE id='verify_orphan'") == [(1,)]
    assert rows(legacy, "SELECT id FROM app_trusted_addresses WHERE id='verify_orphan_address'") == []
    assert rows(legacy, "SELECT id FROM app_trusted_addresses WHERE id='verify_kept_address'") == [("verify_kept_address",)]


@pytest.mark.parametrize("change", ["new_binding", "new_recent_use", "new_owner"])
def test_apply_refuses_changed_accepted_decision_without_writes(legacy, change):
    accept(legacy)
    if change == "new_binding":
        write(legacy, "UPDATE credentials SET created_by=? WHERE id='verify_unknown'", (TIM,))
    elif change == "new_recent_use":
        write(legacy, "UPDATE credentials SET last_used_at=? WHERE id='cred_pi'", (int(time.time()),))
    else:
        write(legacy, "UPDATE apps SET owner_user_id=? WHERE id='app_museum'", (SIMON,))
    before = snapshot(legacy)
    with pytest.raises(cutover.CutoverError, match="changed since"):
        cutover.apply(legacy)
    assert snapshot(legacy) == before
    assert not list(legacy.parent.glob("*.pre-ownership-cutover-*"))


def test_blocks_apply_is_enforced_even_with_a_matching_acceptance(legacy):
    write(legacy, "UPDATE credentials SET last_used_at=? WHERE id='cred_pi'", (int(time.time()),))
    digest = preview.digest(body(legacy))
    with pytest.raises(cutover.CutoverError, match="blocks apply"):
        cutover.accept(legacy, digest, "scratch verifier")
    with closing(sqlite3.connect(legacy)) as conn, conn:
        cutover.record_acceptance(conn, digest, "simulated old acceptance")
    before = snapshot(legacy)
    with pytest.raises(cutover.CutoverError, match="blocks apply"):
        cutover.apply(legacy)
    assert snapshot(legacy) == before


class InjectedFailure(RuntimeError):
    pass


class ObservedConnection(sqlite3.Connection):
    """Pause/fail after a successful statement, outside sqlite's trace callback.

    Exceptions raised by trace callbacks are ignored by sqlite, so they cannot
    establish rollback. The hook also covers savepoints and guard self-tests.
    """

    hook = None
    statement_count = 0

    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if self.in_transaction and sql.strip().upper() != "COMMIT":
            self.statement_count += 1
            if self.hook is not None:
                self.hook(self.statement_count, sql)
        return result


def observed_connect(path, hook):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, factory=ObservedConnection)
    conn.row_factory = sqlite3.Row
    conn.hook = hook
    return conn


def statement_schedule(path, destination, monkeypatch):
    copy_db(path, destination)
    schedule = []
    with monkeypatch.context() as patch:
        patch.setattr(cutover, "_connect", lambda p: observed_connect(p, lambda n, sql: schedule.append(sql)))
        cutover.apply(destination)
    assert any(sql.startswith("UPDATE credentials") for sql in schedule)
    assert any(sql.startswith("DELETE FROM app_trusted_addresses") for sql in schedule)
    assert any("CREATE TRIGGER" in sql for sql in schedule)
    assert any(cutover.ACTION in str(r) for r in rows(destination, "SELECT action FROM audit_events"))
    return schedule


def test_exception_after_every_transaction_statement_rolls_back(legacy, tmp_path, monkeypatch, record_property):
    accept(legacy)
    before = snapshot(legacy)
    schedule = statement_schedule(legacy, tmp_path / "successful.db", monkeypatch)
    record_property("interruption_points", len(schedule))
    for step, statement in enumerate(schedule, 1):
        directory = tmp_path / f"exception-{step}"
        directory.mkdir()
        path = directory / "admin.db"
        copy_db(legacy, path)

        def fail(n, sql):
            if n == step:
                raise InjectedFailure(f"after statement {n}")

        with monkeypatch.context() as patch:
            patch.setattr(cutover, "_connect", lambda p: observed_connect(p, fail))
            with pytest.raises(InjectedFailure, match="after statement"):
                cutover.apply(path)
        assert snapshot(path) == before, f"partial migration after statement {step}: {statement}"
        assert_backup(path, before)


@contextmanager
def child(mode, path, *args):
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), mode, str(path), *map(str, args)],
                            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            kill_child(proc)
        proc.communicate(timeout=20)


def kill_child(proc):
    # As in test_ci_local, a Python controller delivers signals inside the
    # runner's private PID namespace: pytest itself forbids os.kill. Only the
    # Popen handle's own child PID is passed; the host is outside this namespace.
    subprocess.run([sys.executable, "-c", "import os,signal,sys; os.kill(int(sys.argv[1]), signal.SIGKILL)",
                    str(proc.pid)], check=True, capture_output=True, timeout=10)


def expect_line(proc, expected, timeout=30):
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    assert ready, f"child did not reach {expected!r} within {timeout}s"
    line = proc.stdout.readline().decode().strip()
    assert line == expected, f"expected {expected!r}, got {line!r}; exit={proc.poll()}"


def test_sigkill_after_every_transaction_statement_recovers_and_backup_opens(
    disk_legacy, host_disk_dir, monkeypatch, record_property,
):
    accept(disk_legacy)
    before = snapshot(disk_legacy)
    schedule = statement_schedule(disk_legacy, host_disk_dir / "successful.db", monkeypatch)
    record_property("interruption_points", len(schedule))
    for step in range(1, len(schedule) + 1):
        directory = host_disk_dir / f"kill-{step}"
        directory.mkdir()
        path = directory / "admin.db"
        copy_db(disk_legacy, path)
        with child("pause-statement", path, step) as proc:
            expect_line(proc, "paused")
            kill_child(proc)
            assert proc.wait(timeout=10) == -signal.SIGKILL
        assert snapshot(path) == before, f"partial migration after SIGKILL at statement {step}"
        assert_backup(path, before)


def test_apply_with_an_open_wal_reader_has_a_consistent_old_snapshot(disk_legacy):
    accept(disk_legacy)
    before = snapshot(disk_legacy)
    with closing(sqlite3.connect(disk_legacy)) as reader:
        assert reader.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        reader.execute("BEGIN")
        old_live = reader.execute("SELECT COUNT(*) FROM credentials WHERE revoked_at IS NULL").fetchone()[0]
        cutover.apply(disk_legacy)
        assert reader.execute("SELECT COUNT(*) FROM credentials WHERE revoked_at IS NULL").fetchone()[0] == old_live
        assert not guard.recorded(reader)
        reader.rollback()
        assert guard.recorded(reader)
    assert_applied(disk_legacy, before)
    assert_backup(disk_legacy, before)


def test_real_gateway_lifespan_refuses_apply_until_shutdown(legacy):
    accept(legacy)
    before = snapshot(legacy)
    with child("gateway", legacy) as proc:
        expect_line(proc, "entering-lifespan")
        expect_line(proc, "requesting-lifetime-lock")
        expect_line(proc, "serving")
        with pytest.raises(cutover.CutoverError, match="gateway is running"):
            cutover.apply(legacy)
        assert snapshot(legacy) == before
        proc.stdin.write(b"stop\n")
        proc.stdin.flush()
        assert proc.wait(timeout=20) == 0
    cutover.apply(legacy)
    assert_applied(legacy, before)


def test_real_gateway_cannot_enter_serving_lifespan_until_apply_commits(legacy):
    accept(legacy)
    with child("pause-statement", legacy, 1) as applying:
        expect_line(applying, "paused")
        with child("gateway", legacy) as gateway:
            expect_line(gateway, "entering-lifespan")
            expect_line(gateway, "requesting-lifetime-lock")
            ready, _, _ = select.select([gateway.stdout], [], [], 0.3)
            assert not ready, "gateway entered serving lifespan during apply"
            assert gateway.poll() is None
            applying.stdin.write(b"continue\n")
            applying.stdin.flush()
            expect_line(applying, "applied")
            assert applying.wait(timeout=20) == 0
            expect_line(gateway, "serving")
            gateway.stdin.write(b"stop\n")
            gateway.stdin.flush()
            assert gateway.wait(timeout=20) == 0


@pytest.mark.parametrize("statement", [
    "INSERT INTO credentials(id,subject_kind,subject_id,hash,prefix,created_at) "
    "VALUES ('legacy_new','app','app_museum','sha256:new','opk_scratch',1)",
    "INSERT INTO credentials(id,subject_kind,subject_id,hash,prefix,created_at,created_by) "
    "VALUES ('legacy_new','app','app_missing','sha256:new','opk_scratch',1,'usr_tim')",
    "INSERT INTO app_trusted_addresses(id,app_id,address,created_at) VALUES ('legacy_tad','app_museum','192.0.2.9',1)",
    "UPDATE credentials SET created_by=NULL WHERE id='cred_museum'",
    "UPDATE credentials SET hash='sha256:changed' WHERE id='cred_museum'",
    "UPDATE credentials SET revoked_at=NULL WHERE id='verify_unknown'",
    "UPDATE apps SET owner_user_id='usr_simon' WHERE id='app_museum'",
])
def test_restart_skips_backfill_restores_guards_and_refuses_legacy_writes(legacy, monkeypatch, statement):
    accept(legacy)
    cutover.apply(legacy)
    write(legacy, "DROP TRIGGER credentials_app_key_added_by_its_person")

    def forbidden_backfill(*args, **kwargs):
        pytest.fail("creator back-fill ran after ownership cut-over")

    monkeypatch.setattr(admin_store, "_backfill_credential_creators", forbidden_backfill)
    admin_store.init_db(legacy)
    admin_store.init_db(legacy)
    with closing(sqlite3.connect(legacy)) as conn:
        assert guard.verify(conn) == []
    with pytest.raises(sqlite3.IntegrityError):
        write(legacy, statement)


@pytest.fixture
def portal(legacy, monkeypatch):
    """Real HTTP handlers and a migrated legacy DB; only cookie lookup is stubbed."""
    import proxy
    from fastapi.testclient import TestClient
    from gateway.admin_oidc import AdminSession

    now = int(time.time())
    sessions = {
        uid: AdminSession(subject=uid, email=email, role=role, issued_at=now, expires_at=now + 3600,
                          authenticated_at=now, session_id="scratch-session-" + uid)
        for uid, email, role in ((TIM, "tim@example.test", "owner"), (SIMON, "simon@example.test", "admin"),
                                 (ODL, "odl@example.test", "none"))
    }
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", legacy)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", legacy)
    monkeypatch.setattr(proxy, "_oidc_session_from_request", lambda request: sessions.get(request.headers.get("x-test-person")))
    monkeypatch.setattr(proxy, "_csrf_token_for_oidc_session", lambda session: "scratch-csrf")
    monkeypatch.setattr(proxy, "ADMIN_SESSION_SIGNING_KEY", b"s" * 32)
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2 ** 10)
    with closing(TestClient(proxy.app, base_url="https://ai.example", client=("127.0.0.1", 50000))) as client:
        yield SimpleNamespace(path=legacy, client=client, sessions=sessions)


def headers(person=TIM):
    return {"x-test-person": person, "x-azizam-csrf": "scratch-csrf", "x-azizam-user-action": "1"}


def assert_key_person(path, key_id, person):
    assert rows(path, "SELECT c.created_by,a.owner_user_id,c.revoked_at FROM credentials c JOIN apps a "
                     "ON a.id=COALESCE(NULLIF(c.app_id,''),c.subject_id) WHERE c.id=?", (key_id,)) == [(person, person, None)]


def test_me_kept_replaced_and_readded_bindings_survive_actual_apply(portal):
    client, path = portal.client, portal.path
    confirmed = {"confirm_admins_could_have_made_it": True}
    key = client.post("/me/api/credentials/verify_unknown/adopt", headers=headers(), json=confirmed)
    assert key.status_code == 200, key.text
    address = client.post("/me/api/apps/app_museum/trusted-addresses/verify_unknown_address/adopt",
                          headers=headers(), json=confirmed)
    assert address.status_code == 200, address.text
    replacement = client.post("/me/api/credentials/verify_foreign/replace", headers=headers())
    assert replacement.status_code == 201, replacement.text
    new_key = replacement.json()["credential"]["id"]
    refused = client.post("/me/api/credentials/verify_member_unknown/adopt", headers=headers(ODL))
    assert refused.status_code == 409
    member_replacement = client.post("/me/api/credentials/verify_member_unknown/replace", headers=headers(ODL))
    assert member_replacement.status_code == 201, member_replacement.text
    member_key = member_replacement.json()["credential"]["id"]
    readded = client.post("/me/api/apps/app_museum/trusted-addresses/verify_foreign_address/readd", headers=headers())
    assert readded.status_code == 201, readded.text
    new_address = readded.json()["trusted_address"]["id"]
    # The startup back-fill also supplies proven makers from authentic creation audit.
    admin_store.record_audit("credential.create", actor_kind="user", actor_id=TIM, target_kind="credential",
                             target_id="verify_empty", metadata={"app_id": "app_museum"}, path=path)
    write(path, "UPDATE credentials SET created_by=NULL WHERE id='verify_empty'")
    admin_store.init_db(path)
    assert_key_person(path, "verify_empty", TIM)
    accept(path)
    before = snapshot(path)
    cutover.apply(path)
    assert_applied(path, before)
    for key_id in ("verify_unknown", "verify_empty", new_key):
        assert_key_person(path, key_id, TIM)
    assert_key_person(path, member_key, ODL)
    assert rows(path, "SELECT id,created_by FROM app_trusted_addresses WHERE id IN (?,?) ORDER BY id",
                ("verify_unknown_address", new_address)) == sorted([("verify_unknown_address", TIM), (new_address, TIM)])
    assert rows(path, "SELECT revoked_at IS NOT NULL FROM credentials WHERE id='verify_foreign'") == [(1,)]


@pytest.mark.parametrize("after_apply", [False, True], ids=["pending", "guarded"])
def test_invite_acceptance_records_the_invitee_as_maker(portal, after_apply):
    if after_apply:
        accept(portal.path)
        cutover.apply(portal.path)
    plan = rows(portal.path, "SELECT plan_id FROM account_access WHERE user_id=?", (TIM,))[0][0]
    _, token = accounts.create_link("invite", TIM, plan_id=plan, email="invited@example.test", path=portal.path)
    result = portal.client.post("/auth/api/invite/accept", json={
        "token": token, "email": "invited@example.test", "name": "Invited Person", "password": "scratch-password-long"})
    assert result.status_code == 200, result.text
    person = rows(portal.path, "SELECT id FROM users WHERE email='invited@example.test'")[0][0]
    assert_key_person(portal.path, result.json()["credential"]["id"], person)


@pytest.mark.parametrize("person", [TIM, SIMON, ODL])
@pytest.mark.parametrize("after_apply", [False, True], ids=["pending", "guarded"])
def test_try_session_route_records_its_signed_in_person(portal, person, after_apply):
    if after_apply:
        accept(portal.path)
        cutover.apply(portal.path)
    result = portal.client.post("/me/api/try-key", headers=headers(person), json={})
    assert result.status_code == 201, result.text
    key = result.json()["credential"]
    assert_key_person(portal.path, key["id"], person)
    assert rows(portal.path, "SELECT session_id,expires_at FROM credentials WHERE id=?", (key["id"],)) == [
        (portal.sessions[person].family_id, int(portal.sessions[person].expires_at))]


@pytest.mark.parametrize("after_apply", [False, True], ids=["pending", "guarded"])
def test_mcp_consent_and_token_bind_to_the_person_who_approved(legacy, after_apply):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gateway.admin_oidc import AdminSession
    from gateway.mcp_oauth import McpOAuth, McpOAuthDeps
    from gateway.mcp_oauth_store import Store
    from tests.test_mcp_consent_session import BASE, CALLBACK, VERIFIER, _authorize
    from urllib.parse import parse_qs, urlsplit

    if after_apply:
        accept(legacy)
        cutover.apply(legacy)
    store = Store(lambda: legacy)
    session = AdminSession(subject=SIMON, email="simon@example.test", role="admin", issued_at=time.time(),
                           expires_at=time.time() + 3600, session_id="scratch-mcp-session")
    oauth = McpOAuth(McpOAuthDeps(
        public_base_url=lambda request: BASE, session=lambda request: session,
        person=lambda found: {"id": found.subject, "email": found.email}, check_action=lambda request, found: None,
        person_can_connect=lambda person: True, plan=lambda person: {"name": "Local only"},
        client_ip=lambda request: "192.0.2.5", fetch_client_document=None, store=store))
    app = FastAPI()
    oauth.register(app)
    with closing(TestClient(app, base_url=BASE, follow_redirects=False)) as client:
        request_id = _authorize(client, BASE + "/mcp")
        consent = client.post(f"/me/api/mcp/requests/{request_id}", json={"decision": "allow"})
        assert consent.status_code == 200, consent.text
        query = parse_qs(urlsplit(consent.json()["redirect"]).query)
        grants = store.grants_for_person(SIMON)
        assert len(grants) == 1 and grants[0].person_id == SIMON
        assert store.grants_for_person(TIM) == []
        token = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": query["code"][0],
                                                 "client_id": grants[0].client_id, "redirect_uri": CALLBACK,
                                                 "code_verifier": VERIFIER, "resource": BASE + "/mcp"})
        assert token.status_code == 200, token.text
        connection = oauth.verify_access_token(token.json()["access_token"], BASE + "/mcp")
        assert connection.person_id == SIMON and connection.grant_id == grants[0].id
    accept_digest = preview.digest(body(legacy))
    if not after_apply:
        cutover.accept(legacy, accept_digest, "scratch verifier")
        cutover.apply(legacy)
    assert store.active_grant(grants[0].id).person_id == SIMON


def test_access_plan_upgrade_preserves_recorded_makers_and_does_not_invent_them(tmp_path):
    path = tmp_path / "admin.db"
    build_v6(path)
    old = rows(path, "SELECT id,created_by FROM credentials ORDER BY id")
    admin_store.init_db(path)
    assert rows(path, "SELECT id,created_by FROM credentials ORDER BY id") == old
    # Assigning a service App to the owner is accountability, not proof the owner
    # made its imported key. It must still be unproven in the cut-over preview.
    assert rows(path, "SELECT owner_user_id FROM apps WHERE id='app_daisy'") == [(TIM,)]
    daisy = next(app for app in body(path)["apps"] if app["app_id"] == "app_daisy")
    assert all(not key["proven"] for key in daisy["keys"])


@pytest.mark.parametrize("after_apply", [False, True], ids=["pending", "guarded"])
def test_signin_save_adopts_control_plane_key_for_the_owner(portal, monkeypatch, after_apply):
    import proxy
    from gateway.admin_oidc import CookieConfig, csrf_token_for_session, encode_session_cookie
    from tests.test_signin_settings_api import proposal
    from tests.test_signin_who_can_enter import live_shaped_config

    config = live_shaped_config(issuer="https://sso.example.test", admin_emails=frozenset({"tim@example.test"}))
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", config)
    signing_key = b"s" * 32
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", signing_key)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", CookieConfig(secure=False))
    admin_store.put_operator_setting("signin_rules", {"fallback": "closed", "rules": [
        {"id": "helper", "field": "email", "values": ["simon@example.test"], "admit": True, "admin": True, "plan": None},
    ]}, path=portal.path)
    write(portal.path, "UPDATE admin_role_assignments SET provenance='oidc',last_confirmed_at=? "
                      "WHERE subject_kind='user' AND subject_id=?", (int(time.time()), SIMON))

    def as_person(person):
        session = portal.sessions[person]
        portal.client.cookies.clear()
        portal.client.cookies.set(proxy.ADMIN_AUTH.cookie.name, encode_session_cookie(session, signing_key=signing_key))
        return {**headers(person), "x-azizam-csrf": csrf_token_for_session(session, signing_key=signing_key)}

    minted = portal.client.post("/admin/api/admin-keys", headers=as_person(SIMON), json={"name": "scratch helper", "role": "admin"})
    assert minted.status_code == 201, minted.text
    key_id = minted.json()["credential"]["id"]
    if after_apply:
        accept(portal.path)
        cutover.apply(portal.path)
    owner_headers = as_person(TIM)
    snapshot_response = portal.client.get("/admin/api/signin", headers=owner_headers)
    assert snapshot_response.status_code == 200, snapshot_response.text
    settings = snapshot_response.json()
    assert settings["viewer"]["can_edit"]
    saved = portal.client.put("/admin/api/signin", headers={**owner_headers, "If-Match": settings["revision"]}, json={
        "proposal": proposal(settings, rules={"fallback": "closed", "rules": []}), "test_id": None,
        "keep_admin_keys": [SIMON], "save_for_later": False})
    assert saved.status_code == 200, saved.text
    assert rows(portal.path, "SELECT subject_kind,created_by,revoked_at FROM credentials WHERE id=?", (key_id,)) == [
        ("service_account", "user:" + TIM, None)]
    assert rows(portal.path, "SELECT granted_by FROM admin_role_assignments WHERE subject_kind='service_account' "
                            "AND subject_id=(SELECT subject_id FROM credentials WHERE id=?) AND revoked_at IS NULL", (key_id,)) == [(TIM,)]


@pytest.mark.parametrize("interruption", ["exception", "sigkill"])
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="_backup publishes the restore filename before copying finishes (app_binding_cutover.py:201).")
def test_interrupted_backup_does_not_publish_an_incomplete_backup(disk_legacy, interruption):
    accept(disk_legacy)
    before = snapshot(disk_legacy)
    with child("pause-backup" if interruption == "sigkill" else "fail-backup", disk_legacy) as proc:
        if interruption == "sigkill":
            expect_line(proc, "paused")
            kill_child(proc)
            assert proc.wait(timeout=10) == -signal.SIGKILL
        else:
            assert proc.wait(timeout=10) == 1
    assert snapshot(disk_legacy) == before
    # A backup can still be staged, but a file published with the documented
    # restore name must already be a complete, usable pre-apply database.
    for backup in published_backups(disk_legacy):
        assert snapshot(backup) == before


def test_sigkill_after_commit_leaves_a_complete_migration_and_backup(disk_legacy):
    accept(disk_legacy)
    before = snapshot(disk_legacy)
    with child("pause-report", disk_legacy) as proc:
        expect_line(proc, "paused")
        kill_child(proc)
        assert proc.wait(timeout=10) == -signal.SIGKILL
    assert_applied(disk_legacy, before)
    assert_backup(disk_legacy, before)


def _run_child(mode, path, args):
    if mode == "pause-statement":
        target = int(args[0])

        def pause(n, sql):
            if n == target:
                print("paused", flush=True)
                sys.stdin.readline()

        cutover._connect = lambda p: observed_connect(p, pause)
        cutover.apply(path)
        print("applied", flush=True)
    elif mode in ("pause-backup", "fail-backup"):
        class BackupConnection(sqlite3.Connection):
            def backup(self, target, **kwargs):
                def progress(status, remaining, total):
                    if remaining:
                        if mode == "fail-backup":
                            raise InjectedFailure("interrupted backup")
                        print("paused", flush=True)
                        sys.stdin.readline()

                return super().backup(target, pages=1, progress=progress)

        def connect(p):
            conn = sqlite3.connect(p, isolation_level=None, factory=BackupConnection)
            conn.row_factory = sqlite3.Row
            return conn

        cutover._connect = connect
        cutover.apply(path)
    elif mode == "pause-report":
        def report(*args):
            print("paused", flush=True)
            sys.stdin.readline()
            return []

        cutover._report = report
        cutover.apply(path)
    elif mode == "gateway":
        from contextlib import asynccontextmanager, redirect_stdout
        from fastapi.testclient import TestClient
        with redirect_stdout(sys.stderr):
            import proxy

        proxy.ADMIN_STORE.path = path
        take_lock = proxy.gateway_lifetime.hold

        def hold(p):
            print("requesting-lifetime-lock", flush=True)
            take_lock(p)

        proxy.gateway_lifetime.hold = hold

        @asynccontextmanager
        async def serving(app):
            # Engines/background workers are outside this lock test. This is the
            # actual production lifespan's transition into its serving body.
            yield

        proxy._serve = serving

        print("entering-lifespan", flush=True)
        with TestClient(proxy.app) as client:
            assert client.get("/auth/api/options").status_code == 200
            print("serving", flush=True)
            sys.stdin.readline()
    else:
        raise AssertionError(mode)


if __name__ == "__main__":
    _run_child(sys.argv[1], Path(sys.argv[2]), sys.argv[3:])
