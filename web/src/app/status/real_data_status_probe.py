#!/usr/bin/env python3
"""Probe Status contracts against a copied live gateway data directory.

This script intentionally prints only aggregate, redacted facts. It mutates the
copied admin store by creating throwaway browser-session users/apps, but it
never opens the live store and never prints keys, raw DB rows, or secrets.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


REPO = Path(__file__).resolve().parents[4]
STATE = REPO / ".ui-e2e-state" / "status-real"
DATA = STATE / "data"
PORTS = {18887, 18888}
sys.path.insert(0, str(REPO))


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def configure_environment() -> None:
    live = Path("/home/tnunamak/applications/azizam/data").resolve()
    if not DATA.exists():
        fail(f"copied data dir missing: {DATA}; run cp -a {live} {DATA}")
    if DATA.resolve() == live:
        fail("refusing to use the live data directory")
    config_path = STATE / "status-probe-config.yml"
    config_path.write_text(
        "proxy:\n"
        "  host: 127.0.0.1\n"
        "  port: 18887\n"
        "admin:\n"
        "  oidc:\n"
        "    cookie:\n"
        "      secure: false\n"
        "backends:\n"
        "  comfyui:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    home = STATE / "home"
    home.mkdir(exist_ok=True)
    path = os.environ.get("PATH", "/usr/bin:/bin")
    os.environ.clear()
    os.environ.update(
        {
            "PATH": path,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "AZIZAM_CONFIG_PATH": str(config_path),
            "AZIZAM_ADMIN_DB": str(DATA / "admin.db"),
            "AZIZAM_METRICS_DB": str(DATA / "metrics.db"),
            "AZIZAM_RESPONSES_DB": str(DATA / "responses.db"),
            "AZIZAM_RECEIPTS_DB": str(DATA / "receipts.db"),
            "AZIZAM_MEDIA_JOBS_DB": str(DATA / "media_jobs.db"),
            "AZIZAM_PROVIDER_LEDGER_DB": str(DATA / "provider_spend.db"),
            "AZIZAM_GRACEFUL_SHUTDOWN_S": "1",
            "AZIZAM_HOST_TELEMETRY_DISABLED": "1",
            "IMAGE_MEDIA_JOB_RESUME_ON_START": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )


def main() -> None:
    configure_environment()

    from fastapi.testclient import TestClient

    from scripts.ui.fixture_guard import install

    install(PORTS)

    from gateway import admin_store
    from gateway.admin_oidc import CookieConfig, encode_session_cookie, session_from_claims
    with (STATE / "real-data-probe-import.log").open("w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            import proxy

    admin_store.init_db(proxy.ADMIN_DB_PATH)
    profiles = admin_store.list_profiles(path=proxy.ADMIN_DB_PATH)
    profile_id = (
        next((row["id"] for row in profiles if row.get("id") == "default-local"), None)
        or (profiles[0]["id"] if profiles else None)
    )
    if not profile_id:
        fail("copied admin store has no access profiles to assign")

    now = int(time.time())
    suffix = str(now)
    member = admin_store.upsert_oidc_user(
        "status-real-probe",
        f"member-{suffix}",
        email=f"status-member-{suffix}@example.test",
        display_name="Status real-data probe member",
        path=proxy.ADMIN_DB_PATH,
    )
    owner = admin_store.upsert_oidc_user(
        "status-real-probe",
        f"owner-{suffix}",
        email=f"status-owner-{suffix}@example.test",
        display_name="Status real-data probe owner",
        path=proxy.ADMIN_DB_PATH,
    )
    admin_store.assign_admin_role(
        role="owner",
        subject_kind="user",
        subject_id=owner["id"],
        granted_by="status-real-probe",
        path=proxy.ADMIN_DB_PATH,
    )
    admin_store.create_app(
        "Status real-data probe app",
        kind="personal",
        owner_user_id=member["id"],
        default_profile_id=profile_id,
        path=proxy.ADMIN_DB_PATH,
    )

    key = proxy._browser_admin_session_signing_key()
    cookie = CookieConfig(secure=False)

    def session_cookie(user: dict[str, object], role: str) -> str:
        session = session_from_claims(
            {"sub": user["id"], "email": user.get("email")},
            role=role,
            cookie=cookie,
        )
        return encode_session_cookie(session, signing_key=key)

    with (STATE / "real-data-probe-lifespan.log").open("w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            client_context = TestClient(proxy.app, raise_server_exceptions=False)
            client = client_context.__enter__()
    try:
        client.cookies.set(cookie.name, session_cookie(member, "none"))
        member_response = client.get("/me/api/status")
        if member_response.status_code != 200:
            fail(f"member /me/api/status returned HTTP {member_response.status_code}")
        member_status = member_response.json()

        forbidden_needles = ("127.0.0.1", "localhost", "GPU", "VRAM", "service_id", "base_url")
        serialized_member = json.dumps(member_status, sort_keys=True)
        leaked = [needle for needle in forbidden_needles if needle in serialized_member]
        if leaked:
            fail(f"member status leaked owner-only implementation details: {', '.join(leaked)}")

        client.cookies.clear()
        client.cookies.set(cookie.name, session_cookie(owner, "owner"))
        registry_response = client.get("/admin/api/operator/service-registry")
        status_response = client.get("/admin/api/status")
        if registry_response.status_code != 200:
            fail(f"owner registry returned HTTP {registry_response.status_code}")
        if status_response.status_code != 200:
            fail(f"owner /admin/api/status returned HTTP {status_response.status_code}")
        registry_payload = registry_response.json()
        registry = registry_payload.get("service_registry") or registry_payload
        owner_status = status_response.json()
    finally:
        with (STATE / "real-data-probe-lifespan.log").open("a", encoding="utf-8") as log:
            with redirect_stdout(log), redirect_stderr(log):
                client_context.__exit__(None, None, None)

    (STATE / "member-status.json").write_text(
        json.dumps(member_status, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (STATE / "owner-service-registry.json").write_text(
        json.dumps(registry_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (STATE / "owner-status.json").write_text(
        json.dumps(owner_status, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    services = registry.get("services") or {}
    enabled = [row for row in services.values() if row.get("enabled")]
    member_caps = member_status.get("capabilities") or []
    member_receipts = member_status.get("receipts") or []
    admin_backends = owner_status.get("backends") or []
    current_health = (owner_status.get("current_health") or {}).get("services") or []
    print(
        "PASS real data status probe: "
        f"copied_services={len(services)} enabled_services={len(enabled)} "
        f"member_capabilities={len(member_caps)} member_receipts={len(member_receipts)} "
        f"admin_backends={len(admin_backends)} health_rows={len(current_health)}"
    )
    print(
        "PASS member contract redaction: no host endpoints, service IDs, GPU, or VRAM terms in /me/api/status"
    )
    print(
        "PASS copied data isolation: admin/metrics/responses/receipts/media/provider stores all point under "
        f"{DATA.relative_to(REPO)}; throwaway ports reserved={sorted(PORTS)}"
    )


if __name__ == "__main__":
    main()
