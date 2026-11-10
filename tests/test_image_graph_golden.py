"""Golden: the exact graph the image routes post to the engine, for every shipped image workflow.

The image routes read their node roles (prompt, size, seed, batch size, image slots, background cut-out) from places that
are about to change. This file freezes what the engine receives, so a change of where a role is read from has to leave
every posted graph equal after parse to its recorded value. ``tests/fixtures/image_graph_golden/graphs.json`` holds, per case, the graphs
posted to ``/prompt`` (parsed from the raw request body, in post order), the receipt's requested and resolved workflow
names, and the key each round asked the VRAM estimate for (the batching key).

Each case runs with the release's graph_roles table in place and with an empty one (the graph's own ``_meta`` block then
supplies what no other home carries), and the engine must receive the same graphs both ways.

Cases: every generate workflow with a size that is not the default, with ``n=2`` (and a known VRAM estimate, so one round
carries the batch), with ``background=transparent``, with no seed sent, and with the workflow's own ``batch_size`` input;
every edit workflow with one source image and with ``background=transparent``; the two-reference workflow with two
references; the mask-bound workflows (and the edit workflows a mask turns into them) with a mask; ``n=2`` edits; and the
seamless-tile composition, which runs two graphs.

Normalised: only the seed values at the manifest's seed targets in the "no seed sent" cases, because the gateway draws
them at random there (``<random-seed>``). Nothing else is normalised: the uploaded file name is the fake engine's constant,
and the comparison parses the raw request body, not the rig's normalised trace.

To record the fixture again after an approved change, run this file once with the fixture missing or stale and read the
``image_graph_golden.*`` suite properties from the junit XML (the property value is the case's JSON).
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
import yaml
from PIL import Image

import proxy
from tests import test_adapter_differential as base
from gateway import image_graph_roles
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, history_done

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "image_graph_golden" / "graphs.json"
PROMPT = "a red fox in snow"
SEED = 41
NAMESPACE = "com.azizam.workflow"
GEN, EDIT = "/v1/images/generations", "/v1/images/edits"
SIZE = (64, 48)


def _manifests() -> dict[str, dict]:
    """Every shipped image manifest that runs a graph, by the graph's file stem."""
    found = {}
    for path in sorted((ROOT / "manifests" / "workflows").glob("*.yml")):
        manifest = yaml.safe_load(path.read_text())
        binding = (manifest.get("bindings") or {}).get("comfyui") or {}
        workflow = binding.get("workflow")
        if manifest.get("intent") in ("image.generate", "image.edit") and str(workflow).startswith("workflows/images/"):
            found[Path(workflow).stem] = manifest
    return found


MANIFESTS = _manifests()
GENERATE = sorted(name for name, m in MANIFESTS.items() if m["intent"] == "image.generate")
EDITS = sorted(name for name, m in MANIFESTS.items() if m["intent"] == "image.edit")
MULTIREF = "qwen-image-2.1-edit-multiref"
MASKED = ("flux2-klein-4b-inpaint", "qwen-image-2.1-inpaint", "flux2-klein-4b-edit", "qwen-image-2.1-edit")
BATCH_EDITS = ("flux2-klein-edit", "flux2-klein-4b-edit", "qwen-image-2.1-edit")
TILE = "flux2-klein-4b-seamless-tile"


def _cases() -> list[tuple[str, str]]:
    cases = []
    for name in GENERATE:
        cases += [("generate", name), ("generate-n2", name), ("generate-transparent", name), ("generate-noseed", name)]
        if "batch_size" in MANIFESTS[name]["inputs"]:
            cases.append(("generate-batch-input", name))
    for name in EDITS:
        if (MANIFESTS[name]["inputs"].get("mask") or {}).get("required"):
            continue  # a mask-bound workflow refuses an edit with no mask: its cases are "edit-mask"
        cases.append(("edit", name))
        if name != MULTIREF:
            cases.append(("edit-transparent", name))
    cases += [("edit-mask", name) for name in MASKED]
    cases += [("edit-n2", name) for name in BATCH_EDITS]
    cases.append(("tile", TILE))
    return cases


CASES = _cases()


def _png(size=SIZE, mode="RGB", color=(200, 40, 40)) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, format="PNG")
    return out.getvalue()


def _mask() -> bytes:
    image = Image.new("RGBA", SIZE, (0, 0, 0, 255))
    for x in range(SIZE[0] // 2):
        for y in range(SIZE[1]):
            image.putpixel((x, y), (0, 0, 0, 0))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _save_node(manifest: dict) -> str:
    return manifest["bindings"]["comfyui"]["selectors"]["image"]["node"]


def _history(*names: str) -> dict:
    done = history_done(node=_save_node(MANIFESTS[names[0]]))
    for name in names[1:]:
        done["p1"]["outputs"][_save_node(MANIFESTS[name])] = done["p1"]["outputs"][_save_node(MANIFESTS[names[0]])]
    return done


def _request(scenario: str, name: str) -> tuple[str, dict]:
    """The route and the ``call`` keyword arguments of one case."""
    extension = {"seed": SEED} if scenario != "generate-noseed" else {}
    fields: dict = {"model": name, "prompt": PROMPT, "response_format": "b64_json", "background": "opaque"}
    if scenario.startswith("generate") or scenario == "tile":
        fields["size"] = "512x768"
        if scenario == "generate-n2":
            fields["n"] = 2
        if scenario == "generate-transparent":
            fields["background"] = "transparent"
        if scenario == "generate-noseed":
            fields["size"] = "1024x1024"
        if scenario == "generate-batch-input":
            extension["batch_size"] = 2
        fields["extensions"] = {NAMESPACE: extension}
        return GEN, {"json_body": fields}
    # An edit: multipart, the extensions block as JSON text.
    fields["extensions"] = json.dumps({NAMESPACE: extension})
    if scenario == "edit-transparent":
        fields["background"] = "transparent"
    if scenario == "edit-n2":
        fields["n"] = "2"
    files = [("image", ("in.png", _png(), "image/png"))]
    if name == MULTIREF:
        files = [("image[]", ("one.png", _png(), "image/png")),
                 ("image[]", ("two.png", _png(color=(20, 200, 40)), "image/png"))]
    if scenario == "edit-mask":
        files.append(("mask", ("mask.png", _mask(), "image/png")))
    return EDIT, {"data": {key: str(value) for key, value in fields.items()}, "files": files}


def _find(value, key):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key:
                yield v
            yield from _find(v, key)
    elif isinstance(value, list):
        for item in value:
            yield from _find(item, key)


def _mask_seeds(graph: dict, manifest: dict) -> None:
    """The draw of a seed the caller did not send is random; mark the manifest's seed targets."""
    for target in manifest["bindings"]["comfyui"]["map"].get("seed", []):
        assert graph[target["node"]]["inputs"][target["input"]] == SEED, "seed binding was not applied"
        graph[target["node"]]["inputs"][target["input"]] = "<random-seed>"


def release_graph_roles() -> dict:
    """The graph_roles table the release ships (registry/defaults/images.yml), checked against the registry.

    The test deployment's images block was imported as the owner's whole state before the table existed, so the
    config it boots with has an empty table; the release's own table is put in place for the "release" runs.
    """
    section = yaml.safe_load((ROOT / "registry" / "defaults" / "images.yml").read_text())
    table = image_graph_roles.load(section, proxy.GATEWAY_WORKFLOW_REGISTRY, proxy.DERIVED._GRAPH_BINDING)
    assert len(table) == 18
    return table


def observe(tmp_path: Path, scenario: str, name: str, run: int, roles: str = "release", *, distinct_uploads: bool = False, reload_reference_order: bool = False) -> dict:
    """One case on a fresh rig: the graphs the engine received and what the route recorded about them.

    ``roles`` is "release" (the shipped graph_roles table is in place) or "graph-meta" (the table is empty, as for an
    install whose images section does not carry it): the engine must receive the same graphs either way.
    """
    path, call = _request(scenario, name)
    posted: list[dict] = []
    vram_keys: list[list] = []
    statuses: list[int] = []
    with GatewayRig(f"image-graph-golden-{scenario}-{name}", tmp_path / f"run{run}") as rig:
        names = ("flux2-klein-4b", "flux2-klein-4b-inpaint") if scenario == "tile" else (name,)
        if scenario == "edit-mask":  # a mask moves an edit to the workflow's mask-bound sibling, whose output node may differ
            names += tuple(n for n in (name.removesuffix("-edit") + "-inpaint",) if n in MANIFESTS)
        base._comfy(rig, comfy_exchanges(views=[(base._png(), "image/png")], history=_history(*names), strict=False))
        inner = httpx.AsyncHTTPTransport.handle_async_request

        uploaded = []

        async def tap(transport, request):
            if distinct_uploads and request.method == "POST" and request.url.path == "/upload/image":
                uploaded.append(f"asset-{len(uploaded) + 1}.png")
                return httpx.Response(200, json={"name": uploaded[-1], "subfolder": "", "type": "input"}, request=request)
            if request.method == "POST" and request.url.path == "/prompt":
                await request.aread()
                posted.append(json.loads(request.content)["prompt"])
            return await inner(transport, request)

        rig.monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", tap)
        if scenario == "generate-noseed":
            from gateway import generated_defaults
            rig.monkeypatch.setattr(generated_defaults, "secure_source", lambda low, high: SEED)
        rig.monkeypatch.setattr(proxy, "_IMAGE_GRAPH_ROLES", release_graph_roles() if roles == "release" else {})
        if reload_reference_order:
            candidate = dict(proxy.config)
            candidate["images"] = yaml.safe_load((ROOT / "registry/defaults/images.yml").read_text())
            entry = candidate["images"]["graph_roles"][f"workflows/images/{name}.json"]
            entry["reference_order"].reverse()
            # Use the real reload/publication path; only its candidate source is stubbed.
            rig.monkeypatch.setattr(proxy, "_runtime_config_for_reload", lambda: (candidate, None))
            rig.monkeypatch.setattr(proxy, "_IMAGE_GRAPH_ROLES", proxy._IMAGE_GRAPH_ROLES)
            proxy.reload_runtime_config()
            assert list(proxy._IMAGE_GRAPH_ROLES[f"workflows/images/{name}.json"]["reference_order"]) == entry["reference_order"]
        estimate = 100 if scenario in ("generate-n2", "edit-n2", "generate-batch-input") else None

        def spy(workflow_name, width, height):
            vram_keys.append([workflow_name, width, height])
            return estimate

        rig.monkeypatch.setattr(proxy, "get_vram_estimate", spy)
        rig.monkeypatch.setattr(proxy, "record_vram_estimate", lambda *a, **k: None)

        async def drive():
            response = await rig.call("POST", path, label=f"{scenario} {name}", **call)
            statuses.append(response.status_code)

        asyncio.run(drive())
        receipts = rig.trace()["receipts"] or {}
    if scenario == "generate-noseed":
        for graph in posted:
            _mask_seeds(graph, MANIFESTS[name])
    return {
        "status": statuses,
        "graphs": posted,
        "requested_workflow": sorted({str(v) for v in _find(receipts, "requested_workflow") if v}),
        "resolved_workflow": sorted({str(v) for v in _find(receipts, "resolved_workflow") if v}),
        "vram_keys": vram_keys,
    }


@pytest.mark.parametrize("roles", ["release", "graph-meta"])
@pytest.mark.parametrize(("scenario", "name"), CASES, ids=[f"{s}:{n}" for s, n in CASES])
def test_the_engine_receives_the_recorded_graph(tmp_path, record_testsuite_property, scenario, name, roles):
    first = observe(tmp_path, scenario, name, 0, roles)
    key = f"{scenario}/{name}"
    if roles == "release":
        record_testsuite_property(f"image_graph_golden.{key}", json.dumps(first, sort_keys=True))
    assert first["status"] == [200], f"{key}: the route did not answer 200, so the scenario proves nothing"
    assert first["graphs"], f"{key}: no graph reached the engine"
    # Two runs on fresh rigs agree: the recorded values do not depend on the run.
    assert observe(tmp_path, scenario, name, 1, roles) == first, f"{key}: two runs differ"
    golden = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}
    assert key in golden, f"{key}: not in {FIXTURE.name}; record it (see the module docstring)"
    assert first == golden[key]


def test_the_fixture_covers_every_case_and_every_shipped_image_graph():
    golden = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}
    assert set(golden) == {f"{s}/{n}" for s, n in CASES}
    graphs = {path.stem for path in (ROOT / "workflows" / "images").glob("*.json") if ".ui" not in path.name}
    assert len(graphs) == 18
    assert graphs <= set(GENERATE) | set(EDITS)
    assert graphs == set(GENERATE) | set(EDITS)


@pytest.mark.parametrize("scenario,name", [("edit", MULTIREF), *[("edit-mask", name) for name in MASKED]])
def test_multi_asset_golden_keeps_each_uploaded_asset_in_its_slot(tmp_path, scenario, name):
    actual = observe(tmp_path, scenario, name, 0, distinct_uploads=True)
    assert actual["status"] == [200]
    expected = json.loads(FIXTURE.read_text())[f"{scenario}/{name}"]
    import copy
    expected = copy.deepcopy(expected)
    for graph in expected["graphs"]:
        # The first two graphs are mask-bound siblings with separate image and mask inputs.
        # Other mask workflows combine alpha into the source before a single upload.
        mapping = ({"10": "asset-1.png", "12": "asset-2.png"} if name in ("flux2-klein-4b-inpaint", "flux2-klein-4b-edit")
                   else {"4": "asset-1.png", "11": "asset-2.png"} if name == MULTIREF
                   else {node: "asset-1.png" for node, spec in graph.items()
                         if isinstance(spec, dict) and spec.get("inputs", {}).get("image") == "uploaded.png"})
        for node, filename in mapping.items():
            assert graph[node]["inputs"]["image"] == "uploaded.png"
            graph[node]["inputs"]["image"] = filename
    assert actual == expected


def test_reload_publishes_changed_reference_order_for_the_next_request(tmp_path):
    before = observe(tmp_path, "edit", MULTIREF, 0, distinct_uploads=True)
    after = observe(tmp_path, "edit", MULTIREF, 1, distinct_uploads=True, reload_reference_order=True)
    assert before["status"] == after["status"] == [200]
    assert before["graphs"][0]["4"]["inputs"]["image"] == "asset-1.png"
    assert before["graphs"][0]["11"]["inputs"]["image"] == "asset-2.png"
    assert after["graphs"][0]["4"]["inputs"]["image"] == "asset-2.png"
    assert after["graphs"][0]["11"]["inputs"]["image"] == "asset-1.png"
