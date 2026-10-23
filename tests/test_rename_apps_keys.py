"""Apps and keys can be renamed from REST (admin and /me): who may, what is checked, what is kept.

A rename changes a label, not a credential (decision D36): no new key, no new destination, so no step-up. It
still needs a live sign-in, checked in the write's own transaction, and the user-action/CSRF header. Receipts
keep the name they were written with; new ones carry the new name.

Real routes and a real database behind a fake identity provider (tests/test_signin_who_can_enter.py).
"""
import sqlite3
from contextlib import closing

import pytest

from gateway import accounts, admin_runtime, admin_store, session_guard
from gateway.admin_oidc import AdminSession
from gateway.runtime_auth import hash_api_key
from gateway import user_runtime as user
from gateway.user_runtime import UserPortalError, build_user_portal_snapshot, mint_user_app_key, rename_user_app, rename_user_app_key
from tests.test_credential_writers_check_the_session import sign_out_just_before
from tests.test_session_lifecycle import (  # noqa: F401  (gateway and sg are fixtures)
    COOKIE, HELPER, OWNER, cookie_session, csrf, forge, gateway, helper_client, owner_client, sg, stale_owner,
)
from tests.test_signin_who_can_enter import csrf_for, mint_admin_key, signed_in
from gateway.session_policy import MINUTE

ACT = {"x-azizam-user-action": "1"}


# -- the store ------------------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def person(db, sub="person"):
    return admin_store.upsert_oidc_user("https://idp.test", sub, email=f"{sub}@example.test", path=db)


def make_app(db, owner, name, kind="personal"):
    return admin_store.create_app(name, kind=kind, owner_user_id=owner["id"], access_plan="person", path=db)


def make_key(db, app, label, secret=None):
    return admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], hash=hash_api_key(secret or f"opk_{label}"), prefix="opk_test_0123456",
        display_name=label, path=db, created_by=accountable_person(db, app["id"]))


@pytest.mark.parametrize("text, code", [
    ("", "app_name_required"), ("   ", "app_name_required"), (None, "app_name_required"), (7, "app_name_required"),
    ("x" * 81, "app_name_too_long"), (" " + "x" * 81 + " ", "app_name_too_long"),
    ("two\nlines", "app_name_not_allowed"), ("tab\there", "app_name_not_allowed"), ("nul\x00", "app_name_not_allowed"),
    ("bell\x07", "app_name_not_allowed"), ("para\u2029graph", "app_name_not_allowed"),
    ("line\u2028break", "app_name_not_allowed"), ("flipped \u202eskcab", "app_name_not_allowed"),
    ("mark\u200ed", "app_name_not_allowed"), ("isolate\u2066x", "app_name_not_allowed"),
])
def test_a_name_that_is_empty_too_long_or_hides_characters_is_refused_with_a_reason(text, code):
    with pytest.raises(admin_store.NameRejected) as refused:
        admin_store.clean_name(text, code="app_name", noun="app")

    assert refused.value.code == code and refused.value.status_code == 400 and str(refused.value)


@pytest.mark.parametrize("text, cleaned", [
    ("  Laptop  ", "Laptop"), ("x" * 80, "x" * 80), ("Caf\u00e9 \u2615 \u65e5\u672c\u8a9e", "Caf\u00e9 \u2615 \u65e5\u672c\u8a9e"),
    ("family \U0001F468\u200d\U0001F469\u200d\U0001F467 phone", "family \U0001F468\u200d\U0001F469\u200d\U0001F467 phone"),
])
def test_a_name_is_trimmed_and_ordinary_text_emoji_and_other_scripts_pass(text, cleaned):
    assert admin_store.clean_name(text, code="app_name", noun="app") == cleaned


def test_a_key_label_has_its_own_codes_and_words():
    with pytest.raises(admin_store.NameRejected) as refused:
        admin_store.clean_name("x" * 81, code="key_label", noun="key")

    assert refused.value.code == "key_label_too_long" and "key" in str(refused.value)


def test_renaming_an_app_changes_only_its_name_and_a_name_another_active_app_of_the_person_has_is_taken(db):
    mine = person(db)
    laptop, phone = make_app(db, mine, "Laptop"), make_app(db, mine, "Phone")

    renamed = admin_store.update_app(laptop["id"], name="Work laptop", path=db)
    assert renamed["name"] == "Work laptop"
    assert {key: renamed[key] for key in ("id", "kind", "owner_user_id", "access_plan", "created_at")} == {
        key: laptop[key] for key in ("id", "kind", "owner_user_id", "access_plan", "created_at")}

    for clash in ("Phone", "phone", "PHONE"):
        with pytest.raises(admin_store.NameRejected) as taken:
            admin_store.update_app(laptop["id"], name=clash, path=db)
        assert taken.value.code == "app_name_taken" and taken.value.status_code == 409
    assert admin_store.get_app(laptop["id"], path=db)["name"] == "Work laptop"
    assert admin_store.get_app(phone["id"], path=db)["name"] == "Phone"


def test_the_same_name_is_not_taken_by_oneself_by_another_person_or_by_a_turned_off_app(db):
    mine, theirs = person(db), person(db, "other")
    laptop, phone = make_app(db, mine, "Laptop"), make_app(db, mine, "Phone")
    make_app(db, theirs, "Tablet")

    assert admin_store.update_app(laptop["id"], name="Laptop", path=db)["name"] == "Laptop"      # no change
    assert admin_store.update_app(laptop["id"], name="laptop", path=db)["name"] == "laptop"      # only the case
    assert admin_store.update_app(laptop["id"], name="Tablet", path=db)["name"] == "Tablet"      # their app, not mine
    admin_store.disable_app(phone["id"], path=db)
    assert admin_store.update_app(laptop["id"], name="Phone", path=db)["name"] == "Phone"        # turned off


def test_the_try_pages_app_cannot_be_renamed_and_its_name_cannot_be_taken(db):
    mine = person(db)
    try_app, other = make_app(db, mine, admin_store.TRY_APP_NAME), make_app(db, mine, "Laptop")

    for app_id, name in ((try_app["id"], "Something else"), (other["id"], "Try"), (other["id"], "try")):
        with pytest.raises(admin_store.NameRejected) as reserved:
            admin_store.update_app(app_id, name=name, path=db)
        assert reserved.value.code == "app_name_reserved" and reserved.value.status_code == 409
    assert admin_store.get_app(try_app["id"], path=db)["name"] == "Try"


def test_a_rename_for_another_owners_app_or_a_missing_app_changes_nothing(db):
    mine, theirs = person(db), person(db, "other")
    laptop = make_app(db, mine, "Laptop")

    assert admin_store.update_app(laptop["id"], name="Stolen", owner_user_id=theirs["id"], path=db) is None
    assert admin_store.update_app("app_missing", name="Nothing", path=db) is None
    assert admin_store.get_app(laptop["id"], path=db)["name"] == "Laptop"


def test_renaming_a_key_changes_only_its_label_and_a_label_another_active_key_of_the_app_has_is_taken(db):
    mine = person(db)
    laptop, phone = make_app(db, mine, "Laptop"), make_app(db, mine, "Phone")
    first, second = make_key(db, laptop, "Editor"), make_key(db, laptop, "Notes")
    make_key(db, phone, "Shared label")

    before, after = admin_store.rename_app_credential(first["id"], "Code editor", path=db)

    assert before["display_name"] == "Editor" and after["display_name"] == "Code editor"
    assert {key: after[key] for key in ("hash", "prefix", "created_at", "app_id", "revoked_at")} == {
        key: before[key] for key in ("hash", "prefix", "created_at", "app_id", "revoked_at")}
    with pytest.raises(admin_store.NameRejected) as taken:
        admin_store.rename_app_credential(first["id"], "notes", path=db)
    assert taken.value.code == "key_label_taken" and taken.value.status_code == 409
    # Another app's key may share the label, and a key may keep or recase its own.
    assert admin_store.rename_app_credential(first["id"], "Shared label", path=db)[1]["display_name"] == "Shared label"
    assert admin_store.rename_app_credential(second["id"], "Notes", path=db)[1]["display_name"] == "Notes"
    # The key still opens the same App.
    assert admin_runtime.runtime_identity_for_app_credential_hash(hash_api_key("opk_Editor"), path=db).app_id == laptop["id"]


def test_a_revoked_expired_missing_or_misplaced_key_is_not_renamed(db):
    mine, theirs = person(db), person(db, "other")
    laptop, phone = make_app(db, mine, "Laptop"), make_app(db, mine, "Phone")
    revoked, expired, live = make_key(db, laptop, "Old"), make_key(db, laptop, "Lapsed"), make_key(db, laptop, "Live")
    admin_store.revoke_credential(revoked["id"], path=db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE credentials SET expires_at = 1 WHERE id = ?", (expired["id"],))
        conn.commit()

    assert admin_store.rename_app_credential(revoked["id"], "New", path=db) is None
    assert admin_store.rename_app_credential(expired["id"], "New", path=db) is None
    assert admin_store.rename_app_credential("cred_missing", "New", path=db) is None
    assert admin_store.rename_app_credential(live["id"], "New", app_id=phone["id"], path=db) is None
    assert admin_store.rename_app_credential(live["id"], "New", owner_user_id=theirs["id"], path=db) is None
    assert admin_store.get_credential(live["id"], path=db)["display_name"] == "Live"
    assert admin_store.rename_app_credential(live["id"], "New", app_id=laptop["id"], owner_user_id=mine["id"], path=db)


def test_a_control_plane_key_is_not_an_app_key_and_is_not_renamed_here(db):
    created = admin_store.create_admin_key_with_role(
        name="robot", description=None, role="admin", granted_by="u", actor=None, hash="ah1", prefix="oak_x",
        display_name="robot admin automation key", created_by="u", session=None, path=db)
    credential = created[2]

    assert admin_store.rename_app_credential(credential["id"], "Renamed", path=db) is None
    assert admin_store.get_credential(credential["id"], path=db)["display_name"] == "robot admin automation key"


def test_both_renames_check_the_sign_in_in_their_own_transaction_and_write_nothing_when_it_ended(db):
    import time

    mine = person(db)
    laptop = make_app(db, mine, "Laptop")
    key = make_key(db, laptop, "Editor")
    accounts.register_session(mine["id"], "live", int(time.time()) + 3600, path=db)
    accounts.register_session(mine["id"], "ended", int(time.time()) + 3600, path=db)
    live, ended = (session_guard.BrowserSession(mine["id"], sid, sid) for sid in ("live", "ended"))
    accounts.revoke_session("ended", int(time.time()) + 3600, path=db, family_id="ended")

    with pytest.raises(session_guard.SessionEnded):
        admin_store.update_app(laptop["id"], name="Late", session=ended, path=db)
    with pytest.raises(session_guard.SessionEnded):
        admin_store.rename_app_credential(key["id"], "Late", session=ended, path=db)
    assert admin_store.get_app(laptop["id"], path=db)["name"] == "Laptop"
    assert admin_store.get_credential(key["id"], path=db)["display_name"] == "Editor"

    assert admin_store.update_app(laptop["id"], name="On time", session=live, path=db)["name"] == "On time"
    assert admin_store.rename_app_credential(key["id"], "On time", session=live, path=db)[1]["display_name"] == "On time"


# -- the signed-in person's own apps and keys (user_runtime) ----------------------------------------

@pytest.fixture
def member(db):
    user = person(db, "member")
    session = AdminSession(subject=user["id"], email="member@example.test", role="none", issued_at=1,
                           expires_at=4_000_000_000, session_id="member-session")
    accounts.register_session(user["id"], "member-session", 4_000_000_000, path=db)
    app = make_app(db, user, "Laptop")
    return type("Member", (), {"db": db, "user": user, "session": session, "app": app})


def test_a_member_renames_their_own_app_and_the_new_name_is_what_the_snapshot_and_audit_show(member):
    payload = rename_user_app(member.session, member.app["id"], "  Work laptop ", path=member.db)

    assert payload["app"]["name"] == "Work laptop" and payload["app"]["id"] == member.app["id"]
    snapshot = build_user_portal_snapshot(member.session, path=member.db)
    assert [item["name"] for item in snapshot["apps"]] == ["Work laptop"]
    [event] = admin_store.list_audit(action_prefix="app.self_rename", path=member.db)
    assert event["actor_id"] == member.user["id"] and event["target_id"] == member.app["id"]
    assert event["metadata"] == {"name": "Work laptop", "previous_name": "Laptop"}


@pytest.mark.parametrize("name, code, status", [
    ("", "app_name_required", 400), ("x" * 81, "app_name_too_long", 400), ("bad\nname", "app_name_not_allowed", 400),
    ("Tablet", "app_name_taken", 409), ("Try", "app_name_reserved", 409),
])
def test_a_member_is_told_in_words_why_a_name_is_refused_and_nothing_changes(member, name, code, status):
    make_app(member.db, member.user, "Tablet")

    with pytest.raises(UserPortalError) as refused:
        rename_user_app(member.session, member.app["id"], name, path=member.db)

    assert (refused.value.code, refused.value.status_code) == (code, status) and refused.value.message
    assert admin_store.get_app(member.app["id"], path=member.db)["name"] == "Laptop"
    assert admin_store.list_audit(action_prefix="app.self_rename", path=member.db) == []


def test_a_member_cannot_rename_another_persons_app_a_turned_off_app_or_the_keyless_session_app(member):
    other = person(member.db, "other")
    theirs = make_app(member.db, other, "Theirs")
    off = make_app(member.db, member.user, "Off")
    admin_store.disable_app(off["id"], path=member.db)
    session_app = admin_store.ensure_session_app_for_user(member.user["id"], name="Browser", path=member.db)

    for app_id, status in ((theirs["id"], 404), ("app_missing", 404), (session_app["id"], 404), (off["id"], 403)):
        with pytest.raises(UserPortalError) as refused:
            rename_user_app(member.session, app_id, "Mine now", path=member.db)
        assert refused.value.status_code == status, app_id
    assert admin_store.get_app(theirs["id"], path=member.db)["name"] == "Theirs"
    assert admin_store.get_app(session_app["id"], path=member.db)["name"] == "Browser"


def test_a_member_renames_their_own_key_and_the_snapshot_audit_and_secret_handling_agree(member):
    key = mint_user_app_key(member.session, member.app["id"], label="Editor", path=member.db)

    renamed = rename_user_app_key(member.session, key["id"], " Code editor ", path=member.db)

    assert renamed["display_name"] == "Code editor" and renamed["active"] is True
    for secret_field in ("hash", "api_key", "session_id"):
        assert secret_field not in renamed
    snapshot = build_user_portal_snapshot(member.session, path=member.db)
    assert [item["display_name"] for item in snapshot["apps"][0]["credentials"]] == ["Code editor"]
    assert key["api_key"] not in repr(snapshot)
    [event] = admin_store.list_audit(action_prefix="credential.self_rename", path=member.db)
    assert event["target_id"] == key["id"]
    assert event["metadata"] == {"app_id": member.app["id"], "prefix": key["prefix"], "name": "Code editor", "previous_name": "Editor"}
    assert key["api_key"] not in repr(event)


@pytest.mark.parametrize("label, code, status", [
    ("", "key_label_required", 400), ("x" * 81, "key_label_too_long", 400), ("a\x07b", "key_label_not_allowed", 400),
    ("Notes", "key_label_taken", 409),
])
def test_a_member_is_told_why_a_key_name_is_refused(member, label, code, status):
    key = mint_user_app_key(member.session, member.app["id"], label="Editor", path=member.db)
    mint_user_app_key(member.session, member.app["id"], label="Notes", path=member.db)

    with pytest.raises(UserPortalError) as refused:
        rename_user_app_key(member.session, key["id"], label, path=member.db)

    assert (refused.value.code, refused.value.status_code) == (code, status)
    assert admin_store.get_credential(key["id"], path=member.db)["display_name"] == "Editor"


def test_a_member_cannot_rename_another_persons_key_a_revoked_key_or_a_missing_one(member):
    other = person(member.db, "other")
    theirs = make_key(member.db, make_app(member.db, other, "Theirs"), "Theirs")
    mine = mint_user_app_key(member.session, member.app["id"], label="Mine", path=member.db)
    admin_store.revoke_credential(mine["id"], path=member.db)

    for credential_id in (theirs["id"], mine["id"], "cred_missing"):
        with pytest.raises(UserPortalError) as refused:
            rename_user_app_key(member.session, credential_id, "Mine now", path=member.db)
        assert (refused.value.code, refused.value.status_code) == ("credential_not_found", 404)
    assert admin_store.get_credential(theirs["id"], path=member.db)["display_name"] == "Theirs"


# -- receipts keep their name; new ones carry the new one -------------------------------------------

def test_a_request_names_its_app_as_it_is_called_when_the_request_arrives_and_old_receipts_keep_their_name(member, tmp_path):
    from gateway import receipt_store
    from gateway.receipts import RouteReceipt

    receipts = tmp_path / "receipts.db"
    receipt_store.init_db(receipts)
    key = mint_user_app_key(member.session, member.app["id"], label="Editor", path=member.db)
    digest = hash_api_key(key["api_key"])

    def receipt_for_a_request(request_id):
        # Every receipt a route writes says ``api_key_name=auth_context.identity.name``: the identity of the request.
        identity = admin_runtime.runtime_identity_for_app_credential_hash(digest, path=member.db)
        receipt_store.record(RouteReceipt(request_id=request_id, protocol="openai.chat", intent="chat.generate",
                                          app_id=identity.app_id, api_key_name=identity.name, status="succeeded"),
                             path=receipts)
        return receipt_store.get(request_id, audience="internal", path=receipts)

    old = receipt_for_a_request("req_before")
    rename_user_app(member.session, member.app["id"], "Work laptop", path=member.db)
    rename_user_app_key(member.session, key["id"], "Code editor", path=member.db)
    new = receipt_for_a_request("req_after")

    assert (old["api_key_name"], new["api_key_name"]) == ("Laptop", "Work laptop")
    assert receipt_store.get("req_before", audience="internal", path=receipts)["api_key_name"] == "Laptop"   # not rewritten
    assert old["app_id"] == new["app_id"] == member.app["id"]
    with closing(sqlite3.connect(receipts)) as conn:
        assert conn.execute("SELECT api_key_name FROM receipts WHERE request_id='req_before'").fetchone() == ("Laptop",)


# -- the REST routes, behind real sign-ins ----------------------------------------------------------

def owner_id(g):
    return g.user(OWNER)["id"]


def owners_plan(g):
    return admin_store.person_plan_id(owner_id(g), path=g.path)


def member_client(g, sub="friend"):
    g.set_rules([], fallback="open")
    client, response = signed_in(g, sub=sub, email=f"{sub}@example.test")
    assert response.status_code == 302, response.text
    return client


def admin_app(client, name, **extra):
    created = client.post("/admin/api/apps", headers=csrf(client), json={"name": name, "access_plan": "person", **extra})
    assert created.status_code == 201, created.text
    return created.json()["app"]["id"]


def admin_key(client, app_id, label="first"):
    """A key on the App, issued by the App's accountable person: an administrator may not mint on someone else's App."""
    import proxy
    from tests.support.app_keys import accountable_person, issue_app_credential

    return issue_app_credential(proxy.ADMIN_STORE.path, app_id, label=label)


def rename_key(client, app_id, credential_id, label, headers=None):
    return client.patch(f"/admin/api/apps/{app_id}/keys/{credential_id}",
                        headers=csrf(client) if headers is None else headers, json={"display_name": label})


def test_the_owner_renames_an_app_and_a_key_over_rest_and_the_audit_records_both(sg):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    key = admin_key(owner, app_id, "Editor")

    app = owner.patch(f"/admin/api/apps/{app_id}", headers=csrf(owner), json={"name": "  Work laptop "})
    renamed = rename_key(owner, app_id, key["id"], " Code editor ")

    assert app.status_code == 200 and app.json()["app"]["name"] == "Work laptop", app.text
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["credential"]["display_name"] == "Code editor"
    assert renamed.json()["credential"]["id"] == key["id"]
    assert "api_key" not in renamed.text and "hash" not in renamed.json()["credential"]
    listed = {item["id"]: item for item in owner.get("/admin/api/credentials").json()["credentials"]}
    assert listed[key["id"]]["display_name"] == "Code editor"
    audit = {row["action"]: row for row in sg.rows("SELECT action, metadata_json FROM audit_events")
             if row["action"] in {"app.update", "credential.rename"}}
    assert "Laptop" in audit["app.update"]["metadata_json"] and "Work laptop" in audit["app.update"]["metadata_json"]
    assert "Editor" in audit["credential.rename"]["metadata_json"] and "Code editor" in audit["credential.rename"]["metadata_json"]
    assert key["api_key"] not in repr(sg.rows("SELECT * FROM audit_events"))


def test_a_rename_asks_for_no_confirmation_from_a_stale_session_but_still_a_live_one(sg):
    """D36: a label is not a credential. A sign-in that has not confirmed itself lately may still rename."""
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    key = admin_key(owner, app_id, "Editor")
    stale = stale_owner(sg)

    app = stale.patch(f"/admin/api/apps/{app_id}", headers=csrf(stale), json={"name": "Work laptop"})
    renamed = rename_key(stale, app_id, key["id"], "Code editor")

    assert app.status_code == 200 and renamed.status_code == 200, (app.text, renamed.text)


def test_an_admin_key_renames_without_a_browser_session_or_csrf_header(sg):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    key = admin_key(owner, app_id, "Editor")
    bearer = {"authorization": f"Bearer {mint_admin_key(owner, role='owner', name='robot')}"}

    renamed = sg.client().patch(f"/admin/api/apps/{app_id}/keys/{key['id']}", headers=bearer, json={"display_name": "By robot"})

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["credential"]["display_name"] == "By robot"
    [event] = [row for row in sg.rows("SELECT * FROM audit_events WHERE action='credential.rename'")]
    assert event["actor_kind"] == "service_account"


def test_a_browser_rename_without_the_csrf_header_is_refused_and_changes_nothing(sg):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    key = admin_key(owner, app_id, "Editor")

    app = owner.patch(f"/admin/api/apps/{app_id}", json={"name": "Nope"})
    renamed = rename_key(owner, app_id, key["id"], "Nope", headers={})

    assert app.status_code == 403 and renamed.status_code == 403, (app.text, renamed.text)
    assert sg.rows("SELECT name FROM apps WHERE id=?", (app_id,)) == [{"name": "Laptop"}]
    assert sg.rows("SELECT display_name FROM credentials WHERE id=?", (key["id"],)) == [{"display_name": "Editor"}]


def test_a_signed_out_caller_and_a_member_cannot_rename_anything_over_the_admin_routes(sg):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    key = admin_key(owner, app_id, "Editor")
    member = member_client(sg)
    signed_out = sg.client()

    for client, headers, expected in ((signed_out, {}, 401), (member, csrf_for(member), 403)):
        assert client.patch(f"/admin/api/apps/{app_id}", headers=headers, json={"name": "Nope"}).status_code == expected
        assert rename_key(client, app_id, key["id"], "Nope", headers=headers).status_code == expected
    assert sg.rows("SELECT name FROM apps WHERE id=?", (app_id,)) == [{"name": "Laptop"}]
    assert sg.rows("SELECT display_name FROM credentials WHERE id=?", (key["id"],)) == [{"display_name": "Editor"}]


def test_an_administrator_renames_a_members_and_their_own_app_and_keys_but_not_the_owners(sg):
    owner = owner_client(sg)
    admin = helper_client(sg)
    member = member_client(sg)
    member_user = sg.user("friend@example.test")
    owners = admin_app(owner, "Owners", owner_user_id=owner_id(sg), kind="service")
    members = admin_app(owner, "Members", owner_user_id=member_user["id"], kind="service")
    mine = admin_app(admin, "Admins")
    keys = {"owners": admin_key(owner, owners, "Editor"), "members": admin_key(owner, members, "Editor"),
            "mine": admin_key(admin, mine, "Editor")}

    refused_app = admin.patch(f"/admin/api/apps/{owners}", headers=csrf(admin), json={"name": "Taken over"})
    refused_key = rename_key(admin, owners, keys["owners"]["id"], "Taken over")

    for refused in (refused_app, refused_key):
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "app_outranks_you", refused.text
    assert sg.rows("SELECT name FROM apps WHERE id=?", (owners,)) == [{"name": "Owners"}]
    assert sg.rows("SELECT display_name FROM credentials WHERE id=?", (keys["owners"]["id"],)) == [{"display_name": "Editor"}]
    assert admin.patch(f"/admin/api/apps/{members}", headers=csrf(admin), json={"name": "Renamed by admin"}).status_code == 200
    assert rename_key(admin, members, keys["members"]["id"], "Renamed by admin").status_code == 200
    assert admin.patch(f"/admin/api/apps/{mine}", headers=csrf(admin), json={"name": "Renamed mine"}).status_code == 200
    assert rename_key(admin, mine, keys["mine"]["id"], "Renamed mine").status_code == 200
    assert rename_key(owner, owners, keys["owners"]["id"], "Owner may").status_code == 200


def test_an_admin_key_of_the_admin_role_is_outranked_by_the_owners_app_too(sg):
    owner = owner_client(sg)
    owners = admin_app(owner, "Owners", owner_user_id=owner_id(sg), kind="service")
    key = admin_key(owner, owners, "Editor")
    bearer = {"authorization": f"Bearer {mint_admin_key(owner, role='admin', name='admin robot')}"}

    refused = sg.client().patch(f"/admin/api/apps/{owners}/keys/{key['id']}", headers=bearer, json={"display_name": "No"})

    assert refused.status_code == 403 and refused.json()["error"]["code"] == "app_outranks_you", refused.text


@pytest.mark.parametrize("label, code, status", [
    ("", "key_label_required", 400), ("   ", "key_label_required", 400), ("x" * 81, "key_label_too_long", 400),
    ("a\nb", "key_label_not_allowed", 400), ("Notes", "key_label_taken", 409),
])
def test_the_key_route_says_why_a_label_is_refused(sg, label, code, status):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    key = admin_key(owner, app_id, "Editor")
    admin_key(owner, app_id, "Notes")

    refused = rename_key(owner, app_id, key["id"], label)

    assert refused.status_code == status and refused.json()["error"]["code"] == code, refused.text
    assert refused.json()["error"]["message"]
    assert sg.rows("SELECT display_name FROM credentials WHERE id=?", (key["id"],)) == [{"display_name": "Editor"}]


@pytest.mark.parametrize("body", [{}, {"display_name": None}, {"display_name": 7}, {"name": "wrong field"}])
def test_the_key_route_needs_a_text_display_name(sg, body):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    key = admin_key(owner, app_id, "Editor")

    refused = owner.patch(f"/admin/api/apps/{app_id}/keys/{key['id']}", headers=csrf(owner), json=body)

    assert refused.status_code == 400 and refused.json()["error"]["code"] == "key_label_required", refused.text


def test_the_app_route_says_why_a_name_is_refused(sg):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    admin_app(owner, "Phone")

    for name, code, status in (("x" * 81, "app_name_too_long", 400), ("a\x07", "app_name_not_allowed", 400),
                               ("Phone", "app_name_taken", 409), ("Try", "app_name_reserved", 409)):
        refused = owner.patch(f"/admin/api/apps/{app_id}", headers=csrf(owner), json={"name": name})
        assert refused.status_code == status and refused.json()["error"]["code"] == code, (name, refused.text)
    assert owner.patch(f"/admin/api/apps/{app_id}", headers=csrf(owner), json={"name": ""}).status_code == 400
    assert sg.rows("SELECT name FROM apps WHERE id=?", (app_id,)) == [{"name": "Laptop"}]


def test_the_key_route_404s_for_a_missing_app_a_missing_key_a_key_of_another_app_and_a_revoked_key(sg):
    owner = owner_client(sg)
    laptop, phone = admin_app(owner, "Laptop"), admin_app(owner, "Phone")
    key, revoked = admin_key(owner, laptop, "Editor"), admin_key(owner, laptop, "Old")
    assert owner.delete(f"/admin/api/credentials/{revoked['id']}", headers=csrf(owner)).status_code == 200

    cases = (("app_missing", key["id"], "app_not_found"), (laptop, "cred_missing", "credential_not_found"),
             (phone, key["id"], "credential_not_found"), (laptop, revoked["id"], "credential_not_found"))
    for app_id, credential_id, code in cases:
        refused = rename_key(owner, app_id, credential_id, "Nope")
        assert refused.status_code == 404 and refused.json()["error"]["code"] == code, (app_id, refused.text)


def test_the_key_route_will_not_rename_an_admin_automation_key(sg):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    admin_key_record = owner.post("/admin/api/admin-keys", headers=csrf(owner),
                                  json={"name": "robot", "role": "admin"}).json()["credential"]

    refused = rename_key(owner, app_id, admin_key_record["id"], "Renamed")

    assert refused.status_code == 404, refused.text
    assert sg.rows("SELECT display_name FROM credentials WHERE id=?", (admin_key_record["id"],)) == [
        {"display_name": "robot admin automation key"}]


def test_an_app_is_not_renamed_after_sign_out_everywhere(sg, monkeypatch):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    headers = csrf(owner)
    sign_out_just_before(sg, monkeypatch, admin_store, "update_app", cookie_session(owner).subject)

    response = owner.patch(f"/admin/api/apps/{app_id}", headers=headers, json={"name": "Late"})

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT name FROM apps WHERE id=?", (app_id,)) == [{"name": "Laptop"}]


def test_a_key_is_not_renamed_after_sign_out_everywhere(sg, monkeypatch):
    owner = owner_client(sg)
    app_id = admin_app(owner, "Laptop")
    key = admin_key(owner, app_id, "Editor")
    headers = csrf(owner)
    sign_out_just_before(sg, monkeypatch, admin_store, "rename_app_credential", cookie_session(owner).subject)

    response = rename_key(owner, app_id, key["id"], "Late", headers=headers)

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT display_name FROM credentials WHERE id=?", (key["id"],)) == [{"display_name": "Editor"}]


# -- /me ----------------------------------------------------------------------------------------------

def me_headers(client):
    return {**csrf_for(client), **ACT}


def me_app(client, name):
    created = client.post("/me/api/apps", headers=me_headers(client), json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["app"]["id"]


def me_key(client, app_id, label="Editor"):
    minted = client.post(f"/me/api/apps/{app_id}/keys", headers=me_headers(client), json={"label": label})
    assert minted.status_code == 201, minted.text
    return minted.json()["credential"]


def test_a_member_renames_their_own_app_and_key_over_me_and_the_session_shows_the_saved_names(sg):
    member = member_client(sg)
    app_id = me_app(member, "Laptop")
    key = me_key(member, app_id)

    app = member.patch(f"/me/api/apps/{app_id}", headers=me_headers(member), json={"name": " Work laptop "})
    renamed = member.patch(f"/me/api/credentials/{key['id']}", headers=me_headers(member), json={"display_name": " Code editor "})

    assert app.status_code == 200 and app.json()["app"]["name"] == "Work laptop", app.text
    assert renamed.status_code == 200 and renamed.json()["credential"]["display_name"] == "Code editor", renamed.text
    assert renamed.headers["cache-control"] == "no-store"
    for secret in ("api_key", "hash"):
        assert secret not in renamed.json()["credential"]
    shown = [item for item in member.get("/me/api/session").json()["apps"] if item["id"] == app_id][0]
    assert shown["name"] == "Work laptop" and [c["display_name"] for c in shown["credentials"]] == ["Code editor"]
    assert key["api_key"] not in member.get("/me/api/session").text


def test_a_member_needs_no_recent_confirmation_to_rename_but_does_need_the_action_and_csrf_headers(sg):
    member = member_client(sg)
    app_id = me_app(member, "Laptop")
    key = me_key(member, app_id)
    forge(sg, member, "friend@example.test", issued_ago=20 * MINUTE, renewed_ago=0, authenticated_ago=20 * MINUTE,
          sid="stale_member_session")

    stale_app = member.patch(f"/me/api/apps/{app_id}", headers=me_headers(member), json={"name": "Stale is fine"})
    stale_key = member.patch(f"/me/api/credentials/{key['id']}", headers=me_headers(member), json={"display_name": "Stale is fine"})
    no_action = member.patch(f"/me/api/apps/{app_id}", headers=csrf_for(member), json={"name": "No action header"})
    no_csrf = member.patch(f"/me/api/credentials/{key['id']}", headers=ACT, json={"display_name": "No csrf"})

    assert stale_app.status_code == 200 and stale_key.status_code == 200, (stale_app.text, stale_key.text)
    assert no_action.status_code == 403 and no_csrf.status_code == 403
    assert sg.rows("SELECT name FROM apps WHERE id=?", (app_id,)) == [{"name": "Stale is fine"}]


def test_a_signed_out_caller_cannot_rename_over_me(sg):
    member = member_client(sg)
    app_id = me_app(member, "Laptop")
    key = me_key(member, app_id)
    headers = me_headers(member)

    signed_out = sg.client()
    app = signed_out.patch(f"/me/api/apps/{app_id}", headers=headers, json={"name": "Nope"})
    renamed = signed_out.patch(f"/me/api/credentials/{key['id']}", headers=headers, json={"display_name": "Nope"})

    assert app.status_code == 401 and renamed.status_code == 401, (app.text, renamed.text)
    assert sg.rows("SELECT name FROM apps WHERE id=?", (app_id,)) == [{"name": "Laptop"}]


def test_a_member_cannot_rename_another_members_app_or_key_and_cannot_tell_it_exists(sg):
    mine, theirs = member_client(sg, "mine"), member_client(sg, "theirs")
    their_app = me_app(theirs, "Theirs")
    their_key = me_key(theirs, their_app, "Theirs")
    missing_app = mine.patch("/me/api/apps/app_missing", headers=me_headers(mine), json={"name": "Nope"})

    stolen_app = mine.patch(f"/me/api/apps/{their_app}", headers=me_headers(mine), json={"name": "Stolen"})
    stolen_key = mine.patch(f"/me/api/credentials/{their_key['id']}", headers=me_headers(mine), json={"display_name": "Stolen"})

    assert stolen_app.status_code == 404 and stolen_app.json() == missing_app.json(), stolen_app.text
    assert stolen_key.status_code == 404 and stolen_key.json()["error"]["code"] == "credential_not_found", stolen_key.text
    assert sg.rows("SELECT name FROM apps WHERE id=?", (their_app,)) == [{"name": "Theirs"}]
    assert sg.rows("SELECT display_name FROM credentials WHERE id=?", (their_key["id"],)) == [{"display_name": "Theirs"}]


def test_an_owner_over_me_renames_only_their_own_apps_the_admin_routes_are_for_others(sg):
    owner = owner_client(sg)
    member = member_client(sg)
    members = me_app(member, "Members")

    refused = owner.patch(f"/me/api/apps/{members}", headers=me_headers(owner), json={"name": "Taken over"})

    assert refused.status_code == 404, refused.text
    assert sg.rows("SELECT name FROM apps WHERE id=?", (members,)) == [{"name": "Members"}]
    assert owner.patch(f"/admin/api/apps/{members}", headers=csrf(owner), json={"name": "Owner may"}).status_code == 200


@pytest.mark.parametrize("body, code, status", [
    ({"name": ""}, "app_name_required", 400), ({}, "app_name_required", 400), ({"name": 3}, "app_name_required", 400),
    ({"name": "x" * 81}, "app_name_too_long", 400), ({"name": "a\nb"}, "app_name_not_allowed", 400),
    ({"name": "Phone"}, "app_name_taken", 409), ({"name": "try"}, "app_name_reserved", 409),
])
def test_me_says_why_an_app_name_is_refused(sg, body, code, status):
    member = member_client(sg)
    app_id = me_app(member, "Laptop")
    me_app(member, "Phone")

    refused = member.patch(f"/me/api/apps/{app_id}", headers=me_headers(member), json=body)

    assert refused.status_code == status and refused.json()["error"]["code"] == code, refused.text
    assert refused.json()["error"]["message"]
    assert sg.rows("SELECT name FROM apps WHERE id=?", (app_id,)) == [{"name": "Laptop"}]


@pytest.mark.parametrize("body, code, status", [
    ({"display_name": ""}, "key_label_required", 400), ({}, "key_label_required", 400),
    ({"display_name": "x" * 81}, "key_label_too_long", 400), ({"display_name": "a\tb"}, "key_label_not_allowed", 400),
    ({"display_name": "Notes"}, "key_label_taken", 409),
])
def test_me_says_why_a_key_name_is_refused(sg, body, code, status):
    member = member_client(sg)
    app_id = me_app(member, "Laptop")
    key = me_key(member, app_id, "Editor")
    me_key(member, app_id, "Notes")

    refused = member.patch(f"/me/api/credentials/{key['id']}", headers=me_headers(member), json=body)

    assert refused.status_code == status and refused.json()["error"]["code"] == code, refused.text
    assert sg.rows("SELECT display_name FROM credentials WHERE id=?", (key["id"],)) == [{"display_name": "Editor"}]


def test_the_try_page_app_keeps_its_name_over_me(sg):
    member = member_client(sg)
    assert member.post("/me/api/try-key", headers=me_headers(member), json={}).status_code == 201
    try_app = sg.rows("SELECT id FROM apps WHERE name='Try'")[0]["id"]

    refused = member.patch(f"/me/api/apps/{try_app}", headers=me_headers(member), json={"name": "Renamed"})

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "app_name_reserved", refused.text
    assert member.post("/me/api/try-key", headers=me_headers(member), json={}).status_code in (200, 201)
    assert sg.rows("SELECT COUNT(*) AS n FROM apps WHERE lower(name)='try'") == [{"n": 1}]


def test_an_app_is_not_renamed_over_me_after_sign_out_everywhere(sg, monkeypatch):
    member = member_client(sg)
    app_id = me_app(member, "Laptop")
    headers = me_headers(member)
    sign_out_just_before(sg, monkeypatch, admin_store, "update_app", cookie_session(member).subject)

    response = member.patch(f"/me/api/apps/{app_id}", headers=headers, json={"name": "Late"})

    assert response.status_code == 401 and response.json()["error"]["code"] == "login_required", response.text
    assert sg.rows("SELECT name FROM apps WHERE id=?", (app_id,)) == [{"name": "Laptop"}]


def test_a_key_is_not_renamed_over_me_after_sign_out_everywhere(sg, monkeypatch):
    member = member_client(sg)
    app_id = me_app(member, "Laptop")
    key = me_key(member, app_id)
    headers = me_headers(member)
    sign_out_just_before(sg, monkeypatch, admin_store, "rename_app_credential", cookie_session(member).subject)

    response = member.patch(f"/me/api/credentials/{key['id']}", headers=headers, json={"display_name": "Late"})

    assert response.status_code == 401 and response.json()["error"]["code"] == "login_required", response.text
    assert sg.rows("SELECT display_name FROM credentials WHERE id=?", (key["id"],)) == [{"display_name": "Editor"}]


def test_a_renamed_key_still_opens_the_same_app_and_a_new_request_carries_the_new_app_name(sg):
    member = member_client(sg)
    app_id = me_app(member, "Laptop")
    key = me_key(member, app_id)
    secret = key["api_key"]
    before = admin_runtime.runtime_identity_for_app_credential_hash(hash_api_key(secret), path=sg.path)

    assert member.patch(f"/me/api/apps/{app_id}", headers=me_headers(member), json={"name": "Work laptop"}).status_code == 200
    assert member.patch(f"/me/api/credentials/{key['id']}", headers=me_headers(member),
                        json={"display_name": "Code editor"}).status_code == 200
    after = admin_runtime.runtime_identity_for_app_credential_hash(hash_api_key(secret), path=sg.path)

    assert (before.name, after.name) == ("Laptop", "Work laptop")
    assert (after.app_id, after.key_hash) == (before.app_id, before.key_hash)


# -- the command line: Restish is generated from the owner OpenAPI document --------------------------------------

def test_the_owner_openapi_document_gives_the_command_line_both_rename_commands(sg):
    sg.proxy.app.openapi_schema = None
    schema = sg.proxy.app.openapi()

    commands = {
        (path, "patch"): schema["paths"][path]["patch"]
        for path in ("/admin/api/apps/{app_id}", "/admin/api/apps/{app_id}/keys/{credential_id}")}

    assert [op["operationId"].replace("_", "-") for op in commands.values()] == [
        "owner-patch-admin-api-apps-app-id", "owner-patch-admin-api-apps-app-id-keys-credential-id"]
    for op in commands.values():
        assert op["security"] == [{"OwnerBearer": []}] and op["tags"] == ["apps and keys"]
    assert not any(path.startswith("/me/api/") for path in schema["paths"]), "a person's own routes stay out of the owner document"


# -- review round: one comparison key, names with nothing visible, audit inside the write --------------------------

@pytest.mark.parametrize("first, second", [
    ("Café", "CAFÉ"), ("Café", "café"), ("Café", "Ｃａｆé"), ("straße", "STRASSE"),
])
def test_two_names_that_differ_only_by_case_or_unicode_form_are_the_same_name(db, first, second):
    mine = person(db)
    one, other = make_app(db, mine, first), make_app(db, mine, "Second")

    with pytest.raises(admin_store.NameRejected) as taken:
        admin_store.update_app(other["id"], name=second, path=db)

    assert taken.value.code == "app_name_taken" and taken.value.status_code == 409
    assert admin_store.get_app(other["id"], path=db)["name"] == "Second"
    assert admin_store.update_app(one["id"], name=second, path=db)["name"] == second      # its own name, written another way


@pytest.mark.parametrize("first, second", [("Café", "CAFÉ"), ("Café", "café")])
def test_key_labels_compare_the_same_way(db, first, second):
    laptop = make_app(db, person(db), "Laptop")
    make_key(db, laptop, first)
    other = make_key(db, laptop, "Notes")

    with pytest.raises(admin_store.NameRejected) as taken:
        admin_store.rename_app_credential(other["id"], second, path=db)

    assert taken.value.code == "key_label_taken"


def test_an_mcp_client_naming_an_app_or_key_uses_the_same_comparison(db):
    mine = person(db)
    cafe = make_app(db, mine, "Café")
    key = make_key(db, cafe, "Notes É")

    renamed = user.rename_person_app(mine["id"], "CAFÉ", "Coffee", via="mcp", path=db)
    labelled = user.rename_person_key(mine["id"], "Coffee", "NOTES é", "Memo", via="mcp", path=db)

    assert renamed["app"]["id"] == cafe["id"] and labelled["id"] == key["id"]


def test_a_fullwidth_try_is_the_reserved_name_but_a_cyrillic_lookalike_is_just_another_name(db):
    other = make_app(db, person(db), "Laptop")

    with pytest.raises(admin_store.NameRejected) as reserved:
        admin_store.update_app(other["id"], name="Ｔｒｙ", path=db)

    assert reserved.value.code == "app_name_reserved"
    assert admin_store.update_app(other["id"], name="Trу", path=db)["name"] == "Trу"


@pytest.mark.parametrize("text", [
    "\u200b", "\u2060", "\ufeff", "\u200b\u2060\ufeff", " \u200b ", "\u0301", "\u200d", "\u00a0\u200b"])
def test_a_name_with_nothing_visible_is_no_name(text):
    with pytest.raises(admin_store.NameRejected) as refused:
        admin_store.clean_name(text, code="app_name", noun="app")

    assert refused.value.code == "app_name_required"


@pytest.mark.parametrize("text", ["T\u200bry", "e\u0301", "\U0001F468\u200d\U0001F469\u200d\U0001F467", "\u2764\ufe0f", "a\u2060b"])
def test_a_name_with_a_visible_character_may_carry_invisible_ones(text):
    assert admin_store.clean_name(text, code="app_name", noun="app") == text


def test_the_previous_name_in_the_audit_is_the_one_the_write_replaced_even_when_another_rename_came_between(db, monkeypatch):
    mine = person(db)
    app = make_app(db, mine, "Initial")
    original = admin_store.update_app

    def interleaved(app_id, **kwargs):
        original(app_id, name="Concurrent", path=db)       # commits between the caller's read and its write
        return original(app_id, **kwargs)

    monkeypatch.setattr(admin_store, "update_app", interleaved)
    user.rename_person_app(mine["id"], app["id"], "Final", via="review", path=db)

    [event] = [e for e in admin_store.list_audit(action_prefix="app.self_rename", path=db)]
    assert event["metadata"]["previous_name"] == "Concurrent" and event["metadata"]["name"] == "Final"


def test_the_previous_label_in_the_audit_is_the_one_the_write_replaced_even_when_another_rename_came_between(db, monkeypatch):
    mine = person(db)
    app = make_app(db, mine, "Laptop")
    key = make_key(db, app, "Initial key")
    original = admin_store.rename_app_credential

    def interleaved(key_id, label, **kwargs):
        original(key_id, "Concurrent key", path=db)
        return original(key_id, label, **kwargs)

    monkeypatch.setattr(admin_store, "rename_app_credential", interleaved)
    user.rename_person_key(mine["id"], app["id"], key["id"], "Final key", via="review", path=db)

    [event] = admin_store.list_audit(action_prefix="credential.self_rename", path=db)
    assert event["metadata"]["previous_name"] == "Concurrent key" and event["metadata"]["name"] == "Final key"


def test_a_rename_and_its_audit_fact_commit_together_or_not_at_all(db, monkeypatch):
    mine = person(db)
    app = make_app(db, mine, "Laptop")
    key = make_key(db, app, "Editor")

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("audit table is locked")

    monkeypatch.setattr(admin_store, "_record_audit_in_transaction", broken)
    with pytest.raises(sqlite3.OperationalError):
        admin_store.update_app(app["id"], name="Work", audit={"action": "app.update"}, path=db)
    with pytest.raises(sqlite3.OperationalError):
        admin_store.rename_app_credential(key["id"], "Code", audit={"action": "credential.rename"}, path=db)

    assert admin_store.get_app(app["id"], path=db)["name"] == "Laptop"
    assert admin_store.get_credential(key["id"], path=db)["display_name"] == "Editor"
