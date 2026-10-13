"""Pins the credential, local-owner and admin-role paths of gateway/admin_store.py.

Each case names a fault the mutation sweep (scripts/mutate) could not see: a write that never
reached the table, an audit row that was never written, a guard that never refused.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from gateway import admin_store as store


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    store.init_db(path)
    return path


def make_app(db, name="a"):
    owner = store.upsert_oidc_user("i", "owner-" + name, path=db)["id"]
    return store.create_app(name, kind="personal", owner_user_id=owner, access_plan="person", path=db)["id"]


def rows(db, sql, *params):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]


def test_a_user_is_created_by_issuer_and_subject_and_found_by_email_within_an_issuer(db):
    with pytest.raises(ValueError, match=r"\Aissuer\ and\ subject\ are\ required\Z"):
        store.upsert_oidc_user("", "s", path=db)
    with pytest.raises(ValueError, match=r"\Aissuer\ and\ subject\ are\ required\Z"):
        store.upsert_oidc_user("i", "", path=db)
    a = store.upsert_oidc_user("issuer-a", "s1", email="Mia@Example.com", now=10, path=db)
    b = store.upsert_oidc_user("issuer-b", "s1", email="mia@example.com", now=20, path=db)
    assert a["id"].startswith("usr") and a["id"] != b["id"]
    assert store.find_user_by_email(" MIA@example.com ", path=db)["id"] == a["id"]       # oldest first
    assert store.find_user_by_email("mia@example.com", issuer="issuer-b", path=db)["id"] == b["id"]
    assert store.find_user_by_email("mia@example.com", issuer="nobody", path=db) is None
    assert store.find_user_by_email("", path=db) is None and store.find_user_by_email(None, path=db) is None
    again = store.upsert_oidc_user("issuer-a", "s1", display_name="Mia", now=30, path=db)
    assert again["id"] == a["id"] and again["display_name"] == "Mia" and again["last_login_at"] == 30
    assert again["email"] == "Mia@Example.com"   # a missing email never blanks the stored one


def test_a_local_password_verifier_is_stored_replaced_read_and_deleted(db):
    user = store.upsert_oidc_user("local", "owner", path=db, now=1)["id"]
    for bad_user, bad_verifier, message in (("", "v", "user_id is required"), (user, "", "verifier is required"),
                                            (user, 7, "verifier is required")):
        with pytest.raises(ValueError, match=r"\A" + re.escape(message) + r"\Z"):
            store.set_local_password_verifier(bad_user, bad_verifier, path=db)
    first = store.set_local_password_verifier(user, "v1", now=100, path=db)
    assert (first["verifier"], first["created_at"], first["updated_at"]) == ("v1", 100, 100)
    second = store.set_local_password_verifier(user, "v2", now=200, path=db)
    assert (second["verifier"], second["created_at"], second["updated_at"]) == ("v2", 100, 200)
    assert len(rows(db, "SELECT * FROM local_password_credentials")) == 1
    assert store.get_local_password_verifier(user, path=db)["verifier"] == "v2"
    assert store.get_local_password_verifier("", path=db) is None
    assert store.delete_local_password_verifier("", path=db) is False
    assert store.delete_local_password_verifier("ghost", path=db) is False
    assert store.delete_local_password_verifier(user, path=db) is True
    assert rows(db, "SELECT * FROM local_password_credentials") == []
    assert store.delete_local_password_verifier(user, path=db) is False


def test_the_first_local_owner_claim_writes_user_password_role_and_audit_once(db):
    for kwargs, message in (
        (dict(issuer="", subject="s", verifier="v"), "issuer and subject are required"),
        (dict(issuer="i", subject="", verifier="v"), "issuer and subject are required"),
        (dict(issuer="i", subject="s", verifier=""), "verifier is required"),
        (dict(issuer="i", subject="s", verifier=3), "verifier is required"),
    ):
        with pytest.raises(ValueError, match=r"\A" + re.escape(message) + r"\Z"):
            store.claim_first_local_owner(granted_by="setup", email=None, display_name=None, path=db, **kwargs)
    claimed = store.claim_first_local_owner(issuer="local", subject="owner", verifier="vv", granted_by="setup",
                                            email="o@x.io", display_name="Olive", now=500, path=db)
    assert claimed["id"].startswith("usr") and claimed["email"] == "o@x.io" and claimed["display_name"] == "Olive"
    uid = claimed["id"]
    assert rows(db, "SELECT user_id, verifier, created_at FROM local_password_credentials") == [
        {"user_id": uid, "verifier": "vv", "created_at": 500}]
    assert rows(db, "SELECT role, subject_kind, subject_id, scope_app_id, granted_by FROM admin_role_assignments") == [
        {"role": "owner", "subject_kind": "user", "subject_id": uid, "scope_app_id": None, "granted_by": "setup"}]
    audit = store.list_audit(path=db)
    assert [(e["action"], e["actor_kind"], e["actor_id"], e["actor_label"], e["target_kind"], e["target_id"])
            for e in audit] == [("setup.local_owner_create", "user", uid, "Olive", "user", uid)]
    assert audit[0]["metadata"] == {"email": "o@x.io", "via": "setup_page"}
    assert store.claim_first_local_owner(issuer="local", subject="other", verifier="x", granted_by="setup", email=None, display_name=None, path=db) is None
    assert len(rows(db, "SELECT * FROM users")) == 1 and len(rows(db, "SELECT * FROM audit_events")) == 1


def test_claiming_for_an_existing_user_keeps_its_id_and_fills_only_given_fields(db):
    existing = store.upsert_oidc_user("local", "owner", email="old@x.io", display_name="Old", now=1, path=db)
    claimed = store.claim_first_local_owner(issuer="local", subject="owner", verifier="v", granted_by="setup",
                                            email=None, display_name="New", now=9, path=db)
    assert claimed["id"] == existing["id"]
    assert (claimed["email"], claimed["display_name"], claimed["last_login_at"]) == ("old@x.io", "New", 9)
    audit = store.list_audit(path=db)[0]
    assert audit["actor_label"] == "New"


def test_audit_labels_fall_back_to_email_then_to_the_user_id(db):
    claimed = store.claim_first_local_owner(issuer="l", subject="a", verifier="v", granted_by="g", email="e@x.io", display_name=None, path=db)
    assert store.list_audit(path=db)[0]["actor_label"] == "e@x.io"


def test_a_failed_claim_leaves_nothing_behind_when_an_owner_already_exists(db):
    store.claim_first_local_owner(issuer="l", subject="a", verifier="v", granted_by="g", email=None, display_name=None, path=db)
    before = {t: rows(db, f"SELECT * FROM {t}") for t in ("users", "local_password_credentials", "admin_role_assignments", "audit_events")}
    assert store.claim_first_local_owner(issuer="l", subject="b", verifier="v", granted_by="g", email=None, display_name=None, path=db) is None
    assert {t: rows(db, f"SELECT * FROM {t}") for t in before} == before


# --- credentials -----------------------------------------------------------------------------

def test_a_credential_needs_a_subject_hash_and_prefix_and_binds_data_plane_keys_to_their_app(db):
    with pytest.raises(ValueError, match=r"\Asubject_id\ is\ required\Z"):
        store.create_credential_hash(subject_kind="app", subject_id="", hash="h", prefix="p", path=db)
    for kwargs in (dict(hash="", prefix="p"), dict(hash="h", prefix="")):
        with pytest.raises(ValueError, match=r"\Ahash\ and\ prefix\ are\ required\Z"):
            store.create_credential_hash(subject_kind="app", subject_id="app1", path=db, **kwargs)
    app1 = make_app(db, "one")
    with pytest.raises(ValueError, match=r"\Adata\-plane\ credentials\ must\ have\ app_id\ ==\ subject_id\Z"):
        store.create_credential_hash(subject_kind="app", subject_id=app1, app_id="app2", hash="h", prefix="p", path=db)
    with pytest.raises(ValueError, match="admin credentials must not be bound to an app_id"):
        store.create_credential_hash(subject_kind="user", subject_id="u1", app_id=app1, hash="h", prefix="p", path=db)
    created = store.create_credential_hash(subject_kind="app", subject_id=app1, hash="h", prefix="p",
                                           scopes=["b", "a", "", "a"], now=7, path=db)
    assert created["app_id"] == app1
    assert rows(db, "SELECT scopes_json FROM credentials") == [{"scopes_json": '["a", "b"]'}]


def test_a_signed_in_app_cannot_hold_an_api_key(db):
    app = make_app(db, "sess")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO session_apps(user_id, app_id) VALUES ('u-sess', ?)", (app,))
    with pytest.raises(ValueError, match=r"\Athis\ App\ is\ a\ person's\ signed\-in\ App\ and\ cannot\ hold\ API\ keys\Z"):
        store.create_credential_hash(subject_kind="app", subject_id=app, hash="h", prefix="p", path=db)


# --- admin roles -----------------------------------------------------------------------------

def test_granting_an_admin_role_validates_its_inputs_and_records_provenance(db):
    with pytest.raises(ValueError, match="admin roles can only be granted to"):
        store.assign_admin_role(role="owner", subject_kind="app", subject_id="x", path=db)
    for role, subject in (("", "u"), ("owner", "")):
        with pytest.raises(ValueError, match=r"\Arole\ and\ subject_id\ are\ required\Z"):
            store.assign_admin_role(role=role, subject_kind="user", subject_id=subject, path=db)
    with pytest.raises(ValueError, match=r"\Aprovenance\ must\ be\ manual\ or\ oidc\Z"):
        store.assign_admin_role(role="owner", subject_kind="user", subject_id="u", provenance="other", path=db)
    granted = store.assign_admin_role(role="admin", subject_kind="user", subject_id="u1", granted_by="boss", now=5, path=db)
    assert (granted["role"], granted["provenance"], granted["granted_by"], granted["granted_at"], granted["revoked_at"]) == (
        "admin", "manual", "boss", 5, None)
    audit = store.list_audit(path=db)[0]
    assert audit["action"] == "admin_role.grant" and audit["target_kind"] == "admin_role_assignment"
    assert audit["actor_kind"] == "system" and audit["actor_id"] == "boss"
    assert audit["metadata"] == {"role": "admin", "subject_kind": "user", "subject_id": "u1",
                                 "scope_app_id": None, "provenance": "manual"}


def test_a_regrant_reopens_a_revoked_role_and_a_claim_never_overwrites_a_manual_grant(db):
    manual = store.assign_admin_role(role="admin", subject_kind="user", subject_id="u1", now=1, path=db)
    claim = store.assign_admin_role(role="admin", subject_kind="user", subject_id="u1", provenance="oidc", now=2, path=db)
    assert claim["provenance"] == "manual" and claim["granted_at"] == 1 and claim["id"] == manual["id"]
    assert len(store.list_audit(path=db)) == 1                       # the refused claim is not audited
    assert store.revoke_admin_role(manual["id"], actor_id="boss", now=3, path=db) is True
    assert store.revoke_admin_role(manual["id"], now=4, path=db) is False
    again = store.assign_admin_role(role="admin", subject_kind="user", subject_id="u1", provenance="oidc", now=5, path=db)
    assert (again["id"], again["provenance"], again["revoked_at"], again["granted_at"]) == (manual["id"], "oidc", None, 5)
    scoped = store.assign_admin_role(role="admin", subject_kind="user", subject_id="u1", scope_app_id=make_app(db, "scoped"), path=db)
    assert scoped["id"] != manual["id"]


def test_revoking_writes_one_audit_row_with_the_actor_and_nothing_for_a_second_revoke(db):
    granted = store.assign_admin_role(role="owner", subject_kind="user", subject_id="u1", now=1, path=db)
    assert store.revoke_admin_role(granted["id"], actor_kind="user", actor_id="boss", now=2, path=db) is True
    revoke = [e for e in store.list_audit(path=db) if e["action"] == "admin_role.revoke"]
    assert len(revoke) == 1 and (revoke[0]["actor_kind"], revoke[0]["actor_id"]) == ("user", "boss")
    store.revoke_admin_role(granted["id"], actor_id="boss", now=3, path=db)
    assert len([e for e in store.list_audit(path=db) if e["action"] == "admin_role.revoke"]) == 1
    assert store.revoke_admin_role(99999, path=db) is False


def test_listing_roles_filters_by_subject_and_hides_revoked_ones_unless_asked(db):
    a = store.assign_admin_role(role="admin", subject_kind="user", subject_id="u1", now=1, path=db)
    store.assign_admin_role(role="owner", subject_kind="service_account", subject_id="u1", now=2, path=db)
    store.assign_admin_role(role="admin", subject_kind="user", subject_id="u2", now=3, path=db)
    store.revoke_admin_role(a["id"], now=4, path=db)
    assert [r["subject_id"] for r in store.list_admin_roles(path=db)] == ["u1", "u2"]
    assert [r["id"] for r in store.list_admin_roles(subject_kind="user", subject_id="u1", include_revoked=True, path=db)] == [a["id"]]
    assert store.list_admin_roles(subject_kind="user", subject_id="u1", path=db) == []
    assert [r["subject_id"] for r in store.list_admin_roles(subject_kind="service_account", path=db)] == ["u1"]
    assert [r["role"] for r in store.list_admin_roles(subject_id="u2", path=db)] == ["admin"]


def test_reconciling_claim_roles_replaces_only_claim_grants_and_audits_each_change(db):
    for roles, message in ((["ghost"], "valid user and control-plane roles are required"),):
        with pytest.raises(ValueError, match=r"\A" + re.escape(message) + r"\Z"):
            store.reconcile_oidc_admin_roles("u1", roles, path=db)
    with pytest.raises(ValueError, match=r"\Avalid\ user\ and\ control\-plane\ roles\ are\ required\Z"):
        store.reconcile_oidc_admin_roles("", ["admin"], path=db)
    store.assign_admin_role(role="owner", subject_kind="user", subject_id="u1", now=1, path=db)       # manual
    result = store.reconcile_oidc_admin_roles("u1", ["admin", "superadmin", "owner"], now=2, path=db)
    assert sorted((r["role"], r["provenance"]) for r in result) == [
        ("admin", "oidc"), ("owner", "manual"), ("superadmin", "oidc")]
    result = store.reconcile_oidc_admin_roles("u1", ["admin"], now=3, path=db)
    assert sorted((r["role"], r["provenance"]) for r in result) == [("admin", "oidc"), ("owner", "manual")]
    again = store.reconcile_oidc_admin_roles("u1", ["admin", "superadmin"], now=4, path=db)
    assert sorted(r["role"] for r in again) == ["admin", "owner", "superadmin"]
    regranted = next(r for r in again if r["role"] == "superadmin")
    assert (regranted["granted_at"], regranted["revoked_at"], regranted["granted_by"]) == (4, None, "oidc")
    actions = [(e["action"], e["actor_kind"], e["actor_id"], e["metadata"]["role"], e["metadata"]["provenance"])
               for e in reversed(store.list_audit(path=db))]
    assert ("admin_role.grant", "system", "oidc", "admin", "oidc-claim") in actions
    assert ("admin_role.revoke", "system", "oidc", "superadmin", "oidc-claim") in actions
    assert ("admin_role.revoke", "system", "oidc", "owner", "manual") not in actions
    unrelated = store.assign_admin_role(role="admin", subject_kind="user", subject_id="u1", scope_app_id=make_app(db, "nine"), now=5, path=db)
    store.reconcile_oidc_admin_roles("u1", [], now=6, path=db)
    assert unrelated["id"] in [r["id"] for r in store.list_admin_roles(subject_id="u1", path=db)]   # scoped grants untouched
    other = store.reconcile_oidc_admin_roles("u2", ["admin"], now=7, path=db)
    assert [r["subject_id"] for r in other] == ["u2"]


# --- identity claims, credential lifecycle and runtime mappings ------------------------------

def test_identity_claims_are_replaced_per_kind_normalized_and_empty_values_dropped(db):
    with pytest.raises(ValueError, match=r"\Auser_id\ is\ required\Z"):
        store.replace_user_identity_claims("", {"oidc_role": ["x"]}, path=db)
    uid = store.upsert_oidc_user("i", "s", path=db)["id"]
    store.replace_user_identity_claims(
        uid, {"oidc_group": ["/Team/Ops/", ""], "oidc_role": ["Admin", "Admin"], "ignored": ["x"]}, now=9, path=db)
    claims = store.list_user_identity_claims(uid, path=db)
    assert [(c["claim_kind"], c["claim_value"], c["updated_at"]) for c in claims] == [
        ("oidc_group", store._normalize_identity_value("oidc_group", "/Team/Ops/"), 9), ("oidc_role", "Admin", 9)]
    store.replace_user_identity_claims(uid, {"oidc_role": ["Viewer"]}, now=10, path=db)
    assert [(c["claim_kind"], c["claim_value"]) for c in store.list_user_identity_claims(uid, path=db)] == [("oidc_role", "Viewer")]
    assert store._normalize_identity_value("oidc_role", None) == "" and store._normalize_identity_value("oidc_group", "") == ""
    assert store._normalize_access_value(" /A  B/ ") == "a b"


def test_a_credential_is_listed_until_revoked_or_expired_and_revocation_reports_change(db):
    app = make_app(db, "keys")
    other = make_app(db, "other")
    live = store.create_credential_hash(subject_kind="app", subject_id=app, hash="h1", prefix="p1", now=1, path=db)
    expiring = store.create_credential_hash(subject_kind="app", subject_id=other, hash="h2", prefix="p2", now=2, expires_at=50, path=db)
    assert [c["id"] for c in store.list_active_credentials(now=10, path=db)] == [live["id"], expiring["id"]]
    assert [c["id"] for c in store.list_active_credentials(now=50, path=db)] == [live["id"]]      # expires_at == now is expired
    assert [c["id"] for c in store.list_active_credentials(subject_kind="app", subject_id=other, now=10, path=db)] == [expiring["id"]]
    assert [c["id"] for c in store.list_active_credentials(app_id=app, now=10, path=db)] == [live["id"]]
    assert store.list_active_credentials(subject_kind="user", now=10, path=db) == []
    assert store.revoke_credential(live["id"], now=11, path=db) is True
    assert store.revoke_credential(live["id"], now=12, path=db) is False
    assert [c["id"] for c in store.list_active_credentials(now=11, path=db)] == [expiring["id"]]
    assert {c["id"] for c in store.list_credentials(path=db)} == {live["id"], expiring["id"]}


def test_older_session_keys_are_revoked_keeping_the_newest_and_the_count_is_returned(db):
    app = make_app(db, "sess-keys")
    ids = [store.create_credential_hash(subject_kind="app", subject_id=app, hash=f"h{i}", prefix=f"p{i}", session_id="S", now=5, path=db)["id"]
           for i in range(4)]
    store.create_credential_hash(subject_kind="app", subject_id=app, hash="hx", prefix="px", session_id="OTHER", now=5, path=db)
    assert store.revoke_older_session_credentials(app, "S", keep=2, now=9, path=db) == 2
    still = {c["id"] for c in store.list_active_credentials(app_id=app, now=9, path=db) if c["session_id"] == "S"}
    assert still == set(ids[2:])
    assert store.revoke_older_session_credentials(app, "S", keep=2, now=9, path=db) == 0
    assert store.revoke_older_session_credentials(app, "S", keep=0, now=9, path=db) == 2


def test_a_credential_row_decodes_its_scopes_and_survives_a_corrupt_scopes_column():
    class Row(dict):
        def keys(self):
            return list(dict.keys(self))
    assert store._credential_row_to_dict(Row(id="c", scopes_json='["a","b"]'))["scopes"] == ["a", "b"]
    assert store._credential_row_to_dict(Row(id="c", scopes_json="{bad"))["scopes"] == []
    assert store._credential_row_to_dict(Row(id="c", scopes_json=None))["scopes"] == []
    assert store._credential_row_to_dict(Row(id="c", scopes_json=""))["scopes"] == []
    assert "scopes_json" not in store._credential_row_to_dict(Row(id="c", scopes_json="[]"))




def test_a_regrant_by_claim_after_a_manual_grant_was_revoked_creates_a_claim_grant(db):
    manual = store.assign_admin_role(role="admin", subject_kind="user", subject_id="u1", now=1, path=db)
    store.revoke_admin_role(manual["id"], now=2, path=db)
    result = store.reconcile_oidc_admin_roles("u1", ["admin"], now=3, path=db)
    assert [(r["role"], r["provenance"], r["granted_at"]) for r in result] == [("admin", "oidc", 3)]




def test_a_claimed_owner_gets_a_prompt_memory_preference_row(db):
    claimed = store.claim_first_local_owner(issuer="l", subject="a", verifier="v", granted_by="g", email=None,
                                            display_name=None, now=5, path=db)
    prefs = rows(db, "SELECT user_id, retention, updated_at FROM prompt_memory_preferences")
    assert prefs == [{"user_id": claimed["id"], "retention": "never", "updated_at": 5}]


def test_an_unknown_claim_kind_is_skipped_without_dropping_the_known_ones_after_it(db):
    uid = store.upsert_oidc_user("i", "s", path=db)["id"]
    store.replace_user_identity_claims(uid, {"ignored": ["x"], "oidc_role": ["Admin"]}, path=db)
    assert [(c["claim_kind"], c["claim_value"]) for c in store.list_user_identity_claims(uid, path=db)] == [("oidc_role", "Admin")]


def test_time_defaults_to_now_when_the_caller_gives_none(db):
    app = make_app(db, "clock")
    store.create_credential_hash(subject_kind="app", subject_id=app, hash="h", prefix="p", session_id="S", path=db)
    assert store.revoke_older_session_credentials(app, "S", keep=0, path=db) == 1
    assert store.revoke_older_session_credentials(app, "S", keep=0, path=db) == 0
