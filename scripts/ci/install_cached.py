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


def output(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


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
        local = any(str(v).startswith(("file:", "link:", "workspace:"))
                    for group in ("dependencies", "devDependencies", "optionalDependencies")
                    for v in package.get(group, {}).values())
        hooks = set(package.get("scripts", {})) & {"preinstall", "install", "postinstall", "prepublish", "preprepare", "prepare", "postprepare"}
        if local or hooks:
            subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], check=True)
            return
        inputs.update(node=output("node", "--version"), npm=output("npm", "--version"),
                      config=output("npm", "config", "list", "--json"),
                      manifest=manifest.read_text(), lock=Path("package-lock.json").read_text())
        command = ["npm", "ci", "--no-audit", "--no-fund"]
    elif kind == "venv":
        interpreter = output("uv", "python", "find", "3.12")
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
        if not (entry / "complete").is_file():
            print(f"dependency cache miss: {kind} {key}", flush=True)
            shutil.rmtree(entry, ignore_errors=True)
            entry.mkdir()
            try:
                if kind == "venv":
                    # Relative console launchers stay valid after the private copy.
                    subprocess.run(["uv", "venv", "-q", "--relocatable", "--python", interpreter, str(destination)], check=True)
                subprocess.run(command, check=True)
                subprocess.run(["cp", "-a", "--reflink=auto", str(destination), str(entry / "environment")], check=True)
                (entry / "complete").touch()
            except BaseException:
                shutil.rmtree(entry, ignore_errors=True)
                raise
        else:
            print(f"dependency cache hit: {kind} {key}", flush=True)
            subprocess.run(["cp", "-a", "--reflink=auto", str(entry / "environment"), str(destination)], check=True)


if __name__ == "__main__":
    install(sys.argv[1], Path(sys.argv[2]).absolute())
