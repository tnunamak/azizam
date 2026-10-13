"""The active owner category rules tighten runtime decisions (design v3)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from gateway import category_enforcement, category_policy as policy
from tests.support.access import access, plan_for  # noqa: E402
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
        access: object
        person_id: str
        app_id: str

    identity = RuntimeLike(access(plan_for("default-local")), "mia", "family-app")
    decision = runtime_auth.evaluate_and_apply(identity, _request())
    assert decision.decision == DENIED


def test_discovery_shows_the_category_decision_for_a_governed_identity(gate):
    from gateway.capabilities import Capability, capability_with_policy_view
    _activate(gate, _snapshot(value="adult"))
    flux = Capability(intent="image.generate", backend="comfyui", workflow="flux")
    governed = capability_with_policy_view(flux, access(plan_for("default-local")), identity=Identity("mia", "family-app"))
    assert governed.policy_available is False
    assert any("category rules: REQUIRE_MISMATCH" in reason for reason in governed.policy_reasons)
    other = capability_with_policy_view(flux, access(plan_for("default-local")), identity=Identity("sam", "work-app"))
    assert other.policy_available is True


def test_repeated_checks_do_not_reread_an_unchanged_store(gate, monkeypatch):
    _activate(gate, _snapshot())
    reads = []
    real = CategoryStore.active_revision_counter.fget
    monkeypatch.setattr(CategoryStore, "active_revision_counter",
                        property(lambda self: reads.append(1) or real(self)))
    for _ in range(64):
        assert category_enforcement.apply(Identity("mia", "family-app"), _request(), ALLOW) is ALLOW
    assert len(reads) == 1
    store = CategoryStore(gate)
    active = store.get_active()
    row = store.create_candidate(snapshot_to_json(_snapshot(value="adult")), active.revision,
                                 "owner-1", "tighten")
    store.activate(row.revision, active.revision)
    assert category_enforcement.apply(
        Identity("mia", "family-app"), _request(), ALLOW).decision == DENIED
    assert len(reads) == 2


def test_losing_an_enforced_store_denies_everyone(gate, tmp_path):
    _activate(gate, _snapshot())
    assert category_enforcement.apply(Identity("mia", "family-app"), _request(), ALLOW) is ALLOW
    gate.rename(tmp_path / "moved.db")
    decision = category_enforcement.apply(Identity("sam", "work-app"), _request(), ALLOW)
    assert decision.decision == DENIED
    assert "POLICY_UNAVAILABLE" in decision.reasons[-1]


def test_two_active_revisions_of_one_service_are_rejected():
    base = _snapshot()
    second = policy.Service("flux@r2", base.services["flux@r1"].members)
    ambiguous = policy.PolicyVersion(base.id, base.categories, base.people, base.apps,
                                     {**base.services, "flux@r2": second})
    with pytest.raises(policy.PolicyError, match="one active revision per service"):
        policy.validate_snapshot(ambiguous)


@pytest.mark.parametrize("identity,workflow,intent,expected", [
    (Identity("sam", "work-app"), "other", "image.edit", True),
    (Identity("mia", "family-app"), "flux", "image.generate", True),
    (Identity("mia", "family-app"), "other", "image.generate", False),
    (Identity("mia", "family-app"), "flux", "image.edit", False),
    (Identity("mia", "other-app"), "flux", "image.generate", False),
])
def test_visible_uses_the_runtime_category_decision(gate, identity, workflow, intent, expected):
    _activate(gate, _snapshot())
    assert category_enforcement.visible(identity, _request(workflow, intent)) is expected


def test_visible_without_a_gate_keeps_discovery():
    category_enforcement.configure(None)
    assert category_enforcement.visible(Identity("mia", "family-app"), _request())


def test_visible_fails_closed_when_the_store_is_unreadable(gate):
    gate.write_text("not a database")
    assert not category_enforcement.visible(Identity("sam", "work-app"), _request())


# Mutation round 2 (scripts/mutate): pins the survivors of the clean baseline sweep.

def test_the_source_label_in_receipts_is_the_literal_policy_categories(gate):
    _activate(gate, _snapshot(value="adult"))
    decision = category_enforcement.apply(Identity("mia", "family-app"), _request(), ALLOW)
    assert decision.sources == ("profile:test", "policy:categories")


def test_a_service_stem_drops_only_the_final_revision_suffix():
    assert category_enforcement._stem("flux@r1") == "flux"
    assert category_enforcement._stem("team@box@r7") == "team@box"
    assert category_enforcement._stem("plain") == "plain"


def test_an_unexpected_store_error_denies_and_the_next_check_recovers(gate, monkeypatch):
    _activate(gate, _snapshot())
    real = CategoryStore.get_active
    calls = []

    def flaky(self):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("store exploded")
        return real(self)

    monkeypatch.setattr(CategoryStore, "get_active", flaky)
    decision = category_enforcement.apply(Identity("sam", "work-app"), _request(), ALLOW)
    assert decision.decision == DENIED
    assert decision.reasons == ("category rules: POLICY_UNAVAILABLE",)
    assert category_enforcement.apply(Identity("mia", "family-app"), _request(), ALLOW) is ALLOW


def _empty_rules(path: Path) -> None:
    plan = policy.Plan("kids", (), ())
    _activate(path, policy.PolicyVersion("rules", {}, {"mia": (plan,)}, {"family-app": (plan,)}, {}))


def test_a_route_check_with_no_services_names_the_requested_capability(gate):
    _empty_rules(gate)
    decision = category_enforcement.apply(
        Identity("mia", "family-app"), PolicyRequest(protocol="openai.images", intent="image.generate"), ALLOW)
    assert decision.reasons == ("category rules: NO_GRANT (no service grants image.generate)",)


def test_a_route_check_with_no_services_and_no_capability_says_this_request(gate):
    _empty_rules(gate)
    decision = category_enforcement.apply(
        Identity("mia", "family-app"), PolicyRequest(protocol="openai.images"), ALLOW)
    assert decision.reasons == ("category rules: NO_GRANT (no service grants this request)",)
