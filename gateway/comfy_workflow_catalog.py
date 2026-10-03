"""Catalog-wide check that every ComfyUI workflow can carry its UI graph.

Pure helpers (no network, no proxy import). The caller supplies the node
definitions, either a recorded ``/object_info`` fixture (tests) or a live
fetch (``scripts/audit_comfy_workflow_metadata.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import yaml

from gateway import comfy_ui_graph
from gateway.comfyui_workflow_validation import validate_comfyui_workflow_graph

# Hidden ComfyUI inputs that make a save node embed `prompt` and `workflow`.
_EMBEDS_WORKFLOW = "EXTRA_PNGINFO"
_SKIP_PARTS = {".backup", "_drafts"}


def catalog_workflow_paths(root: Path) -> list[Path]:
    """Every API-format workflow JSON the gateway can load, sorted.

    Union of the paths manifests bind (``bindings.comfyui.workflow``) and every
    ``workflows/**/*.json`` (dir-loaded names, identity, drafts excluded).
    """
    paths: set[Path] = set()
    for manifest in sorted((root / "manifests" / "workflows").glob("*.yml")):
        data = yaml.safe_load(manifest.read_text()) or {}
        artifact = ((data.get("bindings") or {}).get("comfyui") or {}).get("workflow")
        if isinstance(artifact, str) and artifact:
            path = root / artifact.removeprefix("package://").lstrip("/")
            if path.suffix == ".json":
                paths.add(path)
    for path in (root / "workflows").rglob("*.json"):
        rel = path.relative_to(root).parts
        if path.name.endswith(".ui.json") or any(p.startswith(s) for p in rel for s in _SKIP_PARTS):
            continue
        paths.add(path)
    return sorted(paths)


def class_types(workflow: Mapping[str, Any]) -> set[str]:
    return {n["class_type"] for k, n in workflow.items()
            if isinstance(n, Mapping) and "class_type" in n and not str(k).startswith("_")}


def embedding_output_nodes(
    workflow: Mapping[str, Any], object_info_for: Callable[[str], Optional[Mapping]]
) -> list[str]:
    """Output node classes that receive ``extra_pnginfo`` (and so embed the workflow)."""
    found = []
    for cls in sorted(class_types(workflow)):
        info = object_info_for(cls)
        if not info or not info.get("output_node"):
            continue
        hidden = (info.get("input") or {}).get("hidden") or {}
        values = hidden.values() if isinstance(hidden, Mapping) else hidden
        flat = [v[0] if isinstance(v, (list, tuple)) and v else v for v in values]
        if _EMBEDS_WORKFLOW in flat:
            found.append(cls)
    return found


def audit_workflow(
    path: Path, object_info_for: Callable[[str], Optional[Mapping]]
) -> list[str]:
    """Problems for one workflow file; empty means the UI graph is complete."""
    workflow = json.loads(path.read_text())
    errors = validate_comfyui_workflow_graph(workflow)
    if errors:
        return [f"invalid API graph: {e}" for e in errors]
    unknown = sorted(c for c in class_types(workflow) if not object_info_for(c))
    if unknown:
        return [f"no object_info for node types {unknown}"]
    ui = comfy_ui_graph.derive_ui_workflow(workflow, object_info_for)
    if ui is None:
        return ["derive_ui_workflow returned None"]
    problems = comfy_ui_graph.validate_ui_workflow(workflow, ui)
    if not embedding_output_nodes(workflow, object_info_for):
        problems.append("no output node embeds the workflow (needs a hidden EXTRA_PNGINFO input)")
    return problems


def trim_object_info(full: Mapping[str, Any], classes: set[str], max_options: int = 4) -> dict[str, Any]:
    """Subset of ``/object_info`` for ``classes`` with long model lists cut.

    Combo option lists only feed default values, so the first few entries keep
    the fixture small without changing the derived graph.
    """

    def cut(value: Any) -> Any:
        if isinstance(value, list):
            if value and all(isinstance(v, str) for v in value):
                return value[:max_options]
            return [cut(v) for v in value]
        if isinstance(value, dict):
            return {k: cut(v) if k != "options" or not isinstance(v, list) or
                    not all(isinstance(o, str) for o in v) else v[:max_options]
                    for k, v in value.items()}
        return value

    return {c: cut(full[c]) for c in sorted(classes) if c in full}
