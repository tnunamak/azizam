"""People and authorization must resolve the same access plan (design §2.4)."""

import pytest

from gateway import accounts, admin_runtime, admin_store
from tests.support.access import store_plan


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def test_people_shows_the_plan_that_authorizes_the_persons_apps(db):
    user = admin_store.upsert_oidc_user("issuer", "subject", email="person@example.test", path=db)
    family = store_plan("Family", path=db, caps=("chat",))
    accounts.change_person(user["id"], "owner", path=db, plan_id=family["id"])
    app = admin_store.ensure_session_app_for_user(user["id"], name="Browser", path=db)

    person = accounts.people(path=db)[0]
    identity = admin_runtime.runtime_identity_for_session_app(app["id"], user["id"], path=db)

    assert (person["plan_id"], person["plan_name"]) == (family["id"], "Family")
    assert identity.access.plan_ids == (family["id"],)


def test_person_without_a_plan_shows_none_and_their_apps_are_denied(db):
    user = admin_store.upsert_oidc_user("issuer", "subject", path=db)
    app = admin_store.ensure_session_app_for_user(user["id"], name="Browser", path=db)

    person = accounts.people(path=db)[0]
    identity = admin_runtime.runtime_identity_for_session_app(app["id"], user["id"], path=db)

    assert person["plan_id"] is None
    assert not identity.access.complete


@pytest.mark.parametrize("state", ["active", "disabled", "removed"])
def test_plan_holders_count_only_active_people_and_direct_apps(db, state):
    user = admin_store.upsert_oidc_user("issuer", state, display_name="Member", path=db)
    family = store_plan("Family", path=db, caps=("chat",))
    accounts.change_person(user["id"], "owner", path=db, plan_id=family["id"])
    admin_store.ensure_session_app_for_user(user["id"], name="Personal", path=db)
    admin_store.create_app("Direct", kind="shared", owner_user_id=user["id"], access_plan=family["id"], path=db)
    disabled = admin_store.create_app("Disabled", kind="shared", owner_user_id=user["id"], access_plan=family["id"], path=db)
    with admin_store._conn(db) as conn:
        conn.execute("UPDATE apps SET disabled_at=1 WHERE id=?", (disabled["id"],))
        # Test owner state independently: Direct remains enabled.
        if state != "active":
            conn.execute(f"UPDATE account_access SET {state}_at=1 WHERE user_id=?", (user["id"],))
        conn.commit()
        used = admin_store.access_plan_users(conn, family["id"])
    assert used["people"] == (["Member"] if state == "active" else [])
    assert used["apps"] == (["Direct"] if state == "active" else [])


def test_plan_holders_report_signin_rule_assignments_separately(db):
    family = store_plan("Family", path=db, caps=("chat",))
    user = admin_store.upsert_oidc_user("issuer", "rule", path=db)
    with admin_store._conn(db) as conn:
        assert admin_store.access_plan_users(conn, family["id"])["signin_rules"] == 0
        conn.execute("INSERT INTO signin_rule_plans(user_id,plan_id,set_at) VALUES (?,?,1)", (user["id"], family["id"]))
        used = admin_store.access_plan_users(conn, family["id"])
    assert used["signin_rules"] == 1
    assert used["people"] == []
    assert used["apps"] == []
