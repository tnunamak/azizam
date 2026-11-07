"""Every bundled graph workflow declares explicit selectors, and they read what the old collection read (S2.4, D5).

The proof does not read the selectors to build its input. For each bundled workflow it takes the graph, finds the nodes
that write result files by their class (what the engine's save nodes put in a history entry is the table below), builds
the history entry the engine would give, and compares what the old per-family collection takes from it with what the
workflow's selectors take. A second history adds what the old collection could not tell from a result: a load node's
echo of its source and a preview.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from gateway import output_selectors
from gateway.engine_protocol import description, node_graph
from gateway.engine_protocol.node_graph import NodeGraphAdapter
from gateway.result_bundle import ResultLimits

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = sorted((ROOT / "manifests" / "workflows").glob("*.yml"))
ADAPTER = NodeGraphAdapter(description.load_registry()["node-graph"])
BINDING = ADAPTER.binding
DESCRIPTORS = output_selectors.shipped_descriptors()

# The history list a node's class writes its files under: read from the engine's save nodes.
WRITES = {"SaveImage": "images", "SaveVideo": "images", "SaveAudio": "audio", "SaveAudioMP3": "audio",
          "TangoFluxVAEDecodeAndPlay": "audios"}


def load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def graph_manifests() -> list[Path]:
    return [path for path in MANIFESTS if "workflow" in (load(path)["bindings"].get(BINDING) or {})]


def template_of(manifest: dict[str, Any], binding: str = BINDING) -> dict[str, Any]:
    return json.loads((ROOT / manifest["bindings"][binding]["workflow"]).read_text(encoding="utf-8"))


def producers(template: dict[str, Any]) -> dict[str, str]:
    return {node_id: WRITES[node["class_type"]] for node_id, node in template.items()
            if isinstance(node, dict) and node.get("class_type") in WRITES}


def family(manifest: dict[str, Any]) -> str:
    (spec,) = manifest["outputs"].values()
    return spec["media_type"].split("/")[0]


SUFFIX = {"image": "png", "video": "mp4", "audio": "flac"}


def files(name: str, count: int, kind: str) -> list[dict[str, str]]:
    return [{"filename": f"{name}_{i:05d}.{SUFFIX[kind]}", "subfolder": "sub", "type": "output"} for i in range(count)]


def history_of(manifest: dict[str, Any], *, with_noise: bool, binding: str = BINDING) -> dict[str, Any]:
    template = template_of(manifest, binding)
    count = 3 if family(manifest) == "image" else 1
    outputs: dict[str, Any] = {}
    if with_noise:
        # A load node echoes its source, ahead of the result in node order; a preview sits in the temp directory.
        outputs["0"] = {"images": [{"filename": "source.png", "subfolder": "", "type": "input"}]}
        outputs["00"] = {"images": [{"filename": "peek.png", "subfolder": "", "type": "temp"}],
                         "audio": [{"filename": "peek.flac", "subfolder": "", "type": "temp"}]}
    for node_id, key in producers(template).items():
        outputs[node_id] = {key: files(f"out{node_id}", count, family(manifest)), "animated": [True]}
    return {"status": {"completed": True, "status_str": "success", "messages": []}, "outputs": outputs}


def old_collection(manifest: dict[str, Any], history: dict[str, Any]) -> list[tuple[str, str]]:
    """What the routes took before selectors: all image files, or the first video or audio file."""
    kind = family(manifest)
    if kind == "image":
        found = node_graph.output_entries(history, node_graph.IMAGE_KEYS)
    elif kind == "video":
        one = node_graph.first_video_output(history)
        found = [one] if one else []
    else:
        one = node_graph.first_audio_output(history)
        found = [one] if one else []
    return [(entry["subfolder"], entry["filename"]) for entry in found]


def through_selectors(manifest: dict[str, Any], history: dict[str, Any]) -> list[tuple[str, str]]:
    chosen = ADAPTER.collect(history, manifest["bindings"][BINDING], manifest["outputs"], limits=ResultLimits(), legacy_allowed=False)
    ((_, picked),) = chosen.items()
    return [(entry["subfolder"], entry["filename"]) for entry in picked]


def test_the_graph_workflows_are_found_and_every_one_has_one_producer():
    assert len(graph_manifests()) >= 30
    for path in graph_manifests():
        assert len(producers(template_of(load(path)))) == 1, path.name


@pytest.mark.parametrize("path", graph_manifests(), ids=lambda p: p.stem)
def test_a_bundled_graph_workflow_passes_the_save_rules(path: Path):
    manifest = load(path)
    chosen = output_selectors.check_for_save(DESCRIPTORS[BINDING], manifest, BINDING, template=template_of(manifest))
    assert set(chosen) == set(manifest["outputs"])


@pytest.mark.parametrize("path", graph_manifests(), ids=lambda p: p.stem)
def test_the_selectors_name_the_node_the_graph_writes_its_file_with(path: Path):
    manifest = load(path)
    (node_id, key), = producers(template_of(manifest)).items()
    ((_, selector),) = manifest["bindings"][BINDING]["selectors"].items()
    assert (selector["node"], selector["key"], selector["from"]) == (node_id, key, "output_dir"), path.name


@pytest.mark.parametrize("path", graph_manifests(), ids=lambda p: p.stem)
def test_the_selectors_read_what_the_old_collection_read_from_a_plain_answer(path: Path):
    manifest = load(path)
    history = history_of(manifest, with_noise=False)
    assert through_selectors(manifest, history) == old_collection(manifest, history)
    assert through_selectors(manifest, history), "the answer holds a result"


@pytest.mark.parametrize("path", graph_manifests(), ids=lambda p: p.stem)
def test_the_selectors_keep_reading_the_result_when_the_answer_also_holds_an_echo_and_a_preview(path: Path):
    manifest = load(path)
    plain = through_selectors(manifest, history_of(manifest, with_noise=False))
    noisy = history_of(manifest, with_noise=True)
    assert through_selectors(manifest, noisy) == plain
    # The legacy collection reads the same, because it also reads the output directory only (and refuses a guess).
    legacy = node_graph.legacy_select(noisy, manifest["outputs"])
    assert [(f["subfolder"], f["filename"]) for f in next(iter(legacy.values()))] == plain


def test_the_old_collection_could_not_tell_an_echo_from_a_result():
    """The reason for selectors: with a source echoed ahead of the result, the old image collection returned both."""
    path = next(p for p in graph_manifests() if family(load(p)) == "image")
    manifest = load(path)
    noisy = history_of(manifest, with_noise=True)
    assert len(old_collection(manifest, noisy)) > len(through_selectors(manifest, noisy))


def test_a_bundled_binding_of_an_adapter_with_no_selector_form_declares_none():
    for path in MANIFESTS:
        for key, block in load(path)["bindings"].items():
            if output_selectors.selector_form(key, block) not in DESCRIPTORS:
                assert "selectors" not in block, (path.name, key)


def test_every_bundled_manifest_loads_through_validate_manifest():
    from gateway.manifests import load_workflow_manifest

    for path in MANIFESTS:
        load_workflow_manifest(path)


# ------------------------------------------------------------------------------- the four that had no selectors (S2 close-out)
#
# Of the four bundled workflows that were left without selectors, only the identity scene reads an engine's answer for
# a result: its route runs one graph and takes a file from it. Its binding is the route's own (``gateway``), so it
# names the graph adapter's selector form with ``selectors_for``. The other three never read a graph's history: the
# likeness check answers from the face service, the seamless tile is made by the gateway from two other workflows (each
# of which has selectors), and the 3D model is the body of one HTTP reply. Their adapters take no selectors, and a save
# refuses selectors on a binding whose adapter takes none.

IDENTITY = ROOT / "manifests" / "workflows" / "image-identity.yml"
NO_ENGINE_HISTORY = {"face-likeness", "flux2-klein-4b-seamless-tile", "mesh.generate.trellis-2"}


def test_the_identity_scene_selector_names_the_node_its_graph_saves_with_and_passes_the_save_rules():
    manifest = load(IDENTITY)
    block = manifest["bindings"]["gateway"]
    template = template_of(manifest, "gateway")
    ((node_id, key),) = producers(template).items()
    assert block["selectors"] == {"image": {"node": node_id, "key": key, "from": "output_dir", "decode": "none", "take": "all"}}
    descriptor = DESCRIPTORS[output_selectors.selector_form("gateway", block)]
    assert set(output_selectors.check_for_save(descriptor, manifest, "gateway", template=template)) == {"image"}
    output_selectors.check_manifest(manifest)


def test_the_identity_scene_selector_reads_what_the_old_collection_read():
    manifest = load(IDENTITY)
    block = manifest["bindings"]["gateway"]
    plain = history_of(manifest, with_noise=False, binding="gateway")
    chosen = ADAPTER.collect(plain, block, manifest["outputs"], limits=ResultLimits(), legacy_allowed=False)
    picked = [(f["subfolder"], f["filename"]) for f in chosen["image"]]
    assert picked and len(picked) == 3
    assert picked == old_collection(manifest, plain)
    legacy = node_graph.legacy_select(plain, manifest["outputs"])
    assert picked == [(f["subfolder"], f["filename"]) for f in legacy["image"]]
    # An echo and a preview ahead of the result change nothing for the selector.
    noisy = history_of(manifest, with_noise=True, binding="gateway")
    again = ADAPTER.collect(noisy, block, manifest["outputs"], limits=ResultLimits(), legacy_allowed=False)
    assert [(f["subfolder"], f["filename"]) for f in again["image"]] == picked


def test_only_the_workflows_that_never_read_an_engine_answer_declare_no_selectors():
    bare = {path.stem for path in MANIFESTS
            if not any("selectors" in block for block in load(path)["bindings"].values())}
    assert bare == NO_ENGINE_HISTORY


@pytest.mark.parametrize("name", sorted(NO_ENGINE_HISTORY))
def test_a_workflow_with_no_engine_history_has_no_graph_binding_and_its_adapter_takes_no_selectors(name: str):
    manifest = load(MANIFESTS[0].parent / f"{name}.yml")
    for key, block in manifest["bindings"].items():
        assert key not in DESCRIPTORS and "workflow" not in block, (name, key)
        assert output_selectors.check_for_save(None, manifest, key) == {}
        declared = {**manifest, "bindings": {key: {**block, "selectors": {"x": {"node": "1"}}}}}
        with pytest.raises(output_selectors.SelectorError) as refused:
            output_selectors.check_for_save(None, declared, key)
        assert refused.value.code == "selector_malformed"


def test_the_seamless_tile_is_made_from_two_workflows_that_have_selectors():
    manifest = load(MANIFESTS[0].parent / "flux2-klein-4b-seamless-tile.yml")
    for needed in manifest["requires"]["workflows"]:
        stem = needed.rsplit(".", 1)[1]
        assert "selectors" in load(MANIFESTS[0].parent / f"{stem}.yml")["bindings"][BINDING], needed
