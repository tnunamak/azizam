"""Owner visibility step 2: the App-binding preview is read-only and says exactly what the cut-over would do.

The preview runs on a copy of admin.db. These tests build real databases through ``init_db``; a database
that predates the guards is simulated by dropping them, which is what the upgrade leaves behind.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import accounts, admin_store, app_binding_guard, app_binding_preview  # noqa: E402
from tests.support.app_keys import unguarded  # noqa: E402
from tests.test_access_plan_migration import build_v6  # noqa: E402

AS_OF = 1_800_000_000
DAY = 86_400


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def _person(path: Path, email: str) -> dict:
    return admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email=email, display_name=email, path=path)


def _service_app(path: Path, owner: dict, name: str = "Desk") -> dict:
    return admin_store.create_app(name, kind="service", owner_user_id=owner["id"], access_plan="person", path=path)


def _unguarded(path: Path) -> None:
    unguarded(path)


def _insert_key(path: Path, app_id: str, *, created_by: str | None, last_used_at: int | None = None, prefix: str = "opk_x") -> str:
    key_id = f"cred_{uuid.uuid4().hex[:10]}"
    _sql(path, "INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at, created_by, last_used_at) "
               "VALUES (?, 'app', ?, ?, ?, ?, 1, ?, ?)",
         (key_id, app_id, app_id, f"sha256:{key_id}", prefix, created_by, last_used_at))
    return key_id


def _insert_address(path: Path, app_id: str, *, created_by: str | None, address: str = "10.0.0.9") -> str:
    entry_id = f"tad_{uuid.uuid4().hex[:10]}"
    _sql(path, "INSERT INTO app_trusted_addresses(id, app_id, address, created_at, created_by) VALUES (?, ?, ?, 1, ?)",
         (entry_id, app_id, address, created_by))
    return entry_id


def _sql(path: Path, statement: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(path)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _preview(path: Path, *extra: str) -> tuple[int, str]:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = app_binding_preview.main(["--admin-db", str(path), "--as-of", str(AS_OF), *extra])
    return code, out.getvalue()


def _json(path: Path) -> dict:
    code, text = _preview(path, "--json")
    assert code == 0, text
    return json.loads(text)


def _app_block(body: dict, app_id: str) -> dict:
    return next(app for app in body["apps"] if app["app_id"] == app_id)


def test_the_preview_writes_nothing_to_the_database(db):
    owner = _person(db, "owner@example.test")
    _service_app(db, owner)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    code, _ = _preview(db)

    assert code == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_a_fresh_database_shows_every_guard_and_its_version_row(db):
    body = _json(db)
    assert body["schema_version"] == admin_store.SCHEMA_VERSION
    assert len(body["guards_installed"]) == 10
    assert body["ownership_version_recorded"] is True


def test_a_key_and_address_the_person_issued_are_kept(db):
    owner = _person(db, "owner@example.test")
    app = _service_app(db, owner)
    key = admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"],
                                             hash="sha256:kept", prefix="opk_kept", created_by=owner["id"], path=db)
    _insert_address(db, app["id"], created_by=owner["id"])

    block = _app_block(_json(db), app["id"])

    assert [(k["id"], k["proven"], k["action"]) for k in block["keys"]] == [(key["id"], True, "keep")]
    assert [(t["proven"], t["action"]) for t in block["trusted_addresses"]] == [(True, "keep")]
    assert block["surviving_bindings"] == 2
    assert block["blocks_apply"] == []


def test_unproven_keys_and_addresses_are_listed_for_revocation_or_removal(db):
    owner, stranger = _person(db, "owner@example.test"), _person(db, "stranger@example.test")
    app = _service_app(db, owner)
    _unguarded(db)
    no_creator = _insert_key(db, app["id"], created_by=None, prefix="opk_null")
    empty_creator = _insert_key(db, app["id"], created_by="", prefix="opk_empty")
    foreign = _insert_key(db, app["id"], created_by=stranger["id"], prefix="opk_foreign")
    unproven_address = _insert_address(db, app["id"], created_by=None)

    body = _json(db)
    block = _app_block(body, app["id"])

    assert {k["id"]: k["action"] for k in block["keys"]} == {no_creator: "revoke", empty_creator: "revoke", foreign: "revoke"}
    assert {t["id"]: t["action"] for t in block["trusted_addresses"]} == {unproven_address: "remove"}
    assert block["would_lose_all_bindings"] is True
    assert body["summary"]["keys_to_revoke"] == 3
    assert body["summary"]["addresses_to_remove"] == 1


def test_an_app_used_in_the_last_thirty_days_that_would_lose_all_bindings_blocks_apply(db):
    owner = _person(db, "owner@example.test")
    app = _service_app(db, owner)
    _unguarded(db)
    _insert_key(db, app["id"], created_by=None, last_used_at=AS_OF - 2 * DAY)

    body = _json(db)

    assert app["id"] in body["summary"]["apps_blocking_apply"]
    assert any("used in the last 30 days" in reason for reason in _app_block(body, app["id"])["blocks_apply"])


def test_an_app_that_is_not_used_lately_is_listed_but_not_blocked(db):
    owner = _person(db, "owner@example.test")
    app = _service_app(db, owner)
    _unguarded(db)
    _insert_key(db, app["id"], created_by=None, last_used_at=AS_OF - 90 * DAY)

    body = _json(db)

    assert _app_block(body, app["id"])["would_lose_all_bindings"] is True
    assert app["id"] not in body["summary"]["apps_blocking_apply"]


def test_an_app_whose_only_binding_is_an_address_is_blocked_while_its_use_history_is_unavailable(db):
    owner = _person(db, "owner@example.test")
    app = _service_app(db, owner)
    _unguarded(db)
    _insert_address(db, app["id"], created_by=None)

    body = _json(db)
    block = _app_block(body, app["id"])

    assert block["trusted_addresses"][0]["use_history"] == "unavailable"
    assert any("use history is unavailable" in reason for reason in block["blocks_apply"])
    assert app["id"] in body["summary"]["apps_blocking_apply"]


def test_session_apps_are_listed_and_marked_and_hold_no_bindings(db):
    # The schema refuses a credential on a session App, so the preview lists it with nothing to classify.
    owner = _person(db, "owner@example.test")
    session_app = admin_store.ensure_session_app_for_user(owner["id"], name="Azizam", path=db)

    body = _json(db)

    listed = next(app for app in body["apps"] if app["app_id"] == session_app["id"])
    assert listed["session_app"] is True
    assert listed["keys"] == [] and listed["trusted_addresses"] == []
    assert body["summary"]["session_apps"] == 1


def test_the_digest_ignores_the_clock_and_the_last_use_time_of_a_key_that_is_not_decisive(db):
    owner = _person(db, "owner@example.test")
    app = _service_app(db, owner)
    _insert_key(db, app["id"], created_by=owner["id"])  # a proven key keeps the App bound at any time
    _unguarded(db)
    _insert_key(db, app["id"], created_by=None, last_used_at=AS_OF - 100)

    def digest_at(as_of: int, last_used_at: int | None) -> str:
        _sql(db, "UPDATE credentials SET last_used_at = ? WHERE created_by IS NULL", (last_used_at,))
        conn = app_binding_preview.connect_read_only(db)
        try:
            return app_binding_preview.digest(app_binding_preview.build(conn, as_of=as_of))
        finally:
            conn.close()

    assert digest_at(AS_OF, AS_OF - 100) == digest_at(AS_OF + 400 * DAY, None)


def test_the_digest_is_stable_and_matches_the_body(db):
    owner = _person(db, "owner@example.test")
    _service_app(db, owner)

    first, second = _json(db), _json(db)

    assert first["digest"] == second["digest"]
    body = {k: v for k, v in first.items() if k != "digest"}
    assert first["digest"] == app_binding_preview.digest(body)


def test_the_preview_says_what_it_does_not_evaluate(db):
    body = _json(db)
    assert any("Q2" in note for note in body["not_evaluated"])


def test_a_database_before_the_access_plan_migration_is_refused_and_not_migrated(tmp_path):
    path = tmp_path / "admin.db"
    build_v6(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    code, _ = _preview(path)

    assert code == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sqlite3.connect(path).execute("SELECT version FROM schema_version").fetchone()[0] == 6


def test_a_missing_database_is_refused_and_no_file_is_created(tmp_path):
    path = tmp_path / "absent.db"
    code, _ = _preview(path)
    assert code == 1
    assert not path.exists()


def test_a_personal_app_another_person_made_is_shown_and_kept_personal_by_default(db):
    owner, maker = _person(db, "owner@example.test"), _person(db, "maker@example.test")
    app = admin_store.create_app("Desk", kind="personal", owner_user_id=owner["id"], access_plan="person", path=db)
    _sql(db, "INSERT INTO audit_events(id, created_at, actor_kind, actor_id, action, target_kind, target_id) "
             "VALUES ('aud_made', 1, 'user', ?, 'app.create', 'app', ?)", (maker["id"], app["id"]))

    code, text = _preview(db)

    assert code == 0
    assert f"created by {maker['id']} (audit): kept personal by default" in text
