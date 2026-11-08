"""A small audio workflow manifest for route tests that serve a made-up workflow.

The audio route reads the request against a manifest and compiles the graph from the manifest's binding map; the
template's ``_meta`` is not read. A test that stubs a workflow template therefore stubs its manifest too. ``targets``
names, for each input, the template nodes it writes: ``{"prompt": [("1", "text")]}``. ``result`` names the history node and
key that the engine saves the audio file under, as the binding's selector; a test whose engine answers with a file needs it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from gateway.manifests import validate_manifest

# The specs a test may declare. The bounds are wide so a test's deployment limits (``audio.workflows.<name>``) only narrow.
INPUT_SPECS: dict[str, dict[str, Any]] = {
    "prompt": {"type": "string"},
    "tags": {"type": "string"},
    "lyrics": {"type": "string"},
    "duration": {"type": "number", "minimum": 0, "maximum": 3600},
    "seed": {"type": "integer", "minimum": 0, "maximum": 2**32 - 1,
             "generate": {"generator": "random_integer", "minimum": 0, "maximum": 2**32 - 1}},
}


def audio_manifest(name: str, targets: Mapping[str, Sequence[tuple[str, str]]], *, binding: str = "comfyui",
                   workflow: str | None = None, result: tuple[str, str] | None = None) -> dict[str, Any]:
    """``workflow`` is the template file the binding names; the route loads it from there."""
    manifest = {
        "schema_version": 2,
        "min_gateway_version": "0.1",
        "version": "0.1.0",
        "id": f"audio.generate.{name}",
        "intent": "audio.generate",
        "capability": "audio",
        "display_name": name,
        "inputs": {input_name: dict(INPUT_SPECS[input_name]) for input_name in targets},
        "outputs": {"audio": {"media_type": "audio/wav"}},
        "bindings": {binding: {
            "workflow": workflow or f"workflows/audio/{name}.json",
            "map": {input_name: [{"node": node, "input": key} for node, key in nodes]
                    for input_name, nodes in targets.items()},
        }},
    }
    if result is not None:
        node, key = result
        manifest["bindings"][binding]["selectors"] = {
            "audio": {"node": node, "key": key, "from": "output_dir", "decode": "none", "take": "first"}}
    validate_manifest(manifest)
    return manifest
