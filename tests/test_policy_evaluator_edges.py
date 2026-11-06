"""Exact texts, defaults and scope matching of gateway/policy.py and runtime_auth.py (mutation round 3).

Each test names the mutants it kills in its docstring (line numbers of gateway/policy.py unless noted).
"""

from __future__ import annotations

import pytest

from gateway import policy, runtime_auth
from gateway.access_plans import AccessPlan, CapabilityGrant, DirectEngineAccess
from gateway.policy import (
    NO_ACCESS,
    EffectiveAccess,
    PolicyDecision,
    PolicyRequest,
    cannot_start_model,
    evaluate,
    matches_operation,
)


def plan(plan_id="p1", **kw) -> AccessPlan:
    return AccessPlan(id=plan_id, name=plan_id, **kw)


def both(person: AccessPlan | None, app: AccessPlan | None = None) -> EffectiveAccess:
    return EffectiveAccess(person_plan=person, app_plan=person if app is None else app)


def test_the_cannot_start_model_denial_has_the_documented_code_text_and_side():
    """:39 code constant; :301 return; :304 and->or (side of a plan that cannot load)."""
    refused = cannot_start_model(both(plan(can_load_models=False)), "Model X")
    assert refused is not None
    assert refused.code == "cannot_start_model" and refused.side == "person" and refused.denied
    assert refused.reasons == (
        "Model X isn't running, and your access plan can't start it. "
        "Try again when it's running, or ask the gateway owner.",)
    by_app = cannot_start_model(both(plan(can_load_models=True), plan("p2", can_load_models=False)), "Model X")
    assert by_app.side == "app"
    assert cannot_start_model(NO_ACCESS, "m").side == "person"


@pytest.mark.parametrize("tags", ["abc", b"abc", [1, 2]], ids=["str", "bytes", "non-string"])
def test_capability_tags_must_be_an_iterable_of_strings(tags):
    """:77 and :80 message."""
    with pytest.raises(ValueError) as raised:
        PolicyRequest(capability_tags=tags)
    assert str(raised.value) == "capability_tags must be an iterable of strings"


def test_capability_tags_become_a_frozenset():
    """:81 setattr removed / attribute name."""
    request = PolicyRequest(capability_tags=["b", "a", "a"])
    assert type(request.capability_tags) is frozenset and request.capability_tags == {"a", "b"}


def test_a_decision_defaults_to_basic_visibility_and_reports_its_receipt_fields():
    """:91 default; :104-108 receipt_fields keys and return."""
    decision = PolicyDecision(decision="allowed", sources=("s1", "s2"), reasons=("r1",))
    assert decision.visibility_level == "basic"
    assert decision.receipt_fields() == {
        "decision": "allowed", "sources": ["s1", "s2"], "reasons": ["r1"], "visibility_level": "basic"}


def test_receipt_detail_is_basic_unless_both_plans_exist():
    """:139 'basic' fallback and its return."""
    full = plan(receipt_detail="full")
    assert EffectiveAccess(person_plan=full, app_plan=None).receipt_detail == "basic"
    assert EffectiveAccess(person_plan=None, app_plan=full).receipt_detail == "basic"
    assert NO_ACCESS.receipt_detail == "basic"
    assert policy.visibility_for(both(plan(receipt_detail="standard"))) == "standard"


def test_operation_scope_matching_edges():
    """:203 split once; :208 invalid path is False; :213 method case; :215 lstrip; :221 prefix cut; :223 prefix hit."""
    assert matches_operation("GET /a b", frozenset({"GET /a b"})) is True  # :203 (split keeps the space in the path)
    assert matches_operation("GET /../x", frozenset({"*"})) is False  # :208
    assert matches_operation("get /x", frozenset({"GET *"})) is True  # :213
    assert matches_operation("GET /Xray", frozenset({"Xray"})) is True  # :215 first lstrip
    assert matches_operation("GET Xray", frozenset({"/Xray"})) is True  # :215 second lstrip
    assert matches_operation("GET /v1x", frozenset({"GET /v1/*"})) is False  # :221
    assert matches_operation("POST /v1/chat", frozenset({"/v1/*"})) is True  # path-only prefix scope
    assert matches_operation("GET /v1/x", frozenset({"GET /v1/*"})) is True  # :223 method-qualified prefix scope
    assert matches_operation("GET /v2/x", frozenset({"GET /v1/*"})) is False
    assert matches_operation("POST /v1/chat", frozenset()) is False


class _InOrder(frozenset):
    """A scope set that iterates in the order given, so a test does not depend on hash order."""

    def __new__(cls, *items):
        self = super().__new__(cls, items)
        self._order = items
        return self

    def __iter__(self):
        return iter(self._order)


def test_a_plain_scope_before_a_prefix_scope_does_not_end_the_search():
    """:220 continue->break (a non-prefix scope must be skipped, not stop the loop)."""
    assert matches_operation("GET /v1/x", _InOrder("GET /other", "GET /v1/*")) is True
    assert matches_operation("GET /v1/x", _InOrder("GET /other", "GET /v2/*")) is False


def test_item_label_prefers_the_workflow_then_model_then_capability_label():
    """:234 capability test; :235 label lookup; :236 default text."""
    assert policy._item_label(PolicyRequest(workflow="w", model="m"), "chat") == "w"
    assert policy._item_label(PolicyRequest(model="m"), "chat") == "m"
    assert policy._item_label(PolicyRequest(), "chat") == "Chat"
    assert policy._item_label(PolicyRequest(), "unlisted") == "unlisted"
    assert policy._item_label(PolicyRequest(), None) == "This request"


def test_a_capability_without_a_grant_is_refused_in_the_label_of_the_capability():
    chat_only = plan(can_use={"chat": CapabilityGrant(models=frozenset({"m1"}))})
    decision = evaluate(both(chat_only), PolicyRequest(intent="image.generate"))
    assert decision.code == "not_in_access_plan" and decision.side == "person"
    assert decision.reasons == ("Images isn't in your access plan.",)


def test_native_passthrough_alone_or_the_direct_intent_alone_enters_direct_engine_checks():
    """:251 or->and; :255 'this engine'; :258 engine label of an operation refusal."""
    none = plan()
    by_flag = evaluate(both(none), PolicyRequest(intent="chat", native_passthrough=True, native_backend="e1"))
    assert by_flag.reasons == ("Direct engine access to e1 isn't in your access plan.",)
    by_intent = evaluate(both(none), PolicyRequest(intent="provider.native"))
    assert by_intent.reasons == ("Direct engine access to this engine isn't in your access plan.",)

    scoped = plan(direct_engine_access=DirectEngineAccess(engines=frozenset({"e1"}), operations=frozenset({"GET /a"})))
    wrong_op = evaluate(both(scoped), PolicyRequest(intent="provider.native", native_backend="e1", native_operation="POST /b"))
    assert wrong_op.reasons == ("POST /b on e1 isn't in your access plan.",)
    any_engine = evaluate(both(scoped), PolicyRequest(intent="provider.native", native_operation="POST /b"))
    assert any_engine.reasons == ("POST /b on this engine isn't in your access plan.",)


def test_the_app_side_texts_when_the_app_has_no_plan_or_the_plan_allows():
    """:286 and :296."""
    missing_app = evaluate(EffectiveAccess(person_plan=plan(), app_plan=None), PolicyRequest())
    assert missing_app.side == "app" and missing_app.code == "no_access_plan"
    assert missing_app.reasons == ("This app has no access plan. Ask the gateway owner to choose one.",)
    assert evaluate(both(plan()), PolicyRequest()).reasons == ("allowed by your access plan",)


class _Receipt:
    def __init__(self, with_visibility: bool):
        self.calls: list[tuple] = []
        if with_visibility:
            self.visibility = "unset"

    def set_policy_decision(self, decision, *, reason=None, source=None):
        self.calls.append((decision, reason, source))


def test_applying_a_decision_pairs_sources_and_reasons_and_sets_the_visibility():
    """:325 index bound for either list; :327 hasattr test; :328 setattr and its attribute name."""
    two_sources = PolicyDecision("allowed", sources=("s1", "s2"), reasons=("r1",), visibility_level="full")
    receipt = _Receipt(with_visibility=True)
    policy.apply_to_receipt(two_sources, receipt)
    assert receipt.calls == [("allowed", "r1", "s1"), ("allowed", None, "s2")] and receipt.visibility == "full"
    two_reasons = PolicyDecision("denied", sources=("s1",), reasons=("r1", "r2"))
    receipt = _Receipt(with_visibility=False)
    policy.apply_to_receipt(two_reasons, receipt)
    assert receipt.calls == [("denied", "r1", "s1"), ("denied", "r2", None)]
    assert not hasattr(receipt, "visibility")


def test_a_receipt_without_a_decision_hook_is_refused_with_the_documented_message():
    """:316 message."""
    with pytest.raises(TypeError) as raised:
        policy.apply_to_receipt(PolicyDecision("allowed"), object())
    assert str(raised.value) == "receipt must expose set_policy_decision(decision, ...)"


def test_runtime_auth_plan_ref_joins_plan_ids_and_makes_policy_requests_with_the_same_tag_rule():
    """runtime_auth :78 '+'; :100 message."""
    identity = runtime_auth.RuntimeIdentity(name="n", key_hash="h", access=both(plan("a"), plan("b")))
    assert runtime_auth.plan_ref(identity) == "a+b"
    assert runtime_auth.plan_ref(None) is None
    with pytest.raises(ValueError) as raised:
        runtime_auth.make_policy_request(capability_tags="abc")
    assert str(raised.value) == "capability_tags must be an iterable of strings"


def test_discovery_audience_without_access_is_the_client_audience():
    """runtime_auth :161 'client'."""
    assert runtime_auth.discovery_audience_for_access(None) == "client"
    assert runtime_auth.discovery_audience_for_access(both(plan(receipt_detail="full"))) == "internal"
    assert runtime_auth.discovery_audience_for_access(both(plan(receipt_detail="standard"))) == "admin"


# --- mut4: auth-module survivors that mut3 called equivalent ---

@pytest.mark.parametrize("scope, own_path", [("X*", "/Xray/status"), ("/X*", "/Xray/status"), ("XX*", "/XXray/status")])
def test_a_scope_prefix_that_starts_with_x_does_not_match_unrelated_paths(scope, own_path):
    """policy.py:224 ``prefix.lstrip("/")`` -> ``lstrip("XX/XX")``.

    The mutant also strips leading X from the prefix, so "X*" becomes "" and the scope matches every
    path: a widening of what a plan allows.
    """
    assert matches_operation("GET /v1/models", frozenset({scope})) is False
    assert matches_operation("GET /v1/models", frozenset({scope, "GET /v1/models"})) is True
    assert matches_operation(f"GET {own_path}", frozenset({scope})) is True


def test_an_unknown_receipt_detail_falls_back_to_the_least_informative_audience():
    """runtime_auth.py:161 default ``"client"`` -> ``"XXclientXX"``.

    EffectiveAccess only yields the four known levels, so a stand-in object supplies an unknown one.
    The fallback must be the lowest tier, never an invented audience name.
    """
    from types import SimpleNamespace

    assert runtime_auth.discovery_audience_for_access(SimpleNamespace(receipt_detail="unheard-of")) == "client"
    assert runtime_auth.discovery_audience_for_access(SimpleNamespace(receipt_detail="none")) == "client"
    assert runtime_auth.discovery_audience_for_access(SimpleNamespace(receipt_detail="basic")) == "client"
