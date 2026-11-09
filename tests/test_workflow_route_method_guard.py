"""A workflow descriptor needs a registered method/path pair, not just a path."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway import workflow_openapi as workflow


ROWS = Path(__file__).resolve().parents[1] / "registry" / "workflow_execution_routes.yml"


def copy_rows(tmp_path):
    data = yaml.safe_load(ROWS.read_text(encoding="utf-8"))
    row = next(row for row in data["rows"] if row["id"] == "generate-image")
    return data, row


def write_rows(tmp_path, data):
    path = tmp_path / "test-workflow-execution-routes.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def record():
    return SimpleNamespace(
        id="image.generate.demo", intent="image.generate",
        contract={"inputs": {}, "bindings": {}, "typical_seconds": 31},
    )


def test_known_path_with_unregistered_method_is_refused(tmp_path):
    data, row = copy_rows(tmp_path)
    assert row["sync"]["path"] == "/v1/images/generations"
    assert row["sync"]["method"] == "POST"
    assert "discovery_only" not in row["sync"]
    # Only the method changes; DELETE exists elsewhere but not at this path.
    row["sync"]["method"] = "DELETE"
    path = write_rows(tmp_path, data)
    with pytest.raises(ValueError) as error:
        workflow.load_execution_routes(path)
    assert str(path) in str(error.value)
    assert "generate-image.sync" in str(error.value)
    assert "DELETE" in str(error.value)
    assert "/v1/images/generations" in str(error.value)


def test_shipped_post_pair_loads_unchanged(tmp_path, monkeypatch):
    data, _ = copy_rows(tmp_path)
    loaded = workflow.load_execution_routes(write_rows(tmp_path, data))
    assert loaded == workflow._EXECUTION_ROUTES
    monkeypatch.setattr(workflow, "_EXECUTION_ROUTES", loaded)
    row, = workflow.workflow_catalog([record()])
    assert (row["execution"]["sync"]["method"], row["execution"]["sync"]["path"]) == (
        "POST", "/v1/images/generations",
    )


def test_registered_alias_keeps_its_method(tmp_path, monkeypatch):
    data, row = copy_rows(tmp_path)
    row["sync"]["path"] = "/openai/v1/images/generations"
    loaded = workflow.load_execution_routes(write_rows(tmp_path, data))
    monkeypatch.setattr(workflow, "_EXECUTION_ROUTES", loaded)
    row, = workflow.workflow_catalog([record()])
    assert (row["execution"]["sync"]["method"], row["execution"]["sync"]["path"]) == (
        "POST", "/openai/v1/images/generations",
    )


def test_discovery_only_unregistered_pair_stays_out_of_public_operations(tmp_path, monkeypatch):
    data, row = copy_rows(tmp_path)
    row["sync"]["method"] = "DELETE"
    row["sync"]["discovery_only"] = True
    loaded = workflow.load_execution_routes(write_rows(tmp_path, data))
    assert loaded["exact"]["image.generate"]["sync"]["discovery_only"] is True
    monkeypatch.setattr(workflow, "_EXECUTION_ROUTES", loaded)
    # Catalog-only metadata must not register the absent HTTP operation.
    spec = {"paths": {"/v1/images/generations": {"post": {"summary": "Registered"}}}}
    result = workflow.augment_openapi(spec, [record()])
    assert set(result["paths"]) == {"/v1/images/generations"}
    assert set(result["paths"]["/v1/images/generations"]) == {"post"}
    row, = result["x-azizam-workflows"]
    assert (row["execution"]["sync"]["method"], row["execution"]["sync"]["path"]) == (
        "DELETE", "/v1/images/generations",
    )
