"""A stable image name pointed at an owner workflow under its own id serves that workflow's graph.

With owner overrides of bundled ids gone, an owner customizes a bundled workflow by saving it under a new id and
pointing the stable name its clients use at the new model. This proves that path end to end on the old image route,
over the golden rig's fake engine: the prompt the engine receives is the owner's graph.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import proxy
from gateway.discovery import WorkflowRegistry

ROOT = Path(__file__).resolve().parents[1]
BINDING = "comfyui"
BUNDLED_ID = "image.generate.krea2-turbo"
OWNER_MODEL = "owner-krea"
OWNER_ID = f"image.generate.{OWNER_MODEL}"
STABLE_NAME = "image"
STEPS_NODE = "7"  # the graph's sampler


def _registry_with_owner_record(graph: dict) -> WorkflowRegistry:
    registry = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
    bundled = next(r for r in registry.records() if r.identity.id == BUNDLED_ID)
    manifest = copy.deepcopy(bundled.manifest)
    manifest["id"] = OWNER_ID
    manifest["bindings"][BINDING]["workflow"] = {"inline": graph}
    registry.add_manifest(manifest, source_path=bundled.source_path, facts=bundled.facts)
    return registry


def test_a_stable_name_pointed_at_an_owner_workflow_serves_its_graph(tmp_path):
    from tests.support.s7_parity_images_facts import engine_bodies
    from tests.support.s7_parity_images_rig import observe
    from tests.test_s7_parity_images import ok

    graph = json.loads((ROOT / "workflows" / "images" / "krea2-turbo.json").read_text(encoding="utf-8"))
    graph[STEPS_NODE]["inputs"]["steps"] = 13  # the owner's edit, so the served graph shows whose it is

    def setup(rig):
        ok(rig)
        registry = _registry_with_owner_record(graph)
        rig.monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry)
        aliases = copy.deepcopy(proxy.config.get("model_aliases") or {})
        aliases.setdefault("images", {}).setdefault("stable", {})[STABLE_NAME] = OWNER_MODEL
        rig.monkeypatch.setitem(proxy.config, "model_aliases", aliases)

    obs = observe(tmp_path, "/v1/images/generations", {"json_body": {
        "model": STABLE_NAME, "prompt": "a red fox", "response_format": "b64_json",
        "background": "opaque"}}, setup, row=False)  # opaque: no background decider call

    assert obs.status == 200, obs.body
    submitted = [json.loads(body["text"])["prompt"] for body in engine_bodies(obs, "/prompt") if body.get("kind") == "json"]
    assert submitted, obs.engine
    assert submitted[0][STEPS_NODE]["inputs"]["steps"] == 13  # the owner's graph, not the bundled file
