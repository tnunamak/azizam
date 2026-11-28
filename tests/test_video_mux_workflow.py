"""video.edit.video-mux: put an audio track onto a video, with core ComfyUI nodes only."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

import proxy
from gateway.capabilities import Capability
from gateway.manifests import BUNDLED_PROVENANCE, load_workflow_manifest, manifest_identity
from gateway.workflows_catalog import build_workflows_catalog

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifests/workflows/video-mux.yml"
GRAPH = ROOT / "workflows/videos/video-mux.json"
WORKFLOW_ID = "video.edit.video-mux"
CORE_NODES = {"LoadVideo", "GetVideoComponents", "LoadAudio", "CreateVideo", "SaveVideo"}


def _graph() -> dict:
    return json.loads(GRAPH.read_text(encoding="utf-8"))


def _nodes() -> dict:
    return {key: node for key, node in _graph().items() if key != "_meta"}


def test_manifest_validates_and_names_the_workflow() -> None:
    manifest = load_workflow_manifest(MANIFEST)
    identity = manifest_identity(manifest, BUNDLED_PROVENANCE)
    assert identity.id == WORKFLOW_ID
    assert manifest["intent"] == "video.edit"
    assert manifest["inputs"]["source_video"]["accepts"] == ["video/*"]
    assert manifest["inputs"]["source_audio"]["accepts"] == ["audio/*"]
    assert all(spec["type"] == "asset" and spec["required"] for spec in manifest["inputs"].values())


def test_purpose_is_unique_among_bundled_manifests() -> None:
    purposes = [yaml.safe_load(path.read_text(encoding="utf-8")).get("purpose") for path in sorted((ROOT / "manifests/workflows").glob("*.yml"))]
    assert purposes.count("Put an audio track onto a video") == 1


def test_graph_uses_only_the_core_node_types() -> None:
    assert {node["class_type"] for node in _nodes().values()} == CORE_NODES


def test_inputs_bind_to_the_load_nodes() -> None:
    manifest = load_workflow_manifest(MANIFEST)
    nodes = _nodes()
    mapping = manifest["bindings"]["comfyui"]["map"]
    for name, class_type in (("source_video", "LoadVideo"), ("source_audio", "LoadAudio")):
        (target,) = mapping[name]
        assert nodes[target["node"]]["class_type"] == class_type
        assert target["input"] in nodes[target["node"]]["inputs"]


def test_the_new_track_and_the_original_frame_rate_feed_create_video() -> None:
    manifest = load_workflow_manifest(MANIFEST)
    mapping = manifest["bindings"]["comfyui"]["map"]
    (source_audio,) = mapping["source_audio"]
    (source_video,) = mapping["source_video"]
    assert source_audio["input"] == "audio"
    assert source_video["input"] == "file"
    nodes = _nodes()
    (create,) = [node for node in nodes.values() if node["class_type"] == "CreateVideo"]
    assert nodes[create["inputs"]["audio"][0]]["class_type"] == "LoadAudio"
    assert create["inputs"]["audio"] == [source_audio["node"], 0]
    components = nodes[create["inputs"]["images"][0]]
    assert components["class_type"] == "GetVideoComponents"
    components_id = create["inputs"]["images"][0]
    assert components["inputs"]["video"] == [source_video["node"], 0]
    assert create["inputs"]["images"] == [components_id, 0]
    assert create["inputs"]["fps"] == [create["inputs"]["images"][0], 2]


def test_member_discovery_lists_it_for_a_plan_that_allows_the_category() -> None:
    records = proxy.GATEWAY_WORKFLOW_REGISTRY.records()
    capabilities = [Capability(intent="video.edit", backend="comfyui", workflow=WORKFLOW_ID, policy_available=True)]
    payload = build_workflows_catalog(records=records, capabilities=capabilities)
    assert [item["id"] for item in payload["workflows"]] == [WORKFLOW_ID]
    capabilities = [Capability(intent="video.edit", backend="comfyui", workflow=WORKFLOW_ID, policy_available=False)]
    assert build_workflows_catalog(records=records, capabilities=capabilities)["workflows"] == []


def test_the_mcp_run_tool_exposes_both_inputs_as_asset_inputs() -> None:
    (entry,) = [entry for entry in proxy._mcp_catalog() if entry["id"] == WORKFLOW_ID]
    properties = entry["inputs"]["properties"]
    assert set(properties) == {"source_video", "source_audio"}
    assert set(entry["inputs"]["required"]) == {"source_video", "source_audio"}
    assert all(properties[name]["x-azizam-asset"] is True for name in properties)


def test_the_source_video_sound_reaches_nothing() -> None:
    # "The new sound replaces the old": GetVideoComponents output 1 (the video's own audio) feeds no node.
    nodes = _nodes()
    (components_id,) = [key for key, node in nodes.items() if node["class_type"] == "GetVideoComponents"]
    links = [value for node in nodes.values() for value in node["inputs"].values() if isinstance(value, list) and len(value) == 2]
    assert [components_id, 1] not in links


def test_the_description_states_the_length_rule_measured_on_live() -> None:
    # Measured 2026-11-09 with ffprobe on live runs: a 10 s video with a 20 s, a 5 s and a 22.05 kHz mono sound.
    description = load_workflow_manifest(MANIFEST)["description"]
    assert "as long as the video" in description
    assert "cut off when the picture ends" in description
    assert "keeps its own sample rate and channels" in description
