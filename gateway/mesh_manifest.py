"""The 3D wire's constants and the file part the multipart sender carries.

The workflow is data, and the registry validates it like any other (``manifests.enforce_contract``). The 3D frontend profile
(``frontend_shapes.mesh.mesh_profile``) says which valid workflows the 3D rows can carry. The form is built by the multipart
adapter from the job's pinned binding; the file is read and measured by the media resolver before the job exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from gateway.capability_ids import MEDIA_TYPE_MODALITIES
from gateway.discovery_metadata import capability_execution_intent

# The root module still compares intents with this name to choose the mesh protocol and job content; workflow readers use the contract intent.
MESH_MEDIA_TYPE = "model/gltf-binary"
MESH_INTENT = capability_execution_intent(MEDIA_TYPE_MODALITIES[MESH_MEDIA_TYPE])


class MeshManifestError(ValueError):
    """The workflow cannot be served by the 3D rows: the frontend profile refused it."""


@dataclass(frozen=True)
class ImageAsset:
    """A validated file part: the bytes plus the filename and content type it is sent with."""

    data: bytes
    filename: str
    content_type: str
