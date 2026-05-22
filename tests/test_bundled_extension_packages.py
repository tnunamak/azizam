from __future__ import annotations

from pathlib import Path

from gateway.extension_packages import (
    imported_registry_records,
    validate_extension_package_source,
)


ROOT = Path(__file__).resolve().parent.parent
BUNDLED_PACKAGE = ROOT / "extensions" / "bundled" / "demo-image-package"


def test_bundled_demo_extension_package_imports_end_to_end() -> None:
    review = validate_extension_package_source(
        {"type": "local_directory", "path": str(BUNDLED_PACKAGE)}
    )

    assert review["ok"] is True
    assert review["errors"] == []
    assert review["warnings"] == []
    assert review["package"]["id"] == "test-demo-image-extension-package"
    assert review["object_counts"] == {
        "services": 1,
        "protocol_frontends": 1,
        "capabilities": 1,
        "workflows": 1,
    }
    assert len(review["package_hash"]) == 64

    records = imported_registry_records(review)

    assert set(records) == {
        "services",
        "protocol_frontends",
        "capabilities",
        "workflows",
    }
    assert "test-demo-comfy-service" in records["services"]
    assert "test-demo-image-protocol" in records["protocol_frontends"]
    assert "test-demo-image-capability" in records["capabilities"]
    assert "image.generate.test-demo-package" in records["workflows"]


def test_bundled_demo_imported_records_include_provenance_and_artifact() -> None:
    review = validate_extension_package_source(
        {"type": "local_directory", "path": str(BUNDLED_PACKAGE)}
    )

    records = imported_registry_records(review)
    workflow = records["workflows"]["image.generate.test-demo-package"]
    capability = records["capabilities"]["test-demo-image-capability"]

    assert workflow["source"] == "imported"
    assert workflow["provenance"] == {
        "package_id": "test-demo-image-extension-package",
        "package_version": "0.1.0",
        "package_hash": review["package_hash"],
    }
    assert workflow["bindings"][0]["backend"] == "comfyui"
    assert (
        workflow["bindings"][0]["workflow_artifact"]
        == "package://workflows/test-demo-image.json"
    )
    assert workflow["manifest"]["bindings"]["comfyui"]["workflow"] == (
        "package://workflows/test-demo-image.json"
    )

    assert capability["source"] == "imported"
    assert capability["workflow"] == "image.generate.test-demo-package"
    assert capability["provenance"]["package_id"] == "test-demo-image-extension-package"
