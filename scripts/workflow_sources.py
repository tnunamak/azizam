"""Which files hold the workflows of a tree (CODE_TO_DATA_S3, S3.5a, the equality check).

Standard library and PyYAML only, so the dump script can run it against any tree and the comparison reads the same list.
A tree's workflows are the documents discovery loads from ``manifests/workflows`` (every ``*.yml`` and ``*.yaml``) and the
workflow objects that each bundled extension package names under ``objects.workflows`` in its ``extension.yml``.
"""
from __future__ import annotations

from pathlib import Path

import yaml

MANIFEST_GLOBS = ("*.yml", "*.yaml")  # the same patterns as gateway.discovery.MANIFEST_GLOBS
PACKAGE_ROOT = ("extensions", "bundled")


def workflow_files(root: Path) -> list[Path]:
    """Every workflow document of the tree at ``root``: the bundled directory first, then each package's named objects."""
    found: set[Path] = set()
    directory = root / "manifests" / "workflows"
    for pattern in MANIFEST_GLOBS:
        found.update(path for path in directory.glob(pattern) if path.is_file())
    for package in sorted((root.joinpath(*PACKAGE_ROOT)).glob("*/extension.yml")):
        document = yaml.safe_load(package.read_text(encoding="utf-8")) or {}
        refs = (document.get("objects") or {}).get("workflows") or []
        found.update(package.parent / ref for ref in refs if isinstance(ref, str) and (package.parent / ref).is_file())
    return sorted(found)


def read_documents(root: Path) -> dict[str, tuple[Path, dict]]:
    """Workflow id -> (file, document). Two files with one id are an error: discovery refuses the second."""
    found: dict[str, tuple[Path, dict]] = {}
    for path in workflow_files(root):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        workflow_id = str(document["id"])
        if workflow_id in found:
            raise ValueError(f"workflow id {workflow_id} is in two files: {found[workflow_id][0]} and {path}")
        found[workflow_id] = (path, document)
    return found
