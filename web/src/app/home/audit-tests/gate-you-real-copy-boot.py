#!/usr/bin/env python3
"""Prepare and optionally run the gate-you real-data copy harness.

This script only mutates the copied data dir under .ui-e2e-state/gate-you-real.
It does not read the original live databases or secret files.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[5]
LIVE_CHECKOUT = Path("/home/tnunamak/applications/openai-proxy")
STATE = ROOT / ".ui-e2e-state" / "gate-you-real"
DATA = STATE / "data"
PRIVATE = STATE / "runtime"
OUT = Path("/home/tnunamak/.tmp/gw-product/gate/v3/gw-gate-you")
GATEWAY_PORT = int(os.environ.get("GATE_YOU_GATEWAY_PORT", "18431"))
SIDECAR_PORT = int(os.environ.get("GATE_YOU_SIDECAR_PORT", "18432"))
if not (18000 <= GATEWAY_PORT <= 18999 and 18000 <= SIDECAR_PORT <= 18999) or GATEWAY_PORT == SIDECAR_PORT:
    raise SystemExit("use two distinct throwaway ports between 18000 and 18999")
SAVED_PUBLIC_URL = "https://gate-you-copy.example.test"
PASSWORD = "Gate You copy member password 2026"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _redacted_summary(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _copy_media_cache() -> dict[str, int]:
    source = LIVE_CHECKOUT / "cache" / "images"
    target = PRIVATE / "cache" / "images"
    target.mkdir(parents=True, exist_ok=True)
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".mp4", ".flac", ".ogg"}
    counts = {"copied": 0, "skipped": 0}
    if not source.is_dir():
        return counts
    for item in source.iterdir():
        if not item.is_file() or item.name.startswith("."):
            counts["skipped"] += 1
            continue
        if item.suffix.lower() not in allowed and not item.name.endswith(".meta.json"):
            counts["skipped"] += 1
            continue
        shutil.copy2(item, target / item.name)
        counts["copied"] += 1
    return counts


def _first_profile_id(db: Path) -> str | None:
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT id FROM profiles ORDER BY created_at LIMIT 1").fetchone()
        return str(row[0]) if row else None


def _owner_user_id(db: Path) -> str:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """
            SELECT u.id
            FROM users u
            JOIN admin_role_assignments r
              ON r.subject_kind='user'
             AND r.subject_id=u.id
             AND r.revoked_at IS NULL
            WHERE r.role='owner'
            ORDER BY u.created_at
            LIMIT 1
            """
        ).fetchone()
    if not row:
        raise RuntimeError("copied admin.db has no active owner user")
    return str(row[0])


def _member_user(db: Path) -> dict[str, str]:
    from gateway import accounts, admin_store

    accounts.initialize(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT u.id, u.email, u.display_name
            FROM users u
            LEFT JOIN account_access a
              ON a.user_id=u.id
            LEFT JOIN admin_role_assignments r
              ON r.subject_kind='user'
             AND r.subject_id=u.id
             AND r.revoked_at IS NULL
             AND r.role IN ('owner','admin','superadmin')
            WHERE r.id IS NULL
              AND a.removed_at IS NULL
              AND a.disabled_at IS NULL
              AND u.email IS NOT NULL
              AND u.email != ''
              AND u.issuer='local'
              AND u.subject NOT LIKE 'service%%'
              AND u.subject NOT LIKE '%%service%%'
            ORDER BY u.created_at
            LIMIT 1
            """
        ).fetchone()
        if row:
            return {"id": row["id"], "email": row["email"], "name": row["display_name"] or row["email"], "created": "false"}

    profile_id = _first_profile_id(db)
    user = admin_store.upsert_oidc_user(
        "local",
        "gate-you-copy-member",
        email="gate-you-copy-member@example.test",
        display_name="Gate You Copy Member",
        path=db,
    )
    verifier = accounts.hash_password(PASSWORD)
    admin_store.set_local_password_verifier(user["id"], verifier, path=db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO account_access(user_id, profile_id, require_registered_session) VALUES(?,?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET profile_id=excluded.profile_id, require_registered_session=1",
            (user["id"], profile_id),
        )
        conn.commit()
    admin_store.ensure_personal_app_for_user(
        user["id"],
        name="Gate You Copy Member",
        default_profile_id=profile_id,
        path=db,
    )
    return {"id": user["id"], "email": user["email"], "name": user["display_name"], "created": "true"}


def _write_sessions(db: Path, signing_key: str, owner_id: str, member: dict[str, str]) -> None:
    from gateway import accounts
    from gateway.admin_oidc import CookieConfig, encode_session_cookie, session_from_claims

    cookie = CookieConfig(secure=False)
    key = signing_key.encode()
    sessions = {}
    for role, user_id, email in (
        ("owner", owner_id, "owner-copy-session@example.test"),
        ("member", member["id"], member["email"]),
    ):
        session = session_from_claims({"sub": user_id, "email": email}, role=("owner" if role == "owner" else "none"), cookie=cookie)
        accounts.register_session(user_id, session.session_id, int(session.expires_at), path=db)
        sessions[role] = {
            "name": cookie.name,
            "value": encode_session_cookie(session, signing_key=key),
            "domain": "127.0.0.1",
            "path": "/",
            "httpOnly": True,
            "secure": False,
            "sameSite": "Lax",
        }
    secret_path = PRIVATE / "session-cookies.json"
    secret_path.write_text(json.dumps(sessions), encoding="utf-8")
    secret_path.chmod(0o600)


def prepare() -> dict[str, Any]:
    if not DATA.is_dir():
        raise RuntimeError(f"missing copied data dir: {DATA}")
    if not (ROOT / "web" / "out" / "index.html").is_file():
        raise RuntimeError("web/out is missing; run `cd web && npm run build` before starting")
    PRIVATE.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    from gateway import admin_store
    from gateway.admin_runtime import generate_admin_api_key
    from gateway.runtime_auth import hash_api_key
    from gateway.settings_api import instance_settings

    db = DATA / "admin.db"
    owner_id = _owner_user_id(db)
    recovery = generate_admin_api_key()
    cred = admin_store.create_credential_hash(
        subject_kind="user",
        subject_id=owner_id,
        hash=hash_api_key(recovery),
        prefix=recovery[:18],
        display_name="Gate You v3 recovery (copy only)",
        expires_at=int(time.time()) + 10800,
        path=db,
    )
    recovery_file = PRIVATE / "owner-recovery-key"
    recovery_file.write_text(recovery, encoding="utf-8")
    recovery_file.chmod(0o600)

    registry_state = admin_store.get_service_registry_with_generation(path=db)
    registry = registry_state["value"]
    for sid, service in registry.get("services", {}).items():
        config = service.get("config") or {}
        if config.get("gateway_lifecycle"):
            config["gateway_lifecycle_base_url"] = f"http://127.0.0.1:{GATEWAY_PORT}/internal/litellm/local"
        if sid == "openai-proxy":
            config["host"] = "127.0.0.1"
            config["port"] = GATEWAY_PORT
    if "litellm" in registry.get("services", {}):
        registry["services"]["litellm"].setdefault("config", {})["default_base_url"] = f"http://127.0.0.1:{SIDECAR_PORT}"
    admin_store.write_service_registry(
        registry,
        expected_generation=registry_state["operator_settings_generation"],
        updated_by="gate-you-copy-boot",
        path=db,
    )

    instance = instance_settings(db)
    instance["base_url"] = SAVED_PUBLIC_URL
    admin_store.put_operator_setting("instance_presentation", instance, updated_by="gate-you-copy-boot", path=db)

    config = yaml.safe_load((ROOT / "proxy_config.yml").read_text(encoding="utf-8"))
    config["proxy"]["host"] = "127.0.0.1"
    config["proxy"]["port"] = GATEWAY_PORT
    config.setdefault("admin", {}).setdefault("oidc", {}).setdefault("cookie", {}).update({"secure": False, "domain": None})
    config["admin"]["db_path"] = str(db)
    config.setdefault("images", {})["cache_dir"] = str(PRIVATE / "cache" / "images")
    signing_key = "gate-you-copy-session-" + secrets.token_urlsafe(40)
    config["admin"]["session_signing_key"] = signing_key
    config_path = PRIVATE / "gate-config.yml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    config_path.chmod(0o600)

    member = _member_user(db)
    _write_sessions(db, signing_key, owner_id, member)
    media_counts = _copy_media_cache()

    manifest = {
        "gateway_port": GATEWAY_PORT,
        "sidecar_port": SIDECAR_PORT,
        "saved_public_url": SAVED_PUBLIC_URL,
        "owner_user_id": owner_id,
        "owner_recovery_credential_id": cred["id"],
        "member_user_id": member["id"],
        "member_created_on_copy": member["created"] == "true",
        "media_cache_files_copied": media_counts["copied"],
        "media_cache_files_skipped": media_counts["skipped"],
        "out_dir": str(OUT),
        "config_path": str(config_path),
    }
    (PRIVATE / "prepare-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _env(config_path: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(PRIVATE / "home"),
        "XDG_CONFIG_HOME": str(PRIVATE / "xdg-config"),
        "XDG_CACHE_HOME": str(PRIVATE / "xdg-cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(env["XDG_CONFIG_HOME"]).mkdir(parents=True, exist_ok=True)
    Path(env["XDG_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "OPENAI_PROXY_PORT": str(GATEWAY_PORT),
            "AI_GATEWAY_ADMIN_DB": str(DATA / "admin.db"),
            "AI_GATEWAY_CONFIG_PATH": str(config_path),
            "AI_GATEWAY_LITELLM_BASE_URL": f"http://127.0.0.1:{SIDECAR_PORT}",
            "AI_GATEWAY_LITELLM_API_KEY": "sk-copy-" + secrets.token_urlsafe(32),
            "AI_GATEWAY_INTERNAL_LITELLM_KEY": secrets.token_urlsafe(32),
            "AI_GATEWAY_METRICS_DB": str(DATA / "metrics.db"),
            "AI_GATEWAY_RESPONSES_DB": str(DATA / "responses.db"),
            "AI_GATEWAY_RECEIPTS_DB": str(DATA / "receipts.db"),
            "AI_GATEWAY_MEDIA_JOBS_DB": str(DATA / "media_jobs.db"),
            "AGW_IMAGE_CACHE_DIR": str(PRIVATE / "cache" / "images"),
            "IMAGE_MEDIA_JOB_RESUME_ON_START": "false",
            "VIDEO_MEDIA_JOB_RESUME_ON_START": "false",
            "AUDIO_MEDIA_JOB_RESUME_ON_START": "false",
            "PYTHONPATH": str(ROOT),
        }
    )
    env["LITELLM_MASTER_KEY"] = env["AI_GATEWAY_LITELLM_API_KEY"]
    return env


def _bwrap_base() -> list[str]:
    uid = os.getuid()
    return [
        "bwrap",
        "--die-with-parent",
        "--bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", f"/run/user/{uid}",
        "--ro-bind", str(LIVE_CHECKOUT), str(LIVE_CHECKOUT),
        "--chdir", str(ROOT),
        "--",
    ]


def _wait(url: str, proc: subprocess.Popen[bytes], label: str, timeout_s: int = 120) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{label} exited before readiness; inspect sanitized logs")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise RuntimeError(f"{label} readiness timeout")


def start() -> None:
    manifest = prepare()
    config_path = Path(manifest["config_path"])
    env = _env(config_path)
    base = _bwrap_base()
    logs = []
    procs: list[subprocess.Popen[bytes]] = []

    def stop(_signum=None, _frame=None):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        gateway_log = (PRIVATE / "gateway.log").open("wb")
        logs.append(gateway_log)
        procs.append(subprocess.Popen(base + [str(ROOT / "venv/bin/python"), "proxy.py"], cwd=ROOT, env=env, stdout=gateway_log, stderr=subprocess.STDOUT))
        _wait(f"http://127.0.0.1:{GATEWAY_PORT}/health", procs[0], "gateway")

        artifact = DATA / "litellm.config.yaml"
        if artifact.exists():
            artifact.write_text(
                artifact.read_text(encoding="utf-8").replace("http://127.0.0.1:5000/internal/", f"http://127.0.0.1:{GATEWAY_PORT}/internal/"),
                encoding="utf-8",
            )
        sidecar_log = (PRIVATE / "litellm.log").open("wb")
        logs.append(sidecar_log)
        procs.append(
            subprocess.Popen(
                base
                + [
                    str(ROOT / "venv/bin/python"),
                    "-m",
                    "gateway.provider_sidecar",
                    "run",
                    "--db",
                    str(DATA / "admin.db"),
                    "--",
                    str(ROOT / "venv/bin/litellm"),
                    "--config",
                    str(artifact),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(SIDECAR_PORT),
                ],
                cwd=ROOT,
                env=env,
                stdout=sidecar_log,
                stderr=subprocess.STDOUT,
            )
        )
        _wait(f"http://127.0.0.1:{SIDECAR_PORT}/health/liveliness", procs[1], "sidecar")
        _redacted_summary({"ready": True, "gateway_port": GATEWAY_PORT, "sidecar_port": SIDECAR_PORT, "out_dir": str(OUT)})
        for line in sys.stdin:
            if line.strip() == "stop":
                break
    except KeyboardInterrupt:
        pass
    finally:
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
        for proc in reversed(procs):
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in logs:
            log.close()
        _redacted_summary({"stopped": True, "processes": len(procs)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true", help="start gateway and sidecar after preparing")
    args = parser.parse_args()
    if args.start:
        start()
    else:
        _redacted_summary({"prepared": True, **prepare()})


if __name__ == "__main__":
    main()
