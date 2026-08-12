#!/usr/bin/env python3
"""Private P1B fixture seeder and real-app launcher.

This is intentionally a single-purpose test helper.  Its only injection is
the already-injected admin status provider, rebound before the app starts.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/macro_ux_fixture.json"
PRIVATE_MODE = 0o600
SCENARIOS = frozenset({"all_clear", "required_down", "degraded", "status_unavailable", "standby_stopped"})


def _private_json(path: Path, value: Mapping[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, sort_keys=True)
        out.write("\n")
    if stat.S_IMODE(path.stat().st_mode) != PRIVATE_MODE:
        raise RuntimeError("fixture credential artifact is not mode 0600")


def _status(scenario: str):
    async def provider() -> Mapping[str, Any]:
        if scenario == "status_unavailable":
            raise RuntimeError("fixture status unavailable")
        required = "down" if scenario == "required_down" else "degraded" if scenario == "degraded" else "up"
        standby = "down" if scenario == "standby_stopped" else "up"
        return {"backends": [
            {"key": "fixture-required", "status": required},
            {"key": "fixture-standby", "status": standby},
        ]}
    return provider


def _seed(proxy: Any, state_root: Path, port: int) -> None:
    from gateway import admin_store, receipt_store
    from gateway.admin_oidc import csrf_token_for_session, encode_session_cookie, session_from_claims
    from gateway.admin_runtime import create_admin_service_account_key
    from gateway.receipts import RouteReceipt
    import metrics_store

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    db = proxy.ADMIN_STORE.path
    now = int(time.time())
    people: dict[str, dict[str, Any]] = {}
    for row in fixture["users"]:
        people[row["label"]] = admin_store.upsert_oidc_user(
            "fixture", row["subject"], email=row["email"], display_name=row["display_name"], now=now, path=db)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=people["admin"]["id"], granted_by="fixture", now=now, path=db)
    principal = admin_store.ensure_service_account("Fixture automation", description="P1B only", now=now, path=db)
    profile = admin_store.create_profile("Fixture profile", {"id": "fixture-profile", "protocols": ["openai.chat"]}, path=db)
    app_a = admin_store.ensure_personal_app_for_user(people["user_a"]["id"], name="User A fixture app", default_profile_id=profile["id"], now=now, path=db)
    app_b = admin_store.ensure_personal_app_for_user(people["user_b"]["id"], name="User B fixture app", default_profile_id=profile["id"], now=now, path=db)
    for app, label in ((app_a, "User A key"), (app_b, "User B key")):
        admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=hashlib.sha256(app["id"].encode()).hexdigest(), prefix="opk_fixture", display_name=label, profile_id=profile["id"], path=db)
    admin_store.record_audit("fixture.seed", actor_kind="user", actor_id=people["admin"]["id"], target_kind="app", target_id=app_a["id"], metadata={"fixture": True}, path=db)
    receipt_store.init_db(state_root / "receipts.db")
    receipt_a = receipt_store.record(RouteReceipt(protocol="openai.chat", intent="chat.generate", app_id=app_a["id"], status="200"), path=state_root / "receipts.db")
    receipt_store.record(RouteReceipt(protocol="openai.chat", intent="chat.generate", app_id=app_b["id"], status="200"), path=state_root / "receipts.db")
    metrics_store.init_db(state_root / "metrics.db")
    with metrics_store.open_writer(state_root / "metrics.db") as conn:
        for index in range(3):
            metrics_store.record_sample(conn, now - index, "fixture.requests", index + 1, {"fixture": "p1b"})
        conn.commit()
    compat_key, _, _ = create_admin_service_account_key("P1B compatibility", path=db)
    sessions: dict[str, dict[str, str]] = {}
    for label, user in people.items():
        role = "owner" if label == "admin" else "none"
        session = session_from_claims({"sub": user["id"], "email": user["email"]}, role=role, cookie=proxy.ADMIN_OIDC_CONFIG.cookie, now=time.time())
        sessions[label] = {"cookie": encode_session_cookie(session, signing_key=proxy.ADMIN_SESSION_SIGNING_KEY), "csrf": csrf_token_for_session(session, signing_key=proxy.ADMIN_SESSION_SIGNING_KEY)}
    _private_json(state_root / "bootstrap-private.json", {
        "base_url": f"http://127.0.0.1:{port}", "cookie_name": proxy.ADMIN_OIDC_CONFIG.cookie.name,
        "sessions": sessions, "compatibility_key": compat_key,
        "resolved_ids": {"users.admin": people["admin"]["id"], "users.user_a": people["user_a"]["id"], "users.user_b": people["user_b"]["id"], "apps.user_a": app_a["id"], "apps.user_b": app_b["id"], "receipts.user_a": receipt_a, "service_principals.fixture_automation": principal["id"]},
    })


async def serve(args: argparse.Namespace) -> None:
    if args.scenario not in SCENARIOS:
        raise SystemExit(f"unsupported fixture scenario: {args.scenario}")
    state_root = Path(args.state_root).resolve()
    listener = socket.socket(fileno=args.fd) if args.fd is not None else socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if args.fd is None:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
    port = listener.getsockname()[1]
    sys.path.insert(0, str(ROOT))
    import proxy
    import uvicorn
    _seed(proxy, state_root, port)
    # Authorized P1B-only seam: proxy's router calls this module attribute lazily.
    proxy.status_endpoint.get_cached_status = lambda _config: _status(args.scenario)()
    _private_json(state_root / "fixture-ready.json", {"port": port, "scenario": args.scenario, "bootstrap": str(state_root / "bootstrap-private.json")})
    await uvicorn.Server(uvicorn.Config(proxy.app, log_level="warning", access_log=False)).serve(sockets=[listener])


def _request(bootstrap: Mapping[str, Any], identity: str, method: str, path: str, body: Mapping[str, Any] | None = None) -> tuple[int, Any]:
    session = bootstrap["sessions"][identity]
    headers = {"Cookie": f"{bootstrap['cookie_name']}={session['cookie']}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["x-ai-gateway-csrf"] = session["csrf"]
        if identity != "admin":
            headers["x-ai-gateway-user-action"] = "1"
    request = urllib.request.Request(f"{bootstrap['base_url']}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def verify(path: Path) -> None:
    bootstrap = json.loads(path.read_text(encoding="utf-8"))
    for identity in ("admin", "user_a", "user_b"):
        status, _ = _request(bootstrap, identity, "GET", "/admin/api/session" if identity == "admin" else "/me/api/session")
        if status != 200:
            raise RuntimeError(f"{identity} session check failed: {status}")
    status, health = _request(bootstrap, "admin", "GET", "/admin/api/status")
    if status != 200 or "current_health" not in health.get("status", {}):
        raise RuntimeError(f"fixture health projection failed: {status}")
    status, app = _request(bootstrap, "user_a", "POST", "/me/api/apps", {"name": "P1B CSRF check"})
    if status != 201:
        raise RuntimeError(f"user CSRF write failed: {status}")
    status, _ = _request(bootstrap, "user_b", "DELETE", f"/me/api/apps/{app['app']['id']}", {})
    if status != 404:
        raise RuntimeError(f"cross-owner denial failed: {status}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--fd", type=int)
    parser.add_argument("--state-root")
    parser.add_argument("--scenario", default="all_clear")
    parser.add_argument("--verify")
    args = parser.parse_args()
    if args.verify:
        verify(Path(args.verify))
        return
    if not args.serve or not args.state_root:
        parser.error("--serve and --state-root are required")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
