"""The oracle for decision D21: no admin-readable route returns a stored secret.

The test seeds a store with a secret in every place one can live (start, stop, status and reload
commands in list and string form, environment values, header values, address user info and query,
probe and observation URLs, package source URLs, LiteLLM headers and base address). It then reads
every GET (and HEAD) route the app mounts under ``/admin/api`` and ``/me/api`` as an administrator and
as the owner, and fails if any seeded value shows in a response body or header. The route list comes
from ``proxy.app.routes``, so a route added later is swept without anyone remembering to add it.

The owner may read the configuration export (decision D18); that is the only exception.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import httpx
import pytest
from starlette.routing import Mount

from gateway.litellm_substrate import compile_litellm_substrate_config
from gateway import admin_store, lifecycle_drivers, media_jobs, receipt_store, service_registry
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

# One value per place a secret can live. Each is unique, so a hit names the leaking place.
S = {
    "start-separate": "SWP-start-separate-1",
    "start-equals": "SWP-start-equals-2",
    "start-attached-p": "SWP-start-attached-p-3",
    "start-attached-user": "SWP-start-attached-user-4",
    "start-jwt": "SWP-start-jwt-5",
    "start-pin": "SWP-start-pin-6",
    "start-otp": "SWP-start-otp-7",
    "start-sessionid": "SWP-start-sessionid-8",
    "start-sig": "SWP-start-sig-9",
    "start-signature": "SWP-start-signature-10",
    "start-big-p": "SWP-start-big-p-11",
    "start-auth-token": "SWP-start-auth-token-12",
    "start-auth": "SWP-start-auth-40",
    "start-pass": "SWP-start-pass-41",
    "start-pwd": "SWP-start-pwd-42",
    "start-cookie": "SWP-start-cookie-43",
    "start-bearer": "SWP-start-bearer-44",
    "start-credential": "SWP-start-credential-45",
    "start-url-userinfo": "SWP-start-url-userinfo-46",
    "stop-string": "SWP-stop-string-13",
    "stop-string-equals": "SWP-stop-string-equals-14",
    "status-list": "SWP-status-list-15",
    "reload-list": "SWP-reload-list-16",
    "reload-string": "SWP-reload-string-17",
    "env": "SWP-env-value-18",
    "env-plain-name": "SWP-env-plain-name-19",
    "legacy-start-string": "SWP-legacy-start-string-20",
    "legacy-env": "SWP-legacy-env-21",
    "header-neutral": "sk-SWP-header-neutral-22",
    "header-named": "SWP-header-named-23",
    "address-password": "SWP-address-password-24",
    "address-query": "SWP-address-query-25",
    "health-query": "SWP-health-query-26",
    "observation-path-query": "SWP-observation-path-27",
    "observation-url-userinfo": "SWP-observation-userinfo-28",
    "observation-url-query": "SWP-observation-url-query-29",
    "package-password": "SWP-package-password-30",
    "package-token": "SWP-package-token-31",
    "other-setting": "SWP-other-setting-32",
    "status-string": "SWP-status-string-33",
    "process-health-query": "SWP-process-health-34",
    "api-base-password": "SWP-api-base-password-35",
    "api-base-query": "SWP-api-base-query-36",
    "address-neutral-query": "SWP-address-neutral-query-37",
    "path-neutral-query": "SWP-path-neutral-query-38",
    "text-key-shaped": "sk-SWP-text-key-shaped-39",
}

# A route parameter is filled from these names when a seeded record has that id. Any other
# parameter (a job, a media asset, an access plan, an invite) names a resource that is not a service
# record, so the route is read with a made-up id: it must still answer without a seeded value.
PARAMETERS = {
    "service_id": "engine-a", "engine_id": "engine-a", "id": "engine-a", "provider_id": "provider-a",
    "provider": "provider-a", "package_id": "package-a",
}
UNSEEDED_ID = "unseeded-id"

# Routes the sweep does not read, each with its reason. A test below fails when an entry matches no
# mounted route, so the list cannot go stale.
EXCLUDED_ROUTES = {
    "/mcp": "opens a server-sent stream that a test client cannot read to its end",
    "/gateway/v1/media/jobs/{job_id}/events": "an event stream that stays open",
    "/me/api/prompt-memory": "writes the member's default preference row into the process-default admin database, "
                             "which this fixture does not redirect (a foreign-key error); it holds one member's own "
                             "settings and no service-record content",
    "/{path:path}": "the catch-all that serves the built web app files; no record content",
}
# Routes that serve the owner a full-fidelity backup (decision D18); an administrator still gets 403.
OWNER_EXPORT_ROUTES = frozenset({"/admin/api/config/export"})


def _generation(gateway) -> int:
    current = admin_store.get_service_registry_with_generation(path=gateway.path)
    return int(current["operator_settings_generation"]) if current else 0


def _seed_registry(gateway) -> None:
    """Write the seeded registry and package index. Safe to call again: it replaces what a call changed."""
    start = [
        "runner", "--token", S["start-separate"], f"--api-key={S['start-equals']}", f"-p{S['start-attached-p']}",
        f"-uadmin:{S['start-attached-user']}", "--jwt", S["start-jwt"], "--pin", S["start-pin"],
        "--otp", S["start-otp"], "--sessionid", S["start-sessionid"], "--sig", S["start-sig"],
        "--signature", S["start-signature"], "-P", S["start-big-p"], "--auth-token", S["start-auth-token"],
        "--auth", S["start-auth"], "--pass", S["start-pass"], "--pwd", S["start-pwd"], "--cookie", S["start-cookie"],
        "--bearer", S["start-bearer"], "--credential", S["start-credential"],
        "--upstream", f"http://u:{S['start-url-userinfo']}@argv.invalid/x",
        "--tokenizer", "plain-tokenizer", "--max-tokens", "4096",
    ]
    native = {
        "id": "engine-a", "name": "Engine A", "kind": "provider", "source": "ui", "enabled": True,
        "capabilities": ["chat"], "editable": True,
        "config": {
            "address": f"http://user:{S['address-password']}@engine.invalid:8080/v1?token={S['address-query']}&sv={S['address-neutral-query']}",
            "protocol": "openai-compatible", "models": [{"id": "first", "upstream_model": "upstream-a"}],
            "headers": [{"name": "X-Vendor", "value": S["header-neutral"]},
                        {"name": "X-Api-Key", "value": S["header-named"]}],
            "vendor_note": {"password": S["other-setting"]},
            "vendor_text": f"served by {S['text-key-shaped']} upstream",
        },
        "process": {
            "driver": "process", "start": start, "stop": f"stopper --password {S['stop-string']} --secret={S['stop-string-equals']}",
            "status": ["status", "--token", S["status-list"]],
            "profile_apply": {"kind": "restart", "reload": ["reload", "--token", S["reload-list"]]},
            "env": {"API_KEY": S["env"], "MODE": "prod", "VENDOR_NOTE": S["env-plain-name"]}, "port": {"fixed": 8080},
            "health_endpoint": f"/health?token={S['process-health-query']}",
        },
        "observations": {
            "process_ready": {"http": {"path": f"/ready?token={S['observation-path-query']}", "expect": 200}},
            "alive": {"http": {"path": f"/alive?sv={S['path-neutral-query']}", "expect": 200}},
            "reachable": {"http": {"url": f"http://u:{S['observation-url-userinfo']}@engine.invalid/h?token={S['observation-url-query']}",
                                   "expect": 200}},
        },
    }
    legacy = {
        "id": "engine-b", "name": "Engine B", "kind": "provider", "source": "ui", "enabled": True,
        "capabilities": ["chat"], "editable": True,
        "config": {"address": "http://engine-b.invalid:9090/v1", "protocol": "openai-compatible",
                   "models": [{"id": "second", "upstream_model": "upstream-b"}]},
        "lifecycle": {"driver": "command", "type": "command", "managed": True, "service_name": "",
                      "driver_config": {
                          "start": f"legacy-runner --token {S['legacy-start-string']}",
                          "stop": ["legacy-stop"], "status": f"legacy-status --password {S['status-string']}",
                          "env": {"LEGACY_PASSWORD": S["legacy-env"]},
                          "health_endpoint": f"/health?token={S['health-query']}",
                          "profile_apply": {"reload": f"reload-it --secret {S['reload-string']}"},
                      }},
    }
    routed = {
        "id": "engine-c", "name": "Engine C", "kind": "provider", "source": "ui", "enabled": True,
        "capabilities": ["chat"], "editable": True,
        "config": {"api_base": f"http://user:{S['api-base-password']}@engine-c.invalid:7070/v1?key={S['api-base-query']}",
                   "protocol": "openai-compatible", "models": [{"id": "third", "upstream_model": "upstream-c"}],
                   "headers": [{"name": "X-Vendor", "value": S["header-neutral"]}]},
    }
    registry = {"schema_version": 1, "services": {"engine-a": native, "engine-b": legacy, "engine-c": routed}}
    service_registry.migrate_engine_records_v2(registry)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry(registry), expected_generation=_generation(gateway), updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)
    admin_store.put_operator_setting("extension_package_index", {
        "schema_version": 1,
        "sources": [{"type": "git", "url": f"https://user:{S['package-password']}@git.invalid/repo.git?access_token={S['package-token']}"}],
        "packages": {"package-a": {"source": {
            "type": "git", "url": f"https://user:{S['package-password']}@git.invalid/repo.git?access_token={S['package-token']}"}}},
        "updated_at": 1,
    }, path=gateway.path)


def test_the_seeded_secrets_reach_the_compiled_router_config_the_route_projects(gateway) -> None:
    """A control: the stored source of the router config route holds the seeds, so a clean sweep means the
    projection removed them, not that the seed never got there."""
    _seed_registry(gateway)
    compiled = json.dumps(compile_litellm_substrate_config(
        admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]))
    assert S["header-neutral"] in compiled and S["api-base-password"] in compiled


def _routes(app) -> list[tuple[str, set[str]]]:
    found: list[tuple[str, set[str]]] = []

    def walk(routes, prefix: str) -> None:
        for route in routes:
            path = prefix + getattr(route, "path", "")
            if isinstance(route, Mount):
                walk(route.routes, path)
                continue
            methods = set(getattr(route, "methods", None) or ())
            if "GET" in methods:
                found.append((path, methods))

    walk(app.routes, "")
    return found


def _fill(path: str) -> str:
    """The concrete URL for a route."""
    def value(match: re.Match[str]) -> str:
        return PARAMETERS.get(match.group(1).split(":", 1)[0], UNSEEDED_ID)

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
    return sorted(name for name, seed in S.items() if seed in haystack)


@pytest.fixture
def host_isolated(monkeypatch):
    """The sweeps call real routes; the host answers "nothing here" so no route reaches outside the test."""
    # The status routes ask the host (systemctl, nvidia-smi); a test host answers "not here".
    def no_host_command(*args, **kwargs):
        raise FileNotFoundError("no host command runs in this test")

    monkeypatch.setattr(subprocess, "Popen", no_host_command)

    async def no_unit_status(self, svc):
        return lifecycle_drivers.ProcessState.UNMANAGED

    monkeypatch.setattr(lifecycle_drivers.SystemdDriver, "status", no_unit_status)

    # The status routes probe each configured engine; in a test every engine refuses the connection.
    async def refuse_async(self, request):
        raise httpx.ConnectError("no engine runs in this test", request=request)

    def refuse(self, request):
        raise httpx.ConnectError("no engine runs in this test", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    media_jobs.init_db()
    receipt_store.init_db()


def test_the_route_list_is_found_from_the_app(gateway) -> None:
    routes = _routes(gateway.proxy.app)
    paths = {path for path, _ in routes}
    assert "/admin/api/operator/service-registry" in paths and "/admin/api/engines/{service_id}" in paths
    assert {"/me/api/jobs/{job_id}", "/admin/api/overview", "/status/raw", "/setup/console"} <= paths
    assert set(EXCLUDED_ROUTES) <= paths, "an excluded route is no longer mounted: drop it from EXCLUDED_ROUTES"
    assert OWNER_EXPORT_ROUTES <= paths


def test_no_admin_or_me_get_route_returns_a_stored_secret(gateway, host_isolated) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _seed_registry(gateway)
    clients = {"admin": admin, "owner": owner, "anonymous": gateway.client()}
    swept: list[str] = []
    errored: list[str] = []
    leaks: list[str] = []
    for path, _methods in _routes(gateway.proxy.app):
        if path in EXCLUDED_ROUTES:
            continue
        concrete = _fill(path)
        for who, client in clients.items():
            if who == "owner" and path in OWNER_EXPORT_ROUTES:
                continue
            for method in ("get", "head"):
                try:
                    response = getattr(client, method)(concrete)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:  # noqa: BLE001  (a route that raises answered nothing; it is listed below)
                    errored.append(f"{who} {method.upper()} {concrete}: {'; '.join(_leaf_errors(exc)) or type(exc).__name__}")
                    continue
                found = _leaks(response)
                if found:
                    leaks.append(f"{who} {method.upper()} {concrete} -> {response.status_code}: {', '.join(found)}")
        swept.append(path)
    assert swept, "the sweep read no route"
    assert not leaks, "stored secrets in responses:\n" + "\n".join(leaks)
    assert not errored, "routes that raised instead of answering:\n" + "\n".join(errored)


# Routes that must answer 200 to an administrator and to the owner in the sweep, with the proof that
# they carry record content (the marker for a set secret). A sweep that read only error pages would pass.
MUST_SHOW_SET_SECRETS = {
    "/admin/api/operator/service-registry": "service_registry",
    "/admin/api/engines/engine-a": "engine",
    "/admin/api/operator/litellm-config": "litellm_config",
    "/admin/api/operator/overrides": "overrides",
}


@pytest.mark.parametrize("who", ["admin", "owner"])
def test_the_swept_record_routes_answer_and_show_that_a_secret_is_set(gateway, who) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _seed_registry(gateway)
    client = admin if who == "admin" else owner
    for route in MUST_SHOW_SET_SECRETS:
        response = client.get(route)
        assert response.status_code == 200, (route, response.text[:200])
        assert "[redacted]" in response.text or "<redacted>" in response.text or "git.invalid" in response.text, route


@pytest.mark.parametrize("who", ["admin", "owner"])
def test_a_registry_read_and_written_back_unchanged_keeps_every_stored_secret(gateway, who) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _seed_registry(gateway)
    client = admin if who == "admin" else owner
    before = admin_store.get_service_registry_with_generation(path=gateway.path)
    read = client.get("/admin/api/operator/service-registry")
    assert read.status_code == 200
    assert not _leaks(read)
    written = client.put("/admin/api/operator/service-registry",
                         headers={**csrf_for(client), "If-Match": read.headers["etag"]},
                         json=read.json()["service_registry"])
    assert written.status_code == 200, written.text
    assert not _leaks(written)
    after = admin_store.get_service_registry_with_generation(path=gateway.path)
    for name, secret in S.items():
        if name.startswith(("package", "other-setting")):
            continue  # the package index and the vendor note are not part of the registry round trip checked here
        assert secret in json.dumps(after["value"]), f"{name} was lost by a registry round trip"
    for service_id in ("engine-a", "engine-b", "engine-c"):
        assert after["value"]["services"][service_id] == before["value"]["services"][service_id]
