"""Workflow schemas publish declared defaults, not private audio graph defaults."""

import asyncio
import copy
import json

import pytest
import yaml

from gateway import workflow_openapi
from tests.support import c2d_parity
from tests.support.c2d_snapshots import ALIASES, VIDEO_LIMITS, _base_openapi


@pytest.mark.parametrize("name, published_default", [
    ("ace-step-1.5-xl-turbo", None),
    ("sa3-small-sfx", 10),
])
def test_duration_publication_matches_manifest(name, published_default):
    manifest = yaml.safe_load((c2d_parity.MANIFESTS / f"{name}.yml").read_text())
    records = c2d_parity.shipped_registry().records()
    spec = workflow_openapi.augment_openapi(
        _base_openapi(), records, copy.deepcopy(ALIASES), copy.deepcopy(VIDEO_LIMITS)
    )
    schema = spec["components"]["schemas"][workflow_openapi.schema_name(manifest["id"])]
    for duration in (manifest["inputs"]["duration"], schema["properties"]["duration"]):
        if published_default is None:
            assert "default" not in duration
        else:
            assert duration["default"] == published_default

    import proxy

    route = proxy.app.openapi()["paths"]["/v1/audio/generate"]["post"]
    request = route["requestBody"]["content"]["application/json"]["schema"]
    assert "default" not in request["properties"]["duration"]


@pytest.mark.parametrize("name, targets, expected, output_node", [
    ("ace-step-1.5-xl-turbo", [("5", "duration"), ("10", "seconds")], 30, "38"),
    ("sa3-small-sfx", [("5", "seconds")], 10, "8"),
])
def test_public_audio_route_submits_workflow_duration_default(tmp_path, name, targets, expected, output_node):
    from tests.test_audio_route_selectors import build, entry, history

    answer = history({output_node: {"audio": [entry("tone.wav", "output")]}})
    rig, made = build(tmp_path, answer)
    try:
        response = asyncio.run(made.call(
            "POST", "/v1/audio/generate", label="generate", json_body={"model": name, "prompt": "rain"}
        ))
        assert response.status_code == 200, response.text
        submissions = [json.loads(entry["body"]["text"])["prompt"]
                       for entry in made.recorder.engine if entry["path"] == "/prompt"]
        assert len(submissions) == 1, response.text
        for node, field in targets:
            assert submissions[0][node]["inputs"][field] == expected
    finally:
        rig.__exit__(None, None, None)
