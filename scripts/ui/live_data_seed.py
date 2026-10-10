#!/usr/bin/env python3
"""Seed and serve an isolated live-data browser fixture."""
from __future__ import annotations

import sys as _compat_sys
from pathlib import Path as _CompatPath
_compat_sys.path.insert(0, str(_CompatPath(__file__).resolve().parents[2]))
from gateway.compat import get_env

import argparse
import contextlib
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
import zlib

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
OWNER_EMAIL = "owner-live-data@example.test"
MEMBER_EMAIL = "member-live-data@example.test"
PASSWORD = "Live data browser password 2026"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _write_config(state: Path, port: int, engine_port: int) -> Path:
    config = state / "config.yml"
    config.write_text(
        f"""proxy:
  host: 127.0.0.1
  port: {port}
auth:
  enabled: true
  public_paths: [/health]
  lan_bypass_enabled: true
  lan_bypass_cidrs: ["127.0.0.0/8", "::1/128"]
backends:
  tabby: {{enabled: false, host: 127.0.0.1, port: {engine_port}}}
  kobold: {{enabled: false, host: 127.0.0.1, port: {engine_port}}}
  comfyui: {{enabled: false, host: 127.0.0.1, port: {engine_port}}}
  embeddings: {{enabled: false, host: 127.0.0.1, port: {engine_port}}}
  litellm: {{enabled: true, base_url: "http://127.0.0.1:{engine_port}", host: 127.0.0.1, port: {engine_port}}}
admin:
  oidc:
    cookie:
      secure: false
model_aliases:
  chat:
    stable:
      fast: demo-chat
images:
  cache_dir: {state / "images"}
  signed_url_ttl: 3600
  vram_estimates_file: {state / "vram.json"}
""",
        encoding="utf-8",
    )
    return config


def _env(state: Path, port: int, engine_port: int, config: Path) -> dict[str, str]:
    home = state / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AZIZAM_CONFIG_PATH": str(config),
        "AZIZAM_GRACEFUL_SHUTDOWN_S": "1",
        "AZIZAM_LITELLM_BASE_URL": f"http://127.0.0.1:{engine_port}",
        "AZIZAM_ADMIN_SESSION_KEY": "live-data-session-signing-key-not-production",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "AZIZAM_IMAGE_CACHE_DIR": str(state / "images"),
    }
    for name, filename in {
        "AZIZAM_ADMIN_DB": "admin.db",
        "AZIZAM_METRICS_DB": "metrics.db",
        "AZIZAM_RESPONSES_DB": "responses.db",
        "AZIZAM_RECEIPTS_DB": "receipts.db",
        "AZIZAM_MEDIA_JOBS_DB": "media_jobs.db",
    }.items():
        env[name] = str(state / filename)
    return env


def _sha_asset(cache: Path, content: bytes, suffix: str, owner_hashes: list[str], request_id: str) -> str:
    digest = hashlib.sha256(content).hexdigest()
    kind = "aud" if suffix == ".wav" else "img"
    asset_id = f"{kind}_{digest}"
    (cache / f"{digest}{suffix}").write_bytes(content)
    (cache / f"{digest}.meta.json").write_text(
        json.dumps({"asset_id": asset_id, "owner_key_hashes": owner_hashes, "updated_at": int(time.time())}),
        encoding="utf-8",
    )
    return asset_id


def _png_rgb(red: int, green: int, blue: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, red, green, blue])))
        + chunk(b"IEND", b"")
    )


def _wav_bytes(seconds: float = 3.0, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = [
            struct.pack("<h", int(12000 * ((i % 80) / 40 - 1)))
            for i in range(int(seconds * rate))
        ]
        wav.writeframes(b"".join(frames))
    return buffer.getvalue()


def seed(state: Path, port: int) -> dict[str, object]:
    state.mkdir(parents=True, exist_ok=True)
    (state / "images").mkdir(exist_ok=True)
    engine_port = port + 1
    config = _write_config(state, port, engine_port)
    os.environ.update(_env(state, port, engine_port, config))

    from gateway import accounts, admin_store, media_jobs, receipt_store
    from gateway.local_owner import hash_password as local_hash_password
    from gateway.receipts import RouteReceipt
    from gateway.runtime_auth import hash_api_key

    admin_path = Path(get_env("AZIZAM_ADMIN_DB"))
    receipts_path = Path(get_env("AZIZAM_RECEIPTS_DB"))
    jobs_path = Path(get_env("AZIZAM_MEDIA_JOBS_DB"))
    admin_store.init_db(admin_path)
    accounts.initialize(admin_path)
    receipt_store.init_db(receipts_path)
    media_jobs.init_db(jobs_path)

    profile = admin_store.create_profile("Live data plan", {"id": "live-data", "protocols": ["openai"], "models": ["demo-chat"], "backends": ["litellm"]}, path=admin_path)
    owner = admin_store.upsert_oidc_user("local", "owner-live-data", email=OWNER_EMAIL, display_name="Owner", path=admin_path)
    member = admin_store.upsert_oidc_user("local", "member-live-data", email=MEMBER_EMAIL, display_name="Member", path=admin_path)
    for user in (owner, member):
        admin_store.set_local_password_verifier(user["id"], local_hash_password(PASSWORD), path=admin_path)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=owner["id"], path=admin_path)

    owner_app = admin_store.create_app("Owner desktop", kind="personal", owner_user_id=owner["id"], default_profile_id=profile["id"], path=admin_path)
    member_app = admin_store.create_app("Member phone", kind="personal", owner_user_id=member["id"], default_profile_id=profile["id"], path=admin_path)
    svc = admin_store.ensure_service_account("daisy-service", path=admin_path)
    service_app = admin_store.create_app("Daisy", kind="service", owner_service_account_id=svc["id"], default_profile_id=profile["id"], path=admin_path)
    owner_key = "opk_live_owner_fixture_key"
    member_key = "opk_live_member_fixture_key"
    owner_hash = hash_api_key(owner_key)
    member_hash = hash_api_key(member_key)
    admin_store.create_credential_hash(subject_kind="app", subject_id=owner_app["id"], app_id=owner_app["id"], hash=owner_hash, prefix="opk_live_owner", profile_id=profile["id"], path=admin_path)
    admin_store.create_credential_hash(subject_kind="app", subject_id=member_app["id"], app_id=member_app["id"], hash=member_hash, prefix="opk_live_member", profile_id=profile["id"], path=admin_path)

    cache = state / "images"
    service_asset = _sha_asset(cache, PNG_1X1, ".png", [], "req_service_media")
    owner_asset = _sha_asset(cache, _png_rgb(50, 120, 200), ".png", [owner_hash], "req_owner_media")
    local_asset = _sha_asset(cache, _wav_bytes(), ".wav", [], "req_local_media")
    member_asset = _sha_asset(cache, PNG_1X1 + b"member", ".png", [member_hash], "req_member_media")

    now_ms = int(time.time() * 1000)
    for request_id, app_id, intent, path, model in [
        ("req_service_media", service_app["id"], "image.generate", "/v1/images/generations", "image"),
        ("req_owner_media", owner_app["id"], "image.generate", "/v1/images/generations", "image"),
        ("req_local_media", None, "audio.speech", "/v1/audio/speech", "voice"),
        ("req_member_media", member_app["id"], "image.generate", "/v1/images/generations", "image"),
    ]:
        receipt = RouteReceipt(protocol="openai", intent=intent, path=path, request_id=request_id, app_id=app_id, requested_model=model, resolved_model=model, status="succeeded")
        receipt.accepted_at_ms = now_ms
        receipt.updated_at_ms = now_ms + 10
        if app_id is None:
            receipt.source_kind = "local-network"
            receipt.source_label = "Local network · no key"
            receipt.client_hint = "Open WebUI"
        receipt_store.record(receipt, created_at=now_ms // 1000, path=receipts_path)
    media_jobs.create(media_jobs.MediaJob(request_id="req_service_media", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100, assets=[{"asset_id": service_asset, "media_type": "image/png"}], app_id=service_app["id"]), path=jobs_path)
    media_jobs.create(media_jobs.MediaJob(request_id="req_owner_media", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100, assets=[{"asset_id": owner_asset, "media_type": "image/png"}], app_id=owner_app["id"]), path=jobs_path)
    media_jobs.create(media_jobs.MediaJob(request_id="req_local_media", intent="audio.speech", status=media_jobs.STATUS_SUCCEEDED, progress=100, assets=[{"asset_id": local_asset, "media_type": "audio/wav"}]), path=jobs_path)
    media_jobs.create(media_jobs.MediaJob(request_id="req_member_media", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100, assets=[{"asset_id": member_asset, "media_type": "image/png"}], app_id=member_app["id"]), path=jobs_path)

    manifest = {
        "base_url": f"http://127.0.0.1:{port}",
        "owner_email": OWNER_EMAIL,
        "member_email": MEMBER_EMAIL,
        "password": PASSWORD,
        "service_asset": service_asset,
        "owner_asset": owner_asset,
        "local_asset": local_asset,
        "member_asset": member_asset,
        "service_app_id": service_app["id"],
        "member_app_id": member_app["id"],
    }
    (state / "live-data-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path = os.environ.get("LIVE_DATA_MANIFEST_PATH")
    if manifest_path:
        target = Path(manifest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


@contextlib.contextmanager
def _copy_state_dir():
    """One reused directory for a copy of production data, removed on exit."""
    state = REPO / ".ui-e2e-state" / "copy"
    shutil.rmtree(state, ignore_errors=True)
    state.mkdir(parents=True)
    try:
        yield str(state)
    finally:
        shutil.rmtree(state, ignore_errors=True)


def serve(state_base: Path, port: int, copy_from: Path | None = None, restarted: bool = False) -> None:
    engine_port = port + 1
    children: list[subprocess.Popen] = []

    def stop(_signum=None, _frame=None):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    holder = _copy_state_dir() if copy_from is not None else tempfile.TemporaryDirectory(prefix="live-data-", dir=state_base)
    with holder as temp:
        state = Path(temp)
        if copy_from is not None:
            # Copy mode: production data, copied; the gateway runs behind the connection guard.
            from scripts.ui import live_data_copy

            manifest = live_data_copy.prepare(state, port, engine_port, source=copy_from, restarted=restarted)
            live_data_copy.write_manifest(manifest, state)
            env = live_data_copy.env_for(state, port, engine_port, Path(str(manifest["config"])))
            env["UI_E2E_ALLOWED_PORTS"] = ""
            commands = [
                [sys.executable, "-m", "uvicorn", "scripts.ui.fake_engine:app", "--host", "127.0.0.1", "--port", str(engine_port)],
                [sys.executable, "-m", "scripts.ui.fixture_gateway", str(port), str(engine_port)],
            ]
        else:
            manifest = seed(state, port)
            env = dict(os.environ)
            commands = [
                [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(child_port)]
                for module, child_port in [("scripts.ui.fake_engine:app", engine_port), ("proxy:app", port)]
            ]
        try:
            for command, child_port in zip(commands, (engine_port, port)):
                log = (state / f"{child_port}.log").open("w")
                children.append(subprocess.Popen(command, cwd=state, env=env, stdout=log, stderr=subprocess.STDOUT))
                log.close()
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("live-data fixture server exited before readiness")
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).close()
                    print(json.dumps({"ready": True, "port": port}), flush=True)
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("live-data fixture readiness timed out")
            while True:
                time.sleep(0.5)
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("live-data fixture server stopped")
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, default=REPO / ".ui-e2e-state" / "live-data")
    parser.add_argument("--port", type=int, default=int(os.environ.get("UI_E2E_PORT", "18182")))
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--copy-from", type=Path, help="serve a COPY of this Azizam checkout's data (sweep)")
    parser.add_argument("--restarted", action="store_true", help="copy mode: no recent health evidence, as after a restart")
    args = parser.parse_args()
    if not 18000 <= args.port < 18998:
        raise SystemExit("port must be between 18000 and 18998")
    args.state_root.mkdir(parents=True, exist_ok=True)
    if args.copy_from is not None and not args.serve:
        raise SystemExit("--copy-from needs --serve")
    if args.serve:
        serve(args.state_root, args.port, args.copy_from, args.restarted)
    else:
        seed(args.state_root, args.port)
        print(json.dumps({"seeded": True, "port": args.port}))


if __name__ == "__main__":
    main()
