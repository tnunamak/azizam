"""Every route, method, status and Allow header matches the r18 baseline.

The baseline was recorded on release 18 (8a3147f0) before the web app moved
from /ui to the root. Any difference must be listed in DELIBERATE below.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import proxy
import route_matrix
import test_proxy_admin_integration as admin_integration
from gateway import accounts

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "route_method_baseline.json").read_text())


@pytest.fixture(scope="module")
def current():
    case = admin_integration.ProxyAdminIntegrationTests(methodName="runTest")
    case.setUp()
    try:
        accounts.initialize(case.db)
        yield route_matrix.baseline(proxy.app, case.client, case.admin_key, route_matrix.mint_app_key(case))
    finally:
        case.tearDown()


def _diff(old: dict, new: dict) -> dict:
    keys = set(old) | set(new)
    return {key: (old.get(key), new.get(key)) for key in sorted(keys) if old.get(key) != new.get(key)}


def test_routing_matches_baseline(current):
    old = {f"{m} {p}": v for p, row in BASELINE["routing"].items() for m, v in row.items()}
    new = {f"{m} {p}": v for p, row in current["routing"].items() for m, v in row.items()}
    assert _diff(old, new) == {}


def test_live_outcomes_match_baseline(current):
    assert _diff(BASELINE["live"], current["live"]) == {}
