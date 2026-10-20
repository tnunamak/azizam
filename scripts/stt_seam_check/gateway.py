"""A disposable gateway on a loopback port, one per source tree, pointed (through a tap) at an existing engine."""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

SEED = r"""
import sqlite3, sys, time
from pathlib import Path
from gateway import accounts, admin_store
from gateway.access_plans import CAPABILITIES
from gateway.runtime_auth import hash_api_key
db, key = sys.argv[1:3]
path = Path(db)
admin_store.init_db(path)
accounts.initialize(path)
now = int(time.time())
plan = admin_store.create_access_plan({
    "name": "seam check", "can_use": {c: {"items": "all"} for c in CAPABILITIES},
    "direct_engine_access": {"engines": [], "operations": []}, "paid_providers": [], "provider_fallback": False,
    "caps": {"usd_per_request": None, "usd_per_day": None, "usd_per_month": None}, "can_load_models": True,
    "receipt_detail": "full", "media_metadata": "none"}, now=now, path=path)
user = admin_store.upsert_oidc_user("local", "seam", email="seam@example.test", display_name="Seam check", now=now, path=path)
with sqlite3.connect(path) as conn:
    conn.execute("INSERT INTO account_access(user_id,plan_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET plan_id=excluded.plan_id",
                 (user["id"], plan["id"]))
app = admin_store.create_app("seam check", kind="personal", owner_user_id=user["id"], access_plan="person", now=now, path=path)
admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=hash_api_key(key),
                                   prefix="seamchk", display_name="seam check", now=now, path=path)
"""


class Gateway:
    """Start ``proxy:app`` from ``repo`` with its own databases and one transcription backend at ``engine_port``."""

    def __init__(self, repo: Path, state: Path, port: int, engine_port: int, chunking: dict, model: str,
                 python: str = sys.executable) -> None:
        if not 18000 <= port < 19000:
            raise SystemExit("the check's gateway must use a port from 18000 to 18999 (never the live ports)")
        self.repo, self.state, self.port, self.python = repo, state, port, python
        self.key = "sk-seamchk-" + secrets.token_urlsafe(24)
        self.model = model
        state.mkdir(parents=True, exist_ok=True)
        disabled = {name: {"enabled": False, "host": "127.0.0.1", "port": 9} for name in ("kobold", "comfyui", "embeddings", "tts", "stt")}
        document = {
            "proxy": {"host": "127.0.0.1", "port": port},
            "backends": {
                **disabled, "litellm": {"enabled": False},
                "stt-engine": {
                    "host": "127.0.0.1", "port": engine_port, "capabilities": ["audio", "transcriptions"],
                    "default_model": model, "transforms": [{"kind": "drop-params", "phase": "response", "drop": ["timing"]}],
                    "chunking": {**chunking, "spool_dir": str(state / "spool")},
                },
            },
            "admin": {"oidc": {"cookie": {"secure": False}}},
            # The engine is reached by the name callers use, as on a real gateway: one model alias for one service.
            "model_aliases": {"transcriptions": {"engines": {"engine": {
                "display_name": "engine under test", "service": "stt-engine", "model": model, "upload_format": "wav16k"}}}},
            "model_compatibility": {"enabled": True, "defaults": {"transcriptions": model}},
        }
        (state / "config.yml").write_text(yaml.safe_dump(document))
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(state / "home"), "PYTHONPATH": str(repo),
            "PYTHONDONTWRITEBYTECODE": "1", "AZIZAM_CONFIG_PATH": str(state / "config.yml"), "AZIZAM_MANAGE_LITELLM": "false",
            "AZIZAM_GRACEFUL_SHUTDOWN_S": "1", "AZIZAM_PORT": str(port),
            "AZIZAM_ADMIN_SESSION_KEY": "stt-seam-check-session-key-not-production-0123456789",
        }
        for name, filename in {"AZIZAM_ADMIN_DB": "admin.db", "AZIZAM_METRICS_DB": "metrics.db", "AZIZAM_RESPONSES_DB": "responses.db",
                               "AZIZAM_RECEIPTS_DB": "receipts.db", "AZIZAM_MEDIA_JOBS_DB": "media_jobs.db"}.items():
            self.env[name] = str(state / filename)
        (state / "home").mkdir(exist_ok=True)
        self.process: subprocess.Popen | None = None

    def start(self, timeout: float = 120.0) -> None:
        subprocess.run([self.python, "-c", SEED, self.env["AZIZAM_ADMIN_DB"], self.key], cwd=self.state, env=self.env, check=True)
        log = (self.state / "gateway.log").open("w")
        self.process = subprocess.Popen(
            [self.python, "-m", "uvicorn", "proxy:app", "--host", "127.0.0.1", "--port", str(self.port), "--timeout-keep-alive", "3600"],
            cwd=self.state, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        log.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"gateway exited early; see {self.state / 'gateway.log'}")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1).close()
                return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("gateway readiness timed out")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
