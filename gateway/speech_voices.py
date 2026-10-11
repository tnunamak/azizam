"""Project configured speech voice choices without guessing engine identities."""

from collections.abc import Mapping
from typing import Any

from gateway.transforms import TransformRegistry, ValueMapTransform


def speech_voice_catalog(spec: Mapping[str, Any], registry: TransformRegistry) -> dict[str, Any]:
    mapping = dict(spec.get("voice_map") or {})
    default = spec.get("default_voice")
    native = list(mapping.values())
    arbitrary = not mapping and not default
    transform = spec.get("voice_transform")
    if transform:
        # Only a single declarative voice map establishes a complete list.
        # An opaque/composed transform is not evidence for arbitrary IDs.
        primitives = registry.resolve(str(transform))
        if len(primitives) != 1 or not isinstance(primitives[0], ValueMapTransform) or primitives[0]._param != "voice":
            return {"voices": [], "default_voice": None, "allow_other": False}
        mapping = dict(primitives[0]._map)
        default = primitives[0]._default
        native = []
        arbitrary = default is None
    ids = list(dict.fromkeys(str(value) for value in [*mapping, *native, *([default] if default else [])]))
    return {
        "voices": [{"id": value, "name": value.replace("_", " ").title()} for value in ids],
        "default_voice": str(default) if default else None,
        "allow_other": arbitrary,
    }
