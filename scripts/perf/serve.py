#!/usr/bin/env python3
"""Serve an instrumented gateway over a COPY of the operator's data (perf harness).

State comes from scripts/ui/live_data_copy.py: read-only SQLite backups of the
admin, receipts, media_jobs, responses and metrics databases, a config pointed
at the fake engine, and sign-in for the copy's owner and one member (set in the
copy only). This adds the media files (copied from the source checkout's image
cache) and runs scripts/perf/gateway_main.py so every response carries
statement, connection and subprocess counts. The source tree is never written.

    scripts/perf/serve.py --port 18710 [--source ~/applications/azizam] [--state DIR [--reuse]]

Prints {"ready": true, ...manifest} on one line, then serves until SIGTERM.
Ports: PORT and PORT+1 (fake engine). Both must be in 18000-18998.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

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


def serve(state: Path, port: int, source: Path, reuse: bool = False) -> None:
    engine_port = port + 1
    if reuse:  # same data as an earlier run, so before/after responses can be compared byte for byte
        manifest = json.loads((state / "perf-manifest.json").read_text(encoding="utf-8"))
        live_data_copy.write_config(state, port, engine_port)
        os.environ["AZIZAM_CONFIG_PATH"] = str(state / "config.yml")
    else:
        manifest = prepare(state, port, engine_port, source)
    env = live_data_copy.env_for(state, port, engine_port, Path(str(manifest["config"])))
    env["PERF_ROUTES_FILE"] = str(state / "routes.json")
    if os.environ.get("PERF_ATTRIBUTE") == "1":  # slow: charge every statement and process to its caller
        env["PERF_ATTRIBUTE"] = "1"
    commands = [
        ([sys.executable, "-m", "uvicorn", "scripts.ui.fake_engine:app", "--host", "127.0.0.1",
          "--port", str(engine_port), "--log-level", "warning"], engine_port),
        ([sys.executable, "-m", "scripts.perf.gateway_main", str(port)], port),
    ]
    children: list[subprocess.Popen] = []

    def stop(_signum=None, _frame=None):
        raise KeyboardInterrupt

    (state / "serve.pid").write_text(str(os.getpid()), encoding="utf-8")  # stop with: kill $(cat STATE/serve.pid)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
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
        manifest = {**manifest, "ready": True, "port": port, "state": str(state),
                    "routes_file": env["PERF_ROUTES_FILE"]}
        (state / "perf-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        print(json.dumps(manifest), flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
    except KeyboardInterrupt:
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
    parser.add_argument("--state", type=Path, default=None, help="state directory (default: fresh under ~/.tmp)")
    parser.add_argument("--reuse", action="store_true", help="serve the existing --state copy instead of copying again")
    args = parser.parse_args()
    if not 18000 <= args.port < 18998:
        raise SystemExit("port must be between 18000 and 18998")
    if args.port in (5000, 15000):
        raise SystemExit("refusing a production port")
    state = args.state or Path.home() / ".tmp" / f"azizam-perf-state-{args.port}"
    if not args.reuse:
        shutil.rmtree(state, ignore_errors=True)
        state.mkdir(parents=True)
    serve(state, args.port, args.source, args.reuse)


if __name__ == "__main__":
    main()
