"""The 3D wire's constants and the file part the multipart sender carries.

The workflow is data, and the registry validates it like any other (``manifests.enforce_contract``). The 3D frontend profile
(``frontend_shapes.mesh.mesh_profile``) says which valid workflows the 3D rows can carry. The form is built by the multipart
adapter from the job's pinned binding; the file is read and measured by the media resolver before the job exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# The root module still compares intents with this name to choose the mesh protocol and job content; workflow readers use the contract intent.
MESH_INTENT = "mesh.generate"
MESH_MEDIA_TYPE = "model/gltf-binary"


class MeshManifestError(ValueError):
    """The workflow cannot be served by the 3D rows: the frontend profile refused it."""


def bundled_manifest_path(workflows_dir: Path) -> Path:
    """The one bundled workflow file whose intent is the 3D intent; none or several is an error."""
    found = [path for path in sorted(workflows_dir.glob("*.yml"))
             if (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("intent") == MESH_INTENT]
    if len(found) != 1:
        raise MeshManifestError(f"{workflows_dir} must hold exactly one workflow with intent {MESH_INTENT}; found {len(found)}")
    return found[0]


@dataclass(frozen=True)
class ImageAsset:
    """A validated file part: the bytes plus the filename and content type it is sent with."""

    data: bytes
    filename: str
    content_type: str
