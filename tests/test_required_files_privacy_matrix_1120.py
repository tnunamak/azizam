"""Positive controls: missing labels are useful without revealing engine location."""
import json

import pytest

from test_required_files_availability import REQUIRES, availability, entry_state, kept, node_list, record

ENGINE_ID = "fixture-owner-only-service"
ENGINE_ADDRESS = "http://10.9.8.7:8188"


@pytest.mark.parametrize(
    ("audience", "shape"),
    [
        ("public", "model"),
        ("member", "model"),
        ("owner", "model"),
        ("owner", "workflow"),
    ],
)
def test_missing_requirements_are_portable_labels(audience, shape):
    workflow = record(REQUIRES, backend=ENGINE_ID)
    inventory = {
        ENGINE_ID: kept(node_list(unet_name=["model-q8.gguf"]), url=ENGINE_ADDRESS)
    }
    if shape == "model":
        # The model-entry function is shared; it does not take an audience.
        projection = entry_state(workflow, inventories=inventory, engine=ENGINE_ID)
        assert projection["state"] == "unavailable", audience
        assert projection["reason"] == "required_file_missing"
    else:
        # The workflow availability row is the owner shape.
        services = {"services": {ENGINE_ID: {"enabled": True, "config": {}}}}
        evidence = {ENGINE_ID: {"status": "ok", "available": True}}
        projection = availability.workflow_availability(
            workflow, service_records=services, evidence=evidence, inventories=inventory
        )
        assert projection["available"] is False
        assert projection["reason_code"] == "required_file_missing"

    assert projection["missing"] == [{"kind": "model", "name": "vae:vae.safetensors"}]
    for item in projection["missing"]:
        assert "/" not in item["name"] and chr(92) not in item["name"]
    serialized = json.dumps(projection)
    for internal in ("10.9.8.7", "8188", ENGINE_ID):
        assert internal not in serialized
