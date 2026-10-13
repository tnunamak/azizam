#!/usr/bin/env python3
"""Prove decision requests through an isolated high-port gateway and real engine.

The engine must be a separately owned throwaway server. This script never starts
systemd units, reads operator configuration, or uses provider credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import statistics
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import yaml

ROUTE = dict.fromkeys(("chat", "image", "video", "speech", "transcribe", "music", "search", "reminder"))
CASES = [
    ("Make me a picture of a red fox in the snow.", "image"),
    ("Turn this voice memo into text please", "transcribe"),
    ("Remind me at 6pm to take the trash out", "reminder"),
    ("Write a lo-fi beat with rain sounds, about a minute long", "music"),
    ("Who won the game last night?", "search"),
    ("Read this paragraph out loud in a calm voice", "speech"),
    ("This is the third time the order arrived broken. I want my money back now.", "negative"),
    ("Thanks so much, the new build works perfectly!", "positive"),
    ("rm -rf / --no-preserve-root", True),
    ("ls -la ~/Downloads", False),
    ("Hey, can you check whether my flight tomorrow is still on time?", True),
    ("What is 2 + 2?", False),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-port", type=int, required=True)
    parser.add_argument("--gateway-port", type=int, default=18881)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not all(18000 <= port <= 18999 for port in (args.engine_port, args.gateway_port)):
        parser.error("proof listeners must use ports 18000 through 18999")
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "scripts"))
    import scratch_owner
    scratch_owner.sweep(repo / "venv", "decisions-proof-")  # what a SIGKILLed proof left
    scratch_owner.exit_on_signals()
    state = Path(tempfile.mkdtemp(prefix="decisions-proof-", dir=repo / "venv"))
    scratch_owner.mark(state)
    key = secrets.token_urlsafe(24)
    config = {
        "proxy": {"host": "127.0.0.1", "port": args.gateway_port},
        "auth": {"enabled": True, "keys": []},
        "backends": {"decisions": {"host": "127.0.0.1", "port": args.engine_port,
                     "enabled": True, "default_model": "decider-2b-v11", "capabilities": ["decisions"],
                     "lifecycle": {"driver": "external"}}},
        "model_aliases": {"decisions": {
            "engines": {"local": {"service": "decisions", "model": "decider-2b-v11", "calibrated": True,
                        "max_options": 255, "max_levels": 10, "context_tokens": 4096}},
            "stable": {"decide": ["local"]}, "openai": [{"match": "jev-*", "to": "decide"}],
        }},
    }
    config_path = state / "config.yml"
    config_path.write_text(yaml.safe_dump(config))
    env = {"PATH": os.environ["PATH"], "HOME": str(state), "PYTHONPATH": str(repo),
           "AZIZAM_CONFIG_PATH": str(config_path), "PYTHONDONTWRITEBYTECODE": "1"}
    for name, filename in (("ADMIN", "admin"), ("METRICS", "metrics"), ("RESPONSES", "responses"),
                           ("RECEIPTS", "receipts"), ("MEDIA_JOBS", "media_jobs")):
        env[f"AZIZAM_{name}_DB"] = str(state / f"{filename}.db")
    # Bootstrap only this temporary App with a generated key that is never printed.
    launcher = state / "launch.py"
    launcher.write_text('''import hashlib, os, sys
import proxy
from gateway import admin_store, receipt_store
owner = admin_store.create_service_account("proof", path=proxy.ADMIN_STORE.path)
app = admin_store.create_app("proof", kind="service", owner_service_account_id=owner["id"], path=proxy.ADMIN_STORE.path)
key = sys.stdin.readline().strip()
admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash="sha256:"+hashlib.sha256(key.encode()).hexdigest(), prefix="proof", app_id=app["id"], path=proxy.ADMIN_STORE.path)
receipt_store.init_db()
# This disposable process has no sidecar and never starts any engine.
proxy._litellm_runtime_config = lambda: __import__('types').SimpleNamespace(enabled=False)
import uvicorn
uvicorn.run(proxy.app, host="127.0.0.1", port=int(sys.argv[1]), lifespan="off", log_level="warning")
''')
    log = (state / "gateway.log").open("w")
    process = subprocess.Popen([sys.executable, str(launcher), str(args.gateway_port)], cwd=repo,
                               env=env, stdin=subprocess.PIPE, stdout=log, stderr=log, text=True)
    process.stdin.write(key + "\n")
    process.stdin.close()
    base = f"http://127.0.0.1:{args.gateway_port}"

    def call(path, body=None):
        request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                                         data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read()), dict(response.headers)

    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"throwaway gateway exited {process.returncode}; last log lines: {(state / 'gateway.log').read_text()[-2000:]}")
            try:
                models, _ = call("/typesafe/v1/models")
                assert any(model["name"] == "decide" for model in models["models"])
                break
            except (OSError, AssertionError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
        results = []
        for index, (state_text, expected) in enumerate(CASES):
            if index < 6:
                question = {"type": "choice", "instructions": "Which capability does this request need?", "criteria": ROUTE}
            elif index < 8:
                question = {"type": "choice", "instructions": "What is the sentiment?", "criteria": dict.fromkeys(("positive", "neutral", "negative"))}
            else:
                question = {"type": "noul", "instructions": "Is this shell command destructive?" if index < 10 else "Does the user ask for an action that needs live, current information?"}
            request = {"model": "jev-latest", "state": state_text, "questions": {"q": question}}
            call("/v1/systemone", request)
            started = time.perf_counter()
            answer, headers = call("/typesafe/v1/systemone", request)
            elapsed = (time.perf_counter() - started) * 1000
            actual = answer["answers"]["q"].get("choice") if question["type"] == "choice" else answer["answers"]["q"]["noul"] >= .5
            assert answer["model"] == "decider-2b-v11"
            assert headers["x-azizam-cost-usd"] == "0.0"
            assert headers["x-azizam-engine"] in ("cpu", "gpu")
            results.append({"case": index + 1, "expected": expected, "actual": actual, "correct": actual == expected,
                            "latency_ms": round(elapsed, 2), "engine": headers["x-azizam-engine"],
                            "request_id": headers["x-azizam-request-id"], "usage": answer["usage"]})
        models, _ = call("/v1/models")
        assert "decide" in {model["id"] for model in models["data"]}
        capabilities, _ = call("/gateway/v1/capabilities")
        result = {"correct": sum(row["correct"] for row in results), "count": len(results),
                  "median_ms": round(statistics.median(row["latency_ms"] for row in results), 2),
                  "max_ms": max(row["latency_ms"] for row in results), "requests": results,
                  "gateway_pid": process.pid, "state_dir": str(state), "capabilities": capabilities}
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({key: result[key] for key in ("correct", "count", "median_ms", "max_ms", "state_dir")}))
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()
        shutil.rmtree(state, ignore_errors=True)  # holds the throwaway App's database


if __name__ == "__main__":
    main()
