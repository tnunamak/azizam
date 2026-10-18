#!/usr/bin/env python3
"""Cache installed dependencies, then give each gate a private copy.

Only successful installs are published. Keys include toolchains, platform,
installer configuration and environment, manifests, and this implementation.
No test result or mutable test environment is shared.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import stat


def output(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def fingerprint(directory: Path) -> str:
    """Detect missing, changed or replaced snapshot files before restoration."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        digest.update(str(path.relative_to(directory)).encode() + b"\0")
        digest.update(str(stat.S_IMODE(path.lstat().st_mode)).encode() + b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
        else:
            digest.update(b"directory\0")
        digest.update(b"\0")
    return digest.hexdigest()


def prune(root: Path, kind: str, current: Path) -> None:
    """Keep the current snapshot and one recent peer; never remove an active key."""
    with (root / ".prune.lock").open("a") as pruning:
        fcntl.flock(pruning, fcntl.LOCK_EX)
        entries = []
        for path in root.iterdir():
            if path == current or path.is_symlink() or not path.is_dir():
                continue
            if re.fullmatch(rf"{kind}-[a-f0-9]{{64}}", path.name):
                marker = path / "complete"
                try:
                    last_used = marker.stat().st_mtime if marker.is_file() else path.stat().st_mtime
                except FileNotFoundError:
                    continue
                entries.append((last_used, path))
        for _, path in sorted(entries, reverse=True)[1:]:
            with (root / f"{path.name}.lock").open("a") as candidate:
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    shutil.rmtree(path)
                except FileNotFoundError:
                    continue
                print(f"dependency cache pruned: {path.name}", flush=True)


def install(kind: str, destination: Path) -> None:
    if kind != "node":
        raise ValueError(kind)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"dependency destination already exists: {destination}")
    root = Path(os.environ.get("AZIZAM_CI_CACHE", str(Path.home() / "Documents/azizam/ci-cache")))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise PermissionError("dependency cache root must be a directory owned by the current user")
    root.chmod(0o700)
    # Session metadata must not invalidate installs between shells. Explicit
    # installer inputs still separate snapshots with different configuration.
    prefixes = ("npm_config_", "NPM_", "NODE_", "UV_", "PIP_", "PYTHON")
    variables = {"CI", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}
    inputs = {
        "helper": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": [platform.platform(), platform.machine(), platform.libc_ver()],
        "environment": {k: v for k, v in os.environ.items()
                        if k.startswith(prefixes) or k.upper() in variables},
    }
    manifest = Path("package.json")
    package = json.loads(manifest.read_text())
    # Local dependencies and project lifecycle scripts can read arbitrary
    # source files. Preserve npm ci semantics instead of guessing their inputs.
    lockfile = Path("package-lock.json").read_text()
    locked = json.loads(lockfile)
    local = bool(package.get("workspaces")) or any(str(v).startswith(("file:", "link:", "workspace:"))
                for group in ("dependencies", "devDependencies", "optionalDependencies")
                for v in package.get(group, {}).values())
    hooks = set(package.get("scripts", {})) & {"preinstall", "install", "postinstall", "prepublish", "preprepare", "prepare", "postprepare"}
    local = local or any(item.get("link") or str(item.get("resolved", "")).startswith(("file:", "link:"))
                         for item in locked.get("packages", {}).values())
    if local or hooks:
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], check=True)
        return
    if any(item.get("hasInstallScript") for item in locked.get("packages", {}).values()):
        # Dependency hooks can inspect INIT_CWD and arbitrary project files.
        # Be conservative rather than maintaining a list of trusted hooks.
        inputs["source_tree"] = output("git", "rev-parse", "HEAD^{tree}")
    inputs.update(node=output("node", "--version"), npm=output("npm", "--version"),
                  config=output("npm", "config", "list", "--json"),
                  manifest=manifest.read_text(), lock=lockfile)
    command = ["npm", "ci", "--no-audit", "--no-fund"]
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    entry = root / f"{kind}-{key}"
    with (root / f"{kind}-{key}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        complete = entry / "complete"
        valid = False
        if complete.is_file() and (entry / "environment").is_dir():
            try:
                valid = complete.read_text() == fingerprint(entry / "environment")
            except OSError:
                pass
        if not valid:
            print(f"dependency cache miss: {kind} {key}", flush=True)
            shutil.rmtree(entry, ignore_errors=True)
            entry.mkdir()
            try:
                subprocess.run(command, check=True)
                subprocess.run(["cp", "-a", "--reflink=auto", str(destination), str(entry / "environment")], check=True)
                (entry / "complete").write_text(fingerprint(entry / "environment"))
            except BaseException:
                shutil.rmtree(entry, ignore_errors=True)
                raise
        else:
            print(f"dependency cache hit: {kind} {key}", flush=True)
            subprocess.run(["cp", "-a", "--reflink=auto", str(entry / "environment"), str(destination)], check=True)
        os.utime(complete, None)
        prune(root, kind, entry)


if __name__ == "__main__":
    install(sys.argv[1], Path(sys.argv[2]).absolute())
