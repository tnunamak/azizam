"""Access plans v4: the plan document and the two-plan evaluator (design §2, §5.1)."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import provenance, route_registry  # noqa: E402
from gateway.access_plans import (  # noqa: E402
    CAPABILITIES, DISCOVERY_INTENTS, CapabilityGrant, DirectEngineAccess, PlanError,
    capability_for_intent, plan_from_document, plan_summary, plan_to_document, union_plans,
)
from gateway.policy import (  # noqa: E402
    APP_SIDE, NO_ACCESS, NOT_IN_ACCESS_PLAN, NO_ACCESS_PLAN, PERSON_SIDE,
    EffectiveAccess, PolicyRequest, evaluate, matches_operation,
)
from tests.support.access import access, everything, plan  # noqa: E402


def _document(**overrides):
    doc = {
        "name": "Family",
        "can_use": {"chat": {"items": [{"model": "family-chat"}]}, "image": {"items": "all"}},
        "direct_engine_access": {"engines": [], "operations": []},
        "paid_providers": [],
        "provider_fallback": False,
        "can_load_models": True,
        "receipt_detail": "basic",
        "media_metadata": "none",
    }
    doc.update(overrides)
    return doc


# -- the document ------------------------------------------------------------


def test_document_round_trips():
    parsed = plan_from_document(_document(), plan_id="plan_1")
    assert parsed.can_use["chat"] == CapabilityGrant(models=frozenset({"family-chat"}))
    assert parsed.can_use["image"].all_items
    again = plan_from_document(plan_to_document(parsed))
    assert again == parsed


@pytest.mark.parametrize("field", [
    "name", "can_use", "direct_engine_access", "paid_providers", "provider_fallback",
    "can_load_models", "receipt_detail", "media_metadata",
])
def test_every_field_is_required(field):
    doc = _document()
    del doc[field]
    with pytest.raises(PlanError, match=field):
        plan_from_document(doc, plan_id="plan_1")


@pytest.mark.parametrize("field", ["blocked", "must_match", "caps"])
def test_parts_not_built_yet_are_refused_not_ignored(field):
    with pytest.raises(PlanError, match="not available"):
        plan_from_document(_document(**{field: []}), plan_id="plan_1")


def test_unknown_field_and_capability_are_refused():
    with pytest.raises(PlanError, match="protocols"):
        plan_from_document(_document(protocols=["openai"]), plan_id="plan_1")
    with pytest.raises(PlanError, match="unknown capability"):
        plan_from_document(_document(can_use={"teleport": {"items": "all"}}), plan_id="plan_1")


def test_summary_names_capabilities_and_paid_models():
    family = plan_from_document(_document(), plan_id="plan_1")
    assert plan_summary(family) == "Chat and Images. No paid models."
    assert plan_summary(everything(paid_providers=frozenset({"openrouter"}))) == (
        "Everything on this gateway. Paid models: openrouter. Direct engine access.")


# -- step 3: a missing plan denies --------------------------------------------


def test_no_plan_anywhere_denies():
    decision = evaluate(NO_ACCESS, PolicyRequest(intent="chat.completions", model="m"))
    assert decision.denied and decision.code == NO_ACCESS_PLAN and decision.side == PERSON_SIDE


def test_app_without_assignment_denies_with_app_side():
    decision = evaluate(access(plan(), None), PolicyRequest(intent="chat.completions"))
    assert decision.denied and decision.code == NO_ACCESS_PLAN and decision.side == APP_SIDE
    assert "This app has no access plan" in decision.reasons[0]


def test_discovery_needs_a_plan_but_no_capability():
    assert evaluate(access(plan(can_use={})), PolicyRequest(intent="models.list")).allowed
    assert evaluate(NO_ACCESS, PolicyRequest(intent="models.list")).denied


# -- step 5: grants on both sides --------------------------------------------


def test_person_allows_app_does_not():
    chat_only = plan("plan_chat", can_use={"chat": CapabilityGrant(all_items=True)})
    decision = evaluate(access(plan(), chat_only), PolicyRequest(intent="image.generate", model="flux"))
    assert decision.denied and decision.code == NOT_IN_ACCESS_PLAN and decision.side == APP_SIDE
    assert decision.reasons[0].startswith("Your access plan allows flux, but this app's access plan doesn't.")


def test_app_allows_person_does_not():
    chat_only = plan("plan_chat", can_use={"chat": CapabilityGrant(all_items=True)})
    decision = evaluate(access(chat_only, plan()), PolicyRequest(intent="image.generate", model="flux"))
    assert decision.denied and decision.side == PERSON_SIDE
    assert decision.reasons[0] == "flux isn't in your access plan."


def test_same_plan_both_sides_is_evaluated_once():
    shared = plan(can_use={})
    decision = evaluate(access(shared, shared), PolicyRequest(intent="chat.completions", model="m"))
    assert decision.side == PERSON_SIDE and decision.sources == ("access_plan:plan_test",)


def test_listed_items_grant_only_those_items():
    listed = plan(can_use={"chat": CapabilityGrant(models=frozenset({"a"})),
                           "image": CapabilityGrant(workflows=frozenset({"flux-dev"}))})
    effective = access(listed)
    assert evaluate(effective, PolicyRequest(intent="chat.completions", model="a")).allowed
    assert evaluate(effective, PolicyRequest(intent="chat.completions", model="b")).denied
    assert evaluate(effective, PolicyRequest(intent="image.generate", workflow="flux-dev")).allowed
    assert evaluate(effective, PolicyRequest(intent="image.generate", workflow="sdxl")).denied


def test_unknown_intent_denies():
    assert evaluate(access(everything()), PolicyRequest(intent="teleport.now")).denied


def test_paid_providers_are_an_intersection():
    both = everything(paid_providers=frozenset({"openrouter", "typesafe"}))
    one = everything("plan_one", paid_providers=frozenset({"typesafe"}))
    effective = access(both, one)
    assert effective.paid_providers == {"typesafe"}
    assert evaluate(effective, PolicyRequest(intent="decision.systemone", remote_provider="typesafe")).allowed
    denied = evaluate(effective, PolicyRequest(intent="chat.completions", remote_provider="openrouter"))
    assert denied.denied and denied.side == APP_SIDE


def test_provider_fallback_needs_both_plans():
    on = everything(paid_providers=frozenset({"openrouter"}), provider_fallback=True)
    off = everything("plan_off", paid_providers=frozenset({"openrouter"}))
    request = PolicyRequest(intent="chat.completions", remote_provider="openrouter", remote_paid_fallback=True)
    assert evaluate(access(on, on), request).allowed
    assert evaluate(access(on, off), request).denied


def test_direct_engine_access_is_bounded_by_engine_and_operation():
    props_only = plan(direct_engine_access=DirectEngineAccess(
        engines=frozenset({"llama"}), operations=frozenset({"GET /props"})))
    effective = access(everything(), props_only)
    def native(engine, operation):
        return PolicyRequest(intent="provider.native", native_passthrough=True,
                             native_backend=engine, native_operation=operation)
    assert evaluate(effective, native("llama", "GET /props")).allowed
    assert evaluate(effective, native("llama", "POST /delete")).denied
    assert evaluate(effective, native("tabby", "GET /props")).denied


def test_empty_direct_engine_access_grants_nothing():
    no_ops = plan(direct_engine_access=DirectEngineAccess(all_engines=True))
    assert not matches_operation("GET /props", frozenset())
    request = PolicyRequest(intent="provider.native", native_passthrough=True, native_backend="llama")
    assert evaluate(access(no_ops), request).denied


# -- composition of settings (design §2.6) -----------------------------------


def test_settings_take_the_lower_of_both_plans():
    high = everything(receipt_detail="full", media_metadata="full", can_load_models=True)
    low = plan(receipt_detail="basic", media_metadata="none", can_load_models=False)
    effective = access(high, low)
    assert effective.receipt_detail == "basic"
    assert effective.media_metadata == "none"
    assert effective.can_load_models is False
    assert access(high, high).media_metadata == "full"


def test_media_metadata_is_a_bound_a_request_can_only_lower():
    assert provenance.resolve_metadata_mode("full", "none") == "none"
    assert provenance.resolve_metadata_mode("none", "full") == "none"
    assert provenance.resolve_metadata_mode(None, "full") == "full"
    assert provenance.resolve_metadata_mode(None, None) == "none"


def test_union_covers_everything_either_plan_allows():
    a = plan("plan_a", can_use={"chat": CapabilityGrant(models=frozenset({"x"}))}, receipt_detail="standard")
    b = plan("plan_b", can_use={"chat": CapabilityGrant(all_items=True), "image": CapabilityGrant(all_items=True)},
             paid_providers=frozenset({"openrouter"}))
    merged = union_plans("plan_u", "U", [a, b])
    assert merged.can_use["chat"].all_items and "image" in merged.can_use
    assert merged.paid_providers == {"openrouter"} and merged.receipt_detail == "standard"


# -- every intent a route emits has a capability ------------------------------


def _emitted_intents() -> set[str]:
    intents: set[str] = set()
    pattern = re.compile(r"""intent\s*=\s*["']([a-z_]+\.[a-z_.]+)["']""")
    for path in [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py"))]:
        intents.update(pattern.findall(path.read_text(encoding="utf-8")))
    config = yaml.safe_load((ROOT / "proxy_config.yml").read_text(encoding="utf-8"))
    for policy in route_registry.runtime_route_policy(proxy_config=config).values():
        if policy.get("intent"):
            intents.add(str(policy["intent"]))
    for _, intent in route_registry.azizam_policy(proxy_config=config).values():
        intents.add(intent)
    return intents


# Intents that are not access-plan capabilities: model management through
# Azizam's own Ollama routes answers 501 and is never grantable (design §2.2);
# realtime and the media job ledger are admission intents, not policy intents.
_NOT_PLAN_INTENTS = {"models.pull", "models.push", "models.create", "models.copy", "models.delete",
                     "audio.realtime"}


def test_every_route_intent_maps_to_a_capability():
    unmapped = sorted(
        intent for intent in _emitted_intents() - DISCOVERY_INTENTS - _NOT_PLAN_INTENTS - {"provider.native"}
        if capability_for_intent(intent) is None
    )
    assert unmapped == []
    assert set(capability_for_intent(i) for i in _emitted_intents() if capability_for_intent(i)) <= set(CAPABILITIES)


def test_access_plans_module_has_no_engine_names():
    tree = ast.parse((ROOT / "gateway" / "access_plans.py").read_text(encoding="utf-8"))
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert not literals & {"kobold", "tabby", "llama", "ollama", "comfyui", "openrouter", "typesafe"}


def test_effective_access_without_both_plans_allows_nothing():
    half = EffectiveAccess(person_plan=everything(), app_plan=None)
    assert half.paid_providers == frozenset() and not half.can_load_models and half.media_metadata == "none"
