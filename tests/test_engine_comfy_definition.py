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


# -- 5. a second engine of this kind changes who serves the work ---------------------------------------------------


def another_install(install: Install, name: str) -> Path:
    """One more allowed folder with an engine install, for a second record."""
    folder = install.root / name
    (folder / ".venv" / "bin").mkdir(parents=True)
    (folder / "main.py").write_text(STUB_MAIN)
    os.symlink(sys.executable, folder / ".venv" / "bin" / "python")
    return folder


def add_named(service: engine_admin.EngineService, folder: Path, name: str, **extra) -> dict:
    generation = service.store.get_service_registry_snapshot()[1]
    body = {"definition": DEFINITION, "name": name, "slots": {"folder": str(folder), "port": free_port()}, **extra}
    return asyncio.run(service.create(OWNER, body, expected_generation=generation))


def saved_ids(service: engine_admin.EngineService) -> set[str]:
    return set((service.store.get_service_registry().get("services") or {}))


@pytest.mark.parametrize("name, service_id, serves_after", [
    ("Studio", "studio", "comfyui"),
    ("ComfyUI 2", "comfyui-2", "comfyui"),
    ("Alpha", "alpha", "alpha"),
])
def test_a_second_graph_engine_says_who_will_serve_and_saves_only_on_a_yes(live, install, name, service_id, serves_after):
    service = service_over(live, install)
    add_named(service, install.folder, "ComfyUI")
    assert saved_ids(service) == {"comfyui"}
    folder = another_install(install, "second")

    with pytest.raises(engine_admin.EngineRouteError) as caught:
        add_named(service, folder, name)
    assert (caught.value.status_code, caught.value.code) == (409, "engine_check_warnings")
    assert saved_ids(service) == {"comfyui"}  # without a yes nothing was saved
    finding = caught.value.check["findings"][0]
    assert finding["code"] == "shadows_engine" and finding["severity"] == "warning"
    assert "“ComfyUI” makes pictures, video and sound and music" in finding["what"]
    if serves_after == "comfyui":
        assert "“ComfyUI” will keep making pictures, video and sound and music" in finding["what"]
        assert "this engine will not be used" in finding["what"]
    else:
        assert "After you add this engine, it will make pictures, video and sound and music instead" in finding["what"]

    saved = add_named(service, folder, name, acknowledge_warnings=True)
    assert saved["service_id"] == service_id and saved_ids(service) == {"comfyui", service_id}
    # What the finding said is what the gateway does with the saved records.
    proxy.reload_runtime_config()
    assert {proxy._graph_service(family).service_id for family in FAMILIES} == {serves_after}


def test_the_first_graph_engine_and_a_disabled_other_raise_no_shadow_warning(live, install):
    service = service_over(live, install)
    add_named(service, install.folder, "ComfyUI", disabled=True)  # off, and it names no capability
    folder = another_install(install, "second")
    add_named(service, folder, "Studio")  # no other engine is on: nothing is shadowed
    assert saved_ids(service) == {"comfyui", "studio"}


def test_the_shadow_order_is_the_gateways_on_three_records():
    from gateway.service_priority import selected_service

    def record(priority=None, enabled=True):
        out = {"capabilities": ["images", "generations", "videos", "audio.generate"], "protocol": "node-graph",
               "enabled": enabled, "host": "127.0.0.1", "port": 8188}
        if priority is not None:
            out["priority"] = priority
        return out

    for backends in (
        {"comfyui": record(), "studio": record(), "alpha": record()},
        {"comfyui": record(50), "studio": record(), "alpha": record(60)},
        {"comfyui": record(), "studio": record(10, enabled=False), "alpha": record(10)},
    ):
        for family, view in proxy._graph_services_of(backends).items():
            assert (selected_service(backends, family) or ("", {}))[0] == view.service_id, (family, backends)


# -- 6. offerings follow the node list and the model files it offers -----------------------------------------------


def loader_list(*classes: str, files=("alpha-weights.safetensors", "sub/beta-weights.safetensors")) -> dict:
    """A node list with a loader whose one combo input offers these model files, as the engine's own list does."""
    out = object_info(*classes)
    out["WeightLoader"] = {"input": {"required": {"weights_name": [list(files), {}]}}, "output": ["MODEL"], "name": "WeightLoader"}
    return out


def needing(identifier: str, *models: tuple[str, str]) -> dict:
    block = {"requires": {"custom_nodes": [], "models": [{"role": role, "path": path} for role, path in models]}}
    return {"id": identifier, "display_name": identifier.title(), "bindings": {"comfyui": block}}


def check_with(live, install, manifests, fetch) -> dict:
    service = service_over(live, install, workflow_manifests=lambda: manifests, fetch=fetch)
    return asyncio.run(service.check(OWNER, {"definition": DEFINITION, "values": {"address": "http://127.0.0.1:40123"}}))


def test_a_workflow_that_needs_a_model_file_is_offered_only_when_a_combo_lists_it(live, install):
    manifests = [needing("present", ("diffusion_model", "diffusion_models/alpha-weights.safetensors")),
                 needing("nested", ("checkpoint", "checkpoints/beta-weights.safetensors")),
                 needing("absent", ("lora", "loras/gamma-weights.safetensors"))]
    fetch, _seen = fetch_for(loader_list("SampleLatent"))
    workflows = check_with(live, install, manifests, fetch)["workflows"]
    assert [row["id"] for row in workflows["offered"]] == ["present", "nested"]
    (absent,) = workflows["unavailable"]
    assert absent["id"] == "absent"
    (missing,) = absent["missing"]
    assert missing["kind"] == "model" and "gamma-weights.safetensors" in missing["name"]
    assert "gamma-weights.safetensors" in missing["reason"]


def test_a_node_with_an_empty_choice_list_does_not_satisfy_a_model_requirement(live, install):
    manifests = [needing("needs-file", ("diffusion_model", "diffusion_models/alpha-weights.safetensors"))]
    fetch, _seen = fetch_for(loader_list("SampleLatent", files=()))
    workflows = check_with(live, install, manifests, fetch)["workflows"]
    assert workflows["offered"] == [] and [row["id"] for row in workflows["unavailable"]] == ["needs-file"]


def test_a_model_file_in_the_newer_combo_form_counts(live, install):
    node_list = object_info("SampleLatent")
    node_list["NewLoader"] = {"input": {"required": {"weights_name": ["COMBO", {"options": ["alpha-weights.safetensors"]}]}}}
    manifests = [needing("present", ("diffusion_model", "diffusion_models/alpha-weights.safetensors"))]
    fetch, _seen = fetch_for(node_list)
    assert [row["id"] for row in check_with(live, install, manifests, fetch)["workflows"]["offered"]] == ["present"]


# -- 7. a node list that cannot be read is said so ------------------------------------------------------------------


def fetch_node_reply(**reply):
    """A fake engine whose status page answers and whose node list answers as ``reply`` says."""
    async def fetch(method, url, **_sent):
        path = "/" + url.split("//", 1)[1].split("/", 1)[1] if url.count("/") > 2 else "/"
        if method == "GET" and path == "/system_stats":
            return Answer(method=method, url=url, status=200, body=json.dumps({"system": {"os": "invented"}}).encode())
        if method == "GET" and path == "/object_info":
            return Answer(method=method, url=url, **{"status": 200, **reply})
        return Answer(method=method, url=url, status=404, body=b"{}")

    return fetch


NODE_LIST_FINDING = "so Azizam cannot say which workflows it can run."


@pytest.mark.parametrize("reply, said", [
    ({"transport": "too_large", "body": b""}, "The engine's node list was too large to read, " + NODE_LIST_FINDING),
    ({"body": b"<html>not json</html>"}, "The engine's node list was not in a form Azizam can read, " + NODE_LIST_FINDING),
    ({"body": b'{"detail": "Not authenticated"}'}, "The engine's answer was not a node list, " + NODE_LIST_FINDING),
    ({"body": b'["SampleLatent"]'}, "The engine's answer was not a node list, " + NODE_LIST_FINDING),
    ({"body": b'{"SampleLatent": {"name": "no inputs key"}}'}, "The engine's answer was not a node list, " + NODE_LIST_FINDING),
])
def test_an_unreadable_node_list_gives_no_offerings_and_a_finding(live, install, reply, said):
    check = check_with(live, install, MANIFESTS, fetch_node_reply(**reply))
    assert "workflows" not in check
    (finding,) = [f for f in check["findings"] if f["code"] == "node_list_unreadable"]
    assert finding["what"] == said and finding["severity"] == "warning"


def test_a_node_list_cut_short_at_the_entry_cap_gives_no_offerings_and_a_finding(live, install):
    cap = load_engine_catalog().definition(DEFINITION)["probes"]["lists"][0]["list"]["max_entries"]
    body = json.dumps({f"Node{i}": {"input": {}} for i in range(cap + 1)}).encode()
    check = check_with(live, install, MANIFESTS, fetch_node_reply(body=body))
    assert "workflows" not in check
    (finding,) = [f for f in check["findings"] if f["code"] == "node_list_unreadable"]
    assert finding["what"] == "The engine's node list was longer than Azizam reads, " + NODE_LIST_FINDING
    # One node fewer is the whole list, and it is read.
    whole = json.dumps({f"Node{i}": {"input": {}} for i in range(cap)}).encode()
    assert "workflows" in check_with(live, install, MANIFESTS, fetch_node_reply(body=whole))


def test_the_node_probe_asks_for_its_own_byte_cap(live, install):
    asked: dict[str, int] = {}

    async def fetch(method, url, *, cap_bytes, **_sent):
        asked[url.rsplit("/", 1)[1]] = cap_bytes
        return Answer(method=method, url=url, status=200, body=b"{}")

    check_with(live, install, MANIFESTS, fetch)
    assert asked["object_info"] == 16 * 1024 * 1024
    assert asked["system_stats"] < asked["object_info"]


# -- 8. the status read a real test makes ---------------------------------------------------------------------------


def run_status_test(live, install, **reply):
    calls: list[tuple[str, str]] = []

    async def fetch(method, url, **_sent):
        calls.append((method, url))
        return Answer(method=method, url=url, **reply)

    service = service_over(live, install, fetch=fetch)
    body = {"definition": DEFINITION, "values": {"address": "http://127.0.0.1:40123"}, "capability": "images"}
    return asyncio.run(service.test(OWNER, body)), calls


def test_the_status_read_passes_on_a_200_with_its_key_and_reads_with_get(live, install):
    result, calls = run_status_test(live, install, status=200, body=json.dumps({"system": {}}).encode())
    assert result["ok"] is True and "finding" not in result
    assert calls == [("GET", "http://127.0.0.1:40123/system_stats")]


def test_the_status_read_fails_on_a_500(live, install):
    result, _calls = run_status_test(live, install, status=500, body=b'{"error": "broken"}')
    assert result["ok"] is False and "500" in result["reply"] and result["finding"]["what"]


def test_the_status_read_fails_on_a_200_without_its_key(live, install):
    result, _calls = run_status_test(live, install, status=200, body=b'{"devices": []}')
    assert result["ok"] is False and "system" in result["reply"]
