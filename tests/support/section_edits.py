"""What a bundled workflow document adds after its schema 1 origin, so a test can compare the two.

The frozen schema 1 release copies (``tests/fixtures`` and ``gateway/migration_data``) predate the section a video
manifest keeps under its capability's name (CODE_TO_DATA S7, V5a). A bundled document equals the conversion of its frozen
copy except for that section and the inputs it adds as roles; this strips exactly those, nothing else.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any


STATUS_CAPABILITIES = "azizam.dev/status-capabilities"


def _without_status_capabilities(document: dict[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    """The status page's capability flags are an annotation the bundled documents gained after the frozen copies (CODE_TO_DATA M16)."""
    annotations = document.get("annotations")
    if isinstance(annotations, dict) and STATUS_CAPABILITIES not in (origin.get("annotations") or {}):
        annotations.pop(STATUS_CAPABILITIES, None)
        if not annotations and "annotations" not in origin:
            del document["annotations"]
    return document


def without_section_additions(bundled: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    """``bundled`` minus its capability section and minus every role input (and its mapping) the origin document lacks."""
    document = copy.deepcopy(dict(bundled))
    document = _without_status_capabilities(document, origin)
    capability = document.get("capability")
    section = document.get(capability) if isinstance(capability, str) and capability not in ("inputs", "outputs", "bindings") else None
    if not isinstance(section, Mapping):
        return document
    del document[capability]
    for input_name in (section.get("roles") or {}).values():
        if input_name in origin.get("inputs", {}):
            continue
        document["inputs"].pop(input_name, None)
        for key, body in document["bindings"].items():
            if input_name not in origin["bindings"].get(key, {}).get("map", {}):
                body.get("map", {}).pop(input_name, None)
    return document
