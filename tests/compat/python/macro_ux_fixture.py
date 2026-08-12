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
import sqlite3
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
SCENARIO_EXPECTATIONS = {
    "all_clear": ("ok", "ok", True, {"fixture-required": ("up", "ok"), "fixture-standby": ("up", "ok"), "litellm": ("up", "ok")}, False),
    "required_down": ("bad", "bad", True, {"fixture-required": ("down", "bad"), "fixture-standby": ("up", "ok"), "litellm": ("up", "ok")}, True),
    "degraded": ("warn", "warn", True, {"fixture-required": ("degraded", "warn"), "fixture-standby": ("up", "ok"), "litellm": ("up", "ok")}, True),
    "status_unavailable": ("unavailable", "neutral", False, {"fixture-required": ("unavailable", "neutral"), "fixture-standby": ("unavailable", "neutral"), "litellm": ("unavailable", "neutral")}, False),
    "standby_stopped": ("ok", "ok", True, {"fixture-required": ("up", "ok"), "fixture-standby": ("standby_stopped", "neutral"), "litellm": ("up", "ok")}, False),
}


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
            {"key": "litellm", "status": "up"},
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
    registry_record = admin_store.get_service_registry_with_generation(path=db)
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"]["litellm"]["config"]["models"] = {"fixture-model": "fixture-model"}
    admin_store.write_service_registry(registry, expected_generation=registry_record["operator_settings_generation"], updated_by="fixture", path=db)
    people: dict[str, dict[str, Any]] = {}
    for row in fixture["users"]:
        people[row["label"]] = admin_store.upsert_oidc_user(
            "fixture", row["subject"], email=row["email"], display_name=row["display_name"], now=now, path=db)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=people["admin"]["id"], granted_by="fixture", now=now, path=db)
    principal = admin_store.ensure_service_account("Fixture automation", description="P1B only", now=now, path=db)
    profile = admin_store.create_profile("Fixture profile", {"id": "fixture-profile", "protocols": ["openai.chat"]}, path=db)
    app_a = admin_store.ensure_personal_app_for_user(people["user_a"]["id"], name=fixture["long_value"], default_profile_id=profile["id"], now=now, path=db)
    app_b = admin_store.ensure_personal_app_for_user(people["user_b"]["id"], name="User B fixture app", default_profile_id=profile["id"], now=now, path=db)
    for index in range(fixture["pagination_apps"]):
        admin_store.create_app(f"Fixture page {index:02d}", kind="personal", owner_user_id=people["user_a"]["id"], default_profile_id=profile["id"], now=now, path=db)
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
        for index in range(2):
            metrics_store.record_sample(conn, now - index, "fixture.two", index + 1, {"shape": "two"})
        metrics_store.record_sample(conn, now - 86400, "fixture.daily", 1, {"shape": "daily"})
        metrics_store.record_sample(conn, now, "fixture.scalar", 7, {})
        conn.commit()
    compat_key, _, _ = create_admin_service_account_key("P1B compatibility", path=db)
    sessions: dict[str, dict[str, str]] = {}
    for label, user in people.items():
        role = "owner" if label == "admin" else "none"
        session = session_from_claims({"sub": user["id"], "email": user["email"]}, role=role, cookie=proxy.ADMIN_OIDC_CONFIG.cookie, now=time.time())
        sessions[label] = {"cookie": encode_session_cookie(session, signing_key=proxy.ADMIN_SESSION_SIGNING_KEY), "csrf": csrf_token_for_session(session, signing_key=proxy.ADMIN_SESSION_SIGNING_KEY)}
    _private_json(state_root / "bootstrap-private.json", {
        "base_url": f"http://127.0.0.1:{port}", "cookie_name": proxy.ADMIN_OIDC_CONFIG.cookie.name,
        "sessions": sessions, "compatibility_key": compat_key, "scenario": os.environ.get("P1B_FIXTURE_SCENARIO", "all_clear"),
        "fixture": fixture,
        "resolved_ids": {"users.admin": people["admin"]["id"], "users.user_a": people["user_a"]["id"], "users.user_b": people["user_b"]["id"], "profiles.fixture": profile["id"], "apps.user_a": app_a["id"], "apps.user_b": app_b["id"], "receipts.user_a": receipt_a, "service_principals.fixture_automation": principal["id"]},
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
    async def _fixture_drift_task(stop_event: asyncio.Event) -> None:
        await stop_event.wait()
    proxy._workflow_drift_loop = _fixture_drift_task
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
    with sqlite3.connect(path.parent / "metrics.db") as conn:
        telemetry = {metric: conn.execute("SELECT COUNT(*) FROM samples WHERE metric = ?", (metric,)).fetchone()[0] for metric in bootstrap["fixture"]["telemetry"].values()}
    if telemetry != {"fixture.absent": 0, "fixture.two": 2, "fixture.requests": 3, "fixture.daily": 1, "fixture.scalar": 1}:
        raise RuntimeError(f"fixture telemetry shapes failed: {telemetry}")
    for identity in ("admin", "user_a", "user_b"):
        status, _ = _request(bootstrap, identity, "GET", "/admin/api/session" if identity == "admin" else "/me/api/session")
        if status != 200:
            raise RuntimeError(f"{identity} session check failed: {status}")
    status, health = _request(bootstrap, "admin", "GET", "/admin/api/status")
    expected_state, expected_tone, expected_available, expected_services, expect_attention = SCENARIO_EXPECTATIONS[bootstrap.get("scenario", "all_clear")]
    projection = health.get("status", {}).get("current_health", {})
    services = {row.get("service_id"): row for row in projection.get("services", [])}
    if (status, projection.get("state"), projection.get("tone"), projection.get("observation_available")) != (200, expected_state, expected_tone, expected_available):
        raise RuntimeError(f"fixture health projection failed: {status} {projection}")
    for service_id, (observation, tone) in expected_services.items():
        if (services.get(service_id, {}).get("observation"), services.get(service_id, {}).get("tone")) != (observation, tone):
            raise RuntimeError(f"fixture service projection failed: {service_id} {services.get(service_id)}")
    status, overview = _request(bootstrap, "admin", "GET", "/admin/api/overview")
    service_attention = [item for item in overview.get("attention", []) if item.get("kind") == "service"]
    if status != 200 or bool(service_attention) != expect_attention:
        raise RuntimeError(f"fixture attention projection failed: {status} {service_attention}")
    if expect_attention and service_attention[0].get("target_id") != "fixture-required":
        raise RuntimeError(f"fixture attention target failed: {service_attention}")
    if not expect_attention and any(item.get("target_id") for item in service_attention):
        raise RuntimeError(f"fixture no-target attention failed: {service_attention}")
    status, users = _request(bootstrap, "admin", "GET", "/admin/api/users")
    if status != 200 or len(users.get("users", [])) != 3:
        raise RuntimeError(f"fixture human-user projection failed: {status} {users}")
    status, apps = _request(bootstrap, "admin", "GET", "/admin/api/apps")
    if status != 200 or len(apps.get("apps", [])) < bootstrap["fixture"]["pagination_apps"] + 2 or bootstrap["fixture"]["long_value"] not in {app.get("name") for app in apps.get("apps", [])}:
        raise RuntimeError(f"fixture long/pagination rows failed: {status} {apps}")
    status, registry = _request(bootstrap, "admin", "GET", "/admin/api/operator/service-registry")
    services_data = registry.get("service_registry", {}).get("services", {})
    if status != 200 or services_data.get("litellm", {}).get("lifecycle", {}).get("managed") is not False or services_data.get("litellm", {}).get("editable") is not False or services_data.get("fixture-required", {}).get("lifecycle", {}).get("managed") is not False or services_data.get("fixture-required", {}).get("editable") is not True:
        raise RuntimeError(f"fixture managed/editable service rows failed: {status} {services_data}")
    status, admin_write = _request(bootstrap, "admin", "PUT", "/admin/api/operator/user-access-defaults", {"self_service_apps": True})
    if status != 200 or admin_write.get("user_access_defaults", {}).get("self_service_apps") is not True:
        raise RuntimeError(f"admin CSRF write failed: {status} {admin_write}")
    status, app = _request(bootstrap, "user_a", "POST", "/me/api/apps", {"name": "P1B CSRF check"})
    if status != 201:
        raise RuntimeError(f"user CSRF write failed: {status}")
    status, _ = _request(bootstrap, "user_b", "DELETE", f"/me/api/apps/{app['app']['id']}", {})
    if status != 404:
        raise RuntimeError(f"cross-owner denial failed: {status}")
    status, invalid = _request(bootstrap, "user_a", "POST", "/me/api/apps", {"name": bootstrap["fixture"]["invalid_app_name"]})
    if status != 400 or invalid.get("error", {}).get("code") != "app_name_required":
        raise RuntimeError(f"fixture invalid-input check failed: {status} {invalid}")
    app_a = bootstrap["resolved_ids"]["apps.user_a"]
    receipt_a = bootstrap["resolved_ids"]["receipts.user_a"]
    status, activity = _request(bootstrap, "user_a", "GET", "/me/api/activity?limit=2")
    if status != 200 or not any(row.get("request_id") == receipt_a for row in activity.get("receipts", [])):
        raise RuntimeError(f"User A receipt visibility failed: {status} {activity}")
    status, _ = _request(bootstrap, "user_b", "GET", f"/me/api/receipts/{receipt_a}")
    if status != 404:
        raise RuntimeError(f"User B receipt denial failed: {status}")
    status, key = _request(bootstrap, "user_a", "POST", f"/me/api/apps/{app_a}/keys", {"label": "Fixture key"})
    if status != 201 or not key.get("credential", {}).get("api_key"):
        raise RuntimeError(f"User A key visibility failed: {status} {key}")
    status, _ = _request(bootstrap, "user_b", "POST", f"/me/api/apps/{app_a}/keys", {"label": "Denied"})
    if status != 404:
        raise RuntimeError(f"User B key denial failed: {status}")
    status, policy = _request(bootstrap, "admin", "POST", "/admin/api/policy/simulate", {"profile": bootstrap["resolved_ids"]["profiles.fixture"], "request": bootstrap["fixture"]["policy_request"]})
    if status != 200 or "decision" not in policy:
        raise RuntimeError(f"fixture policy input failed: {status} {policy}")


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
