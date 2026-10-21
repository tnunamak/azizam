"""An alias and the model it points at are two names for one thing, and every surface agrees.

The rules these tests hold in place:

1. Every name a caller can send has its own row on the Models page; a row says
   which other names reach the same model (``same_model_as``). No name hides in
   another row's ``use_names``.
2. Names that a wildcard pattern accepts (``jev-*``) but cannot list are
   published as data (``names:``), not by code.
3. A plan may grant an alias (it then grants requests made through that name)
   or the model behind it (it then grants every name that reaches it). Owner
   category rules follow the model, and a restricted category hides both
   spellings. Requests, ``/v1/models`` and Check access judge a name the same
   way.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi import Response
from fastapi.testclient import TestClient

import proxy
from gateway import category_enforcement, media_jobs, receipt_store, stable_aliases
from gateway.access_plans import capability_for_intent
from gateway.decision_routing import DecisionRuntime
from gateway.local_capabilities import build_alias_capabilities
from gateway.models_catalog import build_models_catalog
from gateway.policy import ALLOWED, PolicyDecision, PolicyRequest, evaluate
from tests.support.access import access, access_context, identity as runtime_identity, plan_for
from tests.test_category_visibility import OWNER, Who, _service, activate, rules
from tests.test_model_metadata_endpoints import QWEN_PATH, _loaded, _start_model_list_patches
from tests.test_runtime_route_policy import AUTH_HEADERS, authenticated_profile, litellm_registry

ALLOW = PolicyDecision(decision=ALLOWED, sources=("plan",), reasons=("allowed",))
CHAT = "chat.completions"


def _seed_catalog():
    """The shipped decision names through the real catalog builder."""
    with open(proxy.CONFIG_PATH) as seed:
        cfg = yaml.safe_load(seed)
    runtime = DecisionRuntime(lambda: cfg, lambda *args: True, AsyncMock())
    views = {}
    for operation in stable_aliases.OPERATIONS:
        views.update(stable_aliases.describe_aliases(cfg.get("model_aliases"), operation=operation))
    caps = [*build_alias_capabilities(views), *runtime.capabilities()]
    return build_models_catalog(alias_views=views, capabilities=caps, service_records={})


# --- 1. one row per name -------------------------------------------------------

def test_every_decision_name_is_a_row_and_says_what_it_shares():
    catalog = _seed_catalog()
    rows = {row["id"]: row for row in [*catalog["aliases"], *catalog["models"]]}
    for name in ("decide", "decide-quality", "jev-latest", "jev-preview",
                 "decider-2b", "decider-4b", "decider-2b-v11", "decider-4b-v2.1"):
        assert name in rows, f"{name} is published by /v1/models and must be a row"
    # jev-* names are aliases that go to the same place as `decide`.
    assert rows["jev-latest"]["alias"] and rows["jev-latest"]["resolves_to"] == {"decisions": "decider-2b"}
    assert rows["jev-latest"]["capability"] == "decide"
    # The engine slot and the model it serves are one model; so is everything pointing at either.
    assert rows["decider-2b"]["same_model_as"] == ["decide", "decider-2b-v11", "jev-latest", "jev-preview"]
    assert rows["decider-2b-v11"]["same_model_as"] == ["decide", "decider-2b", "jev-latest", "jev-preview"]
    # A different model on the same kind of engine is not the same model.
    assert "decide-quality" not in rows["decider-2b"]["same_model_as"]
    assert rows["decide-quality"]["same_model_as"] == ["decider-4b", "decider-4b-v2.1"]


def test_names_that_share_a_target_each_keep_a_row():
    """Synonyms (`tts` and `tts-fast`) are two names an app can send, so two rows."""
    catalog = _seed_catalog()
    rows = {row["id"]: row for row in catalog["aliases"]}
    assert {"stt", "stt-quality", "tts", "tts-fast", "video", "video-fast"} <= set(rows)
    assert "tts-fast" in rows["tts"]["same_model_as"] and "tts" in rows["tts-fast"]["same_model_as"]
    assert "tts-quality" not in rows["tts"]["same_model_as"]


def test_a_pattern_publishes_literal_names_as_data():
    cfg = {"decisions": {
        "engines": {"slot": {"service": "svc", "model": "slot-v1"}},
        "stable": {"decide": ["slot"]},
        "openai": [{"match": "jev-*", "to": "decide", "names": ["jev-latest"]}],
    }}
    assert stable_aliases.validate_aliases(cfg) == []
    rows = stable_aliases.describe_aliases(cfg, operation="decisions")["decisions"]["stable"]
    assert [row["alias"] for row in rows] == ["decide", "jev-latest"]
    assert rows[1]["resolves_to"] == "slot" and rows[1]["via"] == "jev-*"
    # A name the pattern would not accept is a mistake in the data.
    cfg["decisions"]["openai"][0]["names"] = ["other-name"]
    assert stable_aliases.validate_aliases(cfg) == [
        "decisions.openai[jev-*]: name 'other-name' does not match the pattern"]


# Names the page cannot show in this fixture, each with the reason.
NOT_ROWS = {
    "rerank": "reranking has no catalog intent yet (audit P2-9)",
    "image-identity": "its workflow is not installed in the test registry",
    "openrouter/openai/gpt-4.1-nano": "a LiteLLM-only id of a provider this fixture does not register",
}
_REAL_DECISION_MODELS_FOR = proxy._decision_models_for


def _page_rows():
    registry = proxy.ADMIN_STORE.get_service_registry()
    capability_sets = [proxy._gateway_capabilities_for_auth_context(access_context())]
    model_entries = [
        *proxy._stable_alias_model_entries(),
        *proxy._build_workflow_model_entries(),
        *proxy._current_loaded_model_entries(),
    ]
    catalog = build_models_catalog(
        model_metadata=proxy._catalog_model_metadata(model_entries, registry, capability_sets),
        alias_views=proxy._gateway_aliases_provider(),
        capability_sets=capability_sets,
        model_entries=model_entries,
        service_records=registry,
        workflow_records=[{"id": r.id, "manifest": r.manifest} for r in proxy.GATEWAY_WORKFLOW_REGISTRY.records()],
    )
    return {row["id"]: row for row in [*catalog["aliases"], *catalog["models"]]}


def test_every_name_v1_models_lists_is_a_row_of_the_models_page():
    """The real app, one caller: `/v1/models` and the Models page name the same things."""
    with ExitStack() as stack:
        class _Case:  # _start_model_list_patches registers its cleanups on a TestCase-like object
            addCleanup = staticmethod(stack.callback)

        _start_model_list_patches(_Case)
        # Real decision models, not the two fake ones the shared patches install.
        stack.enter_context(patch.object(proxy, "_decision_models_for", _REAL_DECISION_MODELS_FOR))
        stack.enter_context(patch.object(proxy, "_current_loaded_llm_model_ids", _loaded(QWEN_PATH)))
        listed = {entry["id"]: entry for entry in TestClient(proxy.app).get("/v1/models").json()["data"]}
        rows = _page_rows()
    missing = sorted(set(listed) - set(rows) - set(NOT_ROWS))
    assert missing == [], "listed by /v1/models but not a row on the Models page"
    for name in ("jev-latest", "jev-preview", "decider-2b", "decider-4b", "stt-quality", "tts-fast", "krea2-turbo"):
        assert name in listed and name in rows, name
    # `/v1/models` says what a decision alias points at, as it does for every other alias.
    assert listed["jev-latest"]["azizam_alias"] is True
    assert listed["jev-latest"]["azizam_resolves_to"] == {"decisions": "decider-2b"}
    assert listed["decide"]["azizam_alias"] is True
    assert "azizam_alias" not in listed["decider-2b"]


# --- 3. one judgement for a name and the model behind it ---------------------------

def _plan_allows(plan, **request):
    request.setdefault("intent", CHAT)
    return evaluate(access(plan), PolicyRequest(**request)).allowed


def test_a_plan_listing_an_alias_grants_requests_made_through_it():
    plan = plan_for("alias-only", caps=("chat",), models=["fast"])
    assert _plan_allows(plan, model="default-fast", requested_model="fast")
    # The alias is not a grant on the model: a request that names the model itself is refused.
    assert not _plan_allows(plan, model="default-fast")
    # Another name for the same model is another name, so it is refused too.
    assert not _plan_allows(plan, model="default-fast", requested_model="gpt-4o-mini")


def test_a_plan_listing_the_model_grants_every_name_that_reaches_it():
    plan = plan_for("model-only", caps=("chat",), models=["default-fast"])
    assert _plan_allows(plan, model="default-fast", requested_model="fast")
    assert _plan_allows(plan, model="default-fast", requested_model="gpt-4o-mini")
    assert _plan_allows(plan, model="default-fast")
    assert not _plan_allows(plan, model="default", requested_model="vision")


def test_a_refusal_names_what_the_caller_sent():
    decision = evaluate(access(plan_for("none", caps=("chat",), models=["other"])),
                        PolicyRequest(intent=CHAT, model="default-fast", requested_model="fast"))
    assert decision.reasons == ("fast isn't in your access plan.",)


@pytest.fixture
def no_gate():
    yield
    category_enforcement.configure(None)


def _hide(stem, tmp_path):
    """A restricted category holding ``stem`` that no plan is listed for."""
    path = tmp_path / "categories.db"
    category_enforcement.configure(path, owner_id=lambda: OWNER)
    activate(path, rules(adult_visible_to=[], extra_services=[_service(stem, "adult")]))


def test_a_restricted_category_hides_a_model_behind_any_name_for_it(tmp_path, no_gate):
    _hide("secret-model", tmp_path)
    outsider = Who("usr_family", "app_family", access(plan_for("plan_family")))
    through_alias = PolicyRequest(intent=CHAT, model="secret-model", requested_model="friendly")
    assert category_enforcement.apply(outsider, through_alias, ALLOW).denied
    # The category may file the alias spelling instead; the model behind it is hidden too.
    filed_alias = PolicyRequest(intent=CHAT, model="plain-model", requested_model="secret-model")
    assert category_enforcement.apply(outsider, filed_alias, ALLOW).denied
    other = PolicyRequest(intent=CHAT, model="plain-model", requested_model="friendly")
    assert not category_enforcement.apply(outsider, other, ALLOW).denied


# --- decisions: every spelling is judged on the engine slot it reaches ------------

def _decision_verdicts(plan, names, service="decisions"):
    who = runtime_identity(access(plan), app_id="app_family", person_id="usr_family")
    auth = proxy.AuthContext(identity=who)
    return {name: proxy._decision_catalog_authorized(auth, name, service) for name in names}


SLOT_2B_NAMES = ("decide", "jev-latest", "jev-preview", "decider-2b", "decider-2b-v11")
DECISIONS = capability_for_intent("decision.systemone")


def test_a_plan_granting_a_decision_slot_covers_every_name_for_it():
    plan = plan_for("slot", caps=(DECISIONS,), models=["decider-2b"])
    assert all(_decision_verdicts(plan, SLOT_2B_NAMES).values())
    # The quality alias reaches a different slot first, and falls back to this one.
    assert _decision_verdicts(plan, ["decide-quality"], service="decisions-quality") == {"decide-quality": False}
    assert _decision_verdicts(plan, ["decide-quality"], service="decisions") == {"decide-quality": True}


def test_a_plan_granting_a_decision_alias_covers_only_that_name():
    plan = plan_for("alias", caps=(DECISIONS,), models=["decide"])
    assert _decision_verdicts(plan, SLOT_2B_NAMES) == {
        "decide": True, "jev-latest": False, "jev-preview": False, "decider-2b": False, "decider-2b-v11": False}


def test_a_restricted_category_on_a_decision_slot_hides_its_aliases(tmp_path, no_gate):
    """Before: only the exact spelling filed was hidden, so `decide` reached a hidden model."""
    _hide("decider-2b", tmp_path)
    plan = plan_for("all-decisions", caps=(DECISIONS,))
    assert _decision_verdicts(plan, SLOT_2B_NAMES) == {name: False for name in SLOT_2B_NAMES}
    assert _decision_verdicts(plan, ["decide-quality"], service="decisions-quality") == {"decide-quality": True}


def test_the_decision_names_an_owner_can_file_are_the_slots():
    names = {entry["stem"] for entry in proxy._category_service_catalog()
             if "decision.systemone" in entry["capabilities"]}
    assert {"decider-2b", "decider-4b"} <= names
    assert not names & {"decide", "decide-quality", "jev-latest", "jev-preview", "decider-2b-v11"}


# --- chat: a request judges the alias and the model behind it ----------------------

@contextmanager
def _gateway(tmp_path):
    receipts, jobs = tmp_path / "receipts.db", tmp_path / "media_jobs.db"
    receipt_store.init_db(receipts)
    media_jobs.init_db(jobs)
    migrated = copy.deepcopy(proxy.config)
    for service in (migrated.get("backends") or {}).values():
        if isinstance(service, dict) and isinstance(service.get("models"), list):
            service["model_policy_schema_version"] = proxy.service_registry.MODEL_POLICY_SCHEMA_VERSION
    with ExitStack() as stack:
        stack.enter_context(patch.object(receipt_store, "DB_PATH", receipts))
        stack.enter_context(patch.object(media_jobs, "DB_PATH", jobs))
        stack.enter_context(patch.object(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache"))
        stack.enter_context(patch.object(proxy, "config", migrated))
        yield TestClient(proxy.app, raise_server_exceptions=False)


def _ask_chat(client, plan, model):
    """Status, message and forwarded model of a chat request under ``plan``; the forward step is stubbed."""
    forwarded = []

    async def forward(*args, **kwargs):
        forwarded.append(kwargs["body_json"]["model"])
        return Response(content="{}", media_type="application/json")

    runtime = type("Runtime", (), {"enabled": True, "service_id": "litellm"})()
    with authenticated_profile(plan), patch.object(proxy.ADMIN_STORE, "get_service_registry", litellm_registry), \
            patch.object(proxy, "_forward_litellm_proxy_request", forward), \
            patch.object(proxy, "_litellm_runtime_config", lambda: runtime), \
            patch.object(proxy, "_service_availability_response", lambda *a, **k: None), \
            patch.object(proxy, "_model_dispatch_admission_response", lambda *a, **k: None):
        response = client.post("/v1/chat/completions", headers=AUTH_HEADERS, json={
            "model": model, "messages": [{"role": "user", "content": "hi"}]})
    message = (response.json().get("error") or {}).get("message") if response.status_code != 200 else None
    return response.status_code, message, forwarded


def test_chat_requests_judge_the_alias_and_the_model_behind_it(tmp_path):
    with _gateway(tmp_path) as client:
        alias_plan = plan_for("alias", caps=("chat",), models=["fast"])
        assert _ask_chat(client, alias_plan, "fast") == (200, None, ["default-fast"])
        assert _ask_chat(client, alias_plan, "default-fast") == (403, "default-fast isn't in your access plan.", [])
        model_plan = plan_for("model", caps=("chat",), models=["default-fast"])
        for name in ("fast", "gpt-4o-mini", "default-fast"):
            assert _ask_chat(client, model_plan, name) == (200, None, ["default-fast"])
        # A refusal names the name the caller sent, not a model they never typed.
        assert _ask_chat(client, plan_for("none", caps=("chat",), models=["rp"]), "fast") == (
            403, "fast isn't in your access plan.", [])


# --- MCP discovery hides what a restricted category hides -------------------------

def test_mcp_capabilities_leave_out_a_workflow_a_restricted_category_hides(tmp_path, no_gate):
    from tests.support.access import authenticated

    hidden = next(record.id for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records()
                  if record.intent == "image.generate")
    _hide(hidden, tmp_path)

    def listed(plan):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "capabilities", "arguments": {"intent": "image.generate"}}}
        with authenticated(runtime_identity(access(plan), app_id="app_family", person_id="usr_family")):
            response = TestClient(proxy.app).post(
                "/mcp", json=rpc, headers={"authorization": "Bearer k", "mcp-protocol-version": "2025-11-25"})
        assert response.status_code == 200, response.text
        return {row["target"] for row in response.json()["result"]["structuredContent"]["workflows"]}

    everything = plan_for("everything")
    assert hidden.rsplit(".", 1)[-1] not in listed(everything)
    category_enforcement.configure(None)
    assert hidden.rsplit(".", 1)[-1] in listed(everything)


# --- speech and transcription are held to the engine a name reaches ---------------

def _post_speech(client, plan, name):
    with authenticated_profile(plan):
        return client.post("/v1/audio/speech", headers=AUTH_HEADERS,
                           json={"model": name, "input": "hi", "voice": "alloy"}).status_code


def _post_transcription(client, plan, name):
    with authenticated_profile(plan):
        return client.post("/v1/audio/transcriptions", headers=AUTH_HEADERS, data={"model": name},
                           files={"file": ("a.wav", b"RIFF", "audio/wav")}).status_code


@pytest.mark.parametrize("operation, alias, post, capability", [
    ("speech", "tts", _post_speech, "speech"),
    ("transcriptions", "stt", _post_transcription, "transcribe"),
])
def test_speech_and_transcription_requests_are_held_to_the_engine_their_name_reaches(
        tmp_path, operation, alias, post, capability):
    """Before: a plan listing any engine let a request through to every engine, and so did a category rule."""
    engine = proxy.resolve_stable_alias(operation, alias).engine
    other = next(row["engine"] for row in proxy._gateway_aliases_provider(operation)[operation]["engines"]
                 if row["engine"] != engine)
    with _gateway(tmp_path) as client:
        def status(models, name=alias):
            return post(client, plan_for("p", caps=(capability,), models=models), name)
        assert status(["nope"]) == 403
        assert status([other]) == 403
        assert status([engine]) != 403  # the model behind the name covers the name
        assert status([alias]) != 403  # the name itself is a grant too
        assert status([alias], engine) == 403  # but a grant of the name is not a grant of the engine


def test_the_engines_an_owner_can_file_include_speech_and_transcription():
    stems = {entry["stem"]: entry["capabilities"] for entry in proxy._category_service_catalog()}
    speech = proxy.resolve_stable_alias("speech", "tts").engine
    transcription = proxy.resolve_stable_alias("transcriptions", "stt").engine
    assert "speech.synthesis" in stems[speech]
    assert "speech.transcription" in stems[transcription]


# --- the other chat frontends judge a name the way the chat route does -------------

FRONTENDS = (
    ("/v1/messages", lambda model: {"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}),
    ("/api/chat", lambda model: {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": False}),
    ("/v1/responses", lambda model: {"model": model, "input": "hi"}),
)


@pytest.mark.parametrize("path, body", FRONTENDS, ids=[path for path, _ in FRONTENDS])
def test_chat_frontends_hide_a_category_model_behind_its_other_names(tmp_path, no_gate, path, body):
    """These routes loop back to the chat route, which then trusts them, so they must resolve the name first."""
    _hide("default-fast", tmp_path)
    with _gateway(tmp_path) as client, authenticated_profile(plan_for("everything", caps=("chat",))):
        for name in ("fast", "claude-3-haiku-20240307", "default-fast"):
            response = client.post(path, headers=AUTH_HEADERS, json=body(name))
            assert response.status_code == 403, (name, response.text)
        # A model that is not in the category gets past the policy (the loopback itself cannot run here).
        assert client.post(path, headers=AUTH_HEADERS, json=body("default")).status_code != 403


@pytest.mark.parametrize("path, body", FRONTENDS, ids=[path for path, _ in FRONTENDS])
def test_chat_frontends_let_a_plan_for_the_model_use_its_other_names(tmp_path, path, body):
    with _gateway(tmp_path) as client:
        with authenticated_profile(plan_for("model", caps=("chat",), models=["default-fast"])):
            for name in ("fast", "claude-3-haiku-20240307", "default-fast"):
                assert client.post(path, headers=AUTH_HEADERS, json=body(name)).status_code != 403, name
        with authenticated_profile(plan_for("other", caps=("chat",), models=["rp"])):
            assert client.post(path, headers=AUTH_HEADERS, json=body("fast")).status_code == 403


def test_alias_policy_rejects_cross_service_ambiguity_but_scopes_explicit_service():
    from gateway.alias_policy import AliasPolicyResolutionError, resolve_request_target

    runtime = {"backends": {
        "alpha": {"models": [{"id": "base-a"}, {"id": "friendly", "base_model_id": "base-a"}]},
        "beta": {"models": [{"id": "base-b"}, {"id": "friendly", "base_model_id": "base-b"}]},
    }}
    with pytest.raises(AliasPolicyResolutionError, match="ambiguous logical alias policies"):
        resolve_request_target(" FRIENDLY ", runtime_config=runtime, required=True)
    target = resolve_request_target(" FRIENDLY ", runtime_config=runtime, service_id="beta", required=True)
    assert (target.logical_service_id, target.base_model_id) == ("beta", "base-b")


def test_alias_policy_rejects_missing_base_instead_of_using_another_service():
    from gateway.alias_policy import AliasPolicyResolutionError, resolve_request_target

    runtime = {"backends": {
        "alpha": {"models": [{"id": "friendly", "base_model_id": "base"}]},
        "beta": {"models": [{"id": "base", "transforms": ["wrong-service"]}]},
    }}
    with pytest.raises(AliasPolicyResolutionError, match="has no declared base policy"):
        resolve_request_target("friendly", runtime_config=runtime, service_id="alpha", required=True)


def test_alias_policy_returns_independent_nested_base_and_overlay_specs():
    from gateway.alias_policy import resolve_request_target

    runtime = {"backends": {"svc": {"models": [
        {"id": "base", "transforms": [{"set": {"nested": ["base"]}}]},
        {"id": "alias", "base_model_id": "base", "policy_overlay": [{"set": {"nested": ["overlay"]}}]},
    ]}}}
    first = resolve_request_target("alias", runtime_config=runtime, required=True)
    first.base_transform_specs[0]["set"]["nested"].append("changed")
    first.alias_overlay_specs[0]["set"]["nested"].append("changed")
    second = resolve_request_target("alias", runtime_config=runtime, required=True)
    assert second.base_transform_specs == ({"set": {"nested": ["base"]}},)
    assert second.alias_overlay_specs == ({"set": {"nested": ["overlay"]}},)
