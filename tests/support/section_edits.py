"""What a capability section adds to a bundled workflow document, so a test can compare the document with its schema 1 origin.

The frozen schema 1 release copies (``tests/fixtures`` and ``gateway/migration_data``) predate the section a video
manifest keeps under its capability's name (CODE_TO_DATA S7, V5a). A bundled document equals the conversion of its frozen
copy except for that section and the inputs it adds as roles; this strips exactly those, nothing else.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any


def without_section_additions(bundled: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    """``bundled`` minus its capability section and minus every role input (and its mapping) the origin document lacks."""
    document = copy.deepcopy(dict(bundled))
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
