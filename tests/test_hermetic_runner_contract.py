from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "hermetic_preflight.py"


def _tree(tmp_path: Path):
    env = tmp_path / "env"; pyroot = tmp_path / "python"
    (env / "bin").mkdir(parents=True); (env / "lib/python3.12/site-packages/demo-1.0.dist-info").mkdir(parents=True)
    (pyroot / "bin").mkdir(parents=True)
    target = pyroot / "bin/python3.12"; target.write_text("#!/bin/sh\n"); target.chmod(0o755)
    (env / "bin/python").symlink_to(target)
    (env / "pyvenv.cfg").write_text(f"home = {pyroot / 'bin'}\n")
    (env / "lib/python3.12/site-packages/demo-1.0.dist-info/METADATA").write_text("Name: demo\nVersion: 1.0\n\n")
    req = tmp_path / "requirements.txt"; inp = tmp_path / "requirements-hermetic.in"
    req.write_text("demo==1.0\n"); inp.write_text("demo==1.0\n")
    lock = tmp_path / "lock"; h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    lock.write_text(f"# hermetic-manifest-sha256: {h(req)}\n# hermetic-input-sha256: {h(inp)}\ndemo==1.0\n")
    return env, pyroot, req, inp, lock


def _run(tmp_path, mutate=None, **extra):
    env, pyroot, req, inp, lock = _tree(tmp_path)
    if mutate: mutate(env, pyroot, req, inp, lock)
    args = [sys.executable, "-S", str(SCRIPT), "--environment-root", str(env), "--python-root", str(pyroot),
            "--requirements", str(req), "--input-manifest", str(inp), "--lock", str(lock), "--python-minor", "3.12"]
    args += sum(([f"--{k.replace('_','-')}", str(v)] for k, v in extra.items()), [])
    return subprocess.run(args, text=True, capture_output=True)


def test_preflight_rejects_missing_malformed_stale_and_package_drift(tmp_path):
    assert _run(tmp_path / "missing", lambda *x: x[0].rename(x[0].parent / "gone")).returncode == 127
    assert _run(tmp_path / "malformed", lambda *x: x[-1].write_text("broken\n")).returncode == 127
    assert _run(tmp_path / "stale", lambda *x: x[2].write_text("changed\n")).returncode == 127
    assert _run(tmp_path / "drift", lambda *x: (x[0] / "lib/python3.12/site-packages/extra-1.0.dist-info").mkdir()).returncode == 127


def test_preflight_rejects_hooks_symlinks_ambiguous_and_untracked(tmp_path):
    assert _run(tmp_path / "hook", lambda *x: (x[0] / "lib/python3.12/site-packages/bad.pth").write_text("x\n")).returncode == 127
    assert _run(tmp_path / "symlink", lambda *x: x[0].rename(x[0].parent / "real") or x[0].symlink_to(x[0].parent / "real")).returncode == 127
    parent = tmp_path / "cache"; parent.mkdir(); (parent / "key").mkdir(); (parent / "key-extra").mkdir()
    assert _run(tmp_path / "ambiguous", cache_parent=parent, cache_key="key").returncode == 127
