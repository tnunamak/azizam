#!/usr/bin/env python3
"""Serve an instrumented gateway over a COPY of the operator's data (perf harness).

State comes from scripts/ui/live_data_copy.py: read-only SQLite backups of the
admin, receipts, media_jobs, responses and metrics databases, a config pointed
at the fake engine, and sign-in for the copy's owner and one member (set in the
copy only). This adds the media files (copied from the source checkout's image
cache) and runs scripts/perf/gateway_main.py so every response carries
statement, connection and subprocess counts. The source tree is never written.

    scripts/perf/serve.py --port 18710 [--source ~/applications/azizam] [--clone STATE_OF_A_RUNNING_SERVER]

The copy lives in a scratch directory (scripts/scratch_owner.py): it is removed when this process
ends, also on TERM, INT or HUP, and a copy left by a killed run is removed by the next start.
Copies of the owner's data never outlive the run; there is no way to keep one.

Prints {"ready": true, "serve_pid": ..., ...manifest} on one line, then serves until SIGTERM.
Ports: PORT and PORT+1 (fake engine). Both must be in 18000-18998.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import scratch_owner  # noqa: E402

SCRATCH_PREFIX = "azizam-perf-run-"

from scripts.ui import live_data_copy  # noqa: E402


def media_source(source: Path) -> Path:
    import yaml

    config = yaml.safe_load((source / "proxy_config.yml").read_text(encoding="utf-8"))
    cache = Path((config.get("images") or {}).get("cache_dir") or "cache/images")
    return cache if cache.is_absolute() else source / cache


def prepare(state: Path, port: int, engine_port: int, source: Path) -> dict:
    manifest = live_data_copy.prepare(state, port, engine_port, source=source, restarted=False)
    media = media_source(source)
    copied = 0
    if media.is_dir():
        for item in media.iterdir():
            if item.is_file():
                shutil.copy2(item, state / "images" / item.name)
                copied += 1
    manifest["media_files_copied"] = copied
    return manifest


def serve(state: Path, port: int, source: Path, clone: Path | None = None) -> None:
    engine_port = port + 1
    if clone is not None:
        # The same data as a running server, so two versions of the code answer the same requests
        # (scripts/perf/snapshot.py). The clone is its own scratch directory, deleted with this process.
        shutil.copytree(clone, state, symlinks=True, dirs_exist_ok=True)
        scratch_owner.mark(state)
        manifest = json.loads((state / "perf-manifest.json").read_text(encoding="utf-8"))
        live_data_copy.write_config(state, port, engine_port)
        os.environ["AZIZAM_CONFIG_PATH"] = str(state / "config.yml")
    else:
        manifest = prepare(state, port, engine_port, source)
    env = live_data_copy.env_for(state, port, engine_port, Path(str(manifest["config"])))
    env["PERF_ROUTES_FILE"] = str(state / "routes.json")
    for flag in ("PERF_ATTRIBUTE", "PERF_PROFILE", "PERF_PLANS"):  # slow modes: charge work to callers / sample stacks
        if os.environ.get(flag) == "1":
            env[flag] = "1"
    commands = [
        ([sys.executable, "-m", "uvicorn", "scripts.ui.fake_engine:app", "--host", "127.0.0.1",
          "--port", str(engine_port), "--log-level", "warning"], engine_port),
        ([sys.executable, "-m", "scripts.perf.gateway_main", str(port)], port),
    ]
    children: list[subprocess.Popen] = []

    try:
        for command, child_port in commands:
            log = (state / f"{child_port}.log").open("w")
            children.append(subprocess.Popen(command, cwd=state, env=env, stdout=log, stderr=subprocess.STDOUT))
            log.close()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f"fixture child exited early; see {state}/*.log")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).close()
                break
            except OSError:
                time.sleep(0.3)
        else:
            raise RuntimeError("fixture gateway did not become ready")
        manifest = {**manifest, "ready": True, "port": port, "gateway_pid": children[1].pid, "serve_pid": os.getpid(), "base_url": f"http://127.0.0.1:{port}", "state": str(state),
                    "routes_file": env["PERF_ROUTES_FILE"]}
        (state / "perf-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        print(json.dumps(manifest), flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--source", type=Path, default=Path.home() / "applications" / "azizam")
    parser.add_argument("--clone", type=Path, help="start from the data of a running perf server (its `state` path), not the live data")
    args = parser.parse_args()
    if not 18000 <= args.port < 18998:
        raise SystemExit("port must be between 18000 and 18998")
    if args.port in (5000, 15000):
        raise SystemExit("refusing a production port")
    root = Path(os.environ.get("AZIZAM_CI_SCRATCH", Path.home() / ".tmp"))
    scratch_owner.sweep(root, SCRATCH_PREFIX)  # copies left by a killed run
    scratch_owner.exit_on_signals()  # TERM, INT and HUP unwind through the finally below
    state = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=root))
    scratch_owner.mark(state)
    try:
        serve(state, args.port, args.source, args.clone)
    finally:
        shutil.rmtree(state, ignore_errors=True)


if __name__ == "__main__":
    main()
