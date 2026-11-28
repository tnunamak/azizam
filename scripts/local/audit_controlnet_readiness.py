#!/usr/bin/env python3
"""Audit whether the local ComfyUI install can honestly expose ControlNet.

The report is intentionally conservative. It only prints safe filenames and
node class names; it never reads model contents, prompts, workflow input values,
or secrets. A workflow manifest should only grow ControlNet controls when this
audit says the local install has all prerequisites.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


CONTROLNET_MANIFEST_CONTROLS: tuple[str, ...] = (
    "control_image",
    "controlnet_strength",
)

CONTROLNET_MODEL_EXTENSIONS: tuple[str, ...] = (
    ".safetensors",
    ".ckpt",
    ".pt",
    ".pth",
    ".bin",
)

CONTROLNET_MODEL_DIRS: tuple[str, ...] = (
    "models/controlnet",
    "models/controlnet_flux",
    "models/controlnet-union",
)

CONTROLNET_CUSTOM_NODE_DIR = "custom_nodes"

CONTROLNET_CLASS_HINTS: tuple[str, ...] = (
    "controlnetloader",
    "controlnetapply",
    "applycontrolnet",
    "loadcontrolnet",
)

PLACEHOLDER_NAMES: tuple[str, ...] = (
    "put_controlnets_and_t2i_here",
    "put_controlnet_models_here",
)


@dataclass(frozen=True)
class PathInventory:
    path: str
    exists: bool
    entries: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ControlNetReadinessReport:
    comfyui_root: str
    can_enable: bool
    valid_manifest_controls: tuple[str, ...]
    missing_prerequisites: tuple[str, ...]
    found_node_classes: tuple[str, ...]
    controlnet_custom_nodes: tuple[str, ...]
    model_dirs: tuple[PathInventory, ...]
    model_files: tuple[str, ...]
    workflow_controlnet_nodes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["model_dirs"] = [d.to_dict() for d in self.model_dirs]
        return data


def _safe_names(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    names = []
    for child in path.iterdir():
        if child.name.startswith("."):
            continue
        suffix = "/" if child.is_dir() else ""
        names.append(f"{child.name}{suffix}")
    return tuple(sorted(names))


def _is_model_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in CONTROLNET_MODEL_EXTENSIONS


def inventory_model_dirs(comfyui_root: Path) -> tuple[tuple[PathInventory, ...], tuple[str, ...]]:
    dirs: list[PathInventory] = []
    model_files: list[str] = []
    for rel in CONTROLNET_MODEL_DIRS:
        model_dir = comfyui_root / rel
        entries = _safe_names(model_dir)
        dirs.append(
            PathInventory(
                path=rel,
                exists=model_dir.is_dir(),
                entries=entries,
            )
        )
        if model_dir.is_dir():
            for child in model_dir.iterdir():
                if child.name.startswith("."):
                    continue
                if _is_model_file(child) and child.name not in PLACEHOLDER_NAMES:
                    model_files.append(f"{rel}/{child.name}")
    return tuple(dirs), tuple(sorted(model_files))


def inventory_controlnet_custom_nodes(comfyui_root: Path) -> tuple[str, ...]:
    custom_nodes_dir = comfyui_root / CONTROLNET_CUSTOM_NODE_DIR
    if not custom_nodes_dir.is_dir():
        return ()
    found = []
    for name in _safe_names(custom_nodes_dir):
        lowered = name.lower().replace("_", "").replace("-", "")
        if "controlnet" in lowered:
            found.append(name)
    return tuple(found)


def controlnet_classes_from_object_info(object_info: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(object_info, dict):
        return ()
    found = []
    for class_name in object_info:
        lowered = str(class_name).lower().replace("_", "")
        if any(hint in lowered for hint in CONTROLNET_CLASS_HINTS):
            found.append(str(class_name))
    return tuple(sorted(found))


def controlnet_nodes_from_workflow(workflow: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(workflow, dict):
        return ()
    found = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if not isinstance(class_type, str):
            continue
        class_l = class_type.lower().replace("_", "")
        inputs = node.get("inputs")
        input_names: Iterable[str] = inputs if isinstance(inputs, dict) else ()
        input_l = " ".join(str(name).lower() for name in input_names)
        if "controlnet" in class_l or "controlnet" in input_l:
            found.append(f"{node_id}:{class_type}")
    return tuple(sorted(found))


def audit_controlnet_readiness(
    comfyui_root: Path,
    *,
    object_info: dict[str, Any] | None = None,
    workflow: dict[str, Any] | None = None,
) -> ControlNetReadinessReport:
    model_dirs, model_files = inventory_model_dirs(comfyui_root)
    controlnet_custom_nodes = inventory_controlnet_custom_nodes(comfyui_root)
    found_node_classes = controlnet_classes_from_object_info(object_info)
    workflow_nodes = controlnet_nodes_from_workflow(workflow)

    missing: list[str] = []
    if not comfyui_root.is_dir():
        missing.append("comfyui_root")
    if not found_node_classes:
        missing.append("controlnet_node_classes")
    if not model_files:
        missing.append("controlnet_model_files")
    if workflow is not None and not workflow_nodes:
        missing.append("workflow_controlnet_nodes")

    can_enable = not missing
    return ControlNetReadinessReport(
        comfyui_root=str(comfyui_root),
        can_enable=can_enable,
        valid_manifest_controls=CONTROLNET_MANIFEST_CONTROLS if can_enable else (),
        missing_prerequisites=tuple(missing),
        found_node_classes=found_node_classes,
        controlnet_custom_nodes=controlnet_custom_nodes,
        model_dirs=model_dirs,
        model_files=model_files,
        workflow_controlnet_nodes=workflow_nodes,
    )


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _render_text(report: ControlNetReadinessReport) -> str:
    lines = [
        f"comfyui_root: {report.comfyui_root}",
        f"can_enable_controlnet: {str(report.can_enable).lower()}",
        "valid_manifest_controls: "
        + (", ".join(report.valid_manifest_controls) if report.valid_manifest_controls else "-"),
        "missing_prerequisites: "
        + (", ".join(report.missing_prerequisites) if report.missing_prerequisites else "-"),
        "found_node_classes: "
        + (", ".join(report.found_node_classes) if report.found_node_classes else "-"),
        "controlnet_custom_nodes: "
        + (
            ", ".join(report.controlnet_custom_nodes)
            if report.controlnet_custom_nodes
            else "-"
        ),
        "workflow_controlnet_nodes: "
        + (", ".join(report.workflow_controlnet_nodes) if report.workflow_controlnet_nodes else "-"),
        "model_files: " + (", ".join(report.model_files) if report.model_files else "-"),
        "model_dirs:",
    ]
    for model_dir in report.model_dirs:
        entries = ", ".join(model_dir.entries) if model_dir.entries else "-"
        lines.append(f"  {model_dir.path}: {'exists' if model_dir.exists else 'missing'} ({entries})")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Audit local ComfyUI ControlNet readiness using safe filenames only."
    )
    parser.add_argument(
        "--comfyui-root",
        default=os.environ.get("AZIZAM_COMFY_DIR", ""),
        help="Path to the local ComfyUI installation (or set AZIZAM_COMFY_DIR)",
    )
    parser.add_argument(
        "--object-info",
        help="Optional saved ComfyUI /object_info JSON for node class availability",
    )
    parser.add_argument(
        "--workflow",
        help="Optional API-format workflow JSON to check for ControlNet nodes",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    try:
        object_info = _read_json(Path(args.object_info)) if args.object_info else None
        workflow = _read_json(Path(args.workflow)) if args.workflow else None
        report = audit_controlnet_readiness(
            Path(args.comfyui_root), object_info=object_info, workflow=workflow
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
