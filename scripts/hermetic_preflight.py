#!/usr/bin/env python3
"""Non-importing structural gate for the hermetic test environment.

Run this with ``python -S`` inside Bubblewrap. It never imports site, pytest,
or a cached package; it only hashes reviewed manifests and inspects files.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys


class PreflightError(RuntimeError):
    pass


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def real_directory(label: str, path: Path) -> None:
    if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
        raise PreflightError(f"{label} must be a real directory: {path}")


def locked_inventory(lock: Path) -> set[str]:
    result: set[str] = set()
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            result.add(f"{match.group(1).lower().replace('_', '-')}=={match.group(2)}")
    if not result:
        raise PreflightError("lock has no package inventory")
    return result


def installed_inventory(site_packages: Path) -> set[str]:
    result: set[str] = set()
    for metadata in sorted(site_packages.glob("*.dist-info/METADATA")):
        fields: dict[str, str] = {}
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if not line:
                break
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.lower()] = value.strip()
        name, version = fields.get("name"), fields.get("version")
        if not name or not version:
            raise PreflightError(f"invalid package metadata: {metadata}")
        result.add(f"{name.lower().replace('_', '-')}=={version}")
    return result


def reject_hooks(*roots: Path) -> None:
    patterns = ("*.pth", "*.egg-link", "sitecustomize.py", "usercustomize.py", "direct_url.json")
    for root in roots:
        for pattern in patterns:
            found = next(root.rglob(pattern), None)
            if found is not None:
                raise PreflightError(f"startup or editable hook is forbidden: {found}")


def validate(args: argparse.Namespace) -> None:
    if args.cache_parent and args.cache_key:
        parent = Path(args.cache_parent)
        candidates = sorted(parent.glob(f"{args.cache_key}*")) if parent.is_dir() else []
        if len(candidates) != 1 or candidates[0].name != args.cache_key:
            raise PreflightError("cache key is missing or ambiguous")
    environment = Path(args.environment_root)
    python_root = Path(args.python_root)
    requirements = Path(args.requirements)
    input_manifest = Path(args.input_manifest)
    lock = Path(args.lock)
    real_directory("environment root", environment)
    real_directory("python root", python_root)
    if environment.name != "venv":
        raise PreflightError("environment root is not the exact venv mount")
    if not requirements.is_file() or not input_manifest.is_file() or not lock.is_file():
        raise PreflightError("manifest or lock input is missing")

    lines = lock.read_text(encoding="utf-8").splitlines()
    headers = {}
    for line in lines[:8]:
        if line.startswith("# hermetic-") and ": " in line:
            key, value = line[2:].split(": ", 1)
            headers[key] = value
    if headers.get("hermetic-manifest-sha256") != digest(requirements):
        raise PreflightError("requirements.txt is stale relative to the lock")
    if headers.get("hermetic-input-sha256") != digest(input_manifest):
        raise PreflightError("requirements-hermetic.in is stale relative to the lock")

    site_packages = environment / "lib" / f"python{args.python_minor}" / "site-packages"
    real_directory("site-packages", site_packages)
    reject_hooks(environment, python_root)
    expected = locked_inventory(lock)
    actual = installed_inventory(site_packages)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise PreflightError(f"installed package inventory drift (missing={missing[:3]}, extra={extra[:3]})")

    cfg = environment / "pyvenv.cfg"
    if not cfg.is_file():
        raise PreflightError("virtualenv metadata is missing")
    home_line = next((line for line in cfg.read_text(encoding="utf-8").splitlines() if line.startswith("home = ")), "")
    if not home_line or Path(home_line[7:]).resolve() != (python_root / "bin").resolve():
        raise PreflightError("virtualenv points outside the approved Python runtime")
    interpreter = environment / "bin" / "python"
    if not interpreter.is_symlink() or interpreter.resolve() != (python_root / "bin" / "python3.12").resolve():
        raise PreflightError("virtualenv interpreter escapes the approved Python runtime")

    if args.git_root:
        result = subprocess.run(
            ["git", "-C", args.git_root, "ls-files", "--others", "--exclude-standard"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PreflightError("cannot inspect Git untracked inputs")
        if result.stdout.strip():
            raise PreflightError("untracked checkout input is forbidden")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-root", required=True)
    parser.add_argument("--python-root", required=True)
    parser.add_argument("--requirements", required=True)
    parser.add_argument("--input-manifest", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--python-minor", required=True)
    parser.add_argument("--git-root")
    parser.add_argument("--cache-parent")
    parser.add_argument("--cache-key")
    args = parser.parse_args()
    try:
        validate(args)
    except (OSError, PreflightError, ValueError) as error:
        print(f"hermetic preflight rejected: {error}", file=sys.stderr)
        return 127
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
