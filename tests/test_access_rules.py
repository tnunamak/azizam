"""Every access decision names the rule that made it, with a stable code.

Check access reads the rule off the decision that enforcement makes. These tests
pin the codes (they are part of the owner-facing contract: scripts and the UI
branch on them, never on the sentence) and show that the rule is there for both
outcomes and for both sides of a request: the person's plan and the app's plan.
"""
from __future__ import annotations

import sqlite3

import pytest

from gateway import category_enforcement
from gateway.capabilities import Capability, capability_with_policy_view
from gateway.policy import APP_SIDE, PERSON_SIDE, PolicyRequest, evaluate
from gateway.runtime_auth import evaluate_and_apply, make_policy_request
from tests.support.access import access, identity as runtime_identity, plan_for
from tests.test_category_visibility import OWNER, activate, rules

KIDS = plan_for("plan_kids", caps=("chat", "image"))
LISTED = plan_for("plan_listed", caps=("chat",), models=["chat-a"])


def _ask(plan_or_access, **fields):
    return evaluate(plan_or_access if hasattr(plan_or_access, "plan_ids") else access(plan_or_access),
                    make_policy_request(protocol="openai.chat", **fields))


# --- a refusal names the rule ---------------------------------------------------

def test_a_capability_the_plan_does_not_include():
    decision = _ask(KIDS, intent="video.generate", model="vid-a")
    assert decision.denied
    rule = decision.rule
    assert (rule.code, rule.kind, rule.id, rule.side, rule.subject) == (
        "plan.capability_not_granted", "plan", "plan_kids", PERSON_SIDE, "video")


def test_an_item_the_plan_does_not_list():
    decision = _ask(LISTED, intent="chat.completions", model="chat-b")
    rule = decision.rule
    assert (rule.code, rule.kind, rule.id, rule.side, rule.subject) == (
        "plan.item_not_listed", "plan", "plan_listed", PERSON_SIDE, "chat-b")


def test_the_app_side_plan_is_named_when_it_is_the_one_that_refuses():
    both = access(plan_for("plan_person"), plan_for("plan_app", caps=("chat",)))
    decision = _ask(both, intent="image.generate", model="img-a")
    assert (decision.rule.code, decision.rule.id, decision.rule.side) == (
        "plan.capability_not_granted", "plan_app", APP_SIDE)


def test_no_plan_at_all():
    person = _ask(access(None, plan_for("plan_app")), intent="chat.completions", model="m")
    assert (person.rule.code, person.rule.side, person.rule.id) == ("plan.none", PERSON_SIDE, None)
    app = _ask(access(plan_for("plan_person"), None), intent="chat.completions", model="m")
    assert (app.rule.code, app.rule.side) == ("plan.none", APP_SIDE)


def test_a_paid_provider_a_fallback_and_direct_engine_access():
    paid = _ask(KIDS, intent="chat.completions", model="m", remote_provider="openai")
    assert (paid.rule.code, paid.rule.subject) == ("plan.provider_not_granted", "openai")
    fallback = _ask(KIDS, intent="chat.completions", model="m", requires_cloud_fallback=True)
    assert fallback.rule.code == "plan.fallback_not_granted"
    direct = _ask(KIDS, native_passthrough=True, native_backend="llama-x", native_operation="GET /props")
    assert (direct.rule.code, direct.rule.subject) == ("plan.direct_engine_not_granted", "llama-x")


# --- an allowance names the rule too --------------------------------------------

def test_a_plan_that_includes_everything_of_a_kind_says_so():
    decision = _ask(KIDS, intent="chat.completions", model="chat-a")
    assert decision.allowed
    assert (decision.rule.code, decision.rule.id, decision.rule.subject) == (
        "plan.allows_all", "plan_kids", "chat")


def test_a_plan_that_lists_the_item_names_the_name_it_listed():
    decision = _ask(LISTED, intent="chat.completions", model="chat-a")
    assert (decision.rule.code, decision.rule.subject) == ("plan.lists_item", "chat-a")


def test_a_plan_may_list_the_alias_or_the_model_behind_it():
    by_alias = _ask(plan_for("p1", caps=("chat",), models=["fast"]),
                    intent="chat.completions", model="chat-a", requested_model="fast")
    by_model = _ask(plan_for("p2", caps=("chat",), models=["chat-a"]),
                    intent="chat.completions", model="chat-a", requested_model="fast")
    assert (by_alias.rule.code, by_alias.rule.subject) == ("plan.lists_item", "fast")
    assert (by_model.rule.code, by_model.rule.subject) == ("plan.lists_item", "chat-a")


def test_the_rule_does_not_change_what_a_receipt_records():
    decision = _ask(KIDS, intent="video.generate", model="vid-a")
    assert set(decision.receipt_fields()) == {"decision", "sources", "reasons", "visibility_level"}


def test_the_decision_and_its_reason_text_are_what_they_were():
    """Adding the rule leaves the sentence the data plane already returns untouched."""
    decision = _ask(KIDS, intent="video.generate", model="vid-a")
    assert decision.reasons == ("vid-a isn't in your access plan.",)
    assert decision.code == "not_in_access_plan"


def test_a_capability_carries_the_rule_of_its_policy_view():
    capability = Capability(intent="video.generate", backend="svc", model="vid-a")
    viewed = capability_with_policy_view(capability, access(KIDS))
    assert viewed.policy_rule.code == "plan.capability_not_granted"


# --- category rules --------------------------------------------------------------

@pytest.fixture
def gate(tmp_path):
    path = tmp_path / "categories.db"
    category_enforcement.configure(path, owner_id=lambda: OWNER)
    yield path
    category_enforcement.configure(None)


def _runs(plan, name):
    return evaluate_and_apply(
        runtime_identity(access(plan), app_id=None, person_id=None),
        make_policy_request(protocol="openai.chat", intent="chat.completions", workflow=name))


def test_a_restricted_category_names_the_category(gate):
    listed = plan_for("plan_listed_for_adult")
    activate(gate, rules(adult_visible_to=[listed.id]))
    hidden = _runs(KIDS, "adult-flow")
    assert hidden.denied
    assert (hidden.rule.code, hidden.rule.kind, hidden.rule.id) == ("category.hidden", "category", "adult")
    seen = _runs(listed, "adult-flow")
    assert seen.allowed and seen.rule.code == "plan.allows_all"


def test_unreadable_category_rules_are_a_cannot_tell(gate):
    activate(gate, rules(adult_visible_to=[]))
    with sqlite3.connect(gate) as conn:
        conn.execute("DROP TRIGGER IF EXISTS policy_snapshots_no_update")
        conn.execute("UPDATE policy_snapshots SET body_json = '{\"broken\": true}'")
    decision = _runs(KIDS, "anything")
    assert decision.denied
    assert (decision.rule.code, decision.rule.kind) == ("category.rules_unavailable", "category")
    assert decision.rule.id is None


def test_the_request_path_and_the_discovery_view_carry_the_same_rule(gate):
    activate(gate, rules(adult_visible_to=[]))
    request = PolicyRequest(intent="chat.completions", workflow="adult-flow")
    capability = Capability(intent="chat.completions", backend="svc", workflow="adult-flow")
    who = runtime_identity(access(KIDS), app_id=None, person_id=None)
    viewed = capability_with_policy_view(capability, access(KIDS), request=request, identity=who)
    assert viewed.policy_rule == evaluate_and_apply(who, request).rule


def test_category_labels_come_from_the_active_rules(gate):
    assert category_enforcement.category_labels() == {}
    activate(gate, rules(adult_visible_to=[]))
    assert category_enforcement.category_labels() == {"adult": "Adult", "experimental": "Experimental"}


def test_there_are_no_category_labels_when_the_gate_is_off():
    category_enforcement.configure(None)
    assert category_enforcement.category_labels() == {}


def test_unreadable_rules_give_no_labels_rather_than_an_error(gate):
    activate(gate, rules(adult_visible_to=[]))
    with sqlite3.connect(gate) as conn:
        conn.execute("DROP TRIGGER IF EXISTS policy_snapshots_no_update")
        conn.execute("UPDATE policy_snapshots SET body_json = '{\"broken\": true}'")
    assert category_enforcement.category_labels() == {}
