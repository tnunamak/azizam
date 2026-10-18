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


def install(kind: str, destination: Path) -> None:
    root = Path(os.environ.get("AZIZAM_CI_CACHE", str(Path.home() / "Documents/azizam/ci-cache")))
    # Exclude only the gate's per-invocation bookkeeping. Keep build flags,
    # npm/uv settings, HOME, PATH and tool-specific environment in the key.
    transient = {"PWD", "OLDPWD", "SHLVL", "_", "receipts", "work", "run", "seed"}
    inputs = {
        "helper": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": [platform.platform(), platform.machine(), platform.libc_ver()],
        "environment": {k: v for k, v in os.environ.items() if k not in transient and not k.startswith("BASH_FUNC_")},
    }
    if kind == "node":
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
    elif kind == "venv":
        interpreter = output("uv", "python", "find", "3.12")
        configs = [parent / name for parent in (Path.cwd(), *Path.cwd().parents)
                   for name in ("uv.toml", "pyproject.toml")]
        configs += [Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "uv/uv.toml",
                    Path("/etc/uv/uv.toml")]
        if os.environ.get("UV_CONFIG_FILE"):
            configs.append(Path(os.environ["UV_CONFIG_FILE"]))
        inputs["uv_configs"] = [hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None for p in configs]
        inputs.update(uv=output("uv", "--version"),
                      python=output(interpreter, "-c", "import sys,sysconfig; print((sys.version,sys.executable,sysconfig.get_config_vars()))"),
                      lock=Path("tests/requirements-hermetic.lock").read_text())
        command = ["uv", "pip", "sync", "-q", "--python", str(destination / "bin/python"),
                   "--require-hashes", "tests/requirements-hermetic.lock"]
    else:
        raise ValueError(kind)
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
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
                if kind == "venv":
                    # Relative console launchers stay valid after the private copy.
                    subprocess.run(["uv", "venv", "-q", "--relocatable", "--python", interpreter, str(destination)], check=True)
                subprocess.run(command, check=True)
                subprocess.run(["cp", "-a", "--reflink=auto", str(destination), str(entry / "environment")], check=True)
                (entry / "complete").write_text(fingerprint(entry / "environment"))
            except BaseException:
                shutil.rmtree(entry, ignore_errors=True)
                raise
        else:
            print(f"dependency cache hit: {kind} {key}", flush=True)
            subprocess.run(["cp", "-a", "--reflink=auto", str(entry / "environment"), str(destination)], check=True)


if __name__ == "__main__":
    install(sys.argv[1], Path(sys.argv[2]).absolute())
