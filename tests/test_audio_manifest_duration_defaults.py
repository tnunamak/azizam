"""Duration policy lives in each workflow document, not route fallback code."""
import math
from pathlib import Path

import pytest
import yaml

from gateway.manifests import load_workflow_manifest


WORKFLOWS = Path(__file__).resolve().parents[1] / "manifests" / "workflows"


def test_all_workflow_duration_defaults_are_declared_and_bounded():
    paths = sorted(WORKFLOWS.glob("*.yml"))
    assert paths, "workflow manifest directory must not be empty"
    duration_paths = []
    for path in paths:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(manifest, dict), path.name
        inputs = manifest.get("inputs", {})
        assert isinstance(inputs, dict), path.name
        if "duration" not in inputs:
            continue
        duration_paths.append(path)
        spec = inputs["duration"]
        assert isinstance(spec, dict), path.name
        for key in ("minimum", "default", "maximum"):
            assert key in spec, f"{path.name}: duration.{key} must be declared"
            assert type(spec[key]) in (int, float), f"{path.name}: duration.{key} must be numeric"
            assert math.isfinite(spec[key]), f"{path.name}: duration.{key} must be finite"
        assert spec["minimum"] <= spec["default"] <= spec["maximum"], path.name
    assert duration_paths, "must exercise at least one duration input"


@pytest.mark.parametrize("filename", ["ace-step-1.5-xl-turbo.yml", "tangoflux.yml"])
def test_edited_audio_manifest_loader_preserves_duration_default(filename):
    manifest = load_workflow_manifest(WORKFLOWS / filename)
    duration = manifest["inputs"]["duration"]
    assert duration["default"] == 30
    assert duration["minimum"] <= duration["default"] <= duration["maximum"]
