"""Check access lists everything a plan can and cannot use, from the real policy view.

The preview must agree with enforcement: an entry is allowed exactly when the
request path (``evaluate_and_apply``) allows a request for it, and a blocked
entry carries the reason the decision recorded.
"""
from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

import proxy
import test_proxy_admin_integration as admin_integration
from gateway import accounts, category_enforcement, category_policy as policy
from gateway.access_preview import build_access_preview, plain_reason
from gateway.capabilities import Capability
from gateway.policy import DecidingRule
from gateway.runtime_auth import evaluate_and_apply, make_policy_request
from gateway import admin_store
from tests.support.access import access, identity as runtime_identity, plan_document, plan_for, store_plan
from tests.test_category_visibility import OWNER, activate, rules
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401  (gateway is a fixture)


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
    assert preview["summary"] == {"usable": 1, "total": 2, "blocked": 1, "unknown": 0}
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
    assert preview["summary"] == {"usable": 1, "total": 2, "blocked": 1, "unknown": 0}


def test_entries_follow_the_models_page_group_order():
    rows = [_row("v", ["image.generate"], kind="video", label="Video"),
            _row("c", ["chat.completions"]),
            _row("s", ["audio.speech"], kind="speech", label="Speech"),
            _row("m", ["music.generate"], kind="music", label="Music")]
    entries = build_access_preview(offered=_catalog(*rows), permitted=_catalog(*rows), denied=[])["entries"]
    assert [e["group"] for e in entries] == ["chat", "video", "music", "speech"]


@pytest.mark.parametrize("reason, expected", [
    ("category rules: CATEGORY_HIDDEN adult", "It is in a restricted category that this plan can't see."),
    ("category rules: POLICY_UNAVAILABLE", "The category rules couldn't be read just now, so access is refused until they can be."),
    ("category rules: NO_GRANT (no service for x)", "The category rules don't give this plan access to it."),
    ("x isn't in your access plan.", "x isn't in your access plan."),
])
def test_a_reason_with_no_recorded_rule_still_reads_in_plain_words(reason, expected):
    assert plain_reason(reason) == expected


# --- the deciding rule, in plain English ------------------------------------------

PLANS = {"plan_kids": "Kids", "plan_app": "Tablet"}
CATEGORIES = {"adult": "Adult"}


def _rule(code, kind="plan", id="plan_kids", side="person", subject=None):
    return DecidingRule(code=code, kind=kind, id=id, side=side, subject=subject)


def _blocked(intent, model, rule):
    return _cap(intent, model, policy_available=False, policy_reasons=("recorded text",), policy_rule=rule)


def _one(row, rule, **kw):
    """The single entry of a one-row catalog the plan cannot use, with ``rule`` as the cause."""
    preview = build_access_preview(
        offered=_catalog(row), permitted=_catalog(),
        denied=[_blocked(row["capabilities"][0]["intent"], row["id"], rule)],
        plan_names=PLANS, category_names=CATEGORIES, **kw)
    return preview["entries"][0]


def test_a_blocked_row_says_which_rule_blocked_it_in_plain_english():
    entry = _one(_row("vid", ["video.generate"], kind="video", label="Video"),
                 _rule("plan.capability_not_granted", subject="video"))
    assert entry["state"] == "blocked"
    assert entry["reason"] == "The Kids plan doesn't include video."
    assert entry["rule"] == {"code": "plan.capability_not_granted", "kind": "plan", "id": "plan_kids",
                             "name": "Kids", "plain_reason": "The Kids plan doesn't include video."}


def test_an_app_side_plan_is_called_the_apps_plan():
    entry = _one(_row("vid", ["video.generate"], kind="video", label="Video"),
                 _rule("plan.capability_not_granted", id="plan_app", side="app", subject="video"))
    assert entry["reason"] == "The app's Tablet plan doesn't include video."


def test_a_plan_that_lists_items_names_the_item_by_its_display_name():
    entry = _one(_row("big-model", ["chat.completions"], display_name="Big chat"),
                 _rule("plan.item_not_listed", subject="big-model"))
    assert entry["reason"] == "The Kids plan lists the chat items it allows, and Big chat isn't one of them."


def test_a_category_is_named_by_its_label_and_its_id_stays_out_of_the_sentence():
    entry = _one(_row("wf", ["chat.completions"]), _rule("category.hidden", kind="category", id="adult", side=None))
    assert entry["reason"] == "It is in the Adult category, which this plan can't see."
    assert entry["rule"]["id"] == "adult" and entry["rule"]["name"] == "Adult"
    assert "adult" not in entry["reason"].replace("Adult", "")


def test_an_administrator_gets_the_rule_without_the_category_name_or_id():
    entry = _one(_row("wf", ["chat.completions"]), _rule("category.hidden", kind="category", id="adult", side=None),
                 detail="limited")
    assert entry["reason"] == "It is in a restricted category that this plan can't see."
    assert entry["rule"] == {"code": "category.hidden", "kind": "category", "id": None, "name": None,
                             "plain_reason": "It is in a restricted category that this plan can't see."}


@pytest.mark.parametrize("rule", [
    _rule("plan.none", id=None),
    _rule("plan.capability_not_granted", subject="video"),
    _rule("plan.item_not_listed", subject="m"),
    _rule("plan.provider_not_granted", subject="openai"),
    _rule("plan.fallback_not_granted"),
    _rule("plan.direct_engine_not_granted", subject="llama-x"),
    _rule("category.hidden", kind="category", id="adult", side=None),
    _rule("category.no_grant", kind="category", id="adult", side=None),
    _rule("category.not_allowed", kind="category", id="adult", side=None),
    _rule("category.rules_unavailable", kind="category", id=None, side=None),
    _rule("provider.config_unavailable", kind="provider", id="svc", side=None),
])
def test_no_sentence_shows_an_internal_id(rule):
    entry = _one(_row("m", ["chat.completions"]), rule)
    sentence = entry["rule"]["plain_reason"]
    assert sentence == entry["reason"]
    for internal in ("plan_kids", "plan_app", "svc", "adult", rule.code, "_"):
        assert internal not in sentence, (rule.code, sentence)


# --- cannot tell ---------------------------------------------------------------------

@pytest.mark.parametrize("rule, sentence", [
    (_rule("category.rules_unavailable", kind="category", id=None, side=None),
     "The category rules couldn't be read just now, so access is refused until they can be."),
    (_rule("provider.config_unavailable", kind="provider", id="svc", side=None),
     "The online provider's setup couldn't be read, so access can't be worked out."),
])
def test_a_row_whose_answer_depends_on_something_unknown_is_cannot_tell(rule, sentence):
    row = _row("m", ["chat.completions"])
    preview = build_access_preview(
        offered=_catalog(row, _row("ok", ["chat.completions"])), permitted=_catalog(_row("ok", ["chat.completions"])),
        denied=[_blocked("chat.completions", "m", rule)], plan_names=PLANS, category_names=CATEGORIES)
    entry = next(e for e in preview["entries"] if e["id"] == "m")
    assert entry["state"] == "unknown"
    assert entry["reason"] == sentence
    assert entry["rule"]["code"] == rule.code
    assert preview["summary"] == {"usable": 1, "total": 2, "blocked": 0, "unknown": 1}


def test_a_definite_block_outranks_a_cannot_tell_on_the_same_row():
    row = {**_row("m", ["chat.completions", "image.generate"]), "capabilities": [
        {"intent": "chat.completions", "label": "Chat", "capability": "chat"},
        {"intent": "image.generate", "label": "Images", "capability": "image"}]}
    entry = build_access_preview(
        offered=_catalog(row), permitted=_catalog(),
        denied=[_blocked("chat.completions", "m", _rule("category.rules_unavailable", kind="category", id=None, side=None)),
                _blocked("image.generate", "m", _rule("plan.capability_not_granted", subject="image"))],
        plan_names=PLANS, category_names=CATEGORIES)["entries"][0]
    assert entry["state"] == "blocked"
    assert entry["rule"]["code"] == "plan.capability_not_granted"
    assert [(k["label"], k["state"]) for k in entry["kinds"]] == [("Chat", "unknown"), ("Images", "blocked")]


def test_the_primary_reason_is_an_explicit_block_before_a_missing_grant():
    row = {**_row("m", ["chat.completions", "image.generate"]), "capabilities": [
        {"intent": "chat.completions", "label": "Chat", "capability": "chat"},
        {"intent": "image.generate", "label": "Images", "capability": "image"}]}
    entry = build_access_preview(
        offered=_catalog(row), permitted=_catalog(),
        denied=[_blocked("chat.completions", "m", _rule("plan.item_not_listed", subject="m")),
                _blocked("image.generate", "m", _rule("category.hidden", kind="category", id="adult", side=None))],
        plan_names=PLANS, category_names=CATEGORIES)["entries"][0]
    assert entry["rule"]["code"] == "category.hidden"
    assert {k["rule"]["code"] for k in entry["kinds"]} == {"plan.item_not_listed", "category.hidden"}


# --- why allowed -----------------------------------------------------------------------

def _allowed(intent, model, rule):
    return _cap(intent, model, policy_rule=rule)


def test_an_allowed_row_says_which_rule_allowed_it():
    row = _row("chat-a", ["chat.completions"])
    entries = {e["id"]: e for e in build_access_preview(
        offered=_catalog(row, _row("chat-b", ["chat.completions"])),
        permitted=_catalog(row, _row("chat-b", ["chat.completions"])), denied=[],
        allowed=[_allowed("chat.completions", "chat-a", _rule("plan.lists_item", subject="chat-a")),
                 _allowed("chat.completions", "chat-b", _rule("plan.allows_all", subject="chat"))],
        plan_names=PLANS, category_names=CATEGORIES)["entries"]}
    assert entries["chat-a"]["reason"] is None
    assert entries["chat-a"]["rule"]["plain_reason"] == "The Kids plan lists Chat-A."
    assert entries["chat-b"]["rule"]["code"] == "plan.allows_all"
    assert entries["chat-b"]["rule"]["plain_reason"] == "The Kids plan includes every chat item, now and as they are added."


# --- an alias row shows its model ---------------------------------------------------------

def test_an_alias_row_shows_the_model_it_points_to():
    alias = _row("fast", ["chat.completions"], alias=True, resolves_to={"chat": "big-model"})
    target = _row("big-model", ["chat.completions"], display_name="Big chat")
    entries = {e["id"]: e for e in build_access_preview(
        offered=_catalog(alias, target), permitted=_catalog(target), denied=[])["entries"]}
    assert entries["fast"]["points_to"] == [{"id": "big-model", "name": "Big chat"}]
    assert entries["big-model"]["points_to"] == []


# --- counts per group ------------------------------------------------------------------------

def test_each_group_has_its_own_count():
    rows = [_row("c1", ["chat.completions"]), _row("c2", ["chat.completions"]), _row("c3", ["chat.completions"]),
            _row("v1", ["video.generate"], kind="video", label="Video")]
    preview = build_access_preview(
        offered=_catalog(*rows), permitted=_catalog(rows[0], rows[1]),
        denied=[_blocked("chat.completions", "c3", _rule("plan.item_not_listed", subject="c3")),
                _blocked("video.generate", "v1", _rule("category.rules_unavailable", kind="category", id=None, side=None))])
    assert preview["groups"] == [
        {"group": "chat", "label": "Chat", "total": 3, "usable": 2, "blocked": 1, "unknown": 0},
        {"group": "video", "label": "Video", "total": 1, "usable": 0, "blocked": 0, "unknown": 1},
    ]


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


@contextlib.contextmanager
def fixture_catalog(capabilities=CATALOG, name_views=None):
    """The gateway offers exactly ``capabilities``; the policy view over them is the real one."""
    with patch.object(proxy, "_gateway_catalog_base", lambda: ({}, [])), \
            patch.object(proxy, "_gateway_catalog_finish", lambda *_a: list(capabilities)), \
            patch.object(proxy, "_remote_decision_models", lambda *_a: []), \
            patch.object(proxy, "_gateway_aliases_provider", return_value=name_views or {}), \
            patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set())):
        yield


def _preview(owner, plan, capabilities=CATALOG, name_views=None):
    """The route's answer for ``plan`` over ``capabilities`` run through the real policy view."""
    with fixture_catalog(capabilities, name_views):
        response = _post(owner, {"plan": plan["id"]})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_preview_lists_allowed_and_blocked_entries_with_reasons(owner):
    plan = store_plan("Some", path=owner.db, caps=("chat",), models=["allowed-model", "multi"])
    body = _preview(owner, plan)
    by_id = {e["id"]: e for e in body["entries"]}
    assert body["plan_ids"] == [plan["id"]]
    assert body["summary"] == {"usable": 2, "total": 3, "blocked": 1, "unknown": 0}
    assert by_id["allowed-model"]["state"] == "allowed"
    assert by_id["denied-model"]["state"] == "blocked"
    assert by_id["denied-model"]["reason"] == (
        f"The Some plan lists the chat items it allows, and {by_id['denied-model']['name']} isn't one of them.")
    assert by_id["denied-model"]["rule"] == {
        "code": "plan.item_not_listed", "kind": "plan", "id": plan["id"], "name": "Some",
        "plain_reason": by_id["denied-model"]["reason"]}
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
            shown = entry["rule"] if not kinds else next(k["rule"] for k in entry["kinds"] if k["label"] == label)
            assert shown["code"] == decision.rule.code


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
        category_id, label = {"adult-flow": ("adult", "Adult"), "exp-flow": ("experimental", "Experimental")}[hidden]
        assert family[hidden]["reason"] == f"It is in the {label} category, which this plan can't see."
        assert family[hidden]["rule"] == {
            "code": "category.hidden", "kind": "category", "id": category_id, "name": label,
            "plain_reason": family[hidden]["reason"]}
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
    assert response.json()["summary"]["usable"] == len(shown)
    assert response.json()["summary"]["total"] == len(entries)


def test_a_plan_that_grants_everything_local_blocks_nothing(owner):
    """No phantom blocked entries: whatever the Models page offers, an everything plan can use."""
    record = store_plan("Everything local", path=owner.db)
    with patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set())):
        body = _post(owner, {"plan": record["id"]}).json()
    assert body["entries"]
    assert [e for e in body["entries"] if e["state"] != "allowed"] == []


def test_unreadable_category_rules_show_as_cannot_tell_not_as_a_plain_block(owner, category_gate):
    plan = store_plan("Chat", path=owner.db, caps=("chat",))
    activate(category_gate, rules(adult_visible_to=[]))
    with sqlite3.connect(category_gate) as conn:
        conn.execute("DROP TRIGGER IF EXISTS policy_snapshots_no_update")
        conn.execute("UPDATE policy_snapshots SET body_json = '{\"broken\": true}'")
    catalog = [_cap("chat.completions", name) for name in ("plain", "other")]
    body = _preview(owner, plan, catalog)
    assert {e["state"] for e in body["entries"]} == {"unknown"}
    assert body["summary"] == {"usable": 0, "total": 2, "blocked": 0, "unknown": 2}
    for entry in body["entries"]:
        assert entry["rule"]["code"] == "category.rules_unavailable"
        assert entry["reason"] == "The category rules couldn't be read just now, so access is refused until they can be."


# --- who sees how much ---------------------------------------------------------------------

def test_the_owner_sees_the_full_rule_and_an_administrator_sees_what_people_rules_allow(gateway, tmp_path):
    """The owner reads the category by name; an administrator, who cannot open Categories, does not."""
    owner_client, _, _, admin = owner_and_admin(gateway)
    category_enforcement.configure(tmp_path / "categories.db", owner_id=lambda: OWNER)
    try:
        activate(tmp_path / "categories.db", rules(adult_visible_to=[]))
        plan = admin_store.create_access_plan(
            plan_document("Kids", caps=("chat",), models=["plain", "adult-flow"]), path=gateway.path)
        catalog = [_cap("chat.completions", name) for name in ("plain", "other", "adult-flow")]

        def ask(client):
            with fixture_catalog(catalog):
                response = client.post("/admin/api/access/preview", json={"plan": plan["id"]}, headers=csrf_for(client))
            assert response.status_code == 200, response.text
            return {e["id"]: e for e in response.json()["entries"]}

        full, limited = ask(owner_client), ask(admin)
    finally:
        category_enforcement.configure(None)
    # Plans are readable by both; the edit link is the page's call (only the owner may edit a plan).
    for entries in (full, limited):
        assert entries["other"]["rule"]["id"] == plan["id"]
        assert entries["other"]["rule"]["name"] == "Kids"
        assert entries["adult-flow"]["rule"]["code"] == "category.hidden"
    assert full["adult-flow"]["rule"]["id"] == "adult" and full["adult-flow"]["rule"]["name"] == "Adult"
    assert "Adult" in full["adult-flow"]["reason"]
    assert limited["adult-flow"]["rule"]["id"] is None and limited["adult-flow"]["rule"]["name"] is None
    assert "Adult" not in limited["adult-flow"]["reason"]

