"""Decision D32 (e): two configurations that differ only in secret bytes give identical management outputs.

A secret changes only through a command (decision D27), and the entry that holds it keeps its id. So the test plants
canaries, reads every GET route as the owner, replaces every secret with different bytes through the replace commands (and
the provider key route), reads every route again, and requires the two readings to be byte-identical once the approved
differences (the revision of the registry and the times a change was made) are set aside. It is stronger than searching
for a literal: it fails for a secret that influences a public field, an ordering, a length or an error text.
"""

from __future__ import annotations

import json
import re

import pytest

from gateway import admin_store
from gateway.availability import EVIDENCE_TIME_FIELDS
from tests.support import canary
from tests.test_admin_secret_sweep import host_isolated, sweep_get_routes  # noqa: F401
from tests.test_secret_canary_matrix import REGISTRY, _etag, _plant
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

M = canary.MARKERS
EPOCH = re.compile(r"\b1[5-9]\d{8}(?:\.\d+)?\b")
# The evidence times are the instants a health check or request produced the evidence. The status cache lives 12 s, so a
# slow run that crosses it stamps a later second. The time is a clock reading, not a function of a secret's bytes.
VOLATILE_KEYS = {
    "updated_at", "created_at", "checked_at", "operator_settings_generation", "etag", "request_id", "latency_ms",
    *EVIDENCE_TIME_FIELDS,
}


# The two attention texts admin_api builds from an app's request counters (error rate, median latency). The app items
# built from configuration ("has no access plan", "has no active key") are not matched and stay compared.
LIVE_TRAFFIC_DETAIL = re.compile(r"\d+% errors across \d+ requests in the last 24h\.|Median latency is \d+(?:\.\d+)?s\.")


def _live_traffic_item(value) -> bool:
    """An attention item computed from an app's request counters of the last 24 h (admin_api: error rate, latency).

    Those counters are live, like the routes in NOT_A_FUNCTION_OF_STATE: this test's own reads move them, so the item's
    numbers, and whether it appears at all, are not a function of the stored configuration."""
    return (
        isinstance(value, dict) and value.get("kind") == "app" and isinstance(value.get("detail"), str)
        and LIVE_TRAFFIC_DETAIL.fullmatch(value["detail"]) is not None
    )


def _normalize(text: str) -> str:
    """The reading with the approved differences set aside: a revision number, the time of a change, live traffic."""
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in VOLATILE_KEYS}
        if isinstance(value, list):
            return [clean(item) for item in value if not _live_traffic_item(item)]
        return value

    try:
        return json.dumps(clean(json.loads(text)), sort_keys=True)
    except ValueError:
        return EPOCH.sub("<time>", text)


# Routes whose answer is not a function of the stored configuration: a history (it grows with every change made, including
# the replacement this test makes) or live counters (uptime, drain state). Each with its reason.
NOT_A_FUNCTION_OF_STATE = {
    "/admin/api/audit": "the audit log grows with every change, the replacement included",
    "/admin/api/audit-events": "the audit log grows with every change, the replacement included",
    "/admin/api/drain": "live drain state", "/admin/api/status": "live counters", "/status/raw": "live counters",
    "/status/raw/": "live counters", "/v1/status": "live counters",
}


def _reading(gateway, owner) -> dict[str, str]:
    from tests.test_admin_secret_sweep import _fill, _get_routes, EXCLUDED_ROUTES

    out: dict[str, str] = {}
    for path in _get_routes(gateway.proxy.app):
        if path in EXCLUDED_ROUTES or path in NOT_A_FUNCTION_OF_STATE:
            continue
        response = owner.get(_fill(path))
        out[path] = f"{response.status_code} {_normalize(response.text)}"
    return out


def _replace_every_secret(owner) -> int:
    """Replace the bytes of every secret entry of every service by a command that names its id, and the provider key."""
    changed = 0
    registry = owner.get(REGISTRY).json()["service_registry"]["services"]
    for service_id, record in registry.items():
        commands = []

        def entries(carrier, items):
            for item in items:
                if isinstance(item, dict) and item.get("configured") is not None and "id" in item and "value" not in item:
                    commands.append({"carrier": carrier, "operation": "replace", "entry_id": item["id"], "value": f"REPLACED-{len(commands)}-{item['id'][-6:]}"})

        entries("config.headers", record.get("config", {}).get("headers") or [])
        for name, item in (record.get("config", {}).get("env") or {}).items():
            entries("config.env", [item])
        if commands:
            response = owner.post(f"/admin/api/operator/services/{service_id}/secret-entries", headers=_etag(owner), json={"commands": commands})
            assert response.status_code == 200, response.text
            changed += len(commands)
    key = owner.put("/admin/api/providers/provider-a/key", headers=csrf_for(owner), json={"api_key": "sk-or-v1-REPLACEDprovider0009abcdef"})
    assert key.status_code == 200, key.text
    return changed + 1


def test_replacing_every_secret_with_different_bytes_changes_no_management_output(gateway, host_isolated, tmp_path, monkeypatch) -> None:
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway, owner, tmp_path, monkeypatch)
    before = _reading(gateway, owner)

    changed = _replace_every_secret(owner)
    after = _reading(gateway, owner)

    assert changed >= 4, "the control must replace a secret of each kind it can"
    assert set(before) == set(after)
    differing = sorted(path for path in before if before[path] != after[path])
    import difflib
    detail = {path: list(difflib.unified_diff(
        json.dumps(json.loads(before[path][4:]), sort_keys=True, indent=2).splitlines(),
        json.dumps(json.loads(after[path][4:]), sort_keys=True, indent=2).splitlines(), n=0,
    )) for path in differing}
    assert not differing, f"outputs that depend on a secret's bytes: {detail}"
    assert len(before) > 100 and sum(1 for text in before.values() if text.startswith("200 ")) > 40


def test_the_reading_does_change_when_a_public_field_changes(gateway, host_isolated, tmp_path, monkeypatch) -> None:
    """A control for the comparison itself: it must be able to see a difference."""
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway, owner, tmp_path, monkeypatch)
    before = _reading(gateway, owner)
    renamed = owner.patch("/admin/api/operator/services/matrix-engine", headers=_etag(owner), json={"name": "A different public name"})
    assert renamed.status_code == 200, renamed.text
    after = _reading(gateway, owner)
    assert before[REGISTRY] != after[REGISTRY]

    # A later clock reading in the evidence is no difference (the run can outlast the 12 s status cache); a changed status is.
    evidence = {"observed_at_s": 1792370504, "last_attempt_at_s": 1792370504, "last_success_at_s": 1792370400, "status": "ready"}
    later = {**evidence, "observed_at_s": 1792370521, "last_attempt_at_s": 1792370521, "last_success_at_s": 1792370510}
    assert _normalize(json.dumps({"evidence": evidence})) == _normalize(json.dumps({"evidence": later}))
    assert _normalize(json.dumps({"evidence": evidence})) != _normalize(json.dumps({"evidence": {**later, "status": "unavailable"}}))

    # An app's live error-rate item is no difference (this test's own reads move the counters); a configuration item still is.
    app_item = {"kind": "app", "target_kind": "app", "severity": "problem", "detail": "32% errors across 106 requests in the last 24h."}
    plan_item = {"kind": "app", "target_kind": "app", "severity": "problem", "detail": "It can't run anything. Choose a plan."}
    assert _normalize(json.dumps({"items": [app_item, plan_item]})) == _normalize(json.dumps({"items": [{**app_item, "detail": "68% errors across 318 requests in the last 24h."}, plan_item]}))
    assert _normalize(json.dumps({"items": [app_item]})) == _normalize(json.dumps({"items": []}))
    assert _normalize(json.dumps({"items": [plan_item]})) != _normalize(json.dumps({"items": []}))
