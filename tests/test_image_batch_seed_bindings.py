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

    def run(workflow, manifest, *, fixed_seed=41, outcomes=None, name="seed-fixture",
            meta=None, n=2, seed_field="seed", status=200):
        seen = []
        results = iter(outcomes or [[b"a"], [b"b"]])

        async def submit(client, graph, *args, **kwargs):
            seen.append(copy.deepcopy(graph))
            return None, next(results)

        monkeypatch.setattr(proxy, "_graph_run_round", submit)
        response = asyncio.run(proxy._generate_images_with_adaptive_batching(
            request=SimpleNamespace(), client=object(), base_workflow=workflow,
            meta=meta or {}, manifest=manifest, workflow_name=name, width=512, height=512,
            n_requested=n, response_format="b64_json", headers={}, fixed_seed=fixed_seed,
            legacy_seed_field=seed_field,
        ))
        assert response.status_code == status
        run.response = response
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


MANIFESTLESS = ["flux-dev", "flux2-klein", "flux2-klein-edit", "ideogram4-fp8",
                "ideogram4-fp8-nocfg", "ideogram4-int8", "ideogram4-nvfp4",
                "ideogram4-nvfp4-split", "ideogram4-gguf-kijai", "ideogram4-gguf-q8"]


@pytest.mark.parametrize("name", MANIFESTLESS)
@pytest.mark.parametrize("fixed_seed", [41, None])
def test_manifestless_submission_matches_main_and_records_seeds(batch, monkeypatch, name, fixed_seed):
    graph = json.loads((ROOT / "workflows/images" / f"{name}.json").read_text())
    meta = graph.pop("_meta")
    field = "noise_seed" if name.endswith("-edit") else "seed"
    delivery = SimpleNamespace(seeds=[])
    monkeypatch.setattr(proxy.media_delivery, "current", lambda: delivery)
    draws = iter([101, 102])
    monkeypatch.setattr(proxy.random, "randint", lambda *args: next(draws))
    seeds = [41, 42] if fixed_seed is not None else [101, 102]
    expected = []
    for seed in seeds:
        submitted = copy.deepcopy(graph)
        submitted[meta["seed_node"]]["inputs"][field] = seed
        proxy.apply_batch_size(submitted, meta, 1)
        expected.append(json.dumps(submitted).encode())
    seen = batch(graph, None, meta=meta, name=name, fixed_seed=fixed_seed, seed_field=field)
    assert [json.dumps(g).encode() for g in seen] == expected
    assert delivery.seeds == seeds


@pytest.mark.parametrize("estimate", [None, 100])
def test_quality_batch_limit_applies_to_every_adaptive_round(batch, monkeypatch, estimate):
    manifest = yaml.safe_load((ROOT / "manifests/workflows/moss-image-quality.yml").read_text())
    assert manifest["inputs"]["batch_size"]["maximum"] == 3
    graph = json.loads((ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text())
    meta = graph.pop("_meta")
    monkeypatch.setattr(proxy, "get_vram_estimate", lambda *args: estimate)
    monkeypatch.setattr(proxy, "compute_max_batch", lambda *args: 100)
    seen = batch(graph, manifest, meta=meta, n=8, outcomes=[[b"a"]] * 8)
    sizes = [g[meta["batch_size_node"]]["inputs"]["batch_size"] for g in seen]
    assert sizes[0] == (1 if estimate is None else 3)
    assert max(sizes) == 3
    assert all(1 <= size <= 3 for size in sizes)


def test_missing_seed_target_returns_configuration_error_without_submitting(batch, capsys):
    manifest = manifest_for("seed")
    manifest["id"] = "image.generate.broken-seed-target"
    for _ in range(2):
        assert batch({}, manifest, name="broken-seed-target", status=503) == []
        error = json.loads(batch.response.body)["error"]
        assert error["code"] == "invalid_workflow_extension"
        assert error["message"] == "The image workflow configuration is invalid."
    assert capsys.readouterr().out.count(manifest["id"]) == 1


def test_manifest_identity_is_computed_once_for_multiround_request(batch, monkeypatch):
    import gateway.manifests as manifests
    calls = []
    original = manifests.manifest_identity

    def identity(manifest):
        calls.append(manifest)
        return original(manifest)

    monkeypatch.setattr(manifests, "manifest_identity", identity)
    monkeypatch.setattr(proxy, "manifest_identity", identity)
    batch({"a": {"inputs": {"seed": 0}}, "b": {"inputs": {"operator_seed": 0}}}, manifest_for("seed"))
    assert len(calls) == 1


def test_manifestless_empty_round_preserves_main_fixed_seed_offset(batch):
    seen = batch(
        {"s": {"inputs": {"seed": 0}}}, None,
        meta={"seed_node": "s"}, outcomes=[[], [b"a"], [b"b"]],
    )
    # Main advances fixed seeds by images collected, including zero-image rounds.
    assert [graph["s"]["inputs"]["seed"] for graph in seen] == [41, 41, 42]
