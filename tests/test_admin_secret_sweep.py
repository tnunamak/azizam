"""The oracle of decisions D26 to D32, read side: no admin or member GET route returns a canary secret.

The test plants a canary in every carrier a record can hold (``tests/support/canary.py``: header values, environment values,
command arguments, URL user info and query, probe paths, an admin key, package sources), moves them into the secret store as
every record write does, and reads every GET and HEAD route of the exported app (found by walking its route table, mounts
and WebSockets included) as an administrator, the owner and a signed-out client. It fails if a canary shows in a body or a
header, and it proves the sweep worked: each canary is in the store, each route that carries records answered 200 with the
public fields present, and the detector fails on a canary placed in an isolated output.

The owner may read the configuration export (decision D18); it holds references and metadata, so it is swept too.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import httpx
import pytest

from gateway import admin_store, egress, lifecycle_drivers, media_jobs, receipt_store, record_carriers
from gateway.receipts import RouteReceipt
from tests.support import canary
from tests.support.route_discovery import discover
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

S = canary.MARKERS

# A route parameter is filled from these names when a seeded record has that id. Any other parameter (a job, a media
# asset, an access plan, an invite) names a resource that is not a service record, so the route is read with a made-up
# id: it must still answer without a canary.
PARAMETERS = {
    "service_id": "engine-a", "engine_id": "engine-a", "id": "engine-a", "provider_id": "provider-a",
    "provider": "provider-a", "package_id": "package-a",
    "system_id": "gateway",
    "request_id": "sweep-request",
    "section": "services", "record_id": "held-url",  # the routes that fix or remove a record the registry leaves out
}
UNSEEDED_ID = "unseeded-id"

# Routes the sweep does not read, each with its reason. A test below fails when an entry matches no mounted route.
EXCLUDED_ROUTES = {
    "/mcp": "opens a server-sent stream that a test client cannot read to its end",
    "/gateway/v1/media/jobs/{job_id}/events": "an event stream that stays open",
    "/me/api/prompt-memory": "writes the member's default preference row into the process-default admin database, "
                             "which this fixture does not redirect (a foreign-key error); it holds one member's own "
                             "settings and no service-record content",
    "/{path:path}": "the catch-all that serves the built web app files; no record content",
}
# The WebSocket routes of the gateway, each with its reason for being outside the management plane (decision D32 b).
WEBSOCKET_INVENTORY = {
    "/v1/realtime": "the inference data plane (a registered stream adapter is phase 2); no record content",
    "/openai/v1/realtime": "the inference data plane; no record content",
}
# Routes that serve the owner a full-fidelity backup (decision D18); an administrator still gets 403.
OWNER_EXPORT_ROUTES = frozenset({"/admin/api/config/export"})


def _seed_registry(gateway) -> None:
    """Plant every canary: records moved into the store, left-out records written raw, a package source."""
    registry = canary.seed_direct(gateway)
    # The long-audio options route is a real success path for a transcription
    # engine. Keep the other canary carriers intact while making engine-a fit.
    registry["services"]["engine-a"]["capabilities"].append("transcriptions")
    current = admin_store.get_service_registry_with_generation(path=gateway.path)
    admin_store.write_service_registry_with_audit(
        registry, expected_generation=int(current["operator_settings_generation"]), updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)


parameters: dict[str, str] = dict(PARAMETERS)


def _discovered_parameters(owner) -> dict[str, str]:
    """Ids of resources the app already holds (an app, an access plan, an audit event), so the routes that read
    one answer with a success and the sweep proves their handler ran."""
    found: dict[str, str] = {}
    apps = owner.get("/admin/api/apps").json().get("apps") or []
    plans = owner.get("/admin/api/access-plans").json().get("access_plans") or owner.get("/admin/api/access-plans").json().get("plans") or []
    events = owner.get("/admin/api/audit").json().get("records") or []
    if apps:
        found["app_id"] = apps[0]["id"]
    if plans:
        found["plan_id"] = plans[0]["id"]
    if events:
        found["event_id"] = str(events[0].get("id") or events[0].get("event_id") or "")
    return found


def _fill(path: str) -> str:
    def value(match: re.Match[str]) -> str:
        return parameters.get(match.group(1).split(":", 1)[0], UNSEEDED_ID)

    return re.sub(r"\{([^}]+)\}", value, path)


def _leaf_errors(exc: BaseException) -> list[str]:
    """The real errors inside the exception groups the test client wraps around a failed request."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaf_errors(inner)]
    if isinstance(exc, RuntimeError) and "No response returned" in str(exc):
        return []
    return [f"{type(exc).__name__}: {str(exc)[:100]}"]


def _leaks(response) -> list[str]:
    haystack = response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
    return canary.leaks_in(haystack)


@pytest.fixture
def host_isolated(monkeypatch):
    """The sweeps call real routes; the host answers "nothing here" so no route reaches outside the test."""
    def no_host_command(*args, **kwargs):
        raise FileNotFoundError("no host command runs in this test")

    monkeypatch.setattr(subprocess, "Popen", no_host_command)

    async def no_unit_status(self, svc):
        return lifecycle_drivers.ProcessState.UNMANAGED

    monkeypatch.setattr(lifecycle_drivers.SystemdDriver, "status", no_unit_status)

    async def refuse_async(self, request):
        raise httpx.ConnectError("no engine runs in this test", request=request)

    def refuse(self, request):
        raise httpx.ConnectError("no engine runs in this test", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    # The routes open the process-default job and receipt stores: create them for this test and take them away after it,
    # so a later test starts as if no sweep had run (a store left behind changes which code path a route takes).
    created = [path for path in (media_jobs.DB_PATH, receipt_store.DB_PATH) if not path.exists()]
    media_jobs.init_db()
    receipt_store.init_db()
    receipt_store.record(RouteReceipt(request_id=PARAMETERS["request_id"], protocol="test", intent="video.generate"),
                         path=receipt_store.DB_PATH)
    yield
    for path in created:
        for leftover in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            leftover.unlink(missing_ok=True)


def _get_routes(app) -> list[str]:
    return sorted({found.path for found in discover(app) if "GET" in found.methods})


def test_the_route_list_is_found_from_the_app_with_its_mounts_and_websockets(gateway) -> None:
    found = discover(gateway.proxy.app)
    paths = {item.path for item in found}
    assert "/admin/api/operator/service-registry" in paths and "/admin/api/engines/{service_id}" in paths
    assert {"/me/api/jobs/{job_id}", "/admin/api/overview", "/status/raw", "/setup/console"} <= paths
    assert set(EXCLUDED_ROUTES) <= paths, "an excluded route is no longer mounted: drop it from EXCLUDED_ROUTES"
    assert OWNER_EXPORT_ROUTES <= paths
    websockets = {item.path for item in found if item.websocket}
    assert websockets == set(WEBSOCKET_INVENTORY), "a WebSocket route is not in the inventory (or one in it is gone)"


def test_every_admin_route_is_a_management_route_or_an_inventoried_exception(gateway) -> None:
    unregistered = []
    for item in discover(gateway.proxy.app):
        if not item.path.startswith(egress.MANAGEMENT_PREFIXES) or item.management:
            continue
        if item.module in egress.UNMANAGED_ADMIN_MODULES:
            continue
        # a route of the gateway module is inventoried one by one (egress.UNMANAGED_ADMIN_ROUTES), never as a module
        if item.module.startswith("proxy") and all((method, item.path) in egress.UNMANAGED_ADMIN_ROUTES for method in item.methods):
            continue
        unregistered.append(f"{item.methods} {item.path} ({item.module})")
    assert not unregistered, unregistered
    egress.verify_and_freeze(gateway.proxy.app)
    assert sum(1 for item in discover(gateway.proxy.app) if item.management) >= 70


def sweep_get_routes(gateway, clients: dict[str, Any]) -> tuple[dict[tuple[str, str, str], int], list[str], list[str]]:
    """Read every GET and HEAD route of the exported app as each client: (statuses, canary leaks, routes that raised)."""
    statuses: dict[tuple[str, str, str], int] = {}
    errored: list[str] = []
    leaks: list[str] = []
    for path in _get_routes(gateway.proxy.app):
        if path in EXCLUDED_ROUTES:
            continue
        concrete = _fill(path)
        for who, client in clients.items():
            for method in ("get", "head"):
                try:
                    response = getattr(client, method)(concrete)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:  # noqa: BLE001  (a route that raises answered nothing; it is listed below)
                    errored.append(f"{who} {method.upper()} {concrete}: {'; '.join(_leaf_errors(exc)) or type(exc).__name__}")
                    continue
                statuses[(who, path, method)] = response.status_code
                found = _leaks(response)
                if found:
                    leaks.append(f"{who} {method.upper()} {concrete} -> {response.status_code}: {', '.join(found)}")
    return statuses, leaks, errored


def test_no_admin_or_me_get_route_returns_a_canary_and_each_record_route_answered(gateway, host_isolated) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _seed_registry(gateway)
    clients = {"admin": admin, "owner": owner, "anonymous": gateway.client()}
    parameters.update(_discovered_parameters(owner))
    planted = canary.plant_provider(owner)
    assert planted.status_code == 201, planted.text  # the provider key reached the store through the real route
    statuses, leaks, errored = sweep_get_routes(gateway, clients)
    assert statuses, "the sweep read no route"
    assert not leaks, "canaries in responses:\n" + "\n".join(leaks)
    assert not errored, "routes that raised instead of answering:\n" + "\n".join(errored)
    # The handler ran: every management GET route the owner can reach answered with a success, or is named here with why not.
    managed = {item.path for item in discover(gateway.proxy.app) if item.management and "GET" in item.methods}
    silent = sorted(path for path in managed if statuses.get(("owner", path, "get"), 0) >= 300 and path not in NO_SUCCESS_FIXTURE)
    assert not silent, f"management routes with no success-path fixture and no reason: {silent}"
    stale = sorted(path for path in NO_SUCCESS_FIXTURE if statuses.get(("owner", path, "get"), 0) < 300 and path in managed)
    assert not stale, f"routes now answered with a success; drop them from NO_SUCCESS_FIXTURE: {stale}"


# Management GET routes whose success needs a resource the sweep does not seed, each with the reason.
NO_SUCCESS_FIXTURE: dict[str, str] = {
    "/admin/api/operator/litellm/keys": "asks the LiteLLM sidecar, which does not run in a test; phase 2 gives it a fake sidecar",
    "/admin/api/engines/{service_id}/stt-measurements/{job_id}": "the sweep does not seed a durable measurement job; owner job reads are covered by tests/test_stt_measurement.py",
}


@pytest.mark.parametrize("who", ["admin", "owner"])
def test_the_record_routes_answer_with_the_public_fields_present_and_the_secrets_configured(gateway, host_isolated, who) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _seed_registry(gateway)
    client = admin if who == "admin" else owner
    read = client.get("/admin/api/operator/service-registry")
    assert read.status_code == 200 and not _leaks(read)
    body = read.json()
    services = body["service_registry"]["services"]
    assert {"engine-a", "engine-b", "engine-d"} <= set(services)
    # unrelated public fields are still there: hiding everything is not the goal
    assert services["engine-a"]["name"] == "Engine A" and services["engine-a"]["config"]["models"][0]["id"] == "first"
    assert services["engine-a"]["config"]["address"] == "http://engine.invalid:8080/v1"
    headers = services["engine-a"]["config"]["headers"]
    assert [row["name"] for row in headers] == ["X-Region", "X-Vendor"] and all(row["configured"] is True for row in headers)
    assert services["engine-a"]["process"]["start"][:2] == ["runner", "--token"] and services["engine-a"]["process"]["start"][2]["configured"] is True
    assert {item["id"] for item in body["excluded"]} == set(canary.held_records())
    engine = client.get("/admin/api/engines/engine-a")
    assert engine.status_code == 200 and not _leaks(engine)
    for route in ("/admin/api/operator/litellm-config", "/admin/api/operator/overrides", "/admin/api/extensions"):
        response = client.get(route)
        assert response.status_code == 200 and not _leaks(response), route


def test_the_detector_fails_on_a_canary_placed_in_an_isolated_output() -> None:
    """A positive control: a clean sweep means something only if the detector can fail."""
    assert canary.leaks_in("nothing here") == []
    for name, value in canary.MARKERS.items():
        assert name in canary.leaks_in(f'{{"x": "{value}"}}'), name
    assert canary.leaks_in("sk-live-UNPLANTED0000000000abcd") != []  # the key-shaped tripwire


def test_every_canary_the_seed_planted_is_in_the_store_and_the_runtime_resolves_it(gateway) -> None:
    """The seeded credentials reached the store, so a clean answer means the layers hid them, not that they were never there."""
    _seed_registry(gateway)
    held = {name for name in S if name.startswith("held-") or name in ("provider-key", "argv-positional")}
    found = canary.stored_values(gateway.path)
    absent = sorted(name for name, value in S.items() if name not in held and not any(value in text for text in found))
    assert not absent, f"canaries that never reached the secret store: {absent}"


@pytest.mark.parametrize("who", ["admin", "owner"])
def test_a_registry_read_and_written_back_unchanged_keeps_every_stored_secret(gateway, who) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    document = canary.seed_direct(gateway)
    client = admin if who == "admin" else owner
    read = client.get("/admin/api/operator/service-registry")
    assert read.status_code == 200 and not _leaks(read)

    written = client.put("/admin/api/operator/service-registry", headers={**csrf_for(client), "If-Match": read.headers["etag"]},
                         json=read.json()["service_registry"])

    assert written.status_code == 200, written.text
    assert not _leaks(written)
    after = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]
    for service_id in ("engine-a", "engine-b", "engine-d"):
        assert sorted(record_carriers.refs_in(after["services"][service_id])) == sorted(
            record_carriers.refs_in(document["services"][service_id])), service_id
    assert set(canary.held_records()) <= set(after["services"]), "the left-out records were kept"
