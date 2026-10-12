#!/usr/bin/env python3
"""Private P1B fixture seeder and real-app launcher.

This is intentionally a single-purpose test helper.  Its only injection is
the already-injected admin status provider, rebound before the app starts.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
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
SCENARIOS = frozenset({"all_clear", "required_down", "degraded", "status_unavailable", "standby_stopped", "history_dense"})
_DISABLED_FIXTURE_SERVICES = {"comfyui": ("disabled", "neutral"), "stt": ("disabled", "neutral"), "tts": ("disabled", "neutral")}
SAFE_RECEIPT_FIELDS = frozenset({
    "_stored_at", "accepted_at_ms", "accepted_inputs", "effective_output_limit",
    "fallback_reasons", "ignored_inputs", "intent", "job_id", "latency_ms", "path",
    "protocol", "rejected_inputs", "request_id", "requested_model", "requested_workflow",
    "resolved_model", "resolved_workflow", "status", "stream", "updated_at_ms", "warnings",
})
CREDENTIAL_FIELDS = ("id", "subject_kind", "subject_id", "app_id", "prefix", "display_name", "scopes", "expires_at")
SCENARIO_EXPECTATIONS = {
    "all_clear": ("ok", "ok", True, {"fixture-required": ("up", "ok"), "fixture-standby": ("up", "ok"), "litellm": ("up", "ok"), **_DISABLED_FIXTURE_SERVICES}, False),
    "required_down": ("bad", "bad", True, {"fixture-required": ("down", "bad"), "fixture-standby": ("up", "ok"), "litellm": ("up", "ok"), **_DISABLED_FIXTURE_SERVICES}, True),
    "degraded": ("warn", "warn", True, {"fixture-required": ("degraded", "warn"), "fixture-standby": ("up", "ok"), "litellm": ("up", "ok"), **_DISABLED_FIXTURE_SERVICES}, True),
    "status_unavailable": ("unavailable", "neutral", False, {"fixture-required": ("unavailable", "neutral"), "fixture-standby": ("unavailable", "neutral"), "litellm": ("unavailable", "neutral"), **_DISABLED_FIXTURE_SERVICES}, False),
    "standby_stopped": ("ok", "ok", True, {"fixture-required": ("up", "ok"), "fixture-standby": ("standby_stopped", "neutral"), "litellm": ("up", "ok"), **_DISABLED_FIXTURE_SERVICES}, False),
    "history_dense": ("ok", "ok", True, {"fixture-required": ("up", "ok"), "fixture-standby": ("up", "ok"), "litellm": ("up", "ok"), **_DISABLED_FIXTURE_SERVICES}, False),
}
# Declared 24h history samples for the history_dense scenario's
# "fixture-required" backend. Values are realistic, not decorative: mostly
# up with one brief degraded dip, so the chart-forward Status page has a
# genuine non-trivial trend to render rather than a flat line.
_HISTORY_DENSE_SAMPLES = (
    # (age_seconds_ago, up, status_code, latency_ms)
    (3600 * 20, 1.0, 1.0, 80.0),
    (3600 * 16, 1.0, 1.0, 95.0),
    (3600 * 12, 1.0, 1.0, 260.0),
    (3600 * 8, 0.0, 0.5, 260.0),
    (3600 * 4, 1.0, 1.0, 110.0),
    (3600 * 1, 1.0, 1.0, 90.0),
)


def _private_json(path: Path, value: Mapping[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, sort_keys=True)
        out.write("\n")
    if stat.S_IMODE(path.stat().st_mode) != PRIVATE_MODE:
        raise RuntimeError("fixture credential artifact is not mode 0600")


def assert_network_trace_is_fixture_only(trace_path: Path, fixture_port: int) -> None:
    """Fail closed unless every traced connect/bind is an approved IPv4 endpoint."""
    import re

    network_call = re.compile(r"\b(?:connect|bind)\b")
    ipv4_call = re.compile(
        r'^\s*(?:(?:\[pid\s+\d+\]|\d+)\s+)?(connect|bind)\(\d+, '
        r'\{sa_family=AF_INET, sin_port=htons\((\d+)\), '
        r'sin_addr=inet_addr\("([^"]+)"\)\}, \d+\) = .+$'
    )
    violations: list[str] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if network_call.search(line) is None:
            continue
        match = ipv4_call.fullmatch(line)
        if match is None:
            violations.append(line)
            continue
        operation, port, host = match.group(1), int(match.group(2)), match.group(3)
        allowed_ports = {fixture_port} if operation == "bind" else {9, fixture_port}
        if operation == "bind":
            allowed_ports.add(0)
        if host != "127.0.0.1" or port not in allowed_ports:
            violations.append(line)
    if violations:
        raise RuntimeError("fixture network trace reached a non-fixture endpoint:\n" + "\n".join(violations))


def assert_os_containment(state_root: Path, readonly_root: Path) -> None:
    """Exercise mutation syscalls that must fail outside bwrap's sole writable root."""
    root = state_root.resolve()
    readonly = readonly_root.resolve()
    outside = readonly / ".p1b-containment-probe"
    dotdot = root / ".." / ".p1b-containment-dotdot"
    escape = root / "p1b-containment-link"
    escape.symlink_to(readonly, target_is_directory=True)

    def denied(label: str, operation: Any) -> None:
        try:
            operation()
        except OSError as exc:
            if exc.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}:
                raise RuntimeError(f"{label} failed for the wrong reason: {exc}") from exc
        else:
            raise RuntimeError(f"{label} mutated outside the fixture root")

    libc = ctypes.CDLL(None, use_errno=True)
    libc.creat.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.creat.restype = ctypes.c_int
    def creat() -> None:
        fd = libc.creat(os.fsencode(outside), PRIVATE_MODE)
        if fd >= 0:
            os.close(fd)
        raise OSError(ctypes.get_errno(), "creat")
    denied("creat", creat)
    denied("mknod", lambda: os.mknod(outside, stat.S_IFREG | PRIVATE_MODE))
    denied("setxattr", lambda: os.setxattr(readonly / "README.md", "user.p1b", b"x"))
    denied("dotdot escape", lambda: os.open(dotdot, os.O_WRONLY | os.O_CREAT, PRIVATE_MODE))
    denied("symlink escape", lambda: os.open(escape / "p1b-containment-symlink", os.O_WRONLY | os.O_CREAT, PRIVATE_MODE))


def _dense_history_block(now: int) -> dict[str, Any]:
    """Explicit, self-consistent fixture history for "fixture-required",
    shaped exactly like status_endpoint's real BackendHistory contract
    (see web/src/lib/api.ts's BackendHistory type). This is declared fixture
    data, not a live computation — verify() below re-derives the same
    aggregates from _HISTORY_DENSE_SAMPLES and asserts they match exactly, so
    a future edit to one without the other fails loudly."""
    latency_series = [[now - age_s, latency] for age_s, _up, _status_code, latency in _HISTORY_DENSE_SAMPLES]
    status_series = [[now - age_s, status_code] for age_s, _up, status_code, _latency in _HISTORY_DENSE_SAMPLES]
    uptime_pct_24h = round(100.0 * sum(up for _, up, _, _ in _HISTORY_DENSE_SAMPLES) / len(_HISTORY_DENSE_SAMPLES), 3)
    avg_latency_ms_24h = round(sum(latency for _, _, _, latency in _HISTORY_DENSE_SAMPLES) / len(_HISTORY_DENSE_SAMPLES))
    return {
        "uptime_pct_24h": uptime_pct_24h,
        "uptime_pct_7d": uptime_pct_24h,
        "avg_latency_ms_24h": avg_latency_ms_24h,
        "p95_latency_ms_24h": max(latency for _, _, _, latency in _HISTORY_DENSE_SAMPLES),
        "avg_latency_ms_7d": avg_latency_ms_24h,
        "p95_latency_ms_7d": max(latency for _, _, _, latency in _HISTORY_DENSE_SAMPLES),
        "sample_count_24h": len(_HISTORY_DENSE_SAMPLES),
        "status_series": status_series,
        "latency_series": latency_series,
    }


def _status(scenario: str):
    async def provider() -> Mapping[str, Any]:
        if scenario == "status_unavailable":
            raise RuntimeError("fixture status unavailable")
        required = "down" if scenario == "required_down" else "degraded" if scenario == "degraded" else "up"
        standby = "down" if scenario == "standby_stopped" else "up"
        required_backend: dict[str, Any] = {"key": "fixture-required", "status": required}
        if scenario == "history_dense":
            required_backend["history"] = _dense_history_block(int(time.time()))
        return {"backends": [
            required_backend,
            {"key": "fixture-standby", "status": standby},
            {"key": "litellm", "status": "up"},
        ]}
    return provider


def _seed(proxy: Any, state_root: Path, port: int) -> None:
    from gateway import accounts, admin_store, receipt_store
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
    principal = admin_store.ensure_service_account(fixture["principal_name"], description="P1B only", now=now, path=db)
    plan = admin_store.create_access_plan({
        "name": fixture["plan_name"], "can_use": {"chat": {"items": "all"}},
        "direct_engine_access": {"engines": [], "operations": []}, "paid_providers": [], "provider_fallback": False,
        "can_load_models": True, "receipt_detail": "basic", "media_metadata": "none",
    }, path=db)
    for label in ("user_a", "user_b"):
        accounts.change_person(people[label]["id"], "fixture", path=db, plan_id=plan["id"])
    app_a = admin_store.ensure_personal_app_for_user(people["user_a"]["id"], name=fixture["long_value"], now=now, path=db)
    app_b = admin_store.ensure_personal_app_for_user(people["user_b"]["id"], name="User B fixture app", now=now, path=db)
    for index in range(fixture["pagination_apps"]):
        admin_store.create_app(f"Fixture page {index:02d}", kind="personal", owner_user_id=people["user_a"]["id"], now=now, access_plan="person", path=db)
    app_credentials = []
    for app, label in ((app_a, "User A key"), (app_b, "User B key")):
        app_credentials.append(admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=hashlib.sha256(app["id"].encode()).hexdigest(), prefix="opk_fixture", display_name=label, path=db))
    principal_credential = admin_store.create_credential_hash(subject_kind="service_account", subject_id=principal["id"], hash=hashlib.sha256(principal["id"].encode()).hexdigest(), prefix="oak_fixture", display_name=fixture["credential_name"], path=db)
    audit_id = admin_store.record_audit(fixture["audit_action"], actor_kind="user", actor_id=people["admin"]["id"], target_kind="app", target_id=app_a["id"], metadata={"fixture": True}, path=db)
    receipt_store.init_db(state_root / "receipts.db")
    receipt_a = receipt_store.record(RouteReceipt(request_id="fixture-safe-receipt", protocol="openai.chat", path="/v1/chat/completions", intent="chat.generate", app_id=app_a["id"], requested_model="fixture-model", resolved_model="fixture-model", status="200"), path=state_root / "receipts.db")
    receipt_store.record(RouteReceipt(protocol="openai.chat", intent="chat.generate", app_id=app_b["id"], status="200"), path=state_root / "receipts.db")
    metrics_store.init_db(state_root / "metrics.db")
    with metrics_store.open_writer(state_root / "metrics.db") as conn:
        for index in range(3):
            metrics_store.record_sample(conn, now - index, "fixture.requests", index + 1, {"fixture": "p1b"})
        for index in range(2):
            metrics_store.record_sample(conn, now - index, "fixture.two", index + 1, {"shape": "two"})
        for index in range(3):
            metrics_store.record_sample(conn, now - index * 86400, "fixture.daily", index + 1, {"shape": "daily"})
        metrics_store.record_sample(conn, now, "fixture.scalar", 7, {})
        conn.commit()
    compat_key, compat_credential, _ = create_admin_service_account_key("P1B compatibility", path=db)
    expected_credentials = sorted([
        {"id": record["id"], "subject_kind": "app", "subject_id": app["id"], "app_id": app["id"], "prefix": "opk_fixture", "display_name": label, "scopes": [], "expires_at": None}
        for record, app, label in zip(app_credentials, (app_a, app_b), ("User A key", "User B key"), strict=True)
    ] + [
        {"id": principal_credential["id"], "subject_kind": "service_account", "subject_id": principal["id"], "app_id": None, "prefix": "oak_fixture", "display_name": fixture["credential_name"], "scopes": [], "expires_at": None},
        {"id": compat_credential["id"], "subject_kind": "service_account", "subject_id": compat_credential["subject_id"], "app_id": None, "prefix": compat_key[:18], "display_name": "P1B compatibility admin key", "scopes": [], "expires_at": None},
    ], key=lambda row: str(row["id"]))
    sessions: dict[str, dict[str, str]] = {}
    for label, user in people.items():
        role = "owner" if label == "admin" else "none"
        session = session_from_claims({"sub": user["id"], "email": user["email"]}, role=role, cookie=proxy.ADMIN_OIDC_CONFIG.cookie, now=time.time())
        sessions[label] = {"cookie": encode_session_cookie(session, signing_key=proxy.ADMIN_SESSION_SIGNING_KEY), "csrf": csrf_token_for_session(session, signing_key=proxy.ADMIN_SESSION_SIGNING_KEY)}
    _private_json(state_root / "bootstrap-private.json", {
        "base_url": f"http://127.0.0.1:{port}", "cookie_name": proxy.ADMIN_OIDC_CONFIG.cookie.name,
        "sessions": sessions, "compatibility_key": compat_key, "scenario": os.environ.get("P1B_FIXTURE_SCENARIO", "all_clear"),
        "fixture": fixture,
        "expected_credentials": expected_credentials,
        "resolved_ids": {"users.admin": people["admin"]["id"], "users.user_a": people["user_a"]["id"], "users.user_b": people["user_b"]["id"], "access_plans.fixture": plan["id"], "apps.user_a": app_a["id"], "apps.user_b": app_b["id"], "receipts.user_a": receipt_a, "service_principals.fixture_automation": principal["id"], "credentials.fixture_automation": principal_credential["id"], "audit.fixture_seed": audit_id["id"]},
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
    # Authorized P1B-only seams: keep fixture lifespan maintenance inert and
    # make every unexpected outbound operator probe fail before it reaches a socket.
    proxy.status_endpoint.get_cached_status = lambda _config: _status(args.scenario)()
    async def _fixture_wait(stop_event: asyncio.Event) -> None:
        await stop_event.wait()
    async def _fixture_maintenance() -> None:
        await asyncio.Event().wait()
    proxy.maintenance_loop = _fixture_maintenance
    proxy._workflow_drift_loop = _fixture_wait
    proxy.metrics_sampler.sampler_loop = lambda: _fixture_maintenance()
    attempts = state_root / "blocked-operator-connects.jsonl"
    connect = socket.socket.connect
    def _block_operator_connect(sock: socket.socket, address: Any) -> Any:
        host, port = address[:2] if isinstance(address, tuple) and len(address) >= 2 else ("", 0)
        if (host, port) != ("127.0.0.1", 9):
            with attempts.open("a", encoding="utf-8") as out:
                out.write(json.dumps({"host": host, "port": port}, sort_keys=True) + "\n")
            raise OSError("P1B fixture blocks outbound operator connections")
        return connect(sock, address)
    socket.socket.connect = _block_operator_connect
    _private_json(state_root / "fixture-ready.json", {"port": port, "scenario": args.scenario, "bootstrap": str(state_root / "bootstrap-private.json")})
    await uvicorn.Server(uvicorn.Config(proxy.app, log_level="warning", access_log=False)).serve(sockets=[listener])


def _request(bootstrap: Mapping[str, Any], identity: str, method: str, path: str, body: Mapping[str, Any] | None = None) -> tuple[int, Any]:
    session = bootstrap["sessions"][identity]
    headers = {"Cookie": f"{bootstrap['cookie_name']}={session['cookie']}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["x-azizam-csrf"] = session["csrf"]
        if identity != "admin":
            headers["x-azizam-user-action"] = "1"
    if method == "PUT" and path == "/admin/api/oidc-config":
        _, current = _request(bootstrap, identity, "GET", path)
        headers["If-Match"] = current["revision"]
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
    if telemetry != {"fixture.absent": 0, "fixture.two": 2, "fixture.requests": 3, "fixture.daily": 3, "fixture.scalar": 1}:
        raise RuntimeError(f"fixture telemetry shapes failed: {telemetry}")
    with sqlite3.connect(path.parent / "metrics.db") as conn:
        daily = conn.execute("SELECT ts, value FROM samples WHERE metric = 'fixture.daily' ORDER BY ts ASC").fetchall()
    if [value for _, value in daily] != [3.0, 2.0, 1.0] or len(daily) != 3 or [daily[index + 1][0] - daily[index][0] for index in range(2)] != [86400, 86400]:
        raise RuntimeError(f"fixture daily series failed: {daily}")
    attempts = path.parent / "blocked-operator-connects.jsonl"
    if attempts.exists() and attempts.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"fixture attempted operator traffic: {attempts.read_text(encoding='utf-8')}")
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
    if set(services) != set(expected_services):
        raise RuntimeError(f"fixture service set failed: {set(services)}")
    for service_id, (observation, tone) in expected_services.items():
        if (services.get(service_id, {}).get("observation"), services.get(service_id, {}).get("tone")) != (observation, tone):
            raise RuntimeError(f"fixture service projection failed: {service_id} {services.get(service_id)}")
    raw_backends = health.get("status", {}).get("backends", [])
    if not raw_backends or any(not row.get("service_id") for row in raw_backends):
        raise RuntimeError(f"fixture backend contract failed: missing service_id: {raw_backends}")
    if any(row.get("service_id") != row.get("key") for row in raw_backends):
        raise RuntimeError(f"fixture backend contract failed: service_id/key mismatch: {raw_backends}")
    if bootstrap.get("scenario") == "history_dense":
        backends_by_key = {row.get("key"): row for row in raw_backends}
        required_history = backends_by_key.get("fixture-required", {}).get("history") or {}
        expected_uptime_pct = round(100.0 * sum(up for _, up, _, _ in _HISTORY_DENSE_SAMPLES) / len(_HISTORY_DENSE_SAMPLES), 3)
        expected_avg_latency = round(sum(latency for _, _, _, latency in _HISTORY_DENSE_SAMPLES) / len(_HISTORY_DENSE_SAMPLES))
        if required_history.get("uptime_pct_24h") != expected_uptime_pct:
            raise RuntimeError(f"fixture dense uptime failed: {required_history.get('uptime_pct_24h')} != {expected_uptime_pct}")
        if required_history.get("avg_latency_ms_24h") != expected_avg_latency:
            raise RuntimeError(f"fixture dense avg latency failed: {required_history.get('avg_latency_ms_24h')} != {expected_avg_latency}")
        latency_series = required_history.get("latency_series") or []
        status_series = required_history.get("status_series") or []
        if len(latency_series) < 3 or len(status_series) < 3:
            raise RuntimeError(f"fixture dense series too short: latency={len(latency_series)} status={len(status_series)}")
        observed_latencies = sorted(point[1] for point in latency_series)
        expected_latencies = sorted(latency for _, _, _, latency in _HISTORY_DENSE_SAMPLES)
        if observed_latencies != expected_latencies:
            raise RuntimeError(f"fixture dense latency series failed: {observed_latencies} != {expected_latencies}")
        observed_statuses = sorted(point[1] for point in status_series)
        expected_statuses = sorted(status_code for _, _, status_code, _ in _HISTORY_DENSE_SAMPLES)
        if observed_statuses != expected_statuses:
            raise RuntimeError(f"fixture dense status series failed: {observed_statuses} != {expected_statuses}")
    status, overview = _request(bootstrap, "admin", "GET", "/admin/api/overview")
    service_attention = [item for item in overview.get("attention", []) if item.get("kind") == "service"]
    expected_attention = {"fixture-required"} if expect_attention else set()
    if status != 200 or {item.get("target_id") for item in service_attention} != expected_attention:
        raise RuntimeError(f"fixture attention projection failed: {status} {service_attention}")
    status, users = _request(bootstrap, "admin", "GET", "/admin/api/users")
    expected_users = [{key: row.get(key) for key in ("id", "issuer", "subject", "email", "display_name")} | {"roles": [row["role"]] if row["role"] != "none" else []} for row in bootstrap["fixture"]["users"] for row in [dict(row, id=bootstrap["resolved_ids"][f"users.{row['label']}"])]]
    observed_users = [{key: row.get(key) for key in ("id", "issuer", "subject", "email", "display_name")} | {"roles": sorted(row.get("roles", []))} for row in users.get("users", [])]
    for row in expected_users:
        row["issuer"] = "fixture"
    if status != 200 or observed_users != expected_users:
        raise RuntimeError(f"fixture human-user projection failed: {status} {users}")
    status, plans = _request(bootstrap, "admin", "GET", "/admin/api/access-plans")
    plan_id = bootstrap["resolved_ids"]["access_plans.fixture"]
    expected_plan = {"id": plan_id, "name": bootstrap["fixture"]["plan_name"], "summary": "Chat. No paid models."}
    observed_plan = next(({key: row.get(key) for key in expected_plan} for row in plans.get("access_plans", []) if row.get("id") == plan_id), None)
    if status != 200 or observed_plan != expected_plan:
        raise RuntimeError(f"fixture access plan projection failed: {status} {plans}")
    status, credentials = _request(bootstrap, "admin", "GET", "/admin/api/credentials")
    credential_id = bootstrap["resolved_ids"]["credentials.fixture_automation"]
    observed_credentials = sorted([{key: row.get(key) for key in CREDENTIAL_FIELDS} for row in credentials.get("credentials", [])], key=lambda row: str(row["id"]))
    if status != 200 or observed_credentials != bootstrap["expected_credentials"] or any({"hash", "api_key", "key", "secret"} & set(row) for row in credentials.get("credentials", [])):
        raise RuntimeError(f"fixture principal credential projection failed: {status} {credentials}")
    with sqlite3.connect(path.parent / "admin.db") as conn:
        principal = conn.execute("SELECT id, name, description FROM service_accounts WHERE id = ?", (bootstrap["resolved_ids"]["service_principals.fixture_automation"],)).fetchone()
    if principal != (bootstrap["resolved_ids"]["service_principals.fixture_automation"], bootstrap["fixture"]["principal_name"], "P1B only"):
        raise RuntimeError(f"fixture service-principal projection failed: {principal}")
    status, audit = _request(bootstrap, "admin", "GET", "/admin/api/audit")
    audit_id = bootstrap["resolved_ids"]["audit.fixture_seed"]
    expected_audit = {"id": audit_id, "action": bootstrap["fixture"]["audit_action"], "actor_kind": "user", "actor_id": bootstrap["resolved_ids"]["users.admin"], "target_kind": "app", "target_id": bootstrap["resolved_ids"]["apps.user_a"], "metadata": {"fixture": True}}
    observed_audit = next(({key: row.get(key) for key in expected_audit} for row in audit.get("records", []) if row.get("id") == audit_id), None)
    if status != 200 or observed_audit != expected_audit:
        raise RuntimeError(f"fixture audit projection failed: {status} {audit}")
    status, apps = _request(bootstrap, "admin", "GET", "/admin/api/apps")
    if status != 200 or len(apps.get("apps", [])) < bootstrap["fixture"]["pagination_apps"] + 2 or bootstrap["fixture"]["long_value"] not in {app.get("name") for app in apps.get("apps", [])}:
        raise RuntimeError(f"fixture long/pagination rows failed: {status} {apps}")
    status, registry = _request(bootstrap, "admin", "GET", "/admin/api/operator/service-registry")
    services_data = registry.get("service_registry", {}).get("services", {})
    if status != 200 or services_data.get("litellm", {}).get("lifecycle", {}).get("managed") is not False or services_data.get("litellm", {}).get("editable") is not False or services_data.get("fixture-required", {}).get("lifecycle", {}).get("managed") is not False or services_data.get("fixture-required", {}).get("editable") is not True:
        raise RuntimeError(f"fixture managed/editable service rows failed: {status} {services_data}")
    status, admin_write = _request(bootstrap, "admin", "PUT", "/admin/api/settings/defaults", {"invite_access_plan": None})
    if status != 200 or admin_write.get("defaults", {}).get("invite_access_plan", "missing") is not None:
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
    status, receipt = _request(bootstrap, "user_a", "GET", f"/me/api/receipts/{receipt_a}")
    expected_receipt = {"request_id": receipt_a, "protocol": "openai.chat", "path": "/v1/chat/completions", "intent": "chat.generate", "requested_model": "fixture-model", "resolved_model": "fixture-model", "status": "200"}
    if status != 200 or {key: receipt.get(key) for key in expected_receipt} != expected_receipt or set(receipt) != SAFE_RECEIPT_FIELDS:
        raise RuntimeError(f"fixture safe receipt failed: {status} {receipt}")
    status, _ = _request(bootstrap, "user_b", "GET", f"/me/api/receipts/{receipt_a}")
    if status != 404:
        raise RuntimeError(f"User B receipt denial failed: {status}")
    status, key = _request(bootstrap, "user_a", "POST", f"/me/api/apps/{app_a}/keys", {"label": "Fixture key"})
    if status != 201 or not key.get("credential", {}).get("api_key"):
        raise RuntimeError(f"User A key visibility failed: {status} {key}")
    status, _ = _request(bootstrap, "user_b", "POST", f"/me/api/apps/{app_a}/keys", {"label": "Denied"})
    if status != 404:
        raise RuntimeError(f"User B key denial failed: {status}")
    status, setting = _request(bootstrap, "admin", "PUT", "/admin/api/oidc-config", {"enabled": False, "client_secret": bootstrap["fixture"]["redacted_setting"]})
    if status != 200 or setting.get("oidc_config", {}).get("client_secret") != "<redacted>" or bootstrap["fixture"]["redacted_setting"] in json.dumps(setting):
        raise RuntimeError(f"fixture redacted setting write failed: {status} {setting}")
    status, setting = _request(bootstrap, "admin", "GET", "/admin/api/oidc-config")
    if status != 200 or setting.get("oidc_config", {}).get("client_secret") != "<redacted>" or bootstrap["fixture"]["redacted_setting"] in json.dumps(setting):
        raise RuntimeError(f"fixture redacted setting projection failed: {status} {setting}")
    plan_id = bootstrap["resolved_ids"]["access_plans.fixture"]
    status, policy = _request(bootstrap, "admin", "POST", "/admin/api/access/check", {"plan": plan_id, "request": bootstrap["fixture"]["policy_request"]})
    expected_policy = bootstrap["fixture"]["policy_result"]
    observed_policy = {key: (policy.get("decision") or {}).get(key) for key in expected_policy}
    if status != 200 or policy.get("plan_ids") != [plan_id] or observed_policy != expected_policy:
        raise RuntimeError(f"fixture check access failed: {status} {policy}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--fd", type=int)
    parser.add_argument("--state-root")
    parser.add_argument("--scenario", default="all_clear")
    parser.add_argument("--verify")
    parser.add_argument("--assert-network-trace")
    parser.add_argument("--fixture-port", type=int)
    parser.add_argument("--assert-os-containment", action="store_true")
    parser.add_argument("--readonly-root")
    args = parser.parse_args()
    if args.verify:
        verify(Path(args.verify))
        return
    if args.assert_network_trace:
        if args.fixture_port is None:
            parser.error("--assert-network-trace requires --fixture-port")
        assert_network_trace_is_fixture_only(Path(args.assert_network_trace), args.fixture_port)
        return
    if args.assert_os_containment:
        if not args.state_root or not args.readonly_root:
            parser.error("--assert-os-containment requires --state-root and --readonly-root")
        assert_os_containment(Path(args.state_root), Path(args.readonly_root))
        return
    if not args.serve or not args.state_root:
        parser.error("--serve and --state-root are required")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
