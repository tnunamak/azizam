"""The "Workflow image engine" definition: an owner adds a node-graph engine as data and gets pictures from it.

Four things are proved here, with no real engine and no real model file:

* the record the owner saves is the graph service for pictures, video and sound once the gateway reloads;
* the engine's own node list decides which workflows are offered, and a missing node names itself;
* one picture goes through the added record to a fake engine (``/prompt``, ``/history/<id>``, ``/view``) and the
  receipt names the record;
* the template refuses an install folder that has no ``main.py``.

The shape of the fake ``/object_info`` follows the engine's own source: a dict keyed by node class, whose combo inputs
read ``[[names], {...}]`` (``nodes.py`` ``INPUT_TYPES`` and ``server.py`` ``node_info``). The names are invented.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path

import pytest

import proxy
from gateway import admin_store, engine_admin, engine_check, process_template
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore
from gateway.engine_check import Answer
from gateway.engine_definitions import load_engine_catalog
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, scenario
from tests.support.gateway import use_admin_db
from tests.test_engine_contract_runtime import free_port

DEFINITION = "workflow-image-engine"
FAMILIES = ("images", "videos", "audio.generate")
OWNER = AdminContext(subject="owner-1", roles=frozenset({"owner", "admin"}), actor_id="owner-1")
STUB_MAIN = "# a stand-in for the engine's main script; the tests never start it\n"


# -- an install folder, a saved record, and a fake node list --------------------------------------------------------


class Install:
    """An allowed folder holding one engine install (``main.py`` and a ``.venv``), and one folder without ``main.py``."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "allowed"
        self.folder = self.root / "studio"
        (self.folder / ".venv" / "bin").mkdir(parents=True)
        (self.folder / "main.py").write_text(STUB_MAIN)
        os.symlink(sys.executable, self.folder / ".venv" / "bin" / "python")
        self.bare = self.root / "bare"
        (self.bare / ".venv" / "bin").mkdir(parents=True)
        os.symlink(sys.executable, self.bare / ".venv" / "bin" / "python")


@pytest.fixture
def install(tmp_path):
    return Install(tmp_path)


def service_over(db: Path, install: Install, **kwargs) -> engine_admin.EngineService:
    admin_store.put_operator_setting(process_template.SETTING, {"folders": [str(install.root)]}, path=db)
    return engine_admin.EngineService(AdminSQLiteStore(db), gateway_ports=lambda: {5000, 15000}, **kwargs)


def add_engine(service: engine_admin.EngineService, folder: Path, port: int, name: str = "Studio pictures") -> dict:
    """Save the record the way the Add engine dialog does: the definition, a name and the two slot values."""
    generation = service.store.get_service_registry_snapshot()[1]
    body = {"definition": DEFINITION, "name": name, "slots": {"folder": str(folder), "port": port}}
    return asyncio.run(service.create(OWNER, body, expected_generation=generation))


@pytest.fixture
def live(monkeypatch, tmp_path):
    """The live gateway on a fresh admin store; the published snapshot is put back after the test (tests/conftest.py)."""
    monkeypatch.setattr(proxy, "config", proxy.config)
    monkeypatch.setattr(proxy, "VIDEO_ALIASES", proxy.VIDEO_ALIASES)
    monkeypatch.setattr(proxy, "AUDIO_ALIASES", proxy.AUDIO_ALIASES)
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", proxy.REGISTRY_APPLY_PROBLEM)
    return use_admin_db(monkeypatch, tmp_path)


def object_info(*classes: str) -> dict:
    """A node list as the engine answers it: one entry per node class, a combo input as ``[[names], {...}]``."""
    return {
        name: {
            "input": {"required": {"choice": [["alpha-one", "alpha-two"], {"default": "alpha-one"}], "count": ["INT", {"default": 1}]}},
            "output": ["IMAGE"], "name": name, "display_name": name, "category": "invented", "output_node": False,
        }
        for name in classes
    }


def workflow(identifier: str, *nodes: str) -> dict:
    return {"id": identifier, "display_name": identifier.title(), "bindings": {"comfyui": {"requires": {"custom_nodes": list(nodes)}}}}


MANIFESTS = [
    workflow("draw", "SampleLatent", "DecodePixels"),
    workflow("scale", "TileScaler"),
    workflow("restyle", "StyleBlend", "SampleLatent"),
]
FULL_LIST = ("SampleLatent", "DecodePixels", "TileScaler", "StyleBlend", "SaveOutput")


def fetch_for(node_list: dict, *, stats: dict | None = None):
    """A fake engine for the check: the status page and the node list answer; every other path is not found."""
    seen: list[str] = []

    async def fetch(method, url, **_sent):
        path = "/" + url.split("//", 1)[1].split("/", 1)[1] if url.count("/") > 2 else "/"
        seen.append(f"{method} {path}")
        if method == "GET" and path == "/system_stats":
            return Answer(method=method, url=url, status=200, body=json.dumps(stats or {"system": {"os": "invented"}, "devices": []}).encode())
        if method == "GET" and path == "/object_info":
            return Answer(method=method, url=url, status=200, body=json.dumps(node_list).encode())
        return Answer(method=method, url=url, status=404, body=b"{}")

    return fetch, seen


# -- 1. the added record is the graph service --------------------------------------------------------------------


def test_added_comfy_record_is_the_images_graph_service(live, install):
    port = free_port()
    service = service_over(live, install)
    created = add_engine(service, install.folder, port)
    service_id = created["service_id"]

    # The saved record is data: a node-graph protocol record with the address the port implies and a literal start command.
    stored = service.store.get_service_registry()["services"][service_id]
    assert stored["enabled"] is True
    assert stored["capabilities"] == ["images", "generations", "videos", "audio.generate"]
    assert stored["config"]["protocol"] == "node-graph" and stored["config"]["address"] == f"http://127.0.0.1:{port}"
    real = os.path.realpath(install.folder)
    assert stored["process"]["start"] == [f"{real}/.venv/bin/python", f"{real}/main.py", "--listen", "127.0.0.1",
                                          "--port", str(port)]

    # The gateway loads the saved registry, and each graph family is that record.
    proxy.reload_runtime_config()
    for family in FAMILIES:
        view = proxy._graph_service(family)
        assert (view.service_id, view.enabled, view.host) == (service_id, True, f"http://127.0.0.1:{port}"), family
    assert proxy.DERIVED.GRAPH_HOST == f"http://127.0.0.1:{port}" and proxy.DERIVED.GRAPH_ENABLED is True


# -- 2. offerings follow the node list ---------------------------------------------------------------------------


def test_offerings_are_the_workflows_whose_requires_the_inventory_satisfies():
    full = engine_check.workflow_offerings(MANIFESTS, "comfyui", FULL_LIST)
    assert [row["id"] for row in full["offered"]] == ["draw", "scale", "restyle"] and full["unavailable"] == []

    # One node gone from the engine's list removes exactly the workflow that needs it, and says which node.
    without = engine_check.workflow_offerings(MANIFESTS, "comfyui", [n for n in FULL_LIST if n != "StyleBlend"])
    assert [row["id"] for row in without["offered"]] == ["draw", "scale"]
    assert [row["id"] for row in without["unavailable"]] == ["restyle"]
    assert [(m["kind"], m["name"]) for m in without["unavailable"][0]["missing"]] == [("custom_node", "StyleBlend")]
    assert without["unavailable"][0]["missing"][0]["reason"]
    assert without["nodes"] == len(FULL_LIST) - 1

    # A workflow that names no engine binding is not this engine's to offer.
    other = {"id": "elsewhere", "bindings": {"someone-else": {"requires": {"custom_nodes": ["Nothing"]}}}}
    assert engine_check.workflow_offerings([other], "comfyui", FULL_LIST) == {"nodes": len(FULL_LIST), "offered": [], "unavailable": []}


def test_the_check_reads_the_node_list_and_reports_the_offerings(live, install):
    fetch, seen = fetch_for(object_info(*[n for n in FULL_LIST if n != "TileScaler"]))
    service = service_over(live, install, workflow_manifests=lambda: MANIFESTS, fetch=fetch)
    body = {"definition": DEFINITION, "values": {"address": "http://127.0.0.1:40123"}}
    check = asyncio.run(service.check(OWNER, body))

    workflows = check["workflows"]
    assert [row["id"] for row in workflows["offered"]] == ["draw", "restyle"]
    assert [row["id"] for row in workflows["unavailable"]] == ["scale"]
    assert workflows["unavailable"][0]["missing"][0]["name"] == "TileScaler"
    assert seen.count("GET /object_info") == 1  # the node list is read once, not once per workflow


def test_a_node_list_cut_short_offers_nothing_rather_than_a_wrong_answer():
    # The engine's real list holds a few thousand nodes; a list that hit the entry cap is not a full inventory.
    big = object_info(*(f"Node{i}" for i in range(60)))
    read = engine_check.read_list(big, {"shape": "object_keys", "max_entries": 10})
    assert read["truncated"] is True and len(read["ids"]) == 10


# -- 3. a first picture through the added record -----------------------------------------------------------------


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 140, 90)).save(out, format="PNG")
    return out.getvalue()


def test_a_first_image_goes_through_the_added_record_to_the_engine(live, install, tmp_path):
    port = free_port()
    service = service_over(live, install)
    created = add_engine(service, install.folder, port)
    proxy.reload_runtime_config()
    png = _png()

    with GatewayRig("comfy-definition", tmp_path / "rig") as rig:
        fake = rig.engine("comfy", scenario("comfyui", "comfy-definition", comfy_exchanges(views=[(png, "image/png")])),
                          host=f"127.0.0.1:{port}")
        assert proxy._graph_service("images").service_id == created["service_id"]
        assert proxy.DERIVED.GRAPH_HOST == fake.base_url

        async def drive():
            return await rig.call("POST", "/v1/images/generations", label="first picture",
                                  json_body={"prompt": "a green square", "response_format": "b64_json", "background": "opaque"})

        response = asyncio.run(drive())
        trace = rig.trace()

    assert response is not None and response.status_code == 200, response.text
    items = response.json()["data"]
    assert len(items) == 1
    import base64

    assert base64.b64decode(items[0]["b64_json"])[:8] == b"\x89PNG\r\n\x1a\n"
    seen = [(call["method"], call["path"]) for call in trace["engine_requests"]]
    prompt_at = seen.index(("POST", "/prompt"))
    assert ("GET", "/history/p1") in seen[prompt_at:] and ("GET", "/view") in seen[prompt_at:]
    receipts = trace["receipts"]["receipts"]
    assert [r["receipt_json"]["backend"] for r in receipts] == [created["service_id"]]


# -- 4. the template needs a main.py -----------------------------------------------------------------------------


def test_the_template_refuses_a_folder_with_no_main_py(live, install):
    service = service_over(live, install)
    with pytest.raises(Exception) as caught:
        add_engine(service, install.bare, free_port())
    assert getattr(caught.value, "code", None) == "process_template_refused"
    assert "main.py" in str(caught.value)
    assert not service.store.get_service_registry().get("services")  # nothing was saved


def test_the_shipped_definition_names_no_engine_product_and_asks_for_one_folder_and_one_port():
    definition = load_engine_catalog().definition(DEFINITION)
    template = definition["process_template"]
    assert sorted(template["slots"]) == ["folder", "port"]
    assert template["slots"]["folder"]["locate"] == ["main.py"]
    assert template["argv"][1] == "{folder}/main.py" and "127.0.0.1" in template["argv"]
