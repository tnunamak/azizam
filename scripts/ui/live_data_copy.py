#!/usr/bin/env python3
"""Copy mode for the live-data browser harness (harness code, never production).

Builds an isolated state directory from a *copy* of the operator's data so a
browser sweep sees real models, apps, people, receipts and spend, without ever
touching the live gateway:

* Production databases are read with SQLite's online backup API from a
  read-only connection straight into the run's single state directory,
  which the caller deletes when the run ends. The source files are never
  written. The provider secrets key is NOT copied: the copy cannot read
  provider API keys, so Providers may say a key is unreadable.
* ``proxy_config.yml`` is copied and every backend address is rewritten to
  the deterministic fake engine, so no real engine, provider or host service
  is ever contacted. The gateway itself runs under ``fixture_gateway``, whose
  audit hook blocks any socket outside the declared loopback ports and any
  child process.
* Sessions are created in the COPY only: the copy's owner and its first
  member without an admin role get a known local password verifier.
* default (warm): the copied request history is moved forward so the newest
  request is one minute old, as on a gateway that has been serving traffic.
* ``restarted``: history keeps its real dates and every receipt and metric
  sample from the last ``RESTART_QUIET_S`` seconds is removed, so the fresh
  gateway has no recent health evidence, as right after a reboot.

Ports are chosen by the caller (18000-18998).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import yaml
from gateway import private_io

PASSWORD = "Sweep copy browser password 2026"
SOURCE_DEFAULT = Path.home() / "applications" / "azizam"
# gateway.availability.RECENT_EVIDENCE_WINDOW_S is 15 minutes; clear past it.
RESTART_QUIET_S = 30 * 60
DATABASES = ("admin.db", "receipts.db", "media_jobs.db", "responses.db", "metrics.db", "provider_spend.db")
# Runtime directories the gateway reads by relative path from its working directory.
SYMLINKED_REPO_ENTRIES = (
    "workflows", "registry", "manifests", "extensions", "engines", "services",
    "providers", "integrations", "skills",
)
COPIED_SOURCE_FILES = ("openai_model_aliases.generated.yml",)


def _backup(source: Path, target: Path) -> None:
    """Copy one SQLite database without writing to the source."""
    private_io.ensure_private_dir(target.parent)
    if not target.exists():
        private_io.create_private_file(target)
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)) as src, \
            closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def copy_data(source: Path, state: Path) -> None:
    """Copy the production databases and config into the run's state directory."""
    data = source / "data"
    for name in DATABASES:
        if (data / name).exists():
            _backup(data / name, state / name)
    private_io.ensure_private_dir(state)
    private_io.atomic_copy_file(source / "proxy_config.yml", state / "source_proxy_config.yml")
    for name in COPIED_SOURCE_FILES:
        if (source / name).exists():
            private_io.atomic_copy_file(source / name, state / name)


def _retarget(node, engine_port: int, engine_url: str):
    """Point every address in the copied config at the fake engine."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "host" and isinstance(value, str):
                out[key] = "127.0.0.1"
            elif key in {"port", "backend_port"} and isinstance(value, int):
                out[key] = engine_port
            elif isinstance(value, str) and value.startswith(("http://", "https://")):
                out[key] = engine_url
            else:
                out[key] = _retarget(value, engine_port, engine_url)
        return out
    if isinstance(node, list):
        return [_retarget(item, engine_port, engine_url) for item in node]
    return node


def write_config(state: Path, port: int, engine_port: int) -> Path:
    config = yaml.safe_load((state / "source_proxy_config.yml").read_text(encoding="utf-8"))
    engine_url = f"http://127.0.0.1:{engine_port}"
    backends = _retarget(config.get("backends") or {}, engine_port, engine_url)
    config["backends"] = backends
    for name in ("identity",):
        if name in config:
            config[name] = _retarget(config[name], engine_port, engine_url)
    config["proxy"] = {"host": "127.0.0.1", "port": port}
    config["auth"] = {"public_paths": ["/health"]}
    admin = dict(config.get("admin") or {})
    admin["db_path"] = str(state / "admin.db")
    admin["oidc"] = {"cookie": {"secure": False}}
    config["admin"] = admin
    images = dict(config.get("images") or {})
    images.update(cache_dir=str(state / "images"), vram_estimates_file=str(state / "vram.json"))
    config["images"] = images
    voices = dict(config.get("voices") or {})
    voices["dir"] = str(state / "voices")
    config["voices"] = voices
    path = state / "config.yml"
    private_io.atomic_write_text(path, yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    return path


def _prepare_sessions(state: Path) -> dict[str, str]:
    """Give the copy's owner and one real member a known password (copy only)."""
    from gateway import admin_store
    from gateway.local_owner import hash_password

    path = state / "admin.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        owners = {r["subject_id"] for r in conn.execute(
            "SELECT subject_id FROM admin_role_assignments WHERE role='owner' AND subject_kind='user' AND revoked_at IS NULL")}
        admins = {r["subject_id"] for r in conn.execute(
            "SELECT subject_id FROM admin_role_assignments WHERE subject_kind='user' AND revoked_at IS NULL")}
        users = [dict(r) for r in conn.execute("SELECT id,email,display_name FROM users WHERE email IS NOT NULL ORDER BY created_at")]
    owner = next((u for u in users if u["id"] in owners), None)
    member = next((u for u in users if u["id"] not in admins), None)
    if owner is None:
        raise SystemExit("The copy has no owner user to sign in as.")
    created_member = False
    if member is None:
        member = admin_store.upsert_oidc_user(
            "local", "sweep-member", email="sweep-member@example.test", display_name="Sweep Member", path=path)
        created_member = True
    verifier = hash_password(PASSWORD)
    # Local password sign-in is only offered for issuer "local" users. Re-home the
    # copy's real owner (an OIDC user) as local so the owner keeps their own data.
    with closing(sqlite3.connect(path)) as conn:
        for user in (owner, member):
            conn.execute("UPDATE users SET issuer='local', subject=email WHERE id=? AND issuer!='local'", (user["id"],))
        conn.commit()
    for user in (owner, member):
        admin_store.set_local_password_verifier(user["id"], verifier, path=path)
    return {
        "owner_email": owner["email"],
        "owner_name": owner.get("display_name") or "",
        "member_email": member["email"],
        "member_name": member.get("display_name") or "",
        "member_created_in_copy": str(created_member).lower(),
    }


def _rebase_receipts(state: Path) -> int:
    """Warm state: move the copied request history so the newest request is one minute old.

    A live gateway always has recent requests. The copy is hours old, so without
    this every service looks unmeasured, which is the restarted state, not the
    warm one. Only receipts move; media jobs and metric samples keep their dates.
    """
    with closing(sqlite3.connect(state / "receipts.db")) as conn:
        newest = conn.execute("SELECT MAX(created_at) FROM receipts").fetchone()[0]
        if newest is None:
            return 0
        delta = int(time.time()) - 60 - int(newest)
        if delta <= 0:
            return 0
        rows = conn.execute("SELECT request_id, receipt_json FROM receipts").fetchall()
        updates = []
        for request_id, raw in rows:
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            for key in ("accepted_at_ms", "updated_at_ms", "created_at_ms"):
                if isinstance(payload.get(key), (int, float)):
                    payload[key] = int(payload[key]) + delta * 1000
            updates.append((json.dumps(payload), request_id))
        conn.executemany("UPDATE receipts SET receipt_json=? WHERE request_id=?", updates)
        conn.execute("UPDATE receipts SET created_at = created_at + ?", (delta,))
        conn.commit()
        return len(updates)


def _quiet_recent_evidence(state: Path) -> dict[str, int]:
    """Drop receipts and samples newer than the restart window."""
    cutoff = int(time.time()) - RESTART_QUIET_S
    removed = {}
    with closing(sqlite3.connect(state / "receipts.db")) as conn:
        removed["receipts"] = conn.execute("DELETE FROM receipts WHERE created_at>?", (cutoff,)).rowcount
        conn.commit()
    with closing(sqlite3.connect(state / "metrics.db")) as conn:
        for table in ("samples", "samples_1min", "samples_1hour"):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            stamp = next((c for c in ("ts", "timestamp", "bucket", "t", "time") if c in columns), None)
            if stamp:
                removed[table] = conn.execute(f"DELETE FROM {table} WHERE {stamp}>?", (cutoff,)).rowcount
        conn.commit()
    return removed


def prepare(state: Path, port: int, engine_port: int, *, source: Path, restarted: bool) -> dict[str, object]:
    private_io.ensure_private_dir(state)
    copy_data(source, state)
    for name in ("images", "voices", "home"):
        private_io.ensure_private_dir(state / name)
    repo = Path(__file__).resolve().parents[2]
    for name in SYMLINKED_REPO_ENTRIES:
        if (repo / name).exists() and not (state / name).exists():
            (state / name).symlink_to(repo / name)
    config = write_config(state, port, engine_port)
    os.environ["AZIZAM_CONFIG_PATH"] = str(config)
    sessions = _prepare_sessions(state)
    quiet = _quiet_recent_evidence(state) if restarted else {}
    rebased = 0 if restarted else _rebase_receipts(state)
    return {
        "base_url": f"http://127.0.0.1:{port}",
        "mode": "copy",
        "state": "restarted" if restarted else "default",
        "password": PASSWORD,
        "copied_at": int(time.time()),
        "restart_quiet_removed": quiet,
        "receipts_rebased": rebased,
        "config": str(config),
        **sessions,
    }


def env_for(state: Path, port: int, engine_port: int, config: Path) -> dict[str, str]:
    home = state / "home"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AZIZAM_CONFIG_PATH": str(config),
        "AZIZAM_GRACEFUL_SHUTDOWN_S": "1",
        "AZIZAM_LITELLM_BASE_URL": f"http://127.0.0.1:{engine_port}",
        "AZIZAM_ADMIN_SESSION_KEY": "sweep-copy-session-signing-key-not-production",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "AZIZAM_IMAGE_CACHE_DIR": str(state / "images"),
        "AZIZAM_PROVIDER_LEDGER_DB": str(state / "provider_spend.db"),
        "AZIZAM_VRAM_ESTIMATES_PATH": str(state / "vram.json"),
    }
    for name, filename in {
        "AZIZAM_ADMIN_DB": "admin.db", "AZIZAM_METRICS_DB": "metrics.db",
        "AZIZAM_RESPONSES_DB": "responses.db", "AZIZAM_RECEIPTS_DB": "receipts.db",
        "AZIZAM_MEDIA_JOBS_DB": "media_jobs.db",
    }.items():
        env[name] = str(state / filename)
    return env


def write_manifest(manifest: dict[str, object], state: Path) -> None:
    private_io.atomic_write_text(state / "live-data-manifest.json", json.dumps(manifest))
    target = os.environ.get("LIVE_DATA_MANIFEST_PATH")
    if target:
        private_io.atomic_write_text(target, json.dumps(manifest))
