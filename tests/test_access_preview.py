"""Check access lists everything a plan can and cannot use, from the real policy view.

The preview must agree with enforcement: an entry is allowed exactly when the
request path (``evaluate_and_apply``) allows a request for it, and a blocked
entry carries the reason the decision recorded.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import proxy
import test_proxy_admin_integration as admin_integration
from gateway import accounts, category_enforcement, category_policy as policy
from gateway.access_preview import build_access_preview, plain_reason
from gateway.capabilities import Capability
from gateway.runtime_auth import evaluate_and_apply, make_policy_request
from tests.support.access import access, identity as runtime_identity, plan_for, store_plan
from tests.test_category_visibility import OWNER, activate, rules


def _cap(intent, model, **kw):
    return Capability(intent=intent, backend="svc", model=model, **kw)


def _row(row_id, intents, *, kind="chat", label="Chat", alias=False, **extra):
    return {
        "id": row_id, "display_name": row_id.title(), "alias": alias, "capability": kind,
        "capability_title": label, "use_names": [row_id], "resolves_to": {}, "workflow": None,
        "capabilities": [{"intent": i, "label": label, "capability": kind} for i in intents],
        **extra,
    }


def _catalog(*rows):
    return {"aliases": [r for r in rows if r["alias"]], "models": [r for r in rows if not r["alias"]]}


def _denied(intent, model, reason):
    return _cap(intent, model, policy_available=False, policy_reasons=(reason,))


# --- the comparison -----------------------------------------------------------

def test_a_row_the_plan_cannot_see_is_blocked_with_the_recorded_reason():
    offered = _catalog(_row("open", ["chat.completions"]), _row("shut", ["chat.completions"]))
    permitted = _catalog(_row("open", ["chat.completions"]))
    preview = build_access_preview(
        offered=offered, permitted=permitted,
        denied=[_denied("chat.completions", "shut", "shut isn't in your access plan.")])
    by_id = {e["id"]: e for e in preview["entries"]}
    assert preview["summary"] == {"usable": 1, "total": 2}
    assert by_id["open"]["state"] == "allowed" and by_id["open"]["reason"] is None
    assert by_id["shut"]["state"] == "blocked"
    assert by_id["shut"]["reason"] == "shut isn't in your access plan."
    assert by_id["shut"]["name"] == "Shut"


def test_one_entry_allowed_for_some_kinds_lists_its_kinds():
    both = ["chat.completions", "image.generate"]
    offered = _catalog({**_row("multi", both), "capabilities": [
        {"intent": "chat.completions", "label": "Chat", "capability": "chat"},
        {"intent": "image.generate", "label": "Images", "capability": "image"}]})
    permitted = _catalog({**_row("multi", ["chat.completions"])})
    entry = build_access_preview(
        offered=offered, permitted=permitted,
        denied=[_denied("image.generate", "multi", "multi isn't in your access plan.")])["entries"][0]
    assert entry["state"] == "partial"
    assert [(k["label"], k["allowed"]) for k in entry["kinds"]] == [("Chat", True), ("Images", False)]
    assert entry["kinds"][1]["reason"] == "multi isn't in your access plan."


def test_kinds_are_left_out_when_every_kind_agrees():
    offered = _catalog(_row("plain", ["chat.completions", "chat.responses"]))
    entry = build_access_preview(offered=offered, permitted=_catalog(), denied=[])["entries"][0]
    assert entry["state"] == "blocked" and entry["kinds"] == []
    assert entry["reason"] == "This access plan doesn't include it."


def test_an_alias_is_blocked_by_the_reason_of_the_model_it_resolves_to():
    alias = _row("fast", ["chat.completions"], alias=True, resolves_to={"chat": "big-model"})
    entry = build_access_preview(
        offered=_catalog(alias), permitted=_catalog(),
        denied=[_denied("chat.completions", "big-model", "big-model isn't in your access plan.")])["entries"][0]
    assert entry["alias"] is True
    assert entry["reason"] == "big-model isn't in your access plan."


def test_a_model_only_the_plan_can_see_is_allowed_beside_its_blocked_alias():
    alias = _row("fast", ["chat.completions"], alias=True, resolves_to={"chat": "big"})
    preview = build_access_preview(
        offered=_catalog(alias), permitted=_catalog(_row("big", ["chat.completions"])),
        denied=[_denied("chat.completions", "fast", "fast isn't in your access plan.")])
    assert {(e["id"], e["state"]) for e in preview["entries"]} == {("fast", "blocked"), ("big", "allowed")}
    assert preview["summary"] == {"usable": 1, "total": 2}


def test_entries_follow_the_models_page_group_order():
    rows = [_row("v", ["image.generate"], kind="video", label="Video"),
            _row("c", ["chat.completions"]),
            _row("s", ["audio.speech"], kind="speech", label="Speech"),
            _row("m", ["music.generate"], kind="music", label="Music")]
    entries = build_access_preview(offered=_catalog(*rows), permitted=_catalog(*rows), denied=[])["entries"]
    assert [e["group"] for e in entries] == ["chat", "video", "music", "speech"]


@pytest.mark.parametrize("reason, expected", [
    ("category rules: CATEGORY_HIDDEN adult", "It is in a restricted category that this plan is not listed for."),
    ("category rules: POLICY_UNAVAILABLE", "The category rules block it."),
    ("x isn't in your access plan.", "x isn't in your access plan."),
])
def test_category_codes_become_plain_sentences(reason, expected):
    assert plain_reason(reason) == expected


# --- the route ----------------------------------------------------------------

@pytest.fixture
def owner():
    case = admin_integration.ProxyAdminIntegrationTests(methodName="runTest")
    case.setUp()
    try:
        accounts.initialize(case.db)
        yield case
    finally:
        case.tearDown()


def _post(owner, body, *, authed=True):
    return owner.client.post("/admin/api/access/preview", json=body, headers=owner._auth() if authed else {})


def test_the_route_needs_the_owner(owner):
    response = _post(owner, {"plan": "any"}, authed=False)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "admin_unauthenticated"


def test_the_route_names_a_plan_or_an_app(owner):
    assert _post(owner, {}).status_code == 400
    missing = _post(owner, {"plan": "plan_missing"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "access_plan_not_found"
    assert _post(owner, {"app": "app_missing"}).json()["error"]["code"] == "app_not_found"


def test_a_failed_preview_is_an_error_not_an_empty_list(owner):
    plan = store_plan("Chat", path=owner.db, caps=("chat",))
    with patch.object(proxy, "_viewed_gateway_capabilities", side_effect=RuntimeError("boom")):
        response = _post(owner, {"plan": plan["id"]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "access_preview_unavailable"
    assert "boom" not in response.text


CATALOG = [
    _cap("chat.completions", "allowed-model", input_modalities=("text",), output_modalities=("text",)),
    _cap("chat.completions", "denied-model", input_modalities=("text",), output_modalities=("text",)),
    _cap("chat.completions", "multi", input_modalities=("text",), output_modalities=("text",)),
    _cap("image.generate", "multi", input_modalities=("text",), output_modalities=("image",)),
]


def _preview(owner, plan, capabilities=CATALOG, name_views=None):
    """The route's answer for ``plan`` over ``capabilities`` run through the real policy view."""
    with patch.object(proxy, "_gateway_catalog_base", lambda: ({}, [])), \
            patch.object(proxy, "_gateway_catalog_finish", lambda *_a: list(capabilities)), \
            patch.object(proxy, "_remote_decision_models", lambda *_a: []), \
            patch.object(proxy, "_gateway_aliases_provider", return_value=name_views or {}), \
            patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set())):
        response = _post(owner, {"plan": plan["id"]})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_preview_lists_allowed_and_blocked_entries_with_reasons(owner):
    plan = store_plan("Some", path=owner.db, caps=("chat",), models=["allowed-model", "multi"])
    body = _preview(owner, plan)
    by_id = {e["id"]: e for e in body["entries"]}
    assert body["plan_ids"] == [plan["id"]]
    assert body["summary"] == {"usable": 2, "total": 3}
    assert by_id["allowed-model"]["state"] == "allowed"
    assert by_id["denied-model"]["state"] == "blocked"
    assert by_id["denied-model"]["reason"] == "denied-model isn't in your access plan."
    # "multi" is chat and images; the plan grants only chat.
    assert by_id["multi"]["state"] == "partial"
    assert [(k["label"], k["allowed"]) for k in by_id["multi"]["kinds"]] == [("Chat", True), ("Images", False)]


def test_the_preview_decides_exactly_as_the_request_path_does(owner):
    plan = store_plan("Some", path=owner.db, caps=("chat",), models=["allowed-model", "multi"])
    body = _preview(owner, plan)
    entries = {e["id"]: e for e in body["entries"]}
    who = runtime_identity(access(_plan_from(owner, plan)), app_id=None, person_id=None)
    for capability in CATALOG:
        decision = evaluate_and_apply(who, make_policy_request(
            protocol="openai.chat", intent=capability.intent, model=capability.model))
        entry = entries[capability.model]
        kinds = {k["label"]: k["allowed"] for k in entry["kinds"]}
        label = "Images" if capability.intent == "image.generate" else "Chat"
        shown = kinds.get(label, entry["state"] == "allowed")
        assert shown is decision.allowed, (capability.model, capability.intent, entry)
        if not decision.allowed:
            assert (entry["reason"] if not kinds else
                    next(k["reason"] for k in entry["kinds"] if k["label"] == label)) == decision.reasons[0]


def test_the_preview_judges_a_second_name_the_way_a_request_for_it_is_judged(owner):
    """A name and the model it points at: a plan may list either; Check access says what a request would."""
    catalog = [
        _cap("chat.completions", "default-fast", input_modalities=("text",), output_modalities=("text",)),
        _cap("chat.completions", "fast", tags=("alias", "resolves_to:default-fast"),
             input_modalities=("text",), output_modalities=("text",)),
    ]
    views = {"chat": {"stable": [{"alias": "fast", "resolves_to": "default-fast", "service": "svc"}]}}

    def states(models):
        plan = store_plan("Plan " + "-".join(models), path=owner.db, caps=("chat",), models=models)
        body = _preview(owner, plan, catalog, views)
        who = runtime_identity(access(_plan_from(owner, plan)), app_id=None, person_id=None)
        by_id = {e["id"]: e["state"] for e in body["entries"]}
        # The same question put to the request path: model resolved, caller's name kept.
        asked = {"fast": make_policy_request(protocol="openai.chat", intent="chat.completions",
                                             model="default-fast", requested_model="fast"),
                 "default-fast": make_policy_request(protocol="openai.chat", intent="chat.completions",
                                                     model="default-fast")}
        for name, request in asked.items():
            assert (by_id[name] == "allowed") is evaluate_and_apply(who, request).allowed, name
        return by_id

    assert states(["default-fast"]) == {"fast": "allowed", "default-fast": "allowed"}
    assert states(["fast"]) == {"fast": "allowed", "default-fast": "blocked"}
    assert states(["other"]) == {"fast": "blocked", "default-fast": "blocked"}


def _plan_from(owner, record):
    from gateway.access_plans import plan_from_document
    return plan_from_document(record["plan"], plan_id=str(record["id"]))


@pytest.fixture
def category_gate(tmp_path):
    path = tmp_path / "categories.db"
    category_enforcement.configure(path, owner_id=lambda: OWNER)
    yield path
    category_enforcement.configure(None)


def test_a_restricted_category_blocks_its_items_for_a_plan_it_does_not_list(owner, category_gate):
    plan = store_plan("Family", path=owner.db, caps=("chat",))
    listed = store_plan("Listed", path=owner.db, caps=("chat",))
    activate(category_gate, rules(adult_visible_to=[listed["id"]]))
    catalog = [_cap("chat.completions", name, workflow=None) for name in ("adult-flow", "exp-flow", "plain")]

    def entries(record):
        return {e["id"]: e for e in _preview(owner, record, catalog)["entries"]}

    family = entries(plan)
    assert family["plain"]["state"] == "allowed"
    for hidden in ("adult-flow", "exp-flow"):
        assert family[hidden]["state"] == "blocked"
        assert family[hidden]["reason"] == "It is in a restricted category that this plan is not listed for."
        assert "CATEGORY_HIDDEN" not in family[hidden]["reason"]
    assert entries(listed)["adult-flow"]["state"] == "allowed"
    assert entries(listed)["exp-flow"]["state"] == "blocked"


def test_the_preview_lists_the_entries_the_models_page_lists_for_the_same_plan(owner):
    """Unpatched catalog: the entries a plan can use are the rows its Models page shows."""
    from gateway.policy import EffectiveAccess

    record = store_plan("Chat and images", path=owner.db, caps=("chat", "image"))
    plan = _plan_from(owner, record)
    context = proxy.AuthContext(identity=runtime_identity(EffectiveAccess(plan, plan), app_id=None, person_id=None))
    records = proxy._workflow_availability_services(proxy.ADMIN_STORE.get_service_registry())
    page = proxy._models_catalog_from(
        [proxy._gateway_capabilities_for_auth_context(context)], service_records=records)
    shown = {row["id"] for row in [*page["aliases"], *page["models"]]}
    with patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set())):
        response = _post(owner, {"plan": record["id"]})
    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    assert shown, "the fixture gateway offers nothing; the comparison would prove nothing"
    assert {e["id"] for e in entries if e["state"] != "blocked"} == shown
    assert response.json()["summary"] == {"usable": len(shown), "total": len(entries)}


def test_a_plan_that_grants_everything_local_blocks_nothing(owner):
    """No phantom blocked entries: whatever the Models page offers, an everything plan can use."""
    record = store_plan("Everything local", path=owner.db)
    with patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set())):
        body = _post(owner, {"plan": record["id"]}).json()
    assert body["entries"]
    assert [e for e in body["entries"] if e["state"] != "allowed"] == []

