"""D1 review fix 4: one effective input spec per workflow. The manifest declares what the model can do; the deployment
(``audio.workflows.<name>``) may narrow the bounds and choose the default, never widen them. The route edge and the
compiler both read that one spec, so a direct compile and the public route agree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.engine_protocol.audio_graph import AudioLimits, InvalidAudioRequest
from gateway.engine_protocol.node_graph import InputRefused, compile_workflow
from gateway.manifests import ManifestError, load_workflow_manifest

ROOT = Path(__file__).resolve().parent.parent
# The deployed limits of yue2-song (proxy_config.yml audio.workflows.yue2-song); the manifest says 180 / 10..360.
DEPLOYED = AudioLimits(default_duration=120, min_seconds=10, max_seconds=240)


def _manifest(name: str):
    return load_workflow_manifest(ROOT / "manifests" / "workflows" / f"{name}.yml")


def _effective(manifest, limits):
    from gateway.engine_protocol.audio_graph import effective_manifest

    return effective_manifest(manifest, limits)


def _template(manifest):
    return json.loads((ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text(encoding="utf-8"))


def test_the_deployment_narrows_the_manifest_and_both_paths_agree():
    from gateway.engine_protocol.audio_graph import manifest_inputs

    manifest = _effective(_manifest("yue2-song"), DEPLOYED)
    spec = manifest["inputs"]["duration"]
    assert (spec["default"], spec["minimum"], spec["maximum"]) == (120, 10, 240)
    edge = manifest_inputs({"tags": "dream pop"}, manifest=manifest)
    assert edge["duration"] == 120
    graph = compile_workflow(manifest, "comfyui", _template(manifest), {"tags": "dream pop"})
    assert graph["5"]["inputs"]["max_duration"] == 120, "the direct compile did not fill the deployment default"
    at_max = manifest_inputs({"tags": "x", "duration": 240}, manifest=manifest)
    assert at_max["duration"] == 240
    compile_workflow(manifest, "comfyui", _template(manifest), {"tags": "x", "duration": 240})


@pytest.mark.parametrize(("limits", "why"), [
    (AudioLimits(default_duration=120, min_seconds=10, max_seconds=400), "maximum above the manifest's 360"),
    (AudioLimits(default_duration=120, min_seconds=1, max_seconds=240), "minimum below the manifest's 10"),
])
def test_a_deployment_cannot_widen_the_manifest(limits, why):
    with pytest.raises(ManifestError):
        _effective(_manifest("yue2-song"), limits)


def test_a_default_outside_the_intersection_is_refused_at_load():
    with pytest.raises(ManifestError):
        _effective(_manifest("yue2-song"), AudioLimits(default_duration=5, min_seconds=10, max_seconds=240))


@pytest.mark.parametrize("duration", [9.5, 241, 0])
def test_the_edge_and_the_compiler_refuse_the_same_duration_by_name(duration):
    from gateway.engine_protocol.audio_graph import manifest_inputs

    manifest = _effective(_manifest("yue2-song"), DEPLOYED)
    with pytest.raises(InvalidAudioRequest) as edge:
        manifest_inputs({"tags": "x", "duration": duration}, manifest=manifest)
    with pytest.raises(InputRefused) as direct:
        compile_workflow(manifest, "comfyui", _template(manifest), {"tags": "x", "duration": duration})
    assert edge.value.param == direct.value.param == "duration"


def test_a_huge_deployment_maximum_is_refused_as_widening_not_by_an_overflow():
    from gateway.manifests import apply_deployment_inputs

    with pytest.raises(ManifestError, match="above the manifest"):
        apply_deployment_inputs(_manifest("sa3-small-sfx"), {"duration": {"maximum": 10**400}})


def test_an_empty_effective_interval_is_refused_even_without_a_default():
    from gateway.manifests import apply_deployment_inputs

    manifest = _manifest("sa3-small-sfx")
    del manifest["inputs"]["duration"]["default"]
    with pytest.raises(ManifestError, match="empty"):
        apply_deployment_inputs(manifest, {"duration": {"minimum": 100, "maximum": 50}})
