"""Frozen extension results, source attachment and manifest validation before code-to-data moves."""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import asdict
from pathlib import Path

import yaml

from gateway import manifests
from gateway.frontend_shapes import shapes
from gateway.video_extensions import bind_video_workflow_extensions
from gateway.video_source import VideoSource
from gateway.workflow_runtime_types import RunRequest
from tests.test_video_create_shape import read, snap

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/c2d_parity/xc_shipped.json"


def capture(monkeypatch):
    found = {}
    class Hooks(shapes.VideoHooks):
        async def source(self, *args):
            return VideoSource(wire_id="video_mj_source", asset_id="asset-source", model="source-model",
                               resolved_model="source-model", fps=24.0, size="960x512", seconds=4.0, has_audio=False)
    monkeypatch.setattr(shapes, "DEFAULT_WORKFLOWS", {})
    monkeypatch.setattr(shapes, "VIDEO_HOOKS", Hooks())
    for path in sorted((ROOT / "manifests").rglob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        try:
            loaded = manifests.load_workflow_manifest(path)
            validation = {"equal": loaded == doc}
        except manifests.ManifestError as exc:
            validation = {"error": str(exc), "errors": exc.as_list() if hasattr(exc, "as_list") else []}
        if "inputs" not in doc or "bindings" not in doc:
            found[str(path.relative_to(ROOT))] = {"validation": validation}
            continue
        bindings = doc.get("bindings") or {}
        graph = {}
        values = {}
        for name, spec in doc.get("inputs", {}).items():
            values[name] = spec.get("default", spec.get("enum", [None])[0] if spec.get("enum") else {
                "string": "sample", "asset": "asset:sample", "integer": 1, "number": 1.0,
                "boolean": False, "array": [], "object": {},
            }.get(spec.get("type")))
        for block in bindings.values():
            for mappings in (block.get("map") or {}).values():
                for mapping in mappings:
                    if "node" in mapping:
                        graph.setdefault(str(mapping["node"]), {"inputs": {}})["inputs"][mapping["input"]] = "original"
                    elif "path" in mapping:
                        target = graph
                        parts = mapping["path"].split(".")
                        for part in parts[:-1]:
                            target = target.setdefault(part, {})
                        target[parts[-1]] = "original"
        try:
            result = bind_video_workflow_extensions({"inputs": {**values, "undeclared_probe": 1}}, doc, graph)
            extension = asdict(result)
        except manifests.ManifestError as exc:
            extension = {"error": str(exc)}
        source = None
        if (doc.get("video") or {}).get("roles", {}).get("source"):
            run = read({"model": doc["id"], "prompt": "sample", "video": {"id": "video_mj_source"}},
                       snap(doc), row="videos-edit")
            if isinstance(run, RunRequest):
                source = {"inputs": dict(run.inputs), "media": {key: [asdict(ref) for ref in refs] for key, refs in run.media.items()},
                          "facts": dict(run.facts)}
            else:
                source = {"status": run.status_code, "body": json.loads(run.body)}
        found[str(path.relative_to(ROOT))] = {"validation": validation, "extension": extension, "source": source}
    return found


def test_shipped_outputs(monkeypatch):
    actual = capture(monkeypatch)
    assert json.loads(json.dumps(actual, default=str)) == json.loads(GOLDEN.read_text())


def source_attachment(monkeypatch):
    """Freeze the attachment reader after workflow selection, independent of route eligibility."""
    from gateway.frontend_shapes import _base
    from tests.test_frontend_shapes import json_request, FakeSpool
    from tests.test_video_create_shape import ROWS
    from gateway.frontend_shapes import build_shape

    found = {}
    class Hooks(shapes.VideoHooks):
        async def source(self, *args):
            return VideoSource(wire_id="video_mj_source", asset_id="asset-source", model="source-model",
                               resolved_model="source-model", fps=24.0, size="960x512", seconds=4.0, has_audio=False)
    monkeypatch.setattr(shapes, "VIDEO_HOOKS", Hooks())
    for path in sorted((ROOT / "manifests/workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        if not (doc.get("video") or {}).get("roles", {}).get("source"):
            continue
        async def selected(*args, **kwargs):
            return RunRequest(doc["id"], {"prompt": "sample", "video": {"id": "video_mj_source"}})
        monkeypatch.setattr(_base.Shape, "to_run_request", selected)
        row = ROWS["videos-edit"]
        result = asyncio.run(build_shape(row).to_run_request(
            json_request({}), row, snap(doc), spool=FakeSpool()))
        assert isinstance(result, RunRequest), getattr(result, "body", result)
        found[doc["id"]] = {"inputs": dict(result.inputs), "media": {
            name: [asdict(ref) for ref in refs] for name, refs in result.media.items()}, "facts": dict(result.facts)}
    return found


def test_shipped_source_attachment(monkeypatch, record_property):
    actual = source_attachment(monkeypatch)
    record_property("source_golden", json.dumps(actual, sort_keys=True))
    path = GOLDEN.with_name("xc_source.json")
    if path.exists():
        assert actual == json.loads(path.read_text())
