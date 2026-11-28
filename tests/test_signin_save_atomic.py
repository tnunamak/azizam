"""Saving Settings > Sign-in is all or nothing.

A save writes the rules, the sign-in switches and the provider connection, then
removes the grants, ends the sessions and revokes the keys of the people those
changes touch. All of that commits as one SQLite transaction. A failure in any
step puts back every step before it, and nobody is signed out. What a rollback
cannot undo (a person's MCP connections) runs only after the commit.
"""
import pytest

from gateway import admin_store, signin_admin
from tests.test_signin_settings_api import (  # noqa: F401  (sg is a fixture)
    HELPER, bearer_status, get_snapshot, helper_client, mint_admin_key, owner_client, proposal, provider_off, save, sg,
)
from tests.test_signin_who_can_enter import TRUSTED, domain_rule, gateway, roles_of, signed_in  # noqa: F401

NO_RULES = {"fallback": "closed", "rules": []}


def fail_at(monkeypatch, action):
    """Make the audit write for one kind of event fail, as a disk or constraint error would."""
    real = admin_store._record_audit_in_transaction

    def audit(conn, name, **kwargs):
        if name == action:
            raise RuntimeError("injected failure at " + action)
        return real(conn, name, **kwargs)

    monkeypatch.setattr(admin_store, "_record_audit_in_transaction", audit)
    return lambda: monkeypatch.setattr(admin_store, "_record_audit_in_transaction", real)


def stored_state(g):
    """Everything a save can touch, read from the database."""
    with admin_store._conn(g.path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        secrets = ([row[0] for row in conn.execute("SELECT id FROM provider_secrets ORDER BY id")]
                   if "provider_secrets" in tables else [])
        return {
            "settings": [tuple(row) for row in conn.execute(
                "SELECT key, value_json, updated_by FROM operator_settings ORDER BY key")],
            "revisions": [tuple(row) for row in conn.execute(
                "SELECT setting_key, operator_settings_generation FROM operator_settings_generations ORDER BY 1")],
            "grants": [tuple(row) for row in conn.execute(
                "SELECT subject_id, role, provenance, revoked_at FROM admin_role_assignments ORDER BY id")],
            "keys": [tuple(row) for row in conn.execute("SELECT id, revoked_at FROM credentials ORDER BY id")],
            "sessions": [tuple(row) for row in conn.execute("SELECT session_id, user_id FROM account_sessions ORDER BY 1")],
            "access": [tuple(row) for row in conn.execute(
                "SELECT user_id, plan_id, require_registered_session FROM account_access ORDER BY 1")],
            "refusals": [tuple(row) for row in conn.execute("SELECT user_id FROM account_signin_refusals ORDER BY 1")],
            "audit": conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
            "secrets": secrets,
        }


def test_a_failure_after_the_grants_are_removed_puts_back_the_rules_grants_keys_and_sessions(sg, monkeypatch):
    owner = owner_client(sg)
    helper = helper_client(sg)
    key = mint_admin_key(helper)
    snapshot = get_snapshot(owner)
    before = stored_state(sg)
    roles_before = roles_of(sg, HELPER)
    assert roles_before
    restore = fail_at(monkeypatch, "signin.admin_removed")     # the last write for the person who loses administrator

    failed = save(owner, snapshot, proposal(snapshot, rules=NO_RULES))
    assert failed.status_code == 500 and "injected" not in failed.text  # the gate answers its fixed 500, never the exception

    restore()
    assert stored_state(sg) == before
    assert get_snapshot(owner)["rules"] == snapshot["rules"]
    assert roles_of(sg, HELPER) == roles_before
    assert helper.get("/admin/api/access-plans").status_code == 200       # still signed in
    assert bearer_status(sg, key) == 200                                  # the key still works
    # Nothing is stuck: the same save, with no fault, goes through.
    saved = save(owner, snapshot, proposal(snapshot, rules=NO_RULES))
    assert saved.status_code == 200, saved.text
    assert saved.json()["applied"]["admin_removed"] == [{"name": HELPER, "keys_revoked": 1, "keys_kept": 0}]


def test_a_failure_while_the_connection_is_written_puts_back_the_rules_and_the_grants(sg, monkeypatch):
    owner = owner_client(sg)
    helper = helper_client(sg)
    key = mint_admin_key(helper)
    snapshot = get_snapshot(owner)
    before = stored_state(sg)
    fail_at(monkeypatch, "operator.oidc_provider_config.update")

    failed = save(owner, snapshot, proposal(snapshot, rules=NO_RULES, name="New name"))
    assert failed.status_code == 500 and "injected" not in failed.text  # the gate answers its fixed 500, never the exception

    assert stored_state(sg) == before
    assert helper.get("/admin/api/access-plans").status_code == 200
    assert bearer_status(sg, key) == 200


def test_a_failure_after_a_new_secret_is_stored_leaves_the_old_secret_and_no_stray_one(sg, monkeypatch):
    owner = owner_client(sg)
    provider_off(sg)
    snapshot = get_snapshot(owner)
    first = save(owner, snapshot, proposal(snapshot, client_secret="the-first-secret"), save_for_later=True)
    assert first.status_code == 200, first.text
    snapshot = first.json()["snapshot"]
    before = stored_state(sg)
    assert len(before["secrets"]) == 1
    fail_at(monkeypatch, "operator.oidc_provider_config.update")

    failed = save(owner, snapshot, proposal(snapshot, client_secret="the-second-secret"), save_for_later=True)
    assert failed.status_code == 500 and "injected" not in failed.text  # the gate answers its fixed 500, never the exception

    assert stored_state(sg) == before


def test_a_failure_leaves_the_people_the_rules_now_keep_out_signed_in_and_their_mcp_connections_alone(sg, monkeypatch):
    sg.set_rules([TRUSTED, domain_rule("example.test")])
    owner = owner_client(sg)
    member, _ = signed_in(sg, sub="member", email="member@example.test")
    ended = []
    monkeypatch.setattr(sg.proxy.MCP_OAUTH.deps.store, "revoke_person_grants",
                        lambda person_id, reason: ended.append((person_id, reason)))
    snapshot = get_snapshot(owner)
    before = stored_state(sg)
    member_id = sg.user("member@example.test")["id"]
    real = signin_admin.sync_rule_plan_in

    def fail_for_the_member(conn, user_id, outcome, **kwargs):
        # The last step for this person: their refusal, session and grants are already written.
        if user_id == member_id:
            raise RuntimeError("injected failure for the member")
        return real(conn, user_id, outcome, **kwargs)

    monkeypatch.setattr(signin_admin, "sync_rule_plan_in", fail_for_the_member)

    failed = save(owner, snapshot, proposal(snapshot, rules={"fallback": "closed", "rules": [snapshot["rules"]["rules"][0]]}))
    assert failed.status_code == 500 and "injected" not in failed.text  # the gate answers its fixed 500, never the exception

    monkeypatch.setattr(signin_admin, "sync_rule_plan_in", real)
    assert stored_state(sg) == before
    assert member.get("/me/api/session").json()["authenticated"] is True
    assert ended == []


def test_the_mcp_connections_of_someone_kept_out_end_only_after_the_commit(sg, monkeypatch):
    sg.set_rules([TRUSTED, domain_rule("example.test")])
    owner = owner_client(sg)
    member, _ = signed_in(sg, sub="member", email="member@example.test")
    seen = []

    def revoke(person_id, reason):
        # By now the save is committed: another connection reads the new rules.
        seen.append((reason, len(get_snapshot(owner)["rules"]["rules"])))

    monkeypatch.setattr(sg.proxy.MCP_OAUTH.deps.store, "revoke_person_grants", revoke)
    snapshot = get_snapshot(owner)

    saved = save(owner, snapshot, proposal(snapshot, rules={"fallback": "closed", "rules": [snapshot["rules"]["rules"][0]]}))

    assert saved.status_code == 200, saved.text
    assert seen == [("sign_in_refused", 1)]
    assert member.get("/me/api/session").json()["authenticated"] is False


def test_a_save_that_loses_a_race_for_the_revision_changes_nothing(sg):
    owner = owner_client(sg)
    helper = helper_client(sg)
    snapshot = get_snapshot(owner)
    first = save(owner, snapshot, proposal(snapshot, rules=NO_RULES))
    assert first.status_code == 200
    before = stored_state(sg)

    stale = save(owner, snapshot, proposal(snapshot, rules=NO_RULES))        # the old revision

    assert stale.status_code == 409 and stale.json()["error"]["code"] == "settings_conflict"
    assert stored_state(sg) == before


# ---------------------------------------------------------------------------
# The transaction itself
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    return path


def test_writes_in_a_transaction_are_invisible_until_it_commits_and_vanish_when_it_fails(db):
    with pytest.raises(RuntimeError):
        with admin_store.transaction(db) as tx:
            admin_store.put_operator_setting_in(tx.conn, "demo", {"a": 1})
            assert admin_store.get_operator_setting("demo", path=db) is None      # not yet visible to anyone else
            raise RuntimeError("boom")

    assert admin_store.get_operator_setting("demo", path=db) is None
    assert admin_store.operator_setting_revision(admin_store.get_operator_setting("demo", path=db)) == "0"


def test_after_commit_actions_run_once_after_the_commit_and_never_after_a_failure(db):
    seen = []

    with admin_store.transaction(db) as tx:
        admin_store.put_operator_setting_in(tx.conn, "demo", {"a": 1})
        tx.after_commit(lambda: seen.append(admin_store.get_operator_setting("demo", path=db)["value"]))
        assert seen == []
    assert seen == [{"a": 1}]

    with pytest.raises(RuntimeError):
        with admin_store.transaction(db) as tx:
            tx.after_commit(lambda: seen.append("must not run"))
            raise RuntimeError("boom")
    assert seen == [{"a": 1}]


def test_a_failing_after_commit_action_does_not_undo_the_commit_or_stop_the_others(db):
    ran = []

    def broken():
        raise OSError("the other store is down")

    with pytest.raises(OSError):
        with admin_store.transaction(db) as tx:
            admin_store.put_operator_setting_in(tx.conn, "demo", {"a": 1})
            tx.after_commit(broken)
            tx.after_commit(lambda: ran.append("second"))

    assert ran == ["second"]
    assert admin_store.get_operator_setting("demo", path=db)["value"] == {"a": 1}
