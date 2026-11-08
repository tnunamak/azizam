"""The owner API of the workflow registry over a real ``admin.db`` (CODE_TO_DATA_S4_S8, I6; acceptance rows 7, 8, 9, 11, 21
and 22, and the store side of 2, 4, 5, 16 and 17). Nothing about the registry is faked; the snapshot builder is the adapter
of ``tests/test_workflow_registry_store.py`` until ``registry_snapshot.build`` lands."""
from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, service_registry
from gateway.admin_api import AdminAuthError, AdminContext, create_admin_router
from gateway.admin_runtime import AdminSQLiteStore
from gateway.definition_store import DefinitionStore
from gateway.workflow_registry_store import WorkflowRegistryStore
from tests.registry_test_helpers import write_service_registry_for_test
from tests.test_workflow_registry_store import (
    BUNDLED_ID, _template, _with_inline, build_snapshot, bundled_template, owner_contract,
)

OWNER = AdminContext(subject="owner-1", roles=frozenset({"owner"}), actor_kind="user", actor_label="Owner")
FACTS_BODY = {"exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "resource_class": "image",
              "tags": ["a"]}
ID = "owner.test.demo"


def _real_snapshot(document, _release, *, generation):
    """The production builder over the bundled release, as the gateway wires it."""
    from gateway import registry_snapshot
    return registry_snapshot.build(document, registry_snapshot.bundled_release(), generation=generation)


@pytest.fixture(params=["fake"])
def api(tmp_path, request):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    write_service_registry_for_test(service_registry.normalize_service_registry({}), path=db)
    config: dict = {}
    builder = _real_snapshot if request.param == "real" else build_snapshot
    registry = WorkflowRegistryStore(path=db, definitions=DefinitionStore(tmp_path / "jobs.db"),
                                     build_snapshot=builder, config=lambda: config)
    roles = {"current": OWNER}

    def auth(_request):
        if roles["current"] is None:
            raise AdminAuthError("no", status_code=401, code="unauthenticated")
        return roles["current"]

    app = FastAPI()
    app.include_router(create_admin_router(AdminSQLiteStore(db), auth=auth, workflow_registry=registry))
    client = TestClient(app, raise_server_exceptions=False)
    client.db, client.config, client.roles, client.registry = db, config, roles, registry
    client.jobs_db = tmp_path / "jobs.db"
    return client


def _restarted(client, release_edits):
    """The gateway restarted on a newer release: the same admin.db, the bundled workflows of ``release_edits`` changed by
    their function (id -> manifest -> manifest), everything else as shipped."""
    from dataclasses import replace

    from gateway import registry_snapshot

    def builder(document, _release, *, generation):
        release = registry_snapshot.bundled_release()
        changed = {wid: replace(record, manifest=release_edits[wid](copy.deepcopy(record.manifest)))
                   if wid in release_edits else record for wid, record in release.workflows.items()}
        return registry_snapshot.build(document, registry_snapshot.BundledRelease(changed, release.frontends),
                                       generation=generation)

    registry = WorkflowRegistryStore(path=client.db, definitions=DefinitionStore(client.jobs_db),
                                     build_snapshot=builder, config=lambda: client.config)
    app = FastAPI()
    app.include_router(create_admin_router(AdminSQLiteStore(client.db), auth=lambda _request: OWNER,
                                           workflow_registry=registry))
    restarted = TestClient(app, raise_server_exceptions=False)
    restarted.db, restarted.config, restarted.registry, restarted.jobs_db = client.db, client.config, registry, client.jobs_db
    return restarted


def os_n(client) -> str:
    return f'"os-{admin_store.get_service_registry_json_with_generation(path=client.db)["operator_settings_generation"]}"'


def create(client, contract=None, **body):
    return client.post("/admin/api/workflows", json={"contract": contract or owner_contract(), **body})


def put(client, contract, match=None, **body):
    return client.put(f"/admin/api/workflows/{contract['id']}", json={"contract": contract, **body},
                      headers={"If-Match": match or os_n(client)})


def test_create_answers_201_with_the_commit_and_the_new_etag(api):
    response = create(api, facts=FACTS_BODY)
    assert response.status_code == 201, response.text
    commit = response.json()["commit"]
    assert commit["workflow_id"] == ID and commit["record_revision"] == 1
    assert response.headers["etag"] == os_n(api) == f'"os-{commit["revision"]}"'
    listed = api.get("/admin/api/workflows")
    assert listed.headers["etag"] == os_n(api)
    row = next(r for r in listed.json()["workflows"] if r["id"] == ID)
    assert (row["published"], row["state"], row["record_revision"]) == (False, "active", 1)
    assert row["contract_digest"] == commit["contract_digest"] and row["needs_configuration"] is False


def test_a_create_of_an_existing_id_is_409_workflow_exists(api):
    create(api)
    response = create(api)
    assert response.status_code == 409 and response.json()["error"]["code"] == "workflow_exists"


def test_two_edits_with_the_same_if_match_the_second_is_412_naming_the_current_revision(api):  # row 7
    create(api)
    match = os_n(api)
    assert put(api, owner_contract(display_name="One"), match).status_code == 200
    second = put(api, owner_contract(display_name="Two"), match)
    assert second.status_code == 412
    assert second.json()["error"]["code"] == "generation_mismatch"
    assert f'"os-{second.json()["error"]["current_revision"]}"' == os_n(api)
    assert api.get(f"/admin/api/workflows/{ID}").json()["workflow"]["contract"]["display_name"] == "One"


def test_a_write_without_if_match_is_412(api):
    create(api)
    response = api.put(f"/admin/api/workflows/{ID}", json={"contract": owner_contract()})
    assert response.status_code == 412


def test_facts_absent_keep_them_and_null_clears_them(api):
    create(api, facts=FACTS_BODY)
    put(api, owner_contract(display_name="a"))
    assert api.get(f"/admin/api/workflows/{ID}").json()["workflow"]["facts"]["resource_class"] == "image"
    put(api, owner_contract(display_name="b"), facts=None)
    detail = api.get(f"/admin/api/workflows/{ID}").json()["workflow"]
    assert detail["facts"] is None and detail["needs_configuration"] is True
    assert api.post("/admin/api/workflows", json={"contract": owner_contract("owner.other.demo"), "facts": {"x": 1}}
                    ).status_code == 422


def test_a_refused_save_is_422_with_every_problem_and_its_path_and_stores_nothing(api):
    bad = owner_contract(intent=7)
    del bad["outputs"]
    before = os_n(api)
    response = create(api, bad)
    assert response.status_code == 422
    problems = response.json()["problems"]
    assert len(problems) >= 2 and all({"path", "pointer", "code", "message"} <= set(p) for p in problems)
    assert os_n(api) == before


def test_check_reports_without_writing(api):
    before = os_n(api)
    good = api.post("/admin/api/workflows/check", json={"contract": owner_contract()})
    assert good.status_code == 200 and good.json()["report"] == {"ok": True, "errors": []}
    bad = api.post("/admin/api/workflows/check", json={"contract": owner_contract(intent=7)}).json()["report"]
    assert bad["ok"] is False and bad["errors"][0]["pointer"]
    assert os_n(api) == before


def test_a_body_that_is_not_a_json_object_is_400_invalid_json_on_every_body_route(api):
    # The admin body reader refuses a list or broken JSON; the owner routes answer its 400, never a 500.
    before = os_n(api)
    headers = {"Content-Type": "application/json", "If-Match": before}
    for method, path in (("POST", "/admin/api/workflows"), ("POST", "/admin/api/workflows/check"),
                         ("PUT", f"/admin/api/workflows/{ID}")):
        for raw in ("[]", "{not json"):
            response = api.request(method, path, content=raw, headers=headers)
            assert (response.status_code, response.json()["error"]["code"]) == (400, "invalid_json"), (method, path, raw)
    assert os_n(api) == before


def test_publish_state_and_delete_are_visible_in_the_listing(api):  # rows 2, 4, 5, 16, 17
    create(api)
    incarnation = api.get(f"/admin/api/workflows/{ID}").json()["workflow"]["incarnation"]

    def row():
        return next((r for r in api.get("/admin/api/workflows").json()["workflows"] if r["id"] == ID), None)

    def post(path, body):
        return api.post(f"/admin/api/workflows/{ID}/{path}", json=body, headers={"If-Match": os_n(api)})

    assert post("publish", {"published": True}).status_code == 200 and row()["published"] is True
    assert post("state", {"state": "disabled"}).status_code == 200 and row()["state"] == "disabled"
    assert post("state", {"state": "active"}).status_code == 200 and row()["state"] == "active"
    assert api.get(f"/admin/api/workflows/{ID}").json()["workflow"]["incarnation"] == incarnation
    assert post("state", {"state": "paused"}).status_code == 422
    assert post("publish", {"published": "yes"}).status_code == 422
    deleted = api.delete(f"/admin/api/workflows/{ID}", headers={"If-Match": os_n(api)})
    assert deleted.status_code == 200 and row() is None
    assert api.get(f"/admin/api/workflows/{ID}").status_code == 404
    create(api)
    assert api.get(f"/admin/api/workflows/{ID}").json()["workflow"]["incarnation"] != incarnation


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_a_bundled_workflow_unpublishes_and_publishes_again_through_the_owner_api(api):  # row 2 for a bundled id
    def detail():
        return api.get(f"/admin/api/workflows/{BUNDLED_ID}").json()["workflow"]

    def publish(value):
        return api.post(f"/admin/api/workflows/{BUNDLED_ID}/publish", json={"published": value},
                        headers={"If-Match": os_n(api)})

    before = detail()
    assert before["published"] is True
    off = publish(False)
    assert off.status_code == 200, off.json()
    assert detail()["published"] is False
    row = next(r for r in api.get("/admin/api/workflows").json()["workflows"] if r["id"] == BUNDLED_ID)
    assert row["published"] is False
    on = publish(True)
    assert on.status_code == 200, on.json()
    after = detail()
    assert after["published"] is True
    # The override keeps what the bundled entry runs: its contract, facts and incarnation (a queued job stays valid),
    # and the provenance it was reviewed under, across both changes.
    assert (after["contract"], after["facts"], after["incarnation"], after["overrides"], after["provenance"]) == \
        (before["contract"], before["facts"], before["incarnation"], BUNDLED_ID, before["provenance"])
    # An edit of the contract is no longer the reviewed bundled one: it drops the bundled provenance.
    edited = {**after["contract"], "display_name": "Edited by the owner"}
    saved = api.put(f"/admin/api/workflows/{BUNDLED_ID}", json={"contract": edited, "overrides": BUNDLED_ID},
                    headers={"If-Match": os_n(api)})
    assert saved.status_code == 200, saved.json()
    assert detail()["provenance"] != before["provenance"]


def test_delete_of_a_bundled_id_is_409_id_is_bundled(api):
    response = api.delete(f"/admin/api/workflows/{BUNDLED_ID}", headers={"If-Match": os_n(api)})
    assert response.status_code == 409 and response.json()["error"]["code"] == "id_is_bundled"
    assert response.json()["bundled_workflow"]["id"] == BUNDLED_ID
    refused = create(api, owner_contract(BUNDLED_ID))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "id_is_bundled"
    assert create(api, owner_contract(BUNDLED_ID), overrides=BUNDLED_ID).status_code == 201


def test_ids_with_one_token_are_422_id_clash_in_both_orders(api):  # rows 8 and 22
    assert create(api, owner_contract("x.a-b")).status_code == 201
    clash = create(api, owner_contract("x.a.b"))
    assert clash.status_code == 422 and clash.json()["error"]["code"] == "id_clash"
    assert "x.a-b" in clash.json()["problems"][0]["message"]


def _registry_with_alias(client, alias_id: str) -> dict:
    """The registry as the owner reads it, with one service that serves a model and one alias of it."""
    registry = client.get("/admin/api/operator/service-registry").json()["service_registry"]
    service = next(iter(registry["services"].values()))
    service["config"] = {**(service.get("config") or {}), "model_policy_schema_version": 1,
                         "models": [{"id": "base-model"}, {"id": alias_id, "base_model_id": "base-model"}]}
    return registry


def test_a_model_alias_whose_token_equals_a_workflow_id_is_422_through_the_whole_document_put(api):  # row 22
    assert create(api).status_code == 201
    registry = _registry_with_alias(api, "Owner-Test-Demo")
    response = api.put("/admin/api/operator/service-registry", json=registry, headers={"If-Match": os_n(api)})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "id_clash"
    fine = api.put("/admin/api/operator/service-registry", json=_registry_with_alias(api, "fresh-alias"),
                   headers={"If-Match": os_n(api)})
    assert fine.status_code == 200, fine.text      # a different alias is not a clash


def test_a_workflow_whose_id_equals_an_existing_alias_is_refused(api):  # row 8, the other direction
    response = api.put("/admin/api/operator/service-registry", json=_registry_with_alias(api, "Owner-Test-Demo"),
                       headers={"If-Match": os_n(api)})
    assert response.status_code == 200, response.text
    clash = create(api)
    assert clash.status_code == 422 and clash.json()["error"]["code"] == "id_clash"


def test_an_inline_template_over_each_bound_is_422_with_the_bound(api):  # row 9
    api.config["workflow_execution"] = {"bounds": {"max_template_bytes": 400, "max_nodes": 4, "max_depth": 5}}
    response = create(api, _with_inline(owner_contract(), _template(nodes=9, depth=9, padding=600)))
    assert response.status_code == 422
    bounds = [p for p in response.json()["problems"] if p["code"] == "workflow_bound_exceeded"]
    assert len(bounds) == 3 and {tuple(p["path"][-2:]) for p in bounds} == {("workflow", "inline")}
    assert {m for p in bounds for m in ("bytes", "nodes", "levels") if m in p["message"]} == {"bytes", "nodes", "levels"}


def test_an_inline_template_inside_the_bounds_is_accepted(api):  # W2: 5.7, the template travels in the saved document
    response = create(api, _with_inline(owner_contract(), bundled_template()))
    assert response.status_code == 201, response.json()
    entry = api.get(f"/admin/api/workflows/{ID}").json()["workflow"]
    key = next(iter(entry["contract"]["bindings"]))
    assert entry["contract"]["bindings"][key]["workflow"] == {"inline": bundled_template()}


def test_two_saves_differing_only_in_annotations_are_two_history_rows_and_restore_brings_the_first_back(api):  # rows 11, 21
    contract = owner_contract()
    key = next(iter(contract["inputs"]))
    first, second = copy.deepcopy(contract), copy.deepcopy(contract)
    first["inputs"][key]["annotations"] = {"example.org/note": "first"}
    second["inputs"][key]["annotations"] = {"example.org/note": "second"}
    create(api, first, facts=FACTS_BODY)
    api.post(f"/admin/api/workflows/{ID}/publish", json={"published": True}, headers={"If-Match": os_n(api)})
    put(api, second)
    detail = api.get(f"/admin/api/workflows/{ID}").json()["workflow"]
    assert [h["record_revision"] for h in detail["history"]] == [3, 2, 1]
    assert detail["history"][0]["contract_digest"] == detail["history"][2]["contract_digest"]
    assert detail["history"][0]["record_digest"] != detail["history"][2]["record_digest"]
    api.post(f"/admin/api/workflows/{ID}/state", json={"state": "disabled"}, headers={"If-Match": os_n(api)})
    restored = api.post(f"/admin/api/workflows/{ID}/restore", json={"record_revision": 1}, headers={"If-Match": os_n(api)})
    assert restored.status_code == 200 and restored.json()["commit"]["record_revision"] == 5
    now = api.get(f"/admin/api/workflows/{ID}").json()["workflow"]
    assert now["contract"]["inputs"][key]["annotations"] == {"example.org/note": "first"}
    assert (now["state"], now["published"], now["facts"]["resource_class"]) == ("disabled", False, "image")
    missing = api.post(f"/admin/api/workflows/{ID}/restore", json={"record_revision": 99}, headers={"If-Match": os_n(api)})
    assert missing.status_code == 422
    assert api.post(f"/admin/api/workflows/{ID}/restore", json={"record_revision": "1"},
                    headers={"If-Match": os_n(api)}).status_code == 422


def test_the_routes_need_an_admin(api):
    api.roles["current"] = None
    assert api.get("/admin/api/workflows").status_code == 401
    api.roles["current"] = AdminContext(subject="viewer", roles=frozenset({"viewer"}))
    assert api.post("/admin/api/workflows", json={"contract": owner_contract()}).status_code == 403


def test_the_listing_offers_the_closed_fact_values_as_choices(api):  # I6 addendum: the web never spells a fact value
    from gateway.capabilities import EXPOSURE_CLASSES, RESOURCE_TIERS, TRUST_TIERS
    from gateway.workflow_policy import known_resource_classes

    choices = api.get("/admin/api/workflows").json()["choices"]
    assert choices["exposure"] == list(EXPOSURE_CLASSES)
    assert choices["trust_tier"] == list(TRUST_TIERS)
    assert choices["resource_tier"] == list(RESOURCE_TIERS)
    assert set(known_resource_classes()) <= set(choices["resource_class"]) and choices["resource_class"]


# -- a bundled workflow and a later release (contract tests; CODE_TO_DATA S4-S8 integration) ---------------------------


def _renamed(title):
    return lambda manifest: {**manifest, "display_name": title}


def _detail(client, workflow_id=BUNDLED_ID):
    return client.get(f"/admin/api/workflows/{workflow_id}").json()["workflow"]


def _post(client, path, body):
    return client.post(f"/admin/api/workflows/{BUNDLED_ID}/{path}", json=body, headers={"If-Match": os_n(client)})


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_a_new_release_of_a_bundled_workflow_shows_through_its_run_control_changes(api):
    # One click must not freeze a bundled workflow: an unpublish or a state change stores the run control only, so the
    # next release's contract and facts serve under it, with the release's provenance and incarnation.
    before = _detail(api)
    assert _post(api, "publish", {"published": False}).status_code == 200
    assert _post(api, "state", {"state": "disabled"}).status_code == 200
    newer = _restarted(api, {BUNDLED_ID: _renamed("New in this release")})
    after = _detail(newer)
    assert after["contract"]["display_name"] == "New in this release"
    assert (after["published"], after["state"]) == (False, "disabled")
    assert (after["provenance"], after["incarnation"], after["facts"]) == \
        (before["provenance"], before["incarnation"], before["facts"])
    assert _post(newer, "publish", {"published": True}).status_code == 200
    assert _post(newer, "state", {"state": "active"}).status_code == 200
    again = _detail(newer)
    assert again["contract"]["display_name"] == "New in this release" and again["published"] is True
    assert again["provenance"] == before["provenance"]


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_a_contract_override_of_a_bundled_workflow_keeps_its_contract_after_a_new_release(api):
    # A contract the owner saved on purpose (saved with overrides) is a fork: a release does not change it, and it is not
    # reviewed, even when it equals a bundled file.
    edited = {**_detail(api)["contract"], "display_name": "Mine"}
    assert create(api, edited, overrides=BUNDLED_ID).status_code == 201
    newer = _restarted(api, {BUNDLED_ID: _renamed("New in this release")})
    after = _detail(newer)
    assert after["contract"]["display_name"] == "Mine"
    assert after["provenance"]["trusted"] is False


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_a_saved_copy_of_the_bundled_contract_is_not_reviewed(api):
    # Trust is the release file's: a stored full contract is never reviewed, also when it is the bundled one unchanged.
    before = _detail(api)
    assert create(api, before["contract"], overrides=BUNDLED_ID).status_code == 201
    assert _detail(api)["provenance"]["trusted"] is False


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_delete_of_an_overridden_bundled_id_reverts_it_to_the_bundled_workflow(api):
    before = _detail(api)
    edited = {**before["contract"], "display_name": "Mine"}
    assert create(api, edited, overrides=BUNDLED_ID).status_code == 201
    assert _post(api, "publish", {"published": False}).status_code == 200
    reverted = api.delete(f"/admin/api/workflows/{BUNDLED_ID}", headers={"If-Match": os_n(api)})
    assert reverted.status_code == 200, reverted.json()
    after = _detail(api)
    assert (after["contract"], after["facts"], after["published"], after["state"], after["provenance"],
            after["incarnation"]) == (before["contract"], before["facts"], True, before["state"], before["provenance"],
                                      before["incarnation"])
    # With no override left, a bundled id cannot be deleted: 409 id_is_bundled, as before.
    again = api.delete(f"/admin/api/workflows/{BUNDLED_ID}", headers={"If-Match": os_n(api)})
    assert again.status_code == 409 and again.json()["error"]["code"] == "id_is_bundled"


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_delete_of_a_bundled_id_with_only_run_control_reverts_it(api):
    before = _detail(api)
    assert _post(api, "publish", {"published": False}).status_code == 200
    reverted = api.delete(f"/admin/api/workflows/{BUNDLED_ID}", headers={"If-Match": os_n(api)})
    assert reverted.status_code == 200, reverted.json()
    assert _detail(api)["published"] is True and _detail(api)["provenance"] == before["provenance"]


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_restore_of_a_run_control_patch_revision_writes_the_patch_and_a_contract_revision_still_restores(api):
    before = _detail(api)
    first = _post(api, "publish", {"published": False})
    assert first.status_code == 200
    patch_revision = first.json()["commit"]["record_revision"]
    edited = {**before["contract"], "display_name": "Mine"}
    assert put(api, edited, overrides=BUNDLED_ID).status_code == 200
    assert _detail(api)["contract"]["display_name"] == "Mine"
    # The patch revision comes back as a patch: the bundled contract serves again, unpublished.
    back = _post(api, "restore", {"record_revision": patch_revision})
    assert back.status_code == 200, back.json()
    now = _detail(api)
    assert (now["contract"], now["published"], now["provenance"]) == (before["contract"], False, before["provenance"])
    # And the contract override revision restores as a contract record.
    contract_revision = patch_revision + 1
    again = _post(api, "restore", {"record_revision": contract_revision})
    assert again.status_code == 200, again.json()
    assert _detail(api)["contract"]["display_name"] == "Mine"


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_a_release_that_takes_an_owner_workflow_name_holds_that_workflow_out_and_every_other_write_works(api):
    # The writers refuse a new clash, but a release can bring one: it gives a bundled workflow an alias with the token of
    # an owner workflow's id. The registry keeps serving (ratcheting validation, Kubernetes KEP-4008): the owner workflow
    # is held out and named in the listing, the rest serves, and a write elsewhere is neither refused nor "saved but not
    # applied". Saving the held-out workflow is refused with the clash; deleting it clears it.
    assert create(api, owner_contract("owner.held.demo"), facts=FACTS_BODY).status_code == 201
    assert create(api, owner_contract("owner.other.demo"), facts=FACTS_BODY).status_code == 201
    newer = _restarted(api, {BUNDLED_ID: lambda manifest: {**manifest, "aliases": ["Owner-Held-Demo"]}})
    listing = newer.get("/admin/api/workflows").json()
    ids = {row["id"] for row in listing["workflows"]}
    assert "owner.other.demo" in ids and BUNDLED_ID in ids and "owner.held.demo" not in ids
    assert [held["id"] for held in listing["held_out"]] == ["owner.held.demo"]
    assert {problem["code"] for problem in listing["held_out"][0]["problems"]} == {"id_clash"}
    assert create(newer, owner_contract("owner.third.demo"), facts=FACTS_BODY).status_code == 201
    edited = put(newer, owner_contract("owner.other.demo", display_name="Edited"))
    assert edited.status_code == 200, edited.text
    refused = put(newer, owner_contract("owner.held.demo", display_name="Still clashing"))
    assert refused.status_code == 422 and "id_clash" in refused.text
    gone = newer.delete("/admin/api/workflows/owner.held.demo", headers={"If-Match": os_n(newer)})
    assert gone.status_code == 200, gone.text
    assert newer.get("/admin/api/workflows").json()["held_out"] == []


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_the_detail_of_a_held_out_owner_workflow_is_its_stored_contract_with_the_hold_problems(api):
    # c2d-held-clash-review-1108 (2): "not serving" is not "not stored". The owner needs the document to repair it.
    assert create(api, owner_contract("owner.held.demo", display_name="Mine"), facts=FACTS_BODY).status_code == 201
    newer = _restarted(api, {BUNDLED_ID: lambda manifest: {**manifest, "aliases": ["Owner-Held-Demo"]}})
    response = newer.get("/admin/api/workflows/owner.held.demo")
    assert response.status_code == 200, response.text
    body = response.json()["workflow"]
    assert body["id"] == "owner.held.demo" and body["held_out"] is True
    assert body["contract"]["display_name"] == "Mine"
    assert {problem["code"] for problem in body["hold_problems"]} == {"id_clash"}
    assert body["serving"] is None
    assert newer.get("/admin/api/workflows/owner.nowhere.demo").status_code == 404




@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_the_detail_of_a_held_out_override_is_the_stored_contract_and_the_serving_bundled_one_is_named_apart(api):
    other = "image.edit.flux2-klein-4b"
    edited = {**_detail(api)["contract"], "display_name": "Mine", "aliases": ["mine-alias"]}
    assert create(api, edited, overrides=BUNDLED_ID).status_code == 201
    newer = _restarted(api, {other: lambda manifest: {**manifest, "aliases": ["mine-alias"]}})
    body = _detail(newer)
    assert body["held_out"] is True and body["contract"]["display_name"] == "Mine"
    assert {problem["code"] for problem in body["hold_problems"]} == {"id_clash"}
    assert body["serving"]["incarnation"] == "bundled" and body["serving"]["contract"]["display_name"] != "Mine"


def _restarted_without(client, workflow_id, monkeypatch):
    """The gateway restarted on a release that no longer ships ``workflow_id``."""
    from dataclasses import replace  # noqa: F401, PLC0415

    from gateway import registry_snapshot

    shipped = service_registry.bundled_workflow_manifests()
    monkeypatch.setattr(service_registry, "bundled_workflow_manifests",
                        lambda directory=None: {k: v for k, v in shipped.items() if k != workflow_id})

    def builder(document, _release, *, generation):
        release = registry_snapshot.bundled_release()
        kept = {k: v for k, v in release.workflows.items() if k != workflow_id}
        return registry_snapshot.build(document, registry_snapshot.BundledRelease(kept, release.frontends),
                                       generation=generation)

    registry = WorkflowRegistryStore(path=client.db, definitions=DefinitionStore(client.jobs_db),
                                     build_snapshot=builder, config=lambda: client.config)
    app = FastAPI()
    app.include_router(create_admin_router(AdminSQLiteStore(client.db), auth=lambda _request: OWNER,
                                           workflow_registry=registry))
    restarted = TestClient(app, raise_server_exceptions=False)
    restarted.db, restarted.config, restarted.registry, restarted.jobs_db = client.db, client.config, registry, client.jobs_db
    return restarted


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_restoring_a_run_control_patch_after_its_workflow_left_the_release_is_a_typed_409(api, monkeypatch):
    # c2d-R-patch-review-1108 (1), first sequence: unpublish a bundled workflow, upgrade to a release without it.
    first = _post(api, "publish", {"published": False})
    assert first.status_code == 200
    patch_revision = first.json()["commit"]["record_revision"]
    newer = _restarted_without(api, BUNDLED_ID, monkeypatch)
    before = admin_store.get_service_registry_json_with_generation(path=api.db)["operator_settings_generation"]
    response = newer.post(f"/admin/api/workflows/{BUNDLED_ID}/restore", json={"record_revision": patch_revision},
                          headers={"If-Match": os_n(newer)})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "bundled_workflow_removed"
    assert admin_store.get_service_registry_json_with_generation(path=api.db)["operator_settings_generation"] == before


@pytest.mark.parametrize("api", ["real"], indirect=True)
def test_restoring_a_patch_over_an_owner_contract_of_a_removed_bundled_id_is_refused_and_keeps_the_contract(api, monkeypatch):
    # Second sequence: the owner then created a full contract under the now unbundled id; the old patch must not replace it.
    first = _post(api, "publish", {"published": False})
    patch_revision = first.json()["commit"]["record_revision"]
    newer = _restarted_without(api, BUNDLED_ID, monkeypatch)
    mine = owner_contract(BUNDLED_ID, display_name="Mine")
    assert create(newer, mine, facts=FACTS_BODY).status_code == 201
    response = newer.post(f"/admin/api/workflows/{BUNDLED_ID}/restore", json={"record_revision": patch_revision},
                          headers={"If-Match": os_n(newer)})
    assert response.status_code == 409 and response.json()["error"]["code"] == "bundled_workflow_removed"
    assert _detail(newer)["contract"]["display_name"] == "Mine"
