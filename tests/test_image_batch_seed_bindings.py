"""Image reseeding follows operator bindings; all submissions stay offline."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import proxy

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def batch(monkeypatch):
    monkeypatch.setattr(proxy, "get_vram_estimate", lambda *args: 100)
    monkeypatch.setattr(proxy, "graph_free_vram_bytes", AsyncMock(return_value=10**12))
    monkeypatch.setattr(proxy, "compute_max_batch", lambda *args: 1)
    monkeypatch.setattr(proxy.media_delivery, "current", lambda: None)
    monkeypatch.setattr(proxy, "_build_image_response_payload", lambda *args, **kwargs: proxy.Response(status_code=200))

    def run(workflow, manifest, *, fixed_seed=41, outcomes=None, name="seed-fixture"):
        seen = []
        results = iter(outcomes or [[b"a"], [b"b"]])

        async def submit(client, graph, *args, **kwargs):
            seen.append(copy.deepcopy(graph))
            return None, next(results)

        monkeypatch.setattr(proxy, "_graph_run_round", submit)
        response = asyncio.run(proxy._generate_images_with_adaptive_batching(
            request=SimpleNamespace(), client=object(), base_workflow=workflow,
            meta={}, manifest=manifest, workflow_name=name, width=512, height=512,
            n_requested=2, response_format="b64_json", headers={}, fixed_seed=fixed_seed,
        ))
        assert response.status_code == 200
        return seen

    return run


def manifest_for(field):
    return {"schema_version": 1, "id": "image.generate.seed-fixture", "intent": "image.generate",
            "inputs": {"seed": {"type": "integer"}}, "outputs": {},
            "bindings": {"comfyui": {"map": {"seed": [
                {"node": "a", "input": field}, {"node": "b", "input": "operator_seed"},
            ]}}}}


@pytest.mark.parametrize("field", ["noise_seed", "seed"])
@pytest.mark.parametrize("fixed_seed", [41, None])
@pytest.mark.parametrize("retry", [False, True])
def test_every_seed_binding_changes_for_batch_items_and_empty_retry_rounds(batch, monkeypatch, field, fixed_seed, retry):
    draws = iter([101, 102, 103])
    monkeypatch.setattr(proxy.random, "randint", lambda *args: next(draws))
    workflow = {"a": {"inputs": {field: 0}}, "b": {"inputs": {"operator_seed": 0}}}
    before = copy.deepcopy(workflow)
    outcomes = [[], [b"a"], [b"b"]] if retry else [[b"a"], [b"b"]]
    seen = batch(workflow, manifest_for(field), fixed_seed=fixed_seed, outcomes=outcomes)
    start = 41 if fixed_seed is not None else 101
    expected = list(range(start, start + len(outcomes)))
    assert [g["a"]["inputs"][field] for g in seen] == expected
    assert [g["b"]["inputs"]["operator_seed"] for g in seen] == expected
    assert all(set(g["a"]["inputs"]) == {field} for g in seen)
    assert workflow == before


@pytest.mark.parametrize("missing", [None, [], "absent"])
def test_no_seed_binding_keeps_graph_and_logs_once_per_workflow(batch, capsys, missing):
    manifest = manifest_for("seed")
    if missing is None:
        manifest = None
    elif missing == "absent":
        del manifest["bindings"]["comfyui"]["map"]["seed"]
    else:
        manifest["bindings"]["comfyui"]["map"]["seed"] = []
    workflow = {"a": {"inputs": {"seed": 17}}}
    for _ in range(2):
        assert batch(workflow, manifest, name=f"unbound-{missing}") == [workflow, workflow]
    output = capsys.readouterr().out
    assert output.count("reseeding unavailable") == 1
    assert f"unbound-{missing}" in output


def legacy_manifests():
    for path in sorted((ROOT / "manifests/workflows").glob("*.yml")):
        manifest = yaml.safe_load(path.read_text())
        binding = manifest["bindings"].get("comfyui", {})
        if manifest["intent"] in {"image.generate", "image.edit"} and binding.get("workflow") and not path.stem.startswith("moss-image"):
            yield pytest.param(manifest, id=path.stem)


@pytest.mark.parametrize("manifest", list(legacy_manifests()))
def test_legacy_submitted_graph_bytes_unchanged(batch, manifest):
    workflow = json.loads((ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text())
    meta = workflow.pop("_meta", {})
    expected = []
    for seed in (41, 42):
        graph = copy.deepcopy(workflow)
        node = meta.get("seed_node")
        if node:
            # Frozen pre-fix route behavior: generation wrote seed; edit detected noise_seed.
            inputs = graph[node]["inputs"]
            field = "noise_seed" if manifest["intent"] == "image.edit" and "noise_seed" in inputs else "seed"
            inputs[field] = seed
        proxy.apply_batch_size(graph, meta, 1)
        expected.append(json.dumps(graph).encode())
    submitted = batch(workflow, manifest)
    assert [json.dumps(graph).encode() for graph in submitted] == expected
