"""Mesh workflow documents for tests: the bundled one as the registry holds it, and a second one written only as data."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from gateway.discovery import WorkflowRegistry
from gateway.mesh_api import MeshWorkflow, mesh_workflow

ROOT = Path(__file__).resolve().parents[1]
BUNDLED_ID = "mesh.generate.trellis-2"
_REGISTRY = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")


def bundled_document() -> dict[str, Any]:
    """The bundled mesh workflow document (schema 2), fresh for each caller to edit."""
    return copy.deepcopy(_REGISTRY.get(BUNDLED_ID).manifest)


def bundled_contract() -> dict[str, Any]:
    return copy.deepcopy(_REGISTRY.get(BUNDLED_ID).contract)


def workflow_of(document: dict[str, Any]) -> MeshWorkflow:
    """The route's reading of a document, through the mesh frontend profile."""
    return mesh_workflow(document, "test-hash")


# A mesh-shaped workflow that shares nothing with the bundled one: its own id, service, path, input names and form fields,
# a picture that is not called "image", and an option the bundled one does not have. It is data only.
SECOND_ID = "mesh.generate.second-engine"
SECOND_SERVICE = "mesher-two"
SECOND_PATH = "/v2/make-model"
SECOND_URL = "http://127.0.0.1:8891"


def second_document() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "min_gateway_version": "0.1",
        "version": "1.2.0",
        "id": SECOND_ID,
        "intent": "mesh.generate",
        "capability": "mesh",
        "display_name": "Second image to 3D",
        "purpose": "One photo to a 3D model",
        "description": "Turns one photo into a 3D model in GLB format.",
        "summary": "Send one photo; receive one 3D model (model/gltf-binary).",
        "inputs": {
            "photo": {"label": "photo", "type": "asset", "accepts": ["image/png", "image/jpeg", "image/webp"], "required": True},
            "detail": {"label": "detail", "type": "integer", "enum": [256, 512], "default": 256},
            "quality": {"label": "quality", "type": "string", "enum": ["fast", "best"], "default": "fast"},
            "salt": {"label": "salt", "type": "integer", "minimum": -5, "maximum": 5},
        },
        "outputs": {"model": {"media_type": "model/gltf-binary"}},
        "bindings": {
            "http-multipart": {
                "service": SECOND_SERVICE,
                "path": SECOND_PATH,
                "map": {"photo": "source_image", "detail": "detail_level", "quality": "mode", "salt": "salt_value"},
            },
        },
    }
