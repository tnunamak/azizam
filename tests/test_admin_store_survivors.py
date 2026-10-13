"""Cases that kill the admin_store auth-scope survivors found by the plans-v4 re-baseline (lane MUT r3).

Each test names the mutants it kills in its docstring (ID prefix: gateway/admin_store.py, line numbers of
the sweep on main a9e8f43d; the trusted-networks merge moved them).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager

import pytest

from gateway import admin_store as store


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    store.init_db(path)
    return path


def rows(db, sql, *params):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]


def run(db, sql, *params):
    with sqlite3.connect(db) as conn:
        conn.execute(sql, params)


def make_app(db, name="a"):
    owner = store.upsert_oidc_user("i", "owner-" + name, path=db)["id"]
    app = store.create_app(name, kind="personal", owner_user_id=owner, access_plan="person", path=db)["id"]
    return owner, app


def add_key(db, app_id, digest):
    return store.create_credential_hash(
        subject_kind="app", subject_id=app_id, app_id=app_id, hash=digest, prefix="k", path=db)["id"]


def claim(db):
    return store.claim_first_local_owner(
        issuer="i", subject="fresh", email="o@example.com", display_name="Owner",
        verifier="verifier", granted_by="test", path=db)


def access_rows(db, user_id):
    return rows(db, "SELECT * FROM account_access WHERE user_id = ?", user_id)


def test_signing_in_creates_or_repairs_the_persons_access_row(db):
    """:780 repair on sign-in of an existing user."""
    user = store.upsert_oidc_user("i", "s", path=db)["id"]
    assert len(access_rows(db, user)) == 1
    run(db, "DELETE FROM account_access WHERE user_id = ?", user)
    store.upsert_oidc_user("i", "s", path=db)
    assert len(access_rows(db, user)) == 1


def test_claiming_the_first_owner_creates_or_repairs_the_access_row(db):
    """:939 new user; :949 existing user."""
    fresh = claim(db)
    assert fresh is not None
    uid = rows(db, "SELECT id FROM users WHERE subject = 'fresh'")[0]["id"]
    assert len(access_rows(db, uid)) == 1
    run(db, "DELETE FROM admin_role_assignments")
    run(db, "DELETE FROM account_access WHERE user_id = ?", uid)
    assert claim(db) is not None
    assert len(access_rows(db, uid)) == 1


def test_a_missing_email_never_matches_a_user_whose_email_is_a_placeholder(db):
    """:830 str-const ''->'XXXX' (an absent email must not become a search term)."""
    store.upsert_oidc_user("i", "s", email="xxxx", path=db)
    assert store.find_user_by_email(None, path=db) is None
    assert store.find_user_by_email("", path=db) is None


def test_touch_debounce_boundaries(db):
    """:1813 max(0, ...); :1815 default 0 and '>' ; :1816 returns False; :1827 rowcount > 0."""
    store._LAST_TOUCH.clear()
    _, app = make_app(db)
    cred = add_key(db, app, "h1")
    assert store.touch_credential_last_used(cred, now=100, min_interval=0, path=db) is True
    # Same second again with no interval: the stored touch equals the cutoff, so it is not debounced.
    assert store.touch_credential_last_used(cred, now=100, min_interval=0, path=db) is True
    # Negative intervals count as zero.
    assert store.touch_credential_last_used(cred, now=101, min_interval=-50, path=db) is True
    # A remembered touch at exactly the cutoff is not newer than it.
    assert store.touch_credential_last_used(cred, now=111, min_interval=10, path=db) is True
    # Inside the interval the write is skipped, and the answer is the literal False.
    assert store.touch_credential_last_used(cred, now=115, min_interval=10, path=db) is False
    # An unknown credential updates no row.
    assert store.touch_credential_last_used("ghost", now=500, min_interval=0, path=db) is False


def test_touch_with_a_cutoff_of_zero_is_not_debounced_by_the_empty_memory(db):
    """:1815 int-const 0->1 (default of the remembered touch)."""
    store._LAST_TOUCH.clear()
    _, app = make_app(db)
    cred = add_key(db, app, "h2")
    assert store.touch_credential_last_used(cred, now=10, min_interval=10, path=db) is True
    assert rows(db, "SELECT last_used_at FROM credentials WHERE id = ?", cred)[0]["last_used_at"] == 10


def test_the_touch_memory_is_cleared_only_when_it_reaches_its_cap(db, monkeypatch):
    """:1824 cond-negated and '>=' ; :1825 clear."""
    store._LAST_TOUCH.clear()
    monkeypatch.setattr(store, "_LAST_TOUCH_MAX", 2)
    store.touch_credential_last_used("c1", now=1, min_interval=0, path=db)
    store.touch_credential_last_used("c2", now=1, min_interval=0, path=db)
    assert len(store._LAST_TOUCH) == 2
    store.touch_credential_last_used("c3", now=1, min_interval=0, path=db)
    assert list(store._LAST_TOUCH) == [(str(db), "c3")]
    store._LAST_TOUCH.clear()


def test_runtime_credential_lookup_reports_session_app_membership(db):
    """:1858 connection choice; :1864 missing row; :1867 'on_session_app' key; :1868 return."""
    owner, app = make_app(db)
    cred = add_key(db, app, "h3")
    found = store.find_runtime_credential_by_hash("h3", path=db)
    assert found is not None and found["id"] == cred and found["on_session_app"] is False
    assert store.find_runtime_credential_by_hash("nope", path=db) is None
    run(db, "INSERT INTO session_apps(user_id, app_id) VALUES (?, ?)", owner, app)
    assert store.find_runtime_credential_by_hash("h3", path=db)["on_session_app"] is True
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        assert store.find_runtime_credential_by_hash("h3", connection=conn)["id"] == cred


def test_runtime_access_row_resolves_by_key_or_by_app_and_needs_one_of_them(db):
    """:1908 message; :1912 'on_session_app' key (by key and by app)."""
    owner, app = make_app(db)
    add_key(db, app, "h4")
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError) as raised:
            store.runtime_access_row(conn)
        assert str(raised.value) == "key_hash or app_id is required"
        assert store.runtime_access_row(conn, key_hash="h4")["on_session_app"] is False
        assert store.runtime_access_row(conn, app_id=app)["on_session_app"] is False
        assert store.runtime_access_row(conn, app_id="ghost") is None
        conn.execute("INSERT INTO session_apps(user_id, app_id) VALUES (?, ?)", (owner, app))
        assert store.runtime_access_row(conn, app_id=app)["on_session_app"] is True
        assert store.runtime_access_rows(conn, [app])[app]["on_session_app"] is True


def _in_list_widths(statements):
    return [len(m.split(",")) for s in statements for m in re.findall(r"app_id IN \(([^)]*)\)", s)]


def test_runtime_access_rows_reads_at_most_500_apps_per_query_and_misses_none(db):
    """:1920 and :1921 500->501."""
    _, app = make_app(db)
    ids = [f"{n:04d}" for n in range(500)] + [app]
    assert sorted(ids)[500] == app
    seen: list[str] = []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(seen.append)
        assert list(store.runtime_access_rows(conn, ids)) == [app]
    assert sorted(_in_list_widths(seen)) == [1, 500]


def test_active_credentials_for_apps_reads_at_most_500_apps_per_query_and_misses_none(db, monkeypatch):
    """:1973 and :1974 500->501."""
    _, app = make_app(db)
    add_key(db, app, "h5")
    ids = [f"{n:04d}" for n in range(500)] + [app]
    seen: list[str] = []
    real = store._conn

    @contextmanager
    def traced(path=store.DB_PATH):
        with real(path) as conn:
            conn.set_trace_callback(seen.append)
            yield conn

    monkeypatch.setattr(store, "_conn", traced)
    out = store.list_active_credentials_for_apps(ids, path=db)
    assert list(out) == [app] and len(out[app]) == 1
    assert sorted(_in_list_widths(seen)) == [1, 500]


def test_revoking_an_admin_role_without_an_actor_is_audited_as_the_system(db):
    """:2134 default actor_kind 'system'."""
    user = store.upsert_oidc_user("i", "adm", path=db)["id"]
    grant = store.assign_admin_role(role="admin", subject_kind="user", subject_id=user, now=1, path=db)
    assert store.revoke_admin_role(grant["id"], now=2, path=db) is True
    audited = rows(db, "SELECT actor_kind FROM audit_events WHERE action = 'admin_role.revoke'")
    assert audited == [{"actor_kind": "system"}]


def test_the_legacy_try_key_migration_audits_what_it_revoked(db):
    """:551 'credential' target kind (563), :552 'reason' and 'unbound_try_key' (564)."""
    import json
    owner, app = make_app(db, name="Try")
    cred = store.create_credential_hash(subject_kind="app", subject_id=app, app_id=app, hash="try-h", prefix="t",
                                        display_name=store.TRY_KEY_LABEL, path=db)["id"]
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        store._revoke_unbound_try_keys(conn)
        conn.commit()
    assert rows(db, "SELECT revoked_at FROM credentials WHERE id = ?", cred)[0]["revoked_at"] is not None
    (event,) = rows(db, "SELECT target_kind, metadata_json FROM audit_events WHERE action = 'credential.try_session_revoke'")
    assert event["target_kind"] == "credential"
    assert json.loads(event["metadata_json"]) == {"credential_ids": [cred], "reason": "unbound_try_key"}
