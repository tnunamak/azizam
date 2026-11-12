"""Access plans v4: the plan document and the two-plan evaluator (design §2, §5.1)."""

from __future__ import annotations

import ast
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import provenance, route_registry  # noqa: E402
from gateway import access_plans  # noqa: E402
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
        "caps": {"usd_per_request": None, "usd_per_day": None, "usd_per_month": None},
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
    "caps", "can_load_models", "receipt_detail", "media_metadata",
])
def test_every_field_is_required(field):
    doc = _document()
    del doc[field]
    with pytest.raises(PlanError, match=field):
        plan_from_document(doc, plan_id="plan_1")


@pytest.mark.parametrize("field", ["blocked", "must_match"])
def test_parts_not_built_yet_are_refused_not_ignored(field):
    with pytest.raises(PlanError, match="not available"):
        plan_from_document(_document(**{field: []}), plan_id="plan_1")


def test_caps_are_dollar_amounts_per_period_and_round_trip():
    parsed = plan_from_document(_document(caps={"usd_per_request": "0.50", "usd_per_day": 5, "usd_per_month": None}), plan_id="p")
    assert (parsed.caps.usd_per_request, parsed.caps.usd_per_day, parsed.caps.usd_per_month) == (
        Decimal("0.50"), Decimal("5"), None)
    assert plan_to_document(parsed)["caps"] == {"usd_per_request": "0.50", "usd_per_day": "5", "usd_per_month": None}
    assert plan_from_document(plan_to_document(parsed)) == parsed


@pytest.mark.parametrize("caps", [
    {"usd_per_request": None, "usd_per_day": None},
    {"usd_per_request": None, "usd_per_day": None, "usd_per_month": None, "usd_per_year": 1},
    {"usd_per_request": 0, "usd_per_day": None, "usd_per_month": None},
    {"usd_per_request": None, "usd_per_day": -1, "usd_per_month": None},
    {"usd_per_request": None, "usd_per_day": None, "usd_per_month": "lots"},
    {"usd_per_request": True, "usd_per_day": None, "usd_per_month": None},
    {"usd_per_request": None, "usd_per_day": "NaN", "usd_per_month": None},
    "5",
])
def test_bad_caps_are_refused(caps):
    with pytest.raises(PlanError, match="caps"):
        plan_from_document(_document(caps=caps), plan_id="p")


def test_unknown_field_and_capability_are_refused():
    with pytest.raises(PlanError, match="protocols"):
        plan_from_document(_document(protocols=["openai"]), plan_id="plan_1")
    with pytest.raises(PlanError, match="unknown capability"):
        plan_from_document(_document(can_use={"teleport": {"items": "all"}}), plan_id="plan_1")


def test_summary_names_capabilities_and_paid_models():
    family = plan_from_document(_document(), plan_id="plan_1")
    assert plan_summary(family, {}) == "Chat and Images. No paid models. No categories."
    assert plan_summary(everything(paid_providers=frozenset({"openrouter"})), {}) == (
        "All types of work. Paid models: openrouter. Direct engine access. All categories.")


# -- the summary names the categories the plan includes ------------------------

# The labels of the active rules, by category id. A plan lists category ids; the rules name them.
LABELS = {"adult": "Adult", "experimental": "Experimental", "legacy": "Legacy"}
# plan("plan_x"): every capability, no paid models, no direct engine access.
ALL_TYPES = "All types of work. No paid models."


def _listing(*categories: str):
    """plan("plan_x") with the category ids it lists instead of every category."""
    return plan("plan_x", all_categories=False, categories=frozenset(categories))


@pytest.mark.parametrize("access, labels, clause", [
    (plan("plan_x"), LABELS, "All categories"),
    (_listing("adult", "legacy"), LABELS, "Adult, Legacy"),
    (_listing("legacy", "adult", "experimental"), LABELS, "Adult, Experimental, Legacy"),
    (_listing("adult", "gone"), LABELS, "Adult"),
    (_listing("gone"), LABELS, "No categories"),
    (_listing(), LABELS, "No categories"),
    (_listing("adult"), None, "Category access unknown"),
    (plan("plan_x"), None, "All categories"),
], ids=[
    "every-category", "listed", "sorted-by-name", "stale-id-not-named", "only-stale-ids", "none-listed",
    "unreadable-rules-listed", "every-category-unreadable-rules",
])
def test_summary_names_the_categories_the_plan_includes(access, labels, clause):
    assert plan_summary(access, labels) == f"{ALL_TYPES} {clause}."


def test_category_ids_are_kept_exactly_as_written():
    """A category id is opaque: "adult " and "adult" name two categories, so the codec must not trim one into the other."""
    kept = plan_from_document(_document(categories=["adult", "adult "]), plan_id="plan_1")
    assert kept.categories == frozenset({"adult", "adult "})
    with pytest.raises(PlanError, match="twice"):
        plan_from_document(_document(categories=["adult", "adult"]), plan_id="plan_1")
    with pytest.raises(PlanError):
        plan_from_document(_document(categories=[""]), plan_id="plan_1")


def test_the_summary_reads_the_category_list_stored_in_a_plan_document():
    family = plan_from_document(_document(categories=["legacy", "adult", "gone"]), plan_id="plan_1")
    assert plan_summary(family, LABELS) == "Chat and Images. No paid models. Adult, Legacy."


def test_the_category_clause_comes_last_after_paid_models_and_direct_access():
    listed = everything("plan_x", paid_providers=frozenset({"openrouter"}), all_categories=False,
                        categories=frozenset({"adult"}))
    assert plan_summary(listed, LABELS) == "All types of work. Paid models: openrouter. Direct engine access. Adult."


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


# -- all types of work, including ones added later ---------------------------


def test_all_work_types_round_trips_as_the_string():
    parsed = plan_from_document(_document(can_use="all"), plan_id="plan_1")
    assert parsed.all_work_types and not parsed.can_use
    assert plan_to_document(parsed)["can_use"] == "all"
    assert plan_from_document(plan_to_document(parsed)) == parsed


def test_object_form_is_not_all_work_types_and_round_trips():
    parsed = plan_from_document(_document(), plan_id="plan_1")
    assert not parsed.all_work_types
    assert plan_to_document(parsed)["can_use"] == _document()["can_use"]
    assert plan_from_document(plan_to_document(parsed)) == parsed


@pytest.mark.parametrize("can_use", ["chat", "ALL", "", ["chat"], None, 3])
def test_can_use_is_all_or_an_object(can_use):
    with pytest.raises(PlanError, match="can_use"):
        plan_from_document(_document(can_use=can_use), plan_id="plan_1")


def test_all_work_types_grants_every_capability_with_all_items():
    grants = plan("plan_all", all_work_types=True, can_use={}).effective_can_use
    assert set(grants) == set(CAPABILITIES)
    assert all(grant.all_items for grant in grants.values())


def test_all_work_types_includes_a_capability_added_later(monkeypatch):
    later = plan_from_document(_document(can_use="all"), plan_id="plan_all")
    listed = plan_from_document(_document(can_use={cap: {"items": "all"} for cap in CAPABILITIES}), plan_id="plan_full")
    # A capability added after both plans were saved: the code gains it, the stored documents do not change.
    monkeypatch.setattr(access_plans, "CAPABILITIES", (*CAPABILITIES, "teleport"))
    # The shipped labels are read-only data; a later capability arrives as a new row.
    monkeypatch.setattr(access_plans, "CAPABILITY_LABELS", {**access_plans.CAPABILITY_LABELS, "teleport": "Teleport"})
    monkeypatch.setattr(access_plans, "INTENT_CAPABILITIES", {**access_plans.INTENT_CAPABILITIES, "teleport.now": "teleport"})
    assert evaluate(access(later), PolicyRequest(intent="teleport.now")).allowed
    decision = evaluate(access(listed), PolicyRequest(intent="teleport.now"))
    assert decision.denied and decision.code == NOT_IN_ACCESS_PLAN
    assert plan_summary(later, {}).startswith("All types of work.")


def test_all_work_types_on_the_person_side_is_bounded_by_the_app_plan():
    everything_person = plan("plan_all", all_work_types=True, can_use={})
    chat_app = plan("plan_chat", can_use={"chat": CapabilityGrant(all_items=True)})
    assert evaluate(access(everything_person, chat_app), PolicyRequest(intent="chat.completions", model="m")).allowed
    decision = evaluate(access(everything_person, chat_app), PolicyRequest(intent="image.generate", model="flux"))
    assert decision.denied and decision.side == APP_SIDE and decision.code == NOT_IN_ACCESS_PLAN


def test_all_work_types_summarizes_as_all_types_of_work():
    assert plan_summary(plan("plan_x", all_work_types=True, can_use={}), LABELS) == (
        "All types of work. No paid models. All categories.")


def test_union_with_an_all_work_types_plan_stays_all_work_types():
    merged = union_plans("plan_u", "Union", [
        plan("plan_all", all_work_types=True, can_use={}),
        plan("plan_chat", can_use={"chat": CapabilityGrant(models=frozenset({"m"}))}),
    ])
    assert merged.all_work_types and not merged.can_use
    assert plan_to_document(merged)["can_use"] == "all"


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
                           "image": CapabilityGrant(workflows=frozenset({"flux2-klein-4b"}))})
    effective = access(listed)
    assert evaluate(effective, PolicyRequest(intent="chat.completions", model="a")).allowed
    assert evaluate(effective, PolicyRequest(intent="chat.completions", model="b")).denied
    assert evaluate(effective, PolicyRequest(intent="image.generate", workflow="flux2-klein-4b")).allowed
    assert evaluate(effective, PolicyRequest(intent="image.generate", workflow="sdxl")).denied


# Literal oracle: do not derive expected record IDs from compatibility data.
_VIDEO_WORKFLOW_IDS = [
    ("video-ltx-2.5-t2v", "video.generate.ltx-2.5-t2v", "video.generate"),
    ("video-ltx-2.5-i2v", "video.generate.ltx-2.5-i2v", "video.generate"),
    ("video-ltx-2.5-keyframes", "video.generate.ltx-2.5-keyframes", "video.generate"),
    ("video-ltx-2.5-keyframes-mid", "video.generate.ltx-2.5-keyframes-mid", "video.generate"),
    ("video-ltx-2.5-long", "video.generate.ltx-2.5-long", "video.generate"),
    ("video-minimax-h3-t2v", "video.generate.minimax-h3-t2v", "video.generate"),
    ("video-minimax-h3-i2v", "video.generate.minimax-h3-i2v", "video.generate"),
    ("video-minimax-h3-keyframes", "video.generate.minimax-h3-keyframes", "video.generate"),
    ("video-minimax-h3-keyframes-mid", "video.generate.minimax-h3-keyframes-mid", "video.generate"),
    ("video-minimax-h3-long", "video.generate.minimax-h3-long", "video.generate"),
    ("video-scail-2-replace", "video.edit.scail-2-replace", "video.edit"),
]


def test_video_workflow_id_oracle_covers_every_shipped_key():
    shipped = yaml.safe_load(
        (ROOT / "registry" / "defaults" / "videos.yml").read_text(encoding="utf-8")
    )["workflows"]
    assert len(_VIDEO_WORKFLOW_IDS) == len({row[0] for row in _VIDEO_WORKFLOW_IDS}) == 11
    assert {row[0] for row in _VIDEO_WORKFLOW_IDS} == set(shipped)


@pytest.mark.parametrize("key,workflow_id,intent", _VIDEO_WORKFLOW_IDS)
@pytest.mark.parametrize("matching", [True, False], ids=["listed", "different-record"])
def test_video_record_id_is_the_access_plan_identity(key, workflow_id, intent, matching):
    from gateway.video_limits import workflow_id_for_video_key

    resolved = workflow_id_for_video_key(key)
    assert resolved == workflow_id
    other_id = next(row[1] for row in _VIDEO_WORKFLOW_IDS if row[1] != workflow_id)
    listed_id = workflow_id if matching else other_id
    listed = plan(can_use={"video": CapabilityGrant(workflows=frozenset({listed_id}))})
    decision = evaluate(access(listed), PolicyRequest(intent=intent, workflow=resolved))
    if matching:
        assert decision.allowed
    else:
        assert decision.denied and decision.code == NOT_IN_ACCESS_PLAN


@pytest.mark.parametrize("listed_id,allowed", [
    ("video.generate.my-own", True),
    ("my-own", False),
    ("video.edit.my-own", False),
])
def test_owner_video_entry_uses_its_generate_record_id(monkeypatch, listed_id, allowed):
    from gateway.video_limits import workflow_id_for_video_key

    # Owner entries need a declared identity, just like shipped entries. Unknown keys are never guessed.
    from gateway import video_limits
    monkeypatch.setattr(video_limits, "_KEY_TO_WORKFLOW_ID", {"my-own": "video.generate.my-own"})
    resolved = workflow_id_for_video_key("my-own")
    assert resolved == "video.generate.my-own"
    listed = plan(can_use={"video": CapabilityGrant(workflows=frozenset({listed_id}))})
    decision = evaluate(access(listed), PolicyRequest(intent="video.generate", workflow=resolved))
    if allowed:
        assert decision.allowed
    else:
        assert decision.denied and decision.code == NOT_IN_ACCESS_PLAN


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
    config = yaml.safe_load((ROOT / "tests/fixtures/config_homes/deployment_config.yml").read_text(encoding="utf-8"))
    for policy in route_registry.runtime_route_policy(proxy_config=config).values():
        if policy.get("intent"):
            intents.add(str(policy["intent"]))
    for _, intent in route_registry.azizam_policy(proxy_config=config).values():
        intents.add(intent)
    # Workflows carry their own intent, and a plan grants them by capability.
    for path in sorted((ROOT / "manifests" / "workflows").glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(document, dict) and document.get("intent"):
            intents.add(str(document["intent"]))
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
