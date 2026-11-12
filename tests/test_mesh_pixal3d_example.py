"""The Pixal3D owner-override example (docs/examples/mesh-pixal3d): data only, served under the bundled mesh id."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import proxy
from gateway import mesh_api
from gateway.manifests import enforce_contract
from tests.mesh_workflows import BUNDLED_ID, ROOT, bundled_document, workflow_of

EXAMPLES = ROOT / "docs" / "examples" / "mesh-pixal3d"


def example_document() -> dict:
    return yaml.safe_load((EXAMPLES / "mesh.generate.pixal3d.example.yml").read_text(encoding="utf-8"))


def test_the_example_passes_the_contract_and_the_mesh_profile() -> None:
    document = example_document()
    enforce_contract(document)
    assert mesh_api.mesh_profile_errors(document) == []
    workflow = workflow_of(document)
    assert (workflow.image_input, workflow.service_id, workflow.transport_path) == ("image", "pixal3d", "/generate")


def test_the_example_keeps_the_bundled_id_and_differs_from_it_only_in_the_engine() -> None:
    document, bundled = example_document(), bundled_document()
    assert document["id"] == BUNDLED_ID
    assert document["bindings"]["http-multipart"]["service"] == "pixal3d"
    assert set(document["inputs"]) <= set(bundled["inputs"])  # no input the route's frontend lacks
    assert document["inputs"]["resolution"] == bundled["inputs"]["resolution"]


def test_the_live_reader_serves_the_example_when_it_is_registered_under_the_bundled_id(monkeypatch: pytest.MonkeyPatch) -> None:
    document = example_document()
    record = SimpleNamespace(contract=document, identity=SimpleNamespace(manifest_hash="h"))
    registry = proxy.GATEWAY_WORKFLOW_REGISTRY
    original = registry.get
    monkeypatch.setattr(registry, "get", lambda key, *a, **k: record if key == BUNDLED_ID else original(key, *a, **k))
    workflow = proxy.current_mesh_workflow()
    assert (workflow.image_input, workflow.service_id) == ("image", "pixal3d")


def test_the_service_record_example_names_the_service_the_document_binds_to() -> None:
    records = yaml.safe_load((EXAMPLES / "service-record.example.yml").read_text(encoding="utf-8"))
    assert list(records) == ["pixal3d"]
    assert records["pixal3d"]["capabilities"] == ["mesh"]
    assert records["pixal3d"]["resource_capacity"]["mode"] == "exclusive-route-pool"


def test_the_examples_hold_no_local_paths_or_addresses() -> None:
    for path in [*EXAMPLES.iterdir(), Path(ROOT / "engines" / "pixal3d_comfy_service.py")]:
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text and "192.168." not in text, path.name
