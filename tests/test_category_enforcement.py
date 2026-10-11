"""The active owner category rules tighten runtime decisions (design v3)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from gateway import category_enforcement, category_policy as policy
from gateway.category_api import snapshot_to_json
from gateway.category_store import CategoryStore
from gateway.policy import ALLOWED, DENIED, PolicyDecision, PolicyRequest


@dataclass
class Identity:
    person_id: str | None
    app_id: str | None


ALLOW = PolicyDecision(decision=ALLOWED, sources=("profile:test",), reasons=("profile allowed",))


def _snapshot(value="general", grant_capability="image.generate"):
    fact = policy.Assertion("audience", value, "owner", True, "2026-01-01", "2026-01-01",
                            "owner-1", "owner-edit-1")
    root = policy.Subject("flux@1", "workflow", (fact,))
    service = policy.Service("flux@r1", (root,))
    plan = policy.Plan("kids", (policy.Grant(grant_capability, "flux@r1"),),
                       (policy.Row("aud", "audience", "in", ("general",)),))
    return policy.PolicyVersion(
        "rules", {"audience": policy.Category("audience", "Audience", values=("general", "adult"))},
        {"mia": (plan,)}, {"family-app": (plan,)}, {"flux@r1": service})


def _activate(path: Path, snapshot) -> None:
    store = CategoryStore(path)
    row = store.create_candidate(snapshot_to_json(snapshot), None, "owner-1", "test rules")
    store.activate(row.revision, None)


@pytest.fixture
def gate(tmp_path):
    path = tmp_path / "categories.db"
    category_enforcement.configure(path)
    yield path
    category_enforcement.configure(None)


def _request(workflow="flux", intent="image.generate"):
    return PolicyRequest(protocol="openai.images", intent=intent, workflow=workflow)


def test_no_store_and_no_active_snapshot_change_nothing(gate):
    assert category_enforcement.apply(Identity("mia", "family-app"), _request(), ALLOW) is ALLOW
    CategoryStore(gate)  # store exists, nothing active
    assert category_enforcement.apply(Identity("mia", "family-app"), _request(), ALLOW) is ALLOW


def test_people_and_apps_not_in_the_rules_keep_their_access_plans(gate):
    _activate(gate, _snapshot())
    assert category_enforcement.apply(Identity("sam", "work-app"), _request(), ALLOW) is ALLOW


def test_a_governed_request_that_passes_the_rules_is_allowed(gate):
    _activate(gate, _snapshot())
    assert category_enforcement.apply(Identity("mia", "family-app"), _request(), ALLOW) is ALLOW


def test_a_governed_request_that_fails_a_required_value_is_denied(gate):
    _activate(gate, _snapshot(value="adult"))
    decision = category_enforcement.apply(Identity("mia", "family-app"), _request(), ALLOW)
    assert decision.decision == DENIED
    assert decision.reasons == ("category rules: REQUIRE_MISMATCH kids:aud audience flux@1",)
    assert category_enforcement.SOURCE in decision.sources


def test_a_governed_request_for_a_service_not_in_the_rules_is_denied(gate):
    _activate(gate, _snapshot())
    decision = category_enforcement.apply(Identity("mia", "family-app"), _request(workflow="other"), ALLOW)
    assert decision.decision == DENIED
    assert "NO_GRANT" in decision.reasons[-1]


def test_a_capability_the_plan_does_not_grant_is_denied(gate):
    _activate(gate, _snapshot())
    decision = category_enforcement.apply(
        Identity("mia", "family-app"), _request(intent="image.edit"), ALLOW)
    assert decision.decision == DENIED


def test_a_governed_person_on_an_unlisted_app_is_denied(gate):
    _activate(gate, _snapshot())
    decision = category_enforcement.apply(Identity("mia", "other-app"), _request(), ALLOW)
    assert decision.decision == DENIED
    assert "IDENTITY_UNAVAILABLE" in decision.reasons[-1]


def test_rules_never_widen_a_denied_decision(gate):
    _activate(gate, _snapshot())
    denied = PolicyDecision(decision=DENIED, sources=("profile:test",), reasons=("no",))
    assert category_enforcement.apply(Identity("mia", "family-app"), _request(), denied) is denied


def test_an_unreadable_active_snapshot_denies_everyone(gate):
    _activate(gate, _snapshot())
    import sqlite3
    with sqlite3.connect(gate) as conn:
        conn.execute("DROP TRIGGER IF EXISTS policy_snapshots_no_update")
        conn.execute("UPDATE policy_snapshots SET body_json = '{\"broken\": true}'")
    decision = category_enforcement.apply(Identity("sam", "work-app"), _request(), ALLOW)
    assert decision.decision == DENIED
    assert "POLICY_UNAVAILABLE" in decision.reasons[-1]


def test_a_new_activation_is_picked_up_without_restart(gate):
    _activate(gate, _snapshot())
    assert category_enforcement.apply(Identity("mia", "family-app"), _request(), ALLOW) is ALLOW
    store = CategoryStore(gate)
    active = store.get_active()
    row = store.create_candidate(snapshot_to_json(_snapshot(value="adult")), active.revision,
                                 "owner-1", "tighten")
    store.activate(row.revision, active.revision)
    assert category_enforcement.apply(
        Identity("mia", "family-app"), _request(), ALLOW).decision == DENIED


def test_runtime_auth_applies_the_rules(gate):
    from gateway import runtime_auth
    _activate(gate, _snapshot(value="adult"))

    @dataclass
    class RuntimeLike:
        profile: object
        person_id: str
        app_id: str

    identity = RuntimeLike(runtime_auth.DEFAULT_PROFILE, "mia", "family-app")
    decision = runtime_auth.evaluate_and_apply(identity, _request())
    assert decision.decision == DENIED


def test_discovery_shows_the_category_decision_for_a_governed_identity(gate):
    from gateway.capabilities import Capability, capability_with_policy_view
    from gateway.policy import DEFAULT_PROFILE
    _activate(gate, _snapshot(value="adult"))
    flux = Capability(intent="image.generate", backend="comfyui", workflow="flux")
    governed = capability_with_policy_view(flux, DEFAULT_PROFILE, identity=Identity("mia", "family-app"))
    assert governed.policy_available is False
    assert any("category rules: REQUIRE_MISMATCH" in reason for reason in governed.policy_reasons)
    other = capability_with_policy_view(flux, DEFAULT_PROFILE, identity=Identity("sam", "work-app"))
    assert other.policy_available is True
