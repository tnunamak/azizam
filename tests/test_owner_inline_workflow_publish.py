"""An owner's inline image workflow saves, publishes, reloads and serves its own graph.

The reload runs the gateway's own two steps (``proxy._publish_runtime_config``): ``proxy._load_gateway_workflow_registry``
(the bundled workflows under the stored ones, read from ``proxy.ADMIN_DB_PATH``, pointed here at the test's database)
and ``proxy._load_image_graph_roles``. One difference: the gateway passes the effective images section (an owner's
values over the release defaults); these tests pass the release defaults, since they set no owner images value. A reload
that raises is the gateway refusing the change after it was saved: the API answers 500 ``registry_not_applied``.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, image_graph_roles, registry_layers, service_registry
from gateway.admin_api import create_admin_router
from gateway.admin_runtime import AdminSQLiteStore
from gateway.definition_store import DefinitionStore
from gateway.discovery import WorkflowRegistry
from gateway.workflow_registry_store import WorkflowRegistryStore
from tests.registry_test_helpers import write_legacy_registry_for_test, write_service_registry_for_test
from tests.test_workflow_owner_api import FACTS_BODY, OWNER, _real_snapshot, os_n

ROOT = Path(__file__).resolve().parents[1]
BINDING = "comfyui"  # every shipped image graph's binding key
WORKFLOW_ID = "image.generate.krea2-turbo"  # a stock image workflow with a shipped graph_roles row
INTENT = "image.generate"
MODEL = "krea2-turbo"
OWNER_MODEL = "owner-krea"
OWNER_ID = f"{INTENT}.{OWNER_MODEL}"
KREA = "workflows/images/krea2-turbo.json"
STEPS_NODE = "7"  # the graph's sampler
PROCESSOR = "10"  # the graph's background-removal node, named by the release row


def _shipped_section() -> dict:
    return yaml.safe_load((ROOT / "registry" / "defaults" / "images.yml").read_text(encoding="utf-8"))


def _gateway_reload():
    """The reload's workflow registry and image graph roles; a refused table raises."""
    registry = proxy._load_gateway_workflow_registry(strict=True)
    return registry, proxy._load_image_graph_roles(_shipped_section(), registry, proxy.DERIVED._GRAPH_BINDING)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    write_service_registry_for_test(service_registry.normalize_service_registry({}), path=db)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    assert proxy.DERIVED._GRAPH_BINDING == BINDING
    _gateway_reload()  # the shipped release reloads cleanly before any owner edit
    registry = WorkflowRegistryStore(path=db, definitions=DefinitionStore(tmp_path / "jobs.db"),
                                     build_snapshot=_real_snapshot, config=dict, on_changed=_gateway_reload)
    app = FastAPI()
    app.include_router(create_admin_router(AdminSQLiteStore(db), auth=lambda _request: OWNER,
                                           workflow_registry=registry))
    test_client = TestClient(app, raise_server_exceptions=False)
    test_client.db = db
    return test_client


def _inline(client, graph):
    contract = client.get(f"/admin/api/workflows/{WORKFLOW_ID}").json()["workflow"]["contract"]
    binding = contract["bindings"][BINDING]
    return {**contract, "bindings": {**contract["bindings"], BINDING: {**binding, "workflow": {"inline": graph}}}}


def _bundled_graph() -> dict:
    return json.loads((ROOT / KREA).read_text(encoding="utf-8"))


def _save_and_publish(client, graph) -> None:
    contract = {**_inline(client, graph), "id": OWNER_ID}
    saved = client.post("/admin/api/workflows", json={"contract": contract, "facts": FACTS_BODY})
    assert saved.status_code == 201, saved.json()
    before = os_n(client)
    published = client.post(f"/admin/api/workflows/{OWNER_ID}/publish", json={"published": True},
                            headers={"If-Match": before})
    assert published.status_code == 200, published.json()
    assert os_n(client) != before  # the publish advanced the serving revision


def _request_cutout(tmp_path, registry, roles, configure_engine):
    from tests.support.s7_parity_images_rig import observe

    def setup(rig):
        configure_engine(rig)
        rig.monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry)
        rig.monkeypatch.setattr(proxy, "_IMAGE_GRAPH_ROLES", roles)

    return observe(tmp_path, "/v1/images/generations", {"json_body": {
        "model": OWNER_MODEL, "prompt": "a red fox", "response_format": "b64_json",
        "background": "transparent"}}, setup, row=False)


def test_an_owner_inline_workflow_publishes_and_serves_its_own_cutout(client, tmp_path):
    from tests.support.s7_parity_images_facts import engine_bodies
    from tests.test_s7_parity_images import cutout_ok

    graph = _bundled_graph()
    graph[STEPS_NODE]["inputs"]["steps"] = 13
    processor = "11"
    graph[processor] = graph.pop(PROCESSOR)
    graph["_meta"]["background_removal"].update(transparent_image=[processor, 0], rmbg_node=processor)
    _save_and_publish(client, graph)

    entry = client.get(f"/admin/api/workflows/{OWNER_ID}").json()["workflow"]
    assert entry["contract"]["bindings"][BINDING]["workflow"] == {"inline": graph}
    assert entry["published"] is True
    registry, roles = _gateway_reload()
    obs = _request_cutout(tmp_path, registry, roles, cutout_ok)

    assert obs.status == 200, (obs.body, obs.extra)
    assert obs.headers["x-image-background-removal"] == "true"
    submitted = [json.loads(body["text"])["prompt"] for body in engine_bodies(obs, "/prompt") if body.get("kind") == "json"]
    assert submitted, obs.engine
    prompt = submitted[0]
    assert prompt[STEPS_NODE]["inputs"]["steps"] == 13
    assert prompt["9"]["inputs"]["images"] == [processor, 0]
    assert prompt[processor]["inputs"]["image"] == graph["9"]["inputs"]["images"]


def test_an_owner_inline_workflow_without_cutout_metadata_reports_it_is_unsupported(client, tmp_path):
    from tests.support.s7_parity_images_facts import engine_bodies
    from tests.test_s7_parity_images import ok

    graph = _bundled_graph()
    graph.pop("_meta")
    _save_and_publish(client, graph)

    registry, roles = _gateway_reload()
    obs = _request_cutout(tmp_path, registry, roles, ok)

    assert obs.status == 200, (obs.body, obs.extra)
    assert obs.headers["x-image-background-removal"] == "false"
    assert obs.headers["x-image-background-removal-source"] == "workflow_unsupported"
    submitted = [json.loads(body["text"])["prompt"] for body in engine_bodies(obs, "/prompt") if body.get("kind") == "json"]
    assert submitted, obs.engine
    assert submitted[0]["9"]["inputs"]["images"] == graph["9"]["inputs"]["images"]


@pytest.mark.parametrize("marker", [{}, {"overrides": WORKFLOW_ID}], ids=["unmarked", "legacy-marker"])
def test_reload_serves_the_release_over_a_kept_legacy_inline_workflow(client, marker):
    release = _gateway_reload()[0].get(WORKFLOW_ID).manifest
    graph = _bundled_graph()
    graph[STEPS_NODE]["inputs"]["steps"] = 13
    manifest = _inline(client, graph)
    write_legacy_registry_for_test({"workflows": {WORKFLOW_ID: {registry_layers.REPLACE_KEY: {
        "manifest": manifest, "source": "ui", "published": True, **marker,
    }}}}, path=client.db)
    stored = admin_store.get_service_registry_json_with_generation(path=client.db)

    registry, _ = _gateway_reload()

    assert registry.get(WORKFLOW_ID).manifest == release
    assert admin_store.get_service_registry_json_with_generation(path=client.db) == stored


def test_a_release_row_whose_workflow_is_not_served_is_still_refused(tmp_path):
    manifests = tmp_path / "manifests" / "workflows"
    shutil.copytree(ROOT / "manifests" / "workflows", manifests)
    (manifests / "krea2-turbo.yml").unlink()
    (tmp_path / "workflows").symlink_to(ROOT / "workflows")  # graphs resolve beside the manifests
    registry = WorkflowRegistry.from_directory(manifests)
    assert WORKFLOW_ID not in registry.ids()
    with pytest.raises(ValueError, match="unknown graph path"):
        image_graph_roles.load(_shipped_section(), registry, BINDING)


def _with_inline(registry, graph):
    """``registry`` with the stock record's graph replaced inline, bypassing the owner API's checks."""
    record = next(r for r in registry.records() if r.identity.id == WORKFLOW_ID)
    manifest = copy.deepcopy(record.manifest)
    manifest["bindings"][BINDING]["workflow"] = {"inline": graph}
    registry.add_manifest(manifest, source_path=record.source_path, replace=True, facts=record.facts)
    return registry


def test_an_invalid_inline_graph_is_refused_and_never_falls_back_to_the_bundled_file(client, monkeypatch):
    registry, _ = _gateway_reload()
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", _with_inline(registry, {"1": {"inputs": {}}}))

    assert proxy._load_graph_workflow_outcome(MODEL, INTENT) == (None, True)
    assert proxy.load_graph_workflow(MODEL, intent=INTENT) is None
    assert proxy.graph_workflow_refused(MODEL, intent=INTENT)


def test_an_inline_graph_is_found_by_its_record_not_by_a_file_name(client, monkeypatch):
    registry, _ = _gateway_reload()
    graph = _bundled_graph()
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", _with_inline(registry, graph))

    from gateway.workflow_run import plain
    assert plain(proxy._graph_workflow_artifact_for_model(MODEL, INTENT)[1].inline) == graph
    # The name fallback matches file stems; an inline graph has none, so a name no record owns finds nothing.
    assert proxy._graph_workflow_artifact_for_model("not-a-workflow") is None


def test_an_empty_inline_graph_is_refused_and_never_falls_back_to_the_bundled_file(client, monkeypatch):
    registry, _ = _gateway_reload()
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", _with_inline(registry, {}))

    assert proxy._load_graph_workflow_outcome(MODEL, INTENT) == (None, True)


def test_graph_roles_ignore_a_row_keyed_by_an_owner_workflow_id(client, monkeypatch):
    graph = _bundled_graph()
    _save_and_publish(client, graph)
    registry, roles = _gateway_reload()
    record = next(record for record in registry.records() if record.identity.id == OWNER_ID)
    monkeypatch.setattr(proxy, "_IMAGE_GRAPH_ROLES", {**roles, OWNER_ID: roles[KREA]})
    cutout = proxy._image_graph_roles(record.manifest, {}, graph, OWNER_MODEL)

    assert "background_removal" not in cutout
