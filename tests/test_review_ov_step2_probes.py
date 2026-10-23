"""Review probes for owner visibility step 2 (feat/owner-visibility-step2 at 960f9db8c).

Each xfail(strict=True) test states the secure behaviour a review finding expects; it is xfail because the
branch does not do it yet, and strict so the test fails loudly once the branch is fixed. The plain tests
prove a claim of the branch on a path its own tests do not cover.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from gateway import accounts, admin_runtime, admin_store, app_binding_cutover, app_binding_guard, app_binding_preview
from tests.support.app_keys import before_the_cut_over
from tests.test_access_plan_migration import build_v6
from tests.test_signin_who_can_enter import OWNER, csrf_for, gateway, signed_in  # noqa: F401  (gateway is a fixture)

REPO_ROOT = Path(__file__).resolve().parents[1]
ACT = {"x-azizam-user-action": "1"}


def _sql(path: Path, statement: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _refused(path: Path, statement: str, params: tuple = ()) -> bool:
    try:
        _sql(path, statement, params)
    except sqlite3.IntegrityError:
        return True
    return False


def _person(path: Path, email: str) -> dict:
    return admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email=email, display_name=email, path=path)


def _digest(path: Path) -> str:
    conn = app_binding_preview.connect_read_only(path)
    try:
        return app_binding_preview.digest(app_binding_preview.build(conn, as_of=int(time.time())))
    finally:
        conn.close()


# --- Finding 1: keeping a key someone else minted gives that key the member's identity -------------------

def test_a_member_cannot_keep_a_key_the_audit_log_shows_someone_else_minted(gateway):
    signed_in(gateway, sub="owner", email=OWNER)
    owner_id = gateway.user(OWNER)["id"]
    gateway.set_rules([], fallback="open")
    member, _ = signed_in(gateway, sub="desk", email="desk@example.test")
    member_id = gateway.user("desk@example.test")["id"]
    before_the_cut_over(gateway.path)
    app = admin_store.create_app("Laptop", kind="personal", owner_user_id=member_id, access_plan="person", path=gateway.path)
    # The owner minted this key on the member's personal App under the old owner-or-accountable rule, so the
    # owner has seen its secret. The audit log says so, and the back-fill records it as foreign ("").
    key_id = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=f"sha256:{uuid.uuid4().hex}",
        prefix="opk_owner_minted", created_by=None, path=gateway.path,
    )["id"]
    admin_store.record_audit("credential.create", actor_kind="user", actor_id=owner_id, target_kind="credential",
                             target_id=key_id, metadata={"app_id": app["id"]}, path=gateway.path)
    admin_store.init_db(gateway.path)  # pre-cut-over start: the back-fill runs
    credential = admin_store.get_credential(key_id, path=gateway.path)
    assert credential["created_by"] == ""
    assert admin_runtime.person_for_app_credential(app, credential) is None  # no saved prompts today

    kept = member.post(f"/me/api/credentials/{key_id}/adopt", headers={**csrf_for(member), **ACT})

    credential = admin_store.get_credential(key_id, path=gateway.path)
    # Secure: the keep is refused (or replaced by a re-issue), so the owner's copy of the secret never
    # becomes the member's identity. On the branch: 200, and the key now reads the member's saved prompts.
    assert kept.status_code != 200, kept.text
    assert admin_runtime.person_for_app_credential(app, credential) is None


# --- Finding 2: apply's "the gateway is running" checks miss a running gateway ---------------------------

def _gateway_process(path: Path, env: dict[str, str]) -> subprocess.Popen:
    """A real second process that holds the database's lifetime lock the way the gateway does, until its stdin closes."""
    script = ("import sys\nfrom pathlib import Path\nfrom gateway import gateway_lifetime\n"
              "gateway_lifetime.hold(Path(sys.argv[1]))\nprint('held', flush=True)\nsys.stdin.read()\n")
    proc = subprocess.Popen([sys.executable, "-c", script, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, cwd=REPO_ROOT, env=env)
    assert proc.stdout.readline().strip() == "held", "the gateway process did not take its lifetime lock"
    return proc


def _stop(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    proc.wait(timeout=30)


def test_apply_refuses_while_a_gateway_process_holds_its_lock_even_when_its_private_directory_is_elsewhere(tmp_path, monkeypatch):
    path = tmp_path / "data" / "admin.db"
    path.parent.mkdir()
    admin_store.init_db(path)
    accounts.initialize(path)
    before_the_cut_over(path)
    owner = _person(path, "owner@example.test")
    desk = admin_store.create_app("Desk", kind="service", owner_user_id=owner["id"], access_plan="person", path=path)["id"]
    admin_store.create_credential_hash(subject_kind="app", subject_id=desk, app_id=desk, hash=f"sha256:{uuid.uuid4().hex}",
                                       prefix="opk_test", created_by=owner["id"], path=path)
    # The gateway runs with AZIZAM_PRIVATE_TMPDIR set (start.sh sources .env.local; the Docker image sets it).
    gateway_env = {**os.environ, "AZIZAM_PRIVATE_TMPDIR": str(tmp_path / "elsewhere" / "private-tmp")}
    # The operator's shell does not carry the gateway's environment.
    monkeypatch.delenv("AZIZAM_PRIVATE_TMPDIR", raising=False)
    gateway = _gateway_process(path, gateway_env)
    try:
        assert app_binding_cutover.main(["accept", "--admin-db", str(path), "--digest", _digest(path), "--by", "t"]) == 0
        code = app_binding_cutover.main(["apply", "--admin-db", str(path)])
    finally:
        _stop(gateway)
    assert code != 0


def test_an_idle_running_gateway_holds_no_store_lock_so_the_open_database_check_cannot_see_it(gateway):
    """Proves the second apply safeguard is per-connection only: between requests the gateway holds no lock."""
    client = gateway.client()
    assert client.get("/auth/api/options").status_code == 200
    handle = app_binding_cutover._hold_store_lock(gateway.path)  # would raise CutoverError if the gateway held it
    handle.close()


# --- Finding 3: guard gaps for direct SQL --------------------------------------------------------------

@pytest.fixture
def guarded(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    alice, bob = _person(path, "alice@example.test"), _person(path, "bob@example.test")
    alice_app = admin_store.create_app("Alice", kind="personal", owner_user_id=alice["id"], access_plan="person", path=path)["id"]
    bob_app = admin_store.create_app("Bob", kind="personal", owner_user_id=bob["id"], access_plan="person", path=path)["id"]
    # A self-service key, made the way mint_user_app_key makes it: subject_id only, app_id NULL (no FK).
    key = admin_store.create_credential_hash(subject_kind="app", subject_id=alice_app, hash=f"sha256:{uuid.uuid4().hex}",
                                             prefix="opk_alice", created_by=alice["id"], path=path)["id"]
    return path, alice, bob, alice_app, bob_app, key


def test_an_app_id_swap_cannot_move_a_key_to_another_person(guarded):
    """Not a finding: the credentials.app_id foreign key refuses the rename (create_credential_hash fills app_id)."""
    path, _alice, _bob, alice_app, _bob_app, _key = guarded
    assert _refused(path, "UPDATE apps SET id = 'app_parked' WHERE id = ?", (alice_app,))


@pytest.mark.xfail(strict=True, reason="review finding 3b: an App key's hash can be rewritten (a new secret with no provenance)")
def test_an_app_keys_secret_cannot_be_rewritten_in_place(guarded):
    path, _alice, _bob, _alice_app, _bob_app, key = guarded
    assert _refused(path, "UPDATE credentials SET hash = 'sha256:attacker' WHERE id = ?", (key,))


# --- Finding 6: the /me address route skips the disabled-App check that key minting has -----------------

def test_a_disabled_app_takes_no_new_trusted_address_from_me(gateway):
    gateway.set_rules([], fallback="open")
    member, _ = signed_in(gateway, sub="desk", email="desk@example.test")
    headers = {**csrf_for(member), **ACT}
    app_id = member.post("/me/api/apps", headers=headers, json={"name": "Desk"}).json()["app"]["id"]
    admin_store.disable_app(app_id, path=gateway.path)
    assert member.post(f"/me/api/apps/{app_id}/keys", headers=headers, json={}).status_code == 403

    added = member.post(f"/me/api/apps/{app_id}/trusted-addresses", headers=headers, json={"address": "10.0.0.9"})

    assert added.status_code == 403, added.text


# --- Claim proven: schema v6 -> current -> accept -> apply -> restart ------------------------------------

def test_a_schema_v6_database_upgrades_cuts_over_and_stays_guarded(tmp_path, monkeypatch):
    monkeypatch.delenv("AZIZAM_PRIVATE_TMPDIR", raising=False)
    path = tmp_path / "admin.db"
    build_v6(path)
    admin_store.init_db(path)
    names = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert not ({name for name, _ in app_binding_guard.GUARDS} & names)
    _sql(path, "UPDATE credentials SET last_used_at = NULL")  # no App blocks on recent use
    unproven_before = {r[0] for r in sqlite3.connect(path).execute(
        "SELECT c.id FROM credentials c JOIN apps a ON a.id = COALESCE(NULLIF(c.app_id, ''), c.subject_id) "
        "WHERE c.subject_kind = 'app' AND c.revoked_at IS NULL AND c.created_by IS NOT a.owner_user_id")}

    assert app_binding_cutover.main(["accept", "--admin-db", str(path), "--digest", _digest(path), "--by", "t"]) == 0
    assert app_binding_cutover.main(["apply", "--admin-db", str(path)]) == 0
    admin_store.init_db(path)

    conn = sqlite3.connect(path)
    try:
        assert app_binding_guard.verify(conn) == []
        assert app_binding_guard.recorded(conn)
        live_unproven = conn.execute(
            "SELECT COUNT(*) FROM credentials c JOIN apps a ON a.id = COALESCE(NULLIF(c.app_id, ''), c.subject_id) "
            "WHERE c.subject_kind = 'app' AND c.revoked_at IS NULL AND c.created_by IS NOT a.owner_user_id").fetchone()[0]
        revoked = {r[0] for r in conn.execute("SELECT id FROM credentials WHERE revoked_at IS NOT NULL")}
    finally:
        conn.close()
    assert live_unproven == 0
    assert unproven_before <= revoked
