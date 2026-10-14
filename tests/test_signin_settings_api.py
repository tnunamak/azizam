"""Settings > Sign-in over HTTP: who may read and change it, what Save does, and what sign-in enforces.

Real routes, a real database, a fake identity provider. The preview and the
sign-in callback are checked against each other: they must give one answer.
"""
from dataclasses import replace

import pytest

from gateway import accounts, admin_store, signin_admin, signin_test
from tests.support.access import store_plan
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    OWNER, SIGNING_KEY, TRUSTED, csrf_for, domain_rule, gateway, roles_of, signed_in,
)

HELPER = "helper@example.test"
ISSUER = "https://id.example/realms/home"


@pytest.fixture
def sg(gateway, monkeypatch):
    """The sign-in tests' gateway: sessions keep one signing key after the settings are saved."""
    monkeypatch.setattr(gateway.proxy, "ADMIN_SESSION_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2 ** 10)       # a real password hash, at a test's cost
    signin_test.clear()
    yield gateway
    signin_test.clear()


def owner_client(g, **claims):
    client, response = signed_in(g, sub="owner", email=OWNER, **claims)
    assert response.status_code == 302, response.text
    return client


def helper_client(g, groups=("/Trusted",)):
    client, response = signed_in(g, sub="helper", email=HELPER, groups=list(groups))
    assert response.status_code == 302, response.text
    return client


def get_snapshot(client):
    response = client.get("/admin/api/signin")
    assert response.status_code == 200, response.text
    return response.json()


def proposal(snapshot, *, methods=None, rules=None, **provider):
    p = snapshot["provider"]
    connection = {
        "name": p["name"], "address": p["address"], "client_id": p["client_id"], "client_secret": "",
        "scopes": p["scopes"], "groups_claim": p["groups_claim"],
        "authorization_endpoint": p["authorization_endpoint"], "token_endpoint": p["token_endpoint"],
        "jwks_uri": p["jwks_uri"], "clock_skew_seconds": p["clock_skew_seconds"],
    }
    connection.update(provider)
    return {"methods": methods or dict(snapshot["methods"]), "provider": connection,
            "rules": rules if rules is not None else snapshot["rules"]}


def rule(field, values=(), *, admit=False, admin=False, plan=None, id="r"):
    return {"id": id, "field": field, "values": list(values), "admit": admit, "admin": admin, "plan": plan}


def save(client, snapshot, body, **extra):
    request = {"proposal": body, "test_id": None, "keep_admin_keys": [], "save_for_later": False, **extra}
    return client.put("/admin/api/signin", json=request,
                      headers={**csrf_for(client), "If-Match": snapshot["revision"]})


def preview(client, body, test_id=None):
    response = client.post("/admin/api/signin/preview", json={"proposal": body, "test_id": test_id},
                           headers=csrf_for(client))
    assert response.status_code == 200, response.text
    return response.json()


def person_line(result, email_or_name):
    return next((p for p in result["people"] if p["name"] == email_or_name), None)


def mint_admin_key(client, name="automation"):
    minted = client.post("/admin/api/admin-keys", headers=csrf_for(client), json={"name": name, "role": "admin"})
    assert minted.status_code == 201, minted.text
    return minted.json()["credential"]["api_key"]


def bearer_status(g, key, path="/admin/api/access-plans"):
    return g.client().get(path, headers={"authorization": f"Bearer {key}"}).status_code


def user_id(g, email):
    return g.user(email)["id"]


# ---------------------------------------------------------------------------
# Only the owner changes sign-in; an administrator may read it
# ---------------------------------------------------------------------------

OWNER_ONLY = [
    ("post", "/admin/api/signin/preview", {"proposal": {}, "test_id": None}),
    ("post", "/admin/api/signin/discover", {"address": "https://x.example"}),
    ("post", "/admin/api/signin/test", {"provider": {}}),
    ("get", "/admin/api/signin/test/nope", None),
    ("delete", "/admin/api/signin/test/nope", None),
    ("get", "/admin/api/signin/test/nope/start", None),
    ("post", "/admin/api/signin/link-owner", {"test_id": "nope"}),
    ("put", "/admin/api/signin", {"proposal": {}}),
    ("post", "/admin/api/signin/notice/dismiss", None),
    ("post", "/admin/api/signin/owner-password", {"password": "a long enough password"}),
]


@pytest.mark.parametrize("method,path,body", OWNER_ONLY)
def test_every_sign_in_write_and_test_is_owner_only(sg, method, path, body):
    owner_client(sg)
    admin = helper_client(sg)
    kwargs = {"headers": csrf_for(admin)}
    if body is not None:
        kwargs["json"] = body
    response = getattr(admin, method)(path, **kwargs)
    assert response.status_code == 403, (path, response.text)
    assert response.json()["error"]["code"] == "owner_required"


def test_an_administrator_can_read_the_sign_in_page_but_not_who_matches_a_rule(sg):
    sg.set_rules([TRUSTED])
    owner = owner_client(sg)
    admin = helper_client(sg)

    as_admin = get_snapshot(admin)
    as_owner = get_snapshot(owner)

    assert as_admin["viewer"] == {"role": "admin", "can_edit": False}
    assert as_owner["viewer"] == {"role": "owner", "can_edit": True}
    assert as_owner["rule_matches"]["trusted"]["names"] == ["helper@example.test"]
    assert as_admin["rule_matches"]["trusted"] == {"count": 1, "names": []}


def test_the_snapshot_needs_a_sign_in(sg):
    assert sg.client().get("/admin/api/signin").status_code in {401, 403}


# ---------------------------------------------------------------------------
# What the snapshot says
# ---------------------------------------------------------------------------

def test_the_provider_name_is_the_typed_name_else_the_address_host(sg):
    owner = owner_client(sg)
    assert get_snapshot(owner)["provider"]["effective_name"] == "id.example"
    assert get_snapshot(owner)["provider"]["host"] == "id.example"

    sg.set_config(replace(sg.config, display_name="Family account"))
    assert get_snapshot(owner)["provider"]["effective_name"] == "Family account"


def test_the_snapshot_never_carries_the_client_secret(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    assert snapshot["provider"]["client_secret_saved"] is True
    assert "test-secret" not in str(snapshot)


def test_the_snapshot_describes_the_methods_the_redirect_address_and_the_rules(sg):
    sg.set_rules([TRUSTED, domain_rule("example.test")], fallback="open")
    owner = owner_client(sg)

    snapshot = get_snapshot(owner)

    assert snapshot["methods"] == {"password": True, "provider": True}
    assert snapshot["provider"]["redirect_uri"] == "https://ai.example/admin/oidc/callback"
    assert snapshot["rules"]["fallback"] == "open"
    assert [r["field"] for r in snapshot["rules"]["rules"]] == ["group", "domain"]
    assert snapshot["recovery_command"].endswith("generate-admin-key owner-cli")


def test_the_pickers_offer_the_values_past_sign_ins_carried_with_counts(sg):
    owner = owner_client(sg, groups=["/Trusted", "/Family"])
    helper_client(sg, groups=("/Trusted",))

    seen = get_snapshot(owner)["seen"]

    assert {v["value"]: v["count"] for v in seen["group"]} == {"/Family": 1, "/Trusted": 2}
    assert {v["value"] for v in seen["email"]} == {OWNER, HELPER}
    assert seen["domain"] == [{"value": "example.test", "count": 2}]
    assert seen["role"] == []


def test_the_owner_without_a_password_is_reported_so_the_page_can_ask_for_one(sg):
    owner = owner_client(sg)
    assert get_snapshot(owner)["owner"] == {"name": OWNER, "email": OWNER, "has_password": False}


# ---------------------------------------------------------------------------
# Preview: by name, by the same evaluator
# ---------------------------------------------------------------------------

def test_an_unchanged_form_changes_nothing_and_needs_no_test(sg):
    owner = owner_client(sg)
    helper_client(sg)
    snapshot = get_snapshot(owner)

    result = preview(owner, proposal(snapshot))

    assert result["people"] == [] and result["settings"] == [] and result["blockers"] == []
    assert set(result["unaffected"]) == {OWNER, HELPER}
    assert result["issuer_change"] is None and result["test_verdict"] is None


def test_removing_the_administrator_rule_names_who_loses_what_and_their_admin_keys(sg):
    owner = owner_client(sg)
    helper = helper_client(sg)
    mint_admin_key(helper, "one")
    mint_admin_key(helper, "two")
    snapshot = get_snapshot(owner)

    result = preview(owner, proposal(snapshot, rules={"fallback": "closed", "rules": []}))

    line = person_line(result, HELPER)
    assert line["admin"] == {"before": True, "after": False}
    assert line["sign_in"]["before"] is True and line["sign_in"]["after"] is False
    assert line["sign_in"]["via"] == "provider"
    assert line["admin_keys"] == 2
    assert OWNER in result["unaffected"]            # the owner is never touched by rules


def test_a_plan_rule_names_who_gets_the_plan(sg):
    plan = store_plan("Family", path=sg.path, caps=("chat",))
    owner = owner_client(sg)
    helper_client(sg)
    snapshot = get_snapshot(owner)
    rules = {"fallback": "closed", "rules": [*snapshot["rules"]["rules"],
                                             rule("group", ["/Trusted"], plan=plan["id"], id="plan")]}

    result = preview(owner, proposal(snapshot, rules=rules))

    assert person_line(result, HELPER)["plan"] == {"before": None, "after": {"id": plan["id"], "name": "Family"}}


def test_a_plan_set_by_hand_is_not_a_change_a_rule_can_make(sg):
    plan = store_plan("Family", path=sg.path, caps=("chat",))
    other = store_plan("Other", path=sg.path, caps=("chat",))
    owner = owner_client(sg)
    helper_client(sg)
    accounts.change_person(user_id(sg, HELPER), "owner", plan_id=other["id"], path=sg.path)
    snapshot = get_snapshot(owner)
    rules = {"fallback": "closed", "rules": [*snapshot["rules"]["rules"],
                                             rule("group", ["/Trusted"], plan=plan["id"], id="plan")]}

    assert person_line(preview(owner, proposal(snapshot, rules=rules)), HELPER) is None


def test_turning_off_a_password_names_the_people_who_use_one(sg):
    owner = owner_client(sg)
    pw = store_plan("Pw", path=sg.path, caps=("chat",))
    _, token = accounts.create_link("invite", "owner", plan_id=pw["id"], email="pat@example.test", path=sg.path)
    accounts.accept_invite(token, "pat@example.test", "Pat", "a sufficiently long password", path=sg.path)
    snapshot = get_snapshot(owner)

    result = preview(owner, proposal(snapshot, methods={"password": False, "provider": True}))

    line = person_line(result, "Pat")
    assert line["sign_in"] == {"before": True, "after": False, "via": "password"}
    assert "password_off" in result["settings"]


def test_turning_the_provider_off_says_it_and_ends_administrators_but_not_members(sg):
    sg.set_rules([TRUSTED, domain_rule("example.test")])
    owner = owner_client(sg)
    helper_client(sg)
    signed_in(sg, sub="member", email="member@example.test")
    snapshot = get_snapshot(owner)

    result = preview(owner, proposal(snapshot, methods={"password": True, "provider": False}))

    assert "provider_off" in result["settings"]
    assert person_line(result, HELPER)["admin"] == {"before": True, "after": False}
    assert person_line(result, "member@example.test")["sign_in"]["after"] is False


def test_a_new_address_with_the_provider_on_is_an_issuer_change_not_a_list_of_people(sg):
    owner = owner_client(sg)
    helper_client(sg)
    snapshot = get_snapshot(owner)

    result = preview(owner, proposal(snapshot, address="https://id2.example/realms/home"))

    assert result["issuer_change"] == {"old_host": "id.example", "new_host": "id2.example", "people": 2}
    assert result["people"] == []
    assert "connection_changed" not in result["settings"]
    assert [b["code"] for b in result["blockers"]] == ["test_required"]


def test_a_new_address_with_the_provider_turned_off_shows_only_the_turn_off(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)

    result = preview(owner, proposal(snapshot, methods={"password": True, "provider": False},
                                     address="https://id2.example/realms/home"))

    assert result["issuer_change"] is None
    assert "provider_off" in result["settings"]


def test_the_preview_names_the_blockers_that_stop_save(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    cases = [
        (proposal(snapshot, methods={"password": False, "provider": False}), "both_methods_off"),
        (proposal(snapshot, address="not an address"), "address_invalid"),
        (proposal(snapshot, rules={"fallback": "closed", "rules": [rule("group", [], admit=True)]}), "incomplete_rule"),
        (proposal(snapshot, client_id="another-client"), "test_required"),
        (proposal(snapshot, methods={"password": True, "provider": False}), "owner_needs_password"),
    ]
    for body, code in cases:
        assert code in [b["code"] for b in preview(owner, body)["blockers"]], code


# ---------------------------------------------------------------------------
# Preview and enforcement give one answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,rules,expect", [
    ("administrator", [rule("group", ["/Trusted"], admin=True)], {"signs_in": True, "admin": True, "plan": None}),
    ("let in only", [rule("domain", ["example.test"], admit=True)], {"signs_in": True, "admin": False, "plan": None}),
    ("plan rule", [rule("domain", ["example.test"], admit=True), rule("group", ["/Trusted"], plan="PLAN")],
     {"signs_in": True, "admin": False, "plan": "Family"}),
    ("kept out", [rule("group", ["/Staff"], admin=True)], {"signs_in": False, "admin": False, "plan": None}),
])
def test_preview_and_enforcement_give_the_same_answer_for_the_same_claims(sg, name, rules, expect):
    plan = store_plan("Family", path=sg.path, caps=("chat",))
    rules = [{**r, "plan": plan["id"] if r["plan"] == "PLAN" else r["plan"]} for r in rules]
    owner = owner_client(sg)
    helper_client(sg)                                  # stores the claims the preview will use
    snapshot = get_snapshot(owner)
    body = proposal(snapshot, rules={"fallback": "closed", "rules": rules})

    # What the preview says will be true of the helper: today's state, unless it lists a change.
    line = person_line(preview(owner, body), HELPER)
    predicted = {"signs_in": True, "admin": True, "plan": None}
    if line is not None:
        predicted = {"signs_in": line["sign_in"]["after"], "admin": line["admin"]["after"],
                     "plan": line["plan"]["after"] and line["plan"]["after"]["name"]}
    assert save(owner, snapshot, body).status_code == 200

    # What enforcement then does when the helper signs in with the same claims.
    _, response = signed_in(sg, sub="helper", email=HELPER, groups=["/Trusted"])
    really_signed_in = response.status_code == 302
    really_admin = ("admin", "oidc") in roles_of(sg, HELPER) if really_signed_in else False
    plan_id = admin_store.person_plan_id(user_id(sg, HELPER), path=sg.path)
    really_plan = admin_store.get_access_plan(plan_id, path=sg.path)["name"] if plan_id else None

    assert {"signs_in": really_signed_in, "admin": really_admin, "plan": really_plan} == predicted == expect, name


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def test_save_needs_the_current_revision(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    body = proposal(snapshot)

    missing = owner.put("/admin/api/signin", json={"proposal": body}, headers=csrf_for(owner))
    stale = save(owner, {**snapshot, "revision": "0-0-0"}, body)

    assert missing.status_code == 428
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "settings_conflict"


def test_the_two_sign_in_switches_can_never_both_be_off(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)

    refused = save(owner, snapshot, proposal(snapshot, methods={"password": False, "provider": False}))

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "last_sign_in_method"
    assert get_snapshot(owner)["methods"] == {"password": True, "provider": True}


def test_a_rule_without_values_or_without_an_effect_cannot_be_saved(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    for bad in (rule("group", [], admit=True), rule("group", ["/G"])):
        refused = save(owner, snapshot, proposal(snapshot, rules={"fallback": "closed", "rules": [bad]}))
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "incomplete_rule"


def test_a_rule_that_gives_a_plan_that_does_not_exist_cannot_be_saved(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    bad = rule("group", ["/G"], admit=True, plan="plan_missing")

    refused = save(owner, snapshot, proposal(snapshot, rules={"fallback": "closed", "rules": [bad]}))

    assert refused.status_code == 400 and refused.json()["error"]["code"] == "plan_not_found"


def test_saving_rules_stores_them_and_returns_the_new_snapshot(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    rules = {"fallback": "open", "rules": [rule("domain", ["Example.Test"], admit=True, id="d")]}

    saved = save(owner, snapshot, proposal(snapshot, rules=rules))

    assert saved.status_code == 200, saved.text
    assert saved.json()["snapshot"]["rules"] == {"fallback": "open", "rules": [
        {"id": "d", "field": "domain", "values": ["example.test"], "admit": True, "admin": False, "plan": None}]}
    assert saved.json()["snapshot"]["revision"] != snapshot["revision"]
    assert signin_admin.load_rules(sg.path).fallback == "open"


def test_save_for_later_keeps_the_connection_with_the_provider_off_and_needs_no_test(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    body = proposal(snapshot, client_id="draft-client", client_secret="draft-secret",
                    address="https://id2.example/realms/home", name="Draft")

    saved = save(owner, snapshot, body, save_for_later=True)

    assert saved.status_code == 200, saved.text
    after = saved.json()["snapshot"]
    assert after["methods"] == {"password": True, "provider": False}
    assert after["provider"]["client_id"] == "draft-client" and after["provider"]["name"] == "Draft"
    assert after["provider"]["address"] == "https://id2.example/realms/home"
    assert after["provider"]["tested_at"] is None
    assert saved.json()["applied"]["provider_turned_on"] is False
    stored = admin_store.get_operator_setting("oidc_provider_config", path=sg.path)["value"]
    assert stored["enabled"] is False
    assert admin_store.get_operator_setting("sign_in", path=sg.path)["value"] == {"mode": "built-in"}


def test_save_for_later_needs_the_address_and_client_id(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)

    refused = save(owner, snapshot, proposal(snapshot, address="", client_id=""), save_for_later=True)

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "connection_incomplete"


def test_changing_the_connection_of_a_live_provider_needs_a_passed_test(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)

    refused = save(owner, snapshot, proposal(snapshot, client_id="other-client"))

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "test_required"
    assert get_snapshot(owner)["provider"]["client_id"] == "gateway"


def test_removing_the_administrator_rule_removes_the_grant_ends_the_session_and_revokes_keys_by_default(sg):
    owner = owner_client(sg)
    helper = helper_client(sg)
    key = mint_admin_key(helper)
    assert helper.get("/admin/api/access-plans").status_code == 200 and bearer_status(sg, key) == 200
    snapshot = get_snapshot(owner)

    saved = save(owner, snapshot, proposal(snapshot, rules={"fallback": "closed", "rules": []}))

    assert saved.status_code == 200, saved.text
    assert saved.json()["applied"]["admin_removed"] == [{"name": HELPER, "keys_revoked": 1, "keys_kept": 0}]
    assert roles_of(sg, HELPER) == set()
    assert helper.get("/admin/api/access-plans").status_code in {401, 403}
    assert bearer_status(sg, key) in {401, 403}
    assert owner.get("/admin/api/access-plans").status_code == 200           # the owner stays


def test_keep_them_working_keeps_the_admin_keys_the_person_made(sg):
    owner = owner_client(sg)
    helper = helper_client(sg)
    key = mint_admin_key(helper)
    snapshot = get_snapshot(owner)

    saved = save(owner, snapshot, proposal(snapshot, rules={"fallback": "closed", "rules": []}),
                 keep_admin_keys=[user_id(sg, HELPER)])

    assert saved.json()["applied"]["admin_removed"] == [{"name": HELPER, "keys_revoked": 0, "keys_kept": 1}]
    assert roles_of(sg, HELPER) == set()
    assert helper.get("/admin/api/access-plans").status_code in {401, 403}
    assert bearer_status(sg, key) == 200                                       # the owner now stands behind it


def test_narrowing_the_administrator_rule_removes_only_the_people_it_no_longer_covers(sg):
    sg.set_rules([{**TRUSTED, "values": ["/Trusted", "/Staff"]}])
    owner = owner_client(sg)
    staff, _ = signed_in(sg, sub="staff", email="staff@example.test", groups=["/Staff"])
    helper = helper_client(sg)
    assert staff.get("/admin/api/access-plans").status_code == 200
    snapshot = get_snapshot(owner)
    narrowed = {**snapshot["rules"]["rules"][0], "values": ["/Trusted"]}

    saved = save(owner, snapshot, proposal(snapshot, rules={"fallback": "closed", "rules": [narrowed]}))

    assert [r["name"] for r in saved.json()["applied"]["admin_removed"]] == ["staff@example.test"]
    assert staff.get("/admin/api/access-plans").status_code in {401, 403}
    assert helper.get("/admin/api/access-plans").status_code == 200


def test_a_grant_the_owner_gave_by_hand_is_never_touched_by_rules(sg):
    owner = owner_client(sg)
    helper = helper_client(sg)
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=user_id(sg, HELPER),
                                  granted_by="owner", path=sg.path)
    snapshot = get_snapshot(owner)

    saved = save(owner, snapshot, proposal(snapshot, rules={"fallback": "closed", "rules": []}))

    assert saved.json()["applied"]["admin_removed"][0]["name"] == HELPER     # the rule's grant is gone
    assert ("admin", "manual") in roles_of(sg, HELPER)                       # the hand-made one stays


def test_someone_the_rules_now_keep_out_loses_their_session(sg):
    sg.set_rules([TRUSTED, domain_rule("example.test")])
    owner = owner_client(sg)
    member, _ = signed_in(sg, sub="member", email="member@example.test")
    assert member.get("/me/api/session").json()["authenticated"] is True
    snapshot = get_snapshot(owner)

    saved = save(owner, snapshot, proposal(snapshot, rules={"fallback": "closed", "rules": [snapshot["rules"]["rules"][0]]}))

    assert saved.status_code == 200
    assert member.get("/me/api/session").json()["authenticated"] is False
    refused = sg.sign_in(sg.client(), sub="member", email="member@example.test")
    assert refused.status_code == 303


def test_turning_the_provider_off_ends_administrators_keeps_members_and_deletes_nobody(sg):
    sg.set_rules([TRUSTED, domain_rule("example.test")])
    owner = owner_client(sg)
    # The owner needs a password before the provider can go off (see the next test).
    owner.post("/admin/api/signin/owner-password", json={"password": "a long enough password"},
               headers=csrf_for(owner))
    helper = helper_client(sg)
    key = mint_admin_key(helper)
    member, _ = signed_in(sg, sub="member", email="member@example.test")
    snapshot = get_snapshot(owner)

    saved = save(owner, snapshot, proposal(snapshot, methods={"password": True, "provider": False}))

    assert saved.status_code == 200, saved.text
    assert saved.json()["snapshot"]["methods"] == {"password": True, "provider": False}
    assert [r["name"] for r in saved.json()["applied"]["admin_removed"]] == [HELPER]
    assert helper.get("/admin/api/access-plans").status_code in {401, 403}
    assert bearer_status(sg, key) in {401, 403}
    assert member.get("/me/api/session").json()["authenticated"] is True      # a member's session is not the switch's to end
    assert {HELPER, "member@example.test", OWNER} <= {u["email"] for u in admin_store.list_users(path=sg.path)}
    # Rules and the connection are kept for when it comes back on.
    assert signin_admin.load_rules(sg.path).rules and get_snapshot(owner)["provider"]["client_id"] == "gateway"


def test_the_provider_cannot_be_turned_off_while_the_owner_has_no_password(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)

    refused = save(owner, snapshot, proposal(snapshot, methods={"password": True, "provider": False}))

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "owner_needs_password"
    assert get_snapshot(owner)["methods"]["provider"] is True


# ---------------------------------------------------------------------------
# The owner's password
# ---------------------------------------------------------------------------

PASSWORD = "a long enough password"


def test_the_owner_can_set_a_password_and_use_it_whatever_made_their_account(sg, monkeypatch):
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2 ** 10)
    owner = owner_client(sg)

    snapshot = owner.post("/admin/api/signin/owner-password", json={"password": PASSWORD},
                          headers=csrf_for(owner)).json()

    assert snapshot["owner"]["has_password"] is True
    login = sg.client().post("/auth/api/login", json={"email": OWNER, "password": PASSWORD})
    assert login.status_code == 200, login.text
    assert login.cookies.get("opx_admin_session")


def test_a_short_password_is_refused(sg, monkeypatch):
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2 ** 10)
    owner = owner_client(sg)
    refused = owner.post("/admin/api/signin/owner-password", json={"password": "short"}, headers=csrf_for(owner))
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_password"


def test_the_owners_password_works_while_built_in_sign_in_is_off_and_nobody_elses_does(sg, monkeypatch):
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2 ** 10)
    sg.set_rules([TRUSTED, domain_rule("example.test")])
    owner = owner_client(sg)
    owner.post("/admin/api/signin/owner-password", json={"password": PASSWORD}, headers=csrf_for(owner))
    plan = store_plan("Pw", path=sg.path, caps=("chat",))
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email="pat@example.test", path=sg.path)
    accounts.accept_invite(token, "pat@example.test", "Pat", PASSWORD, path=sg.path)
    admin_store.put_operator_setting("sign_in", {"mode": "oidc"}, path=sg.path)
    assert get_snapshot(owner)["methods"]["password"] is False

    owners = sg.client().post("/auth/api/login", json={"email": OWNER, "password": PASSWORD})
    pats = sg.client().post("/auth/api/login", json={"email": "pat@example.test", "password": PASSWORD})
    wrong = sg.client().post("/auth/api/login", json={"email": "pat@example.test", "password": "wrong password!!"})

    assert owners.status_code == 200, owners.text
    assert pats.status_code == 403 and pats.json()["error"]["code"] == "builtin_disabled"
    assert wrong.status_code == 401                       # only a correct password learns the switch is off


def test_the_setup_page_sign_in_also_lets_the_owner_in_when_built_in_sign_in_is_off(sg, monkeypatch):
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2 ** 10)
    owner = owner_client(sg)
    owner.post("/admin/api/signin/owner-password", json={"password": PASSWORD}, headers=csrf_for(owner))
    admin_store.put_operator_setting("sign_in", {"mode": "oidc"}, path=sg.path)

    response = sg.client().post("/setup/local-login", data={"email": OWNER, "password": PASSWORD},
                                follow_redirects=False)

    assert response.status_code in {302, 303}, response.text
    assert "opx_admin_session" in response.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Plans from rules at sign-in
# ---------------------------------------------------------------------------

def test_a_plan_rule_gives_the_plan_at_sign_in_and_the_rule_going_away_takes_it_back(sg):
    plan = store_plan("Family", path=sg.path, caps=("chat",))
    sg.set_rules([TRUSTED, rule("group", ["/Family"], admit=True, plan=plan["id"], id="family")])
    signed_in(sg, sub="kid", email="kid@example.test", groups=["/Family"])
    kid = user_id(sg, "kid@example.test")
    assert admin_store.person_plan_id(kid, path=sg.path) == plan["id"]

    sg.set_rules([TRUSTED, rule("group", ["/Family"], admit=True, id="family")])
    signed_in(sg, sub="kid", email="kid@example.test", groups=["/Family"])

    assert admin_store.person_plan_id(kid, path=sg.path) is None


def test_the_first_matching_plan_rule_wins_at_sign_in_and_reordering_changes_the_winner(sg):
    a = store_plan("A", path=sg.path, caps=("chat",))
    b = store_plan("B", path=sg.path, caps=("chat",))
    first = rule("group", ["/Family"], admit=True, plan=a["id"], id="a")
    second = rule("domain", ["example.test"], admit=True, plan=b["id"], id="b")
    sg.set_rules([first, second])
    signed_in(sg, sub="kid", email="kid@example.test", groups=["/Family"])
    kid = user_id(sg, "kid@example.test")
    assert admin_store.person_plan_id(kid, path=sg.path) == a["id"]

    sg.set_rules([second, first])
    signed_in(sg, sub="kid", email="kid@example.test", groups=["/Family"])

    assert admin_store.person_plan_id(kid, path=sg.path) == b["id"]


def test_a_plan_set_by_hand_beats_a_rule_and_is_never_replaced(sg):
    rule_plan = store_plan("Rule", path=sg.path, caps=("chat",))
    hand = store_plan("Hand", path=sg.path, caps=("chat",))
    sg.set_rules([rule("domain", ["example.test"], admit=True, plan=rule_plan["id"])])
    signed_in(sg, sub="kid", email="kid@example.test")
    kid = user_id(sg, "kid@example.test")
    assert admin_store.person_plan_id(kid, path=sg.path) == rule_plan["id"]

    accounts.change_person(kid, "owner", plan_id=hand["id"], path=sg.path)
    signed_in(sg, sub="kid", email="kid@example.test")

    assert admin_store.person_plan_id(kid, path=sg.path) == hand["id"]
    sg.set_rules([domain_rule("example.test")])      # even when no rule gives a plan any more
    signed_in(sg, sub="kid", email="kid@example.test")
    assert admin_store.person_plan_id(kid, path=sg.path) == hand["id"]


def test_a_plan_carried_by_an_invitation_beats_the_rule_plan(sg):
    rule_plan = store_plan("Rule", path=sg.path, caps=("chat",))
    invite_plan = store_plan("Invite", path=sg.path, caps=("chat",))
    sg.set_rules([rule("domain", ["example.test"], admit=True, plan=rule_plan["id"])])
    client, _ = signed_in(sg, sub="kid", email="kid@example.test")
    kid = user_id(sg, "kid@example.test")
    assert admin_store.person_plan_id(kid, path=sg.path) == rule_plan["id"]
    _, token = accounts.create_link("invite", "owner", plan_id=invite_plan["id"], email="kid@example.test", path=sg.path)

    csrf = client.get("/auth/api/options").json()["signed_in_oidc"]["csrf_token"]
    accepted = client.post("/auth/api/invite/accept", headers={"x-azizam-csrf": csrf},
                           json={"token": token, "email": "kid@example.test", "name": "Kid"})

    assert accepted.status_code == 200, accepted.text
    assert admin_store.person_plan_id(kid, path=sg.path) == invite_plan["id"]
    signed_in(sg, sub="kid", email="kid@example.test")
    assert admin_store.person_plan_id(kid, path=sg.path) == invite_plan["id"]     # the next sign-in leaves it


def test_save_gives_and_takes_rule_plans_for_people_who_have_already_signed_in(sg):
    plan = store_plan("Family", path=sg.path, caps=("chat",))
    owner = owner_client(sg)
    helper_client(sg)
    snapshot = get_snapshot(owner)
    with_plan = {"fallback": "closed", "rules": [*snapshot["rules"]["rules"],
                                                 rule("group", ["/Trusted"], plan=plan["id"], id="plan")]}

    assert save(owner, snapshot, proposal(snapshot, rules=with_plan)).status_code == 200
    assert admin_store.person_plan_id(user_id(sg, HELPER), path=sg.path) == plan["id"]

    snapshot = get_snapshot(owner)
    assert save(owner, snapshot, proposal(snapshot, rules=snapshot["rules"] | {"rules": snapshot["rules"]["rules"][:1]})
                ).status_code == 200
    assert admin_store.person_plan_id(user_id(sg, HELPER), path=sg.path) is None


# ---------------------------------------------------------------------------
# Invitations and the open door
# ---------------------------------------------------------------------------

def test_an_open_invitation_names_the_email_the_provider_vouches_for_and_nothing_else(sg):
    plan = store_plan("Family", path=sg.path, caps=("chat",))
    accounts.create_link("invite", "owner", plan_id=plan["id"], email="friend@elsewhere.test", path=sg.path)
    sg.set_rules([])

    _, ok = signed_in(sg, sub="friend", email="friend@elsewhere.test")
    _, other = signed_in(sg, sub="other", email="other@elsewhere.test")

    assert ok.status_code == 302 and other.status_code == 303


def test_a_person_who_accepted_an_invitation_keeps_being_let_in_by_the_provider(sg):
    plan = store_plan("Family", path=sg.path, caps=("chat",))
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email="friend@elsewhere.test", path=sg.path)
    sg.set_rules([])
    client, _ = signed_in(sg, sub="friend", email="friend@elsewhere.test")
    csrf = client.get("/auth/api/options").json()["signed_in_oidc"]["csrf_token"]
    assert client.post("/auth/api/invite/accept", headers={"x-azizam-csrf": csrf},
                       json={"token": token, "email": "friend@elsewhere.test", "name": "Friend"}).status_code == 200

    _, again = signed_in(sg, sub="friend", email="friend@elsewhere.test")

    assert again.status_code == 302


def test_everyone_else_open_lets_a_stranger_in_with_no_plan_and_no_role(sg):
    sg.set_rules([TRUSTED], fallback="open")

    _, response = signed_in(sg, sub="anyone", email="anyone@elsewhere.test")

    assert response.status_code == 302
    assert roles_of(sg, "anyone@elsewhere.test") == set()
    assert admin_store.person_plan_id(user_id(sg, "anyone@elsewhere.test"), path=sg.path) is None


def test_deny_by_default_a_damaged_rules_document_keeps_everyone_but_the_owner_out(sg):
    admin_store.put_operator_setting("signin_rules", {"fallback": "bogus", "rules": "x"}, path=sg.path)

    _, stranger = signed_in(sg, sub="s", email="s@example.test", groups=["/Trusted"])
    _, owner = signed_in(sg, sub="owner", email=OWNER)

    assert stranger.status_code == 303 and owner.status_code == 302


def test_a_rule_derived_admin_grant_is_re_derived_at_every_sign_in(sg):
    sg.set_rules([TRUSTED])
    helper_client(sg)
    grant = admin_store.list_admin_roles(subject_kind="user", subject_id=user_id(sg, HELPER), path=sg.path)[0]
    first = grant["last_confirmed_at"]
    with accounts.connection(sg.path) as conn:
        conn.execute("UPDATE admin_role_assignments SET last_confirmed_at=? WHERE id=?", (first - 3600, grant["id"]))

    helper_client(sg)

    again = admin_store.list_admin_roles(subject_kind="user", subject_id=user_id(sg, HELPER), path=sg.path)[0]
    assert again["id"] == grant["id"] and again["last_confirmed_at"] > first - 3600


def test_a_rule_that_stops_matching_removes_the_grant_at_the_next_sign_in(sg):
    sg.set_rules([TRUSTED, domain_rule("example.test")])
    helper_client(sg)
    assert ("admin", "oidc") in roles_of(sg, HELPER)
    sg.set_rules([domain_rule("example.test")])

    _, response = signed_in(sg, sub="helper", email=HELPER, groups=["/Trusted"])

    assert response.status_code == 302
    assert roles_of(sg, HELPER) == set()
