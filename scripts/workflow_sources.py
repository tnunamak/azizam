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


def workflow_files(root: Path) -> list[tuple[Path, Path | None]]:
    """Every workflow document of the tree at ``root`` as (file, package root): the bundled directory first (no package root),
    then each package's named objects, with the folder that holds the declaring ``extension.yml`` as the package root. A
    package that names a file that does not exist is an error, not a workflow left out."""
    found: dict[Path, Path | None] = {}
    directory = root / "manifests" / "workflows"
    for pattern in MANIFEST_GLOBS:
        found.update((path, None) for path in directory.glob(pattern) if path.is_file())
    for package in sorted((root.joinpath(*PACKAGE_ROOT)).glob("*/extension.yml")):
        document = yaml.safe_load(package.read_text(encoding="utf-8")) or {}
        for ref in (document.get("objects") or {}).get("workflows") or []:
            if not isinstance(ref, str):
                continue
            if not (package.parent / ref).is_file():
                raise ValueError(f"{package} names the workflow file {ref}, which does not exist")
            found[package.parent / ref] = package.parent
    return sorted(found.items())


def read_documents(root: Path) -> dict[str, tuple[Path, dict, Path | None]]:
    """Workflow id -> (file, document, package root or None). Two files with one id are an error: discovery refuses the second."""
    found: dict[str, tuple[Path, dict, Path | None]] = {}
    for path, package_root in workflow_files(root):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        workflow_id = str(document["id"])
        if workflow_id in found:
            raise ValueError(f"workflow id {workflow_id} is in two files: {found[workflow_id][0]} and {path}")
        found[workflow_id] = (path, document, package_root)
    return found
