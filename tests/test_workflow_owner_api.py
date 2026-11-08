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
from tests.test_workflow_registry_store import BUNDLED_ID, _template, _with_inline, build_snapshot, owner_contract

OWNER = AdminContext(subject="owner-1", roles=frozenset({"owner"}), actor_kind="user", actor_label="Owner")
FACTS_BODY = {"exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "resource_class": "image",
              "tags": ["a"]}
ID = "owner.test.demo"


@pytest.fixture
def api(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    write_service_registry_for_test(service_registry.normalize_service_registry({}), path=db)
    config: dict = {}
    registry = WorkflowRegistryStore(path=db, definitions=DefinitionStore(tmp_path / "jobs.db"),
                                     build_snapshot=build_snapshot, config=lambda: config)
    roles = {"current": OWNER}

    def auth(_request):
        if roles["current"] is None:
            raise AdminAuthError("no", status_code=401, code="unauthenticated")
        return roles["current"]

    app = FastAPI()
    app.include_router(create_admin_router(AdminSQLiteStore(db), auth=auth, workflow_registry=registry))
    client = TestClient(app, raise_server_exceptions=False)
    client.db, client.config, client.roles, client.registry = db, config, roles, registry
    return client


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
    assert len(bounds) == 3 and {tuple(p["path"][-2:]) for p in bounds} == {("template", "inline")}
    assert {m for p in bounds for m in ("bytes", "nodes", "levels") if m in p["message"]} == {"bytes", "nodes", "levels"}


@pytest.mark.xfail(strict=True, reason="binding_descriptors reads a template as a path string; an inline template is "
                                       "refused as template_invalid until the descriptor accepts {inline: ...} (gap for the integrator)")
def test_an_inline_template_inside_the_bounds_is_accepted(api):
    assert create(api, _with_inline(owner_contract(), _template(2, 1))).status_code == 201


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
