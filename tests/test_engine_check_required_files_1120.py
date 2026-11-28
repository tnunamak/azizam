"""Owner engine check: distinguish a known missing dependency from no inventory."""

import asyncio
import json
from urllib.parse import urlsplit

import pytest

from gateway.engine_check import Answer, KeyInput, run_check
from gateway.engine_definitions import load_engine_catalog
from gateway.engine_protocol import description


def check(*, has_model=True, has_node=True, reachable=True):
    catalog = load_engine_catalog()
    definition = catalog.definition("workflow-image-engine")
    binding = description.load_registry().get(definition["record"]["protocol"]).binding
    assert binding
    manifest = {
        "id": "fixture-workflow",
        "display_name": "Fixture workflow",
        "bindings": {binding: {"requires": {
            "custom_nodes": ["RequiredFixtureNode"],
            "models": [{"role": "checkpoint", "path": "Fixture.safetensors"}],
        }}},
    }
    inventory = {
        "AlwaysPresentLoader": {"input": {"required": {
            "model": [["Fixture.safetensors"] if has_model else ["Other.safetensors"], {}],
        }}},
    }
    if has_node:
        inventory["RequiredFixtureNode"] = {"input": {}}

    async def fetch(method, url, **kwargs):
        path = urlsplit(url).path
        if not reachable:
            assert path != "/object_info", "do not read inventory when the engine has not answered"
            return Answer(method, url, transport="refused")
        if path == "/system_stats":
            return Answer(method, url, status=200, body=b'{"system": {}}')
        if path == "/object_info":
            return Answer(method, url, status=200, body=json.dumps(inventory).encode())
        assert path == "/zz-not-a-route-fixture123"
        return Answer(method, url, status=404, body=b'{"error": "not found"}')

    return asyncio.run(run_check(
        catalog, definition, {"address": "http://127.0.0.1:8188"},
        key=KeyInput(), fetch=fetch, baseline_token="fixture123", workflows=[manifest],
    ))


NODE = {
    "kind": "custom_node",
    "name": "RequiredFixtureNode",
    "reason": "custom node class is not present in backend inventory",
}
MODEL = {
    "kind": "model",
    "name": "checkpoint:Fixture.safetensors",
    "reason": "the engine's node list offers no model file Fixture.safetensors",
}
ROW = {"id": "fixture-workflow", "name": "Fixture workflow"}


@pytest.mark.parametrize("has_model,has_node,missing", [
    (True, True, []),
    (False, True, [MODEL]),
    (True, False, [NODE]),
    (False, False, [NODE, MODEL]),
])
def test_required_files_report_what_the_reachable_engine_is_missing(has_model, has_node, missing):
    result = check(has_model=has_model, has_node=has_node)
    assert result["reachable"] is True
    assert result["workflows"]["offered"] == ([] if missing else [ROW])
    assert result["workflows"]["unavailable"] == ([{**ROW, "missing": missing}] if missing else [])


def test_unreachable_engine_returns_a_remedy_without_a_missing_files_claim():
    result = check(reachable=False)
    assert result["reachable"] is False
    assert result["outcome"] == "error"
    assert "workflows" not in result
    assert any(finding.get("fix") for finding in result["findings"])
    claims = json.dumps(result["findings"])
    for named in ("Fixture.safetensors", "RequiredFixtureNode", "missing"):
        assert named not in claims
