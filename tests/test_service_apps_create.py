"""One request creates a service app: the same core as the admin route, with the role checks on the server."""
from __future__ import annotations

from gateway import accounts, admin_store
from gateway.admin_runtime import runtime_identity_for_app_credential_hash
from gateway.runtime_auth import hash_api_key
from tests.support.access import store_plan
from tests.support.app_keys import issue_app_key
from tests.test_signin_who_can_enter import OWNER, TRUSTED, csrf_for, gateway, signed_in  # noqa: F401  (gateway is a fixture)

ACT = {"x-azizam-user-action": "1"}


def _people(g):
    """The owner, an administrator (trusted group) and a plain member, all signed in."""
    g.set_rules([TRUSTED], fallback="open")
    owner, _ = signed_in(g, sub="owner", email=OWNER)
    admin, _ = signed_in(g, sub="helper", email="helper@example.test", groups=["/Trusted"])
    member, _ = signed_in(g, sub="member", email="member@example.test")
    return owner, admin, member


def _create(client, **body):
    return client.post("/me/api/apps", headers={**csrf_for(client), **ACT}, json=body)


def _apps(g):
    return g.rows("SELECT * FROM apps")


def test_a_member_is_refused_a_service_app(gateway):
    _, _, member = _people(gateway)
    before = len(_apps(gateway))
    refused = _create(member, name="Bot", kind="service")
    assert refused.status_code == 403, refused.text
    assert len(_apps(gateway)) == before


def test_a_member_is_refused_a_plan_other_than_their_own(gateway):
    _, _, member = _people(gateway)
    plan = store_plan("Chat only", path=gateway.path, caps=("chat",))
    before = len(_apps(gateway))
    refused = _create(member, name="Desk", access_plan=plan["id"])
    assert refused.status_code == 403, refused.text
    assert len(_apps(gateway)) == before


def test_a_members_plain_name_still_makes_a_personal_app_on_their_plan(gateway):
    _, _, member = _people(gateway)
    created = _create(member, name="Desk")
    assert created.status_code == 201, created.text
    stored = admin_store.get_app(created.json()["app"]["id"], path=gateway.path)
    assert (stored["kind"], stored["access_plan"]) == ("personal", "person")
    assert stored["owner_user_id"] == gateway.user("member@example.test")["id"]


def test_an_administrator_cannot_name_another_accountable_person(gateway):
    owner, admin, member = _people(gateway)
    plan = store_plan("Chat only", path=gateway.path, caps=("chat",))
    member_id = gateway.user("member@example.test")["id"]
    before = len(_apps(gateway))
    refused = _create(admin, name="Bot", kind="service", access_plan=plan["id"], owner_user_id=member_id)
    assert refused.status_code == 403, refused.text
    assert len(_apps(gateway)) == before
    # The owner may.
    allowed = _create(owner, name="Bot", kind="service", access_plan=plan["id"], owner_user_id=member_id)
    assert allowed.status_code == 201, allowed.text
    assert admin_store.get_app(allowed.json()["app"]["id"], path=gateway.path)["owner_user_id"] == member_id


def test_an_administrator_makes_a_service_app_with_a_plan_in_one_request_and_its_first_key_runs_under_it(gateway):
    _, admin, _ = _people(gateway)
    helper_id = gateway.user("helper@example.test")["id"]
    plan = store_plan("Chat only", path=gateway.path, caps=("chat",))
    accounts.change_person(helper_id, OWNER, plan_id=plan["id"], path=gateway.path)
    created = _create(admin, name="Nightly job", kind="service", access_plan=plan["id"])
    assert created.status_code == 201, created.text
    app_id = created.json()["app"]["id"]
    stored = admin_store.get_app(app_id, path=gateway.path)
    assert (stored["kind"], stored["access_plan"], stored["owner_user_id"]) == ("service", plan["id"], helper_id)

    key = issue_app_key(gateway.path, app_id)
    identity = runtime_identity_for_app_credential_hash(hash_api_key(key), path=gateway.path)
    assert identity is not None and not identity.denial
    assert identity.auth_kind == "service_key"
    assert identity.access.app_plan.id == plan["id"]


def test_the_audit_row_records_the_kind_and_the_plan(gateway):
    _, admin, _ = _people(gateway)
    plan = store_plan("Chat only", path=gateway.path, caps=("chat",))
    app_id = _create(admin, name="Audited", kind="service", access_plan=plan["id"]).json()["app"]["id"]
    [row] = gateway.rows("SELECT metadata_json FROM audit_events WHERE action='app.create' AND target_id=?", (app_id,))
    import json
    metadata = json.loads(row["metadata_json"])
    assert metadata["kind"] == "service" and metadata["access_plan"] == plan["id"]
