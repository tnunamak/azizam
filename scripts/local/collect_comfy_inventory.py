#!/usr/bin/env python3
"""Collect a safe ComfyUI inventory for workflow requirement checks.

The collector lists node class names from a saved ``/object_info`` response and
model filenames from known ComfyUI model directories. It never reads model file
contents and only emits root-relative paths.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


MODEL_DIR_ROLES = {
    "checkpoints": "checkpoint",
    "checkpoint": "checkpoint",
    "vae": "vae",
    "loras": "lora",
    "lora": "lora",
    "controlnet": "controlnet",
    "controlnets": "controlnet",
    "upscale_models": "upscaler",
    "upscaler": "upscaler",
    "upscalers": "upscaler",
}

MODEL_EXTENSIONS = {
    ".ckpt",
    ".gguf",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
}

CLASS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")


def collect_inventory(
    comfy_root: str | Path,
    object_info_path: str | Path | None = None,
    hash_inventory_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(comfy_root).expanduser().resolve()
    hash_inventory = load_hash_inventory(hash_inventory_path)
    artifacts = collect_model_artifacts(root, hash_inventory)
    node_classes = collect_node_classes(object_info_path)

    return {
        "source": {
            "type": "comfyui",
            "root": "<redacted>",
        },
        "node_classes": node_classes,
        "model_artifacts": artifacts,
        "models": artifacts,
    }


def collect_node_classes(object_info_path: str | Path | None) -> list[str]:
    if object_info_path is None:
        return []

    with Path(object_info_path).expanduser().open(encoding="utf-8") as handle:
        object_info = json.load(handle)
    if not isinstance(object_info, dict):
        raise ValueError("object_info JSON must be an object")

    classes = {
        class_name
        for class_name in object_info
        if isinstance(class_name, str) and CLASS_NAME_RE.match(class_name)
    }
    return sorted(classes)


def collect_model_artifacts(
    comfy_root: Path,
    hash_inventory: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    models_root = comfy_root / "models"
    if not models_root.is_dir():
        return []

    artifacts: list[dict[str, str]] = []
    for role_dir in sorted(models_root.iterdir(), key=lambda item: item.name.lower()):
        if not role_dir.is_dir():
            continue
        role = MODEL_DIR_ROLES.get(role_dir.name.lower())
        if role is None:
            continue
        for model_file in sorted_model_files(role_dir):
            rel_path = safe_relative_path(model_file, comfy_root)
            artifact = {
                "role": role,
                "path": rel_path,
                "name": model_file.name,
            }
            sha256 = (hash_inventory or {}).get(rel_path) or (hash_inventory or {}).get(
                model_file.name
            )
            if sha256:
                artifact["sha256"] = sha256.lower()
            artifacts.append(artifact)
    return artifacts


def sorted_model_files(role_dir: Path) -> list[Path]:
    files = [
        path
        for path in role_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS
    ]
    return sorted(files, key=lambda item: item.relative_to(role_dir).as_posix().lower())


def safe_relative_path(path: Path, root: Path) -> str:
    relative = path.resolve().relative_to(root)
    return relative.as_posix()


def load_hash_inventory(path: str | Path | None) -> dict[str, str] | None:
    if path is None:
        return None

    with Path(path).expanduser().open(encoding="utf-8") as handle:
        raw = json.load(handle)

    entries = raw.get("model_artifacts", raw.get("models", raw)) if isinstance(raw, dict) else raw
    if isinstance(entries, dict):
        pairs = entries.items()
    elif isinstance(entries, list):
        pairs = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            sha256 = entry.get("sha256")
            key = entry.get("path") or entry.get("name")
            if isinstance(key, str) and isinstance(sha256, str):
                pairs.append((key, sha256))
    else:
        raise ValueError("hash inventory must be an object or list")

    hashes: dict[str, str] = {}
    for key, sha256 in pairs:
        if not isinstance(key, str) or not isinstance(sha256, str):
            continue
        safe_key = safe_inventory_key(key)
        if safe_key and SHA256_RE.match(sha256):
            hashes[safe_key] = sha256.lower()
    return hashes


def safe_inventory_key(key: str) -> str | None:
    normalized = key.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or "://" in normalized:
        return None
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect safe ComfyUI node/model inventory as JSON.",
    )
    parser.add_argument("comfy_root", help="Path to the ComfyUI root directory")
    parser.add_argument(
        "--object-info",
        help="Saved JSON response from ComfyUI /object_info",
    )
    parser.add_argument(
        "--hash-inventory",
        help="Optional JSON inventory containing precomputed sha256 values",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    inventory = collect_inventory(
        args.comfy_root,
        object_info_path=args.object_info,
        hash_inventory_path=args.hash_inventory,
    )
    json.dump(inventory, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
