"""D1 review fix 2: a number or integer input is finite. The check is in the shared validator, so every route that
validates through ``gateway.manifests`` refuses NaN and infinities, and a manifest cannot declare a non-finite default."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from gateway.manifests import ManifestError, load_workflow_manifest, validate_input_value, validate_manifest

ROOT = Path(__file__).resolve().parent.parent
NON_FINITE = [float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("kind", [["image"], {"type": "image"}], ids=["list", "dict"])
def test_non_string_input_types_are_refused(kind):
    assert validate_input_value("photo", "placeholder", {"type": kind}) == f"photo has unsupported type: {kind}"


@pytest.mark.parametrize("value", NON_FINITE)
@pytest.mark.parametrize("spec", [
    {"type": "number"},
    {"type": "number", "minimum": 0, "maximum": 10},
    {"type": "integer", "minimum": 0},
], ids=["number", "number-bounded", "integer-bounded"])
def test_non_finite_supplied_values_are_refused(value, spec):
    assert validate_input_value("amount", value, spec) is not None


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_non_finite_default_is_refused_at_load(value):
    manifest = copy.deepcopy(load_workflow_manifest(ROOT / "manifests" / "workflows" / "tangoflux.yml"))
    manifest["inputs"]["guidance"]["default"] = value
    with pytest.raises(ManifestError, match="guidance"):
        validate_manifest(manifest)


def test_a_finite_default_still_loads():
    manifest = copy.deepcopy(load_workflow_manifest(ROOT / "manifests" / "workflows" / "tangoflux.yml"))
    manifest["inputs"]["guidance"]["default"] = 4.0
    validate_manifest(manifest)


def test_compile_refuses_nan_by_name():
    from gateway.engine_protocol.node_graph import InputRefused, compile_workflow

    manifest = load_workflow_manifest(ROOT / "manifests" / "workflows" / "tangoflux.yml")
    template = __import__("json").loads((ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text(encoding="utf-8"))
    with pytest.raises(InputRefused) as refused:
        compile_workflow(manifest, "comfyui", template, {"prompt": "x", "guidance": float("nan")})
    assert refused.value.param == "guidance"


def test_a_huge_integer_is_refused_by_its_range_not_by_an_overflow():
    # math.isfinite(10**400) raises OverflowError; the validator must refuse by the bound instead.
    assert "<=" in validate_input_value("duration", 10**400, {"type": "number", "maximum": 120})
    assert "<=" in validate_input_value("steps", 10**400, {"type": "integer", "maximum": 50})
    assert validate_input_value("duration", 10**400, {"type": "number"}) is None


def test_compile_refuses_a_huge_integer_by_name():
    from gateway.engine_protocol.node_graph import InputRefused, compile_workflow

    manifest = load_workflow_manifest(ROOT / "manifests" / "workflows" / "sa3-small-sfx.yml")
    template = __import__("json").loads((ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text(encoding="utf-8"))
    with pytest.raises(InputRefused) as refused:
        compile_workflow(manifest, "comfyui", template, {"prompt": "x", "duration": 10**400})
    assert refused.value.param == "duration"
