"""A shared App's request-history setting, set by its manager (/me) or by the household owner (admin API).

The setting is read on the App's record. Only the App's manager (the accountable person) or the owner may change it;
a personal App has none; the period must be one the request history group offers; and each change is audited.
"""
import pytest

from gateway import accounts, admin_runtime, admin_store
from gateway.admin_api import AdminContext
from gateway.admin_oidc import AdminSession
from gateway.user_runtime import UserPortalError, build_user_portal_snapshot, set_user_app_request_history
from tests.support.access import service_app


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def _signed_in(db, sub, email):
    user = admin_store.upsert_oidc_user("https://idp.test", sub, email=email, path=db)
    session = AdminSession(subject=user["id"], email=email, role="none", issued_at=1, expires_at=4_000_000_000,
                           session_id=f"{sub}-session")
    accounts.register_session(user["id"], f"{sub}-session", 4_000_000_000, path=db)
    return user, session


@pytest.fixture
def manager(db):
    user, session = _signed_in(db, "manager", "manager@example.test")
    app = service_app("Home voice", path=db, owner_user_id=user["id"])
    return type("Manager", (), {"db": db, "user": user, "session": session, "app": app})


def _setting(app_id, db):
    row = admin_store.get_app(app_id, path=db)
    return {"keep": bool(row["history_keep"]), "retention": row["history_period"]}


def test_a_shared_app_starts_off_on_the_managers_snapshot_with_the_manager_named(manager):
    snapshot = build_user_portal_snapshot(manager.session, path=manager.db)
    [listed] = snapshot["apps"]
    assert listed["request_history"] == {"keep": False, "retention": "7_days", "manager_name": "manager@example.test"}


def test_the_manager_turns_it_on_and_the_snapshot_and_audit_show_it(manager):
    payload = set_user_app_request_history(manager.session, manager.app["id"],
                                           {"keep": True, "retention": "1_day"}, path=manager.db)

    assert payload["app"]["request_history"] == {"keep": True, "retention": "1_day", "manager_name": "manager@example.test"}
    assert _setting(manager.app["id"], manager.db) == {"keep": True, "retention": "1_day"}
    [event] = admin_store.list_audit(action_prefix="app.request_history", path=manager.db)
    assert event["actor_id"] == manager.user["id"] and event["target_id"] == manager.app["id"]
    assert event["metadata"] == {"request_history": {"keep": True, "period": "1_day"}}


@pytest.mark.parametrize("body, code", [
    ({"keep": True, "retention": "1_year"}, "request_history_period_invalid"),
    ({"keep": True, "retention": "never"}, "request_history_period_invalid"),
    ({"keep": True, "retention": None}, "request_history_invalid"),
    ({"keep": "yes", "retention": "7_days"}, "request_history_invalid"),
    ("on", "request_history_invalid"),
])
def test_a_bad_setting_is_refused_in_words_and_nothing_changes(manager, body, code):
    with pytest.raises(UserPortalError) as refused:
        set_user_app_request_history(manager.session, manager.app["id"], body, path=manager.db)

    assert refused.value.code == code and refused.value.status_code == 400 and refused.value.message
    assert _setting(manager.app["id"], manager.db) == {"keep": False, "retention": "7_days"}
    assert admin_store.list_audit(action_prefix="app.request_history", path=manager.db) == []


def test_a_personal_app_has_no_request_history_setting(manager):
    mine = admin_store.create_app("Mine", kind="personal", owner_user_id=manager.user["id"], access_plan="person",
                                  path=manager.db)

    with pytest.raises(UserPortalError) as refused:
        set_user_app_request_history(manager.session, mine["id"], {"keep": True, "retention": "7_days"},
                                     path=manager.db)

    assert (refused.value.code, refused.value.status_code) == ("not_a_shared_app", 400)
    snapshot = build_user_portal_snapshot(manager.session, path=manager.db)
    [listed] = [item for item in snapshot["apps"] if item["id"] == mine["id"]]
    assert "request_history" not in listed


def test_another_persons_app_or_a_disabled_one_is_refused_as_the_rename_is(manager):
    _, stranger_session = _signed_in(manager.db, "stranger", "stranger@example.test")
    turned_off = service_app("Off", path=manager.db, owner_user_id=manager.user["id"])
    admin_store.disable_app(turned_off["id"], path=manager.db)

    with pytest.raises(UserPortalError) as other:
        set_user_app_request_history(stranger_session, manager.app["id"], {"keep": True, "retention": "7_days"},
                                     path=manager.db)
    with pytest.raises(UserPortalError) as off:
        set_user_app_request_history(manager.session, turned_off["id"], {"keep": True, "retention": "7_days"},
                                     path=manager.db)

    assert other.value.status_code == 404 and off.value.status_code == 403
    assert _setting(manager.app["id"], manager.db) == {"keep": False, "retention": "7_days"}


def _admin(roles, actor_id):
    return AdminContext(subject=actor_id, actor_kind="user", actor_id=actor_id, actor_label="actor",
                        roles=frozenset(roles))


def test_the_household_owner_can_set_it_through_the_admin_api(manager, db):
    owner, _ = _signed_in(db, "owner", "owner@example.test")
    store = admin_runtime.AdminSQLiteStore(db)

    record = store.update_app(app_id=manager.app["id"], data={"request_history": {"keep": True, "retention": "30_days"}},
                              actor=_admin({"owner", "admin"}, owner["id"]))

    assert record["history_keep"] == 1 and record["history_period"] == "30_days"
    [event] = admin_store.list_audit(action_prefix="app.update", path=db)
    assert event["metadata"]["request_history"] == {"keep": True, "period": "30_days"}


def test_an_administrator_who_does_not_answer_for_the_app_cannot_set_it(manager, db):
    other_admin, _ = _signed_in(db, "other-admin", "other-admin@example.test")
    store = admin_runtime.AdminSQLiteStore(db)

    with pytest.raises(PermissionError):
        store.update_app(app_id=manager.app["id"], data={"request_history": {"keep": True, "retention": "7_days"}},
                         actor=_admin({"admin"}, other_admin["id"]))
    assert _setting(manager.app["id"], db) == {"keep": False, "retention": "7_days"}


def test_the_admin_api_refuses_a_personal_app_and_an_unoffered_period(manager, db):
    owner, _ = _signed_in(db, "owner", "owner@example.test")
    store = admin_runtime.AdminSQLiteStore(db)
    mine = admin_store.create_app("Mine", kind="personal", owner_user_id=manager.user["id"], access_plan="person",
                                  path=db)

    with pytest.raises(ValueError):
        store.update_app(app_id=mine["id"], data={"request_history": {"keep": True, "retention": "7_days"}},
                         actor=_admin({"owner", "admin"}, owner["id"]))
    with pytest.raises(ValueError):
        store.update_app(app_id=manager.app["id"], data={"request_history": {"keep": True, "retention": "1_year"}},
                         actor=_admin({"owner", "admin"}, owner["id"]))
    assert _setting(manager.app["id"], db) == {"keep": False, "retention": "7_days"}


# -- over the real routes ---------------------------------------------------------------------------

from tests.test_rename_apps_keys import ACT, csrf, me_headers, member_client  # noqa: E402
from tests.test_session_lifecycle import gateway, owner_client, sg  # noqa: E402,F401  (fixtures)


def _admin_db():
    import proxy

    return proxy.ADMIN_STORE.path


def _person_id(email):
    return next(user["id"] for user in admin_store.list_users(path=_admin_db()) if user["email"] == email)


def test_the_manager_sets_their_shared_apps_history_over_me_and_nobody_else_can(sg):
    owner = owner_client(sg)
    manager = member_client(sg, sub="manager")
    created = owner.post("/admin/api/apps", headers=csrf(owner), json={
        "name": "Kids tablet", "kind": "service", "access_plan": "person",
        "owner_user_id": _person_id("manager@example.test")})
    assert created.status_code == 201, created.text
    app_id = created.json()["app"]["id"]
    stranger = member_client(sg, sub="stranger")

    set_by_manager = manager.patch(f"/me/api/apps/{app_id}", headers=me_headers(manager),
                                   json={"request_history": {"keep": True, "retention": "7_days"}})
    set_by_stranger = stranger.patch(f"/me/api/apps/{app_id}", headers=me_headers(stranger),
                                     json={"request_history": {"keep": False, "retention": "1_day"}})
    set_by_member_over_admin = manager.patch(f"/admin/api/apps/{app_id}", headers=me_headers(manager),
                                             json={"request_history": {"keep": False, "retention": "1_day"}})

    assert set_by_manager.status_code == 200, set_by_manager.text
    assert set_by_manager.json()["app"]["request_history"]["keep"] is True
    assert set_by_stranger.status_code == 404
    assert set_by_member_over_admin.status_code == 403
    assert _setting(app_id, _admin_db()) == {"keep": True, "retention": "7_days"}


def test_the_owner_sets_a_shared_apps_history_over_the_admin_api(sg):
    owner = owner_client(sg)
    created = owner.post("/admin/api/apps", headers=csrf(owner), json={
        "name": "Voice", "kind": "service", "access_plan": "person"})
    app_id = created.json()["app"]["id"]
    assert created.json()["app"]["request_history"] == {"keep": False, "retention": "7_days"}

    changed = owner.patch(f"/admin/api/apps/{app_id}", headers=csrf(owner),
                          json={"request_history": {"keep": True, "retention": "30_days"}})

    assert changed.status_code == 200, changed.text
    assert changed.json()["app"]["request_history"] == {"keep": True, "retention": "30_days"}
    refused = owner.patch(f"/admin/api/apps/{app_id}", headers=csrf(owner),
                          json={"request_history": {"keep": True, "retention": "1_year"}})
    assert refused.status_code == 400
