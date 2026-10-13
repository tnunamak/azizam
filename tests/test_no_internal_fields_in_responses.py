"""Guard: no JSON GET response may carry an internal-only key.

Data-driven. It walks every GET route under /me/api, /gateway/v1 and /v1 of the
production app, on fixture data, at every audience tier, and fails when any
response body contains a key from the denylist. A new internal column that
reaches a serializer fails here without a per-route test.
"""

from __future__ import annotations

import re

import pytest
from starlette.routing import Route

import proxy
import responses_store
import status_endpoint
from test_people_isolation import people  # noqa: F401  (fixture)

PREFIXES = ("/me/api", "/gateway/v1", "/v1")

# Exact key names that identify owners, hashes of inputs or credentials.
DENY_EXACT = frozenset({
    "owner_key_hash", "owner_key_hashes", "owner_scope", "key_hash", "api_key_hash",
    "input_hash", "token_hash", "secret", "client_secret", "password",
    "password_hash", "creating_receipt_id", "request_hash", "prompt_hash",
})
DENY_PATTERN = re.compile(r"(?:^|_)(?:key_hash|token_hash|password|secret_key)$")


def _denied(key: str) -> bool:
    return key in DENY_EXACT or bool(DENY_PATTERN.search(key))


def _leaks(value, path="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _denied(key):
                yield f"{path}.{key}"
            yield from _leaks(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaks(item, f"{path}[{index}]")


def _get_routes():
    routes = []
    for route in proxy.app.routes:
        if isinstance(route, Route) and "GET" in (route.methods or ()) and route.path.startswith(PREFIXES):
            routes.append(route.path)
    return sorted(set(routes))


def _fill(path: str, member) -> str:
    values = {
        "job_id": member.job_id, "request_id": member.receipt_id,
        "receipt_id": member.receipt_id, "asset_id": member.asset_id,
        "image_id": member.asset_id, "video_id": member.job_id,
        "response_id": member.job_id, "credential_id": member.key["id"],
        "provider": "example", "model_id": "example",
    }
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: values.get(m.group(1), "x"), path)


def test_route_walk_finds_the_surface():
    paths = _get_routes()
    assert len(paths) >= 20
    assert "/gateway/v1/media/jobs/{job_id}" in paths and "/me/api/session" in paths


def test_no_json_get_response_contains_an_internal_key(people):  # noqa: F811
    async def no_grid(*_args, **_kwargs):
        return []

    async def no_status(*_args, **_kwargs):
        return {"backends": [], "generated_at": 0}

    async def no_probe(*_args, **_kwargs):
        return {}

    # Availability evidence probes host services; it carries no record data.
    people.monkeypatch.setattr(proxy, "_capabilities_availability_evidence", no_probe)
    # The status grid probes systemd and engine ports; it carries no record data.
    people.monkeypatch.setattr(status_endpoint, "get_cached_status", no_status)
    people.monkeypatch.setattr(status_endpoint, "_backend_grid", no_grid)
    responses_db = people.paths["jobs"].with_name("responses.db")
    responses_store.init_db(responses_db)
    connect = responses_store._conn
    people.monkeypatch.setattr(responses_store, "_conn", lambda _path: connect(responses_db))
    member = people.alice
    failures = []
    for audience in ("client", "admin", "internal"):
        # The router captured the resolver at import; the profile mapping is looked up per request.
        people.monkeypatch.setattr(proxy, "discovery_audience_for_profile", lambda _profile: audience)
        checked = 0
        for template in _get_routes():
            headers = member.browser if template.startswith("/me/api") else member.bearer
            try:
                response = people.client.get(_fill(template, member), headers=headers)
            except BaseException as exc:  # noqa: BLE001
                leaf = exc
                while getattr(leaf, "exceptions", None):
                    leaf = leaf.exceptions[0]
                failures.append(f"[{audience}] {template} crashed: {type(leaf).__name__}: {str(leaf)[:150]}")
                continue
            if "json" not in response.headers.get("content-type", ""):
                continue
            checked += 1
            failures += [f"[{audience}] {template} {leak}" for leak in _leaks(response.json())]
        assert checked >= 10
    assert not failures, failures
