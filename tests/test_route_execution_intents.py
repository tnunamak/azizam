"""A row's ``execution_intents`` reach its handler from the mount, unchanged (docs/design/CODE_TO_DATA.md).

The route handlers no longer spell the intent they admit and receipt under: the matched frontend row holds it,
and the mount puts it in the request scope after its caller check. The first tests mount a synthetic row whose
intents are made-up words, the "unfamiliar workflow" oracle.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import frontend_mount, operation_security

MADE_UP = "frobnicate.widget"
MADE_UP_RECEIPT = "quux.receipt"


def _app(*, self_authenticates: bool, intents: dict | None, seen: list):
    async def endpoint(request: Request):
        seen.append(request.scope.get(frontend_mount.EXECUTION_INTENTS_SCOPE_KEY))
        return JSONResponse({"ok": True})

    if self_authenticates:
        operation = frontend_mount.Operation(endpoint, frozenset({"POST"}), self_authenticates=True,
                                              auth=operation_security.APP_KEY)
    else:
        operation = frontend_mount.Operation(endpoint, frozenset({"POST"}))
    translators = frontend_mount.Translators({"identity": {"probe.op": operation}})
    frontends = [{"id": "probe", "source": "builtin", "registered_routes": [
        {"methods": ["POST"], "canonical": "/v1/probe", "translator": "identity", "intent": "probe.op",
         **({"execution_intents": intents} if intents is not None else {})}]}]
    rows = frontend_mount.rows_from_frontends(frontends)
    order: list[str] = []

    async def authenticate(request: Request):
        order.append("authenticated")
        if request.headers.get("authorization") != "Bearer good":
            return JSONResponse({"error": "no"}, status_code=401)
        return None

    app = FastAPI()
    frontend_mount.mount(app, rows, translators, authenticate=authenticate)
    return app, order


@pytest.mark.parametrize("self_authenticates", [True, False])
def test_a_made_up_intent_reaches_the_handler_unchanged(self_authenticates):
    seen: list = []
    app, _order = _app(self_authenticates=self_authenticates,
                       intents={"admission": MADE_UP, "receipt": MADE_UP_RECEIPT}, seen=seen)
    response = TestClient(app).post("/v1/probe", headers={"authorization": "Bearer good"})
    assert response.status_code == 200
    expected = frontend_mount.ExecutionIntents(admission=MADE_UP, receipt=MADE_UP_RECEIPT)
    assert seen == [frontend_mount.MatchedOperation("identity", "probe.op", expected)]
    assert frontend_mount.matched_intents({frontend_mount.EXECUTION_INTENTS_SCOPE_KEY: seen[0]}, "identity", "probe.op") == expected
    assert frontend_mount.matched_intents({frontend_mount.EXECUTION_INTENTS_SCOPE_KEY: seen[0]}, "identity", "other.op") is None


def test_the_mount_checks_the_caller_before_it_binds_the_intents():
    seen: list = []
    app, order = _app(self_authenticates=False, intents={"admission": MADE_UP}, seen=seen)
    response = TestClient(app).post("/v1/probe", headers={"authorization": "Bearer bad"})
    assert response.status_code == 401
    assert order == ["authenticated"] and seen == []


def test_a_row_without_intents_binds_nothing_and_adds_no_route_class():
    seen: list = []
    app, _order = _app(self_authenticates=True, intents=None, seen=seen)
    TestClient(app).post("/v1/probe")
    assert seen == [None]
    [route] = [r for r in app.router.routes if getattr(r, "path", "") == "/v1/probe"]
    assert type(route).__name__ == "APIRoute"


@pytest.mark.parametrize("bad", [{}, {"admission": ""}, {"admission": 3}, {"policy": "x"}, ["admission"], "image.generate"])
def test_a_malformed_intents_field_refuses_the_frontend(bad):
    with pytest.raises(frontend_mount.FrontendMountError):
        frontend_mount.rows_from_frontends([{"id": "p", "registered_routes": [
            {"methods": ["POST"], "canonical": "/v1/x", "translator": "identity", "intent": "x",
             "execution_intents": bad}]}])


# --- the shipped rows hold today's values -----------------------------------------------------------------

import ast
import inspect
import textwrap

from starlette.requests import Request as StarletteRequest

import proxy

SHIPPED = {
    "images.generate": frontend_mount.ExecutionIntents(admission="image.generate"),
    "images.edit": frontend_mount.ExecutionIntents(admission="image.edit"),
    "images.variations": frontend_mount.ExecutionIntents(admission="image.variation"),
    "audio.generate": frontend_mount.ExecutionIntents(admission="audio.generate", receipt="audio.generate"),
    # Route policy differs on purpose: videos.edit has policy video.remix and videos.extend has policy video.generate.
    "videos.remix": frontend_mount.ExecutionIntents(admission="video.remix"),
    "videos.edit": frontend_mount.ExecutionIntents(admission="video.edit"),
    "videos.extend": frontend_mount.ExecutionIntents(admission="video.extend", receipt="video.extend"),
}


def _shipped_rows():
    from gateway import route_registry
    records = route_registry.protocol_frontend_records(proxy_config=proxy.config)
    return [row for row in frontend_mount.rows_from_frontends(records) if row.execution_intents is not None]


@pytest.mark.parametrize("intent", sorted(SHIPPED))
def test_every_shipped_row_of_an_operation_holds_the_value_its_handler_used_to_spell(intent):
    rows = [row for row in _shipped_rows() if row.intent == intent]
    assert rows, intent
    assert {row.execution_intents for row in rows} == {SHIPPED[intent]}


def test_only_these_operations_declare_execution_intents_so_far():
    assert {row.intent for row in _shipped_rows()} == set(SHIPPED)


# --- a row with a different intent reaches admission and the receipt ---------------------------------------

class _Seen(Exception):
    pass


def _request(operation: tuple[str, str] | None, intents) -> StarletteRequest:
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""}
    if operation is not None:
        scope[frontend_mount.EXECUTION_INTENTS_SCOPE_KEY] = frontend_mount.MatchedOperation(*operation, intents)
    return StarletteRequest(scope)


def _spy_admission(monkeypatch):
    from gateway import media_job_intents
    # Synthetic frontend rows still need modality rows before graph admission can resolve them.
    for intent in (MADE_UP, "edit.made_up", "extend.made_up"):
        monkeypatch.setitem(media_job_intents.MODALITY_BY_INTENT, intent, "image")
    seen: list[str] = []

    def spy(intent, **_kwargs):
        seen.append(intent)
        return JSONResponse({"stopped": True}, status_code=503)

    monkeypatch.setattr(proxy, "_dispatch_admission_response", spy)
    return seen


HANDLERS = {
    "images.generate": proxy.proxy_graph_image_request,
    "images.edit": proxy.proxy_graph_image_edit_request,
    "images.variations": proxy.proxy_graph_image_variations_request,
    "audio.generate": proxy.proxy_audio_generate,
}


@pytest.mark.parametrize("operation", sorted(HANDLERS))
def test_the_matched_rows_admission_intent_reaches_admission(operation, monkeypatch):
    seen = _spy_admission(monkeypatch)
    intents = frontend_mount.ExecutionIntents(admission=MADE_UP, receipt=MADE_UP_RECEIPT)
    asyncio.run(HANDLERS[operation](_request(("identity", operation), intents), object()))
    assert seen == [MADE_UP]


@pytest.mark.parametrize("operation", sorted(HANDLERS))
def test_a_caller_outside_a_request_acts_under_the_mounted_row_of_the_operation(operation, monkeypatch):
    seen = _spy_admission(monkeypatch)
    row = frontend_mount.RouteRow(
        frontend="probe", path="/v1/probe", methods=frozenset({"POST"}), intent=operation, translator="identity",
        execution_intents=frontend_mount.ExecutionIntents(admission=MADE_UP),
    )
    monkeypatch.setattr(proxy.FRONTEND_MOUNTS, "_rows", [row])
    asyncio.run(HANDLERS[operation](_request(None, None), object()))
    assert seen == [MADE_UP]


def test_a_request_matched_to_another_operation_does_not_lend_its_intents(monkeypatch):
    """The variations handler calls the edit handler with the same request: each acts under its own row."""
    seen = _spy_admission(monkeypatch)
    edit_row = frontend_mount.RouteRow(
        frontend="probe", path="/v1/probe", methods=frozenset({"POST"}), intent="images.edit", translator="identity",
        execution_intents=frontend_mount.ExecutionIntents(admission="edit.made_up"),
    )
    monkeypatch.setattr(proxy.FRONTEND_MOUNTS, "_rows", [edit_row])
    variation = frontend_mount.ExecutionIntents(admission="variation.made_up")
    asyncio.run(proxy.proxy_graph_image_edit_request(_request(("identity", "images.variations"), variation), object()))
    assert seen == ["edit.made_up"]


def test_a_row_without_the_intent_stops_the_work(monkeypatch):
    seen = _spy_admission(monkeypatch)
    with pytest.raises(frontend_mount.FrontendMountError):
        asyncio.run(proxy.proxy_graph_image_request(
            _request(("identity", "images.generate"), frontend_mount.ExecutionIntents(receipt="only.receipt")), object()))
    assert seen == []


def test_the_matched_rows_receipt_intent_starts_the_audio_receipt(monkeypatch):
    from gateway import media_ingestion
    started: list[str] = []

    async def start_receipt(_request, _auth, *, intent, **_kwargs):
        started.append(intent)
        raise _Seen

    monkeypatch.setattr(proxy, "authenticate_request", lambda *_args: object())
    monkeypatch.setattr(proxy, "_enforce_runtime_route_policy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(media_ingestion, "start_receipt", start_receipt)
    intents = frontend_mount.ExecutionIntents(admission=MADE_UP, receipt=MADE_UP_RECEIPT)
    with pytest.raises(_Seen):
        asyncio.run(proxy.audio_generate(_request(("identity", "audio.generate"), intents)))
    assert started == [MADE_UP_RECEIPT]


# --- no intent literal comes back into a handler ---------------------------------------------------------------

def _body(function) -> ast.AST:
    source = inspect.getsource(inspect.unwrap(function))
    [node] = ast.parse(textwrap.dedent(source)).body
    node.decorator_list = []
    return node


def _spelled_intents(function) -> list[str]:
    """Literal strings given as the intent of an admission call or a receipt start in the function body."""
    spelled: list[str] = []
    for node in ast.walk(_body(function)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        candidates: list[ast.expr] = []
        if name in {"_dispatch_admission_response", "_media_job_admission_response"} and node.args:
            candidates.append(node.args[0])
        if name == "start_receipt":
            candidates.extend(keyword.value for keyword in node.keywords if keyword.arg == "intent")
        spelled += [c.value for c in candidates if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        spelled += [c.value for c in candidates if isinstance(c, ast.JoinedStr)]
    return spelled


@pytest.mark.parametrize("function", [
    proxy.proxy_graph_image_request, proxy.proxy_graph_image_edit_request,
    proxy.proxy_graph_image_variations_request, proxy.proxy_audio_generate, proxy.audio_generate,
    proxy.proxy_graph_video_extend, proxy.proxy_graph_video_remix, proxy.proxy_graph_video_edit,
], ids=lambda function: function.__name__)
def test_no_handler_spells_the_intent_it_admits_or_receipts(function):
    assert _spelled_intents(function) == []
    assert "intents" in inspect.signature(inspect.unwrap(function)).parameters


def test_the_ast_check_catches_a_spelled_intent():
    async def regressed(request, auth_context, *, intents):
        _dispatch_admission_response("image.generate", auth_context=auth_context)  # noqa: F821
        await media_ingestion.start_receipt(request, auth_context, intent="audio.generate")  # noqa: F821

    assert _spelled_intents(regressed) == ["image.generate", "audio.generate"]


# --- the video routes: three roles, three values --------------------------------------------------------------

import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _noop_warm(monkeypatch):
    async def warm(_auth_context):
        return None

    monkeypatch.setattr(proxy, "_warm_video_owner_keys", warm)


def _video_call(operation: str, request):
    """The handler of a video operation, called the way its route calls it, with a request."""
    if operation == "videos.remix":
        return proxy.proxy_graph_video_remix("video_x", object(), request)
    if operation == "videos.edit":
        return proxy.proxy_graph_video_edit(request, object())
    return proxy.proxy_graph_video_extend(request, object(), receipt=object())


VIDEO_OPERATIONS = ["videos.remix", "videos.edit", "videos.extend"]


@pytest.mark.parametrize("operation", VIDEO_OPERATIONS)
def test_the_matched_video_rows_admission_intent_reaches_admission(operation, monkeypatch):
    seen = _spy_admission(monkeypatch)
    _noop_warm(monkeypatch)
    intents = frontend_mount.ExecutionIntents(admission=MADE_UP, receipt=MADE_UP_RECEIPT)
    asyncio.run(_video_call(operation, _request(("identity", operation), intents)))
    assert seen == [MADE_UP]


@pytest.mark.parametrize("operation", VIDEO_OPERATIONS)
def test_a_video_handler_called_outside_a_request_acts_under_the_mounted_row(operation, monkeypatch):
    seen = _spy_admission(monkeypatch)
    _noop_warm(monkeypatch)
    row = frontend_mount.RouteRow(
        frontend="probe", path="/v1/probe", methods=frozenset({"POST"}), intent=operation, translator="identity",
        execution_intents=frontend_mount.ExecutionIntents(admission=MADE_UP),
    )
    monkeypatch.setattr(proxy.FRONTEND_MOUNTS, "_rows", [row])
    asyncio.run(_video_call(operation, _request(None, None)))
    assert seen == [MADE_UP]


@pytest.mark.parametrize("operation", VIDEO_OPERATIONS)
def test_a_video_row_without_an_admission_intent_stops_the_work(operation, monkeypatch):
    seen = _spy_admission(monkeypatch)
    _noop_warm(monkeypatch)
    with pytest.raises(frontend_mount.FrontendMountError):
        asyncio.run(_video_call(operation, _request(
            ("identity", operation), frontend_mount.ExecutionIntents(receipt="only.receipt"))))
    assert seen == []


def test_the_extend_receipt_and_admission_are_two_roles_that_each_reach_their_own_site(monkeypatch):
    from gateway import media_ingestion
    started: list[str] = []
    admitted = _spy_admission(monkeypatch)
    _noop_warm(monkeypatch)

    async def start_receipt(_request, _auth, *, intent, **_kwargs):
        started.append(intent)
        raise _Seen

    monkeypatch.setattr(media_ingestion, "start_receipt", start_receipt)
    intents = frontend_mount.ExecutionIntents(admission=MADE_UP, receipt=MADE_UP_RECEIPT)
    request = _request(("identity", "videos.extend"), intents)
    with pytest.raises(_Seen):
        asyncio.run(proxy.proxy_graph_video_extend(request, object()))
    assert started == [MADE_UP_RECEIPT] and admitted == []
    asyncio.run(proxy.proxy_graph_video_extend(request, object(), receipt=object()))
    assert admitted == [MADE_UP]


def test_a_request_matched_to_another_video_operation_does_not_lend_its_intents(monkeypatch):
    seen = _spy_admission(monkeypatch)
    _noop_warm(monkeypatch)
    extend_row = frontend_mount.RouteRow(
        frontend="probe", path="/v1/probe", methods=frozenset({"POST"}), intent="videos.extend", translator="identity",
        execution_intents=frontend_mount.ExecutionIntents(admission="extend.made_up", receipt="extend.receipt"),
    )
    monkeypatch.setattr(proxy.FRONTEND_MOUNTS, "_rows", [extend_row])
    edit = frontend_mount.ExecutionIntents(admission="edit.made_up")
    asyncio.run(proxy.proxy_graph_video_extend(_request(("identity", "videos.edit"), edit), object(), receipt=object()))
    assert seen == ["extend.made_up"]


def test_the_shared_video_graph_helper_admits_under_the_intent_its_caller_passes(monkeypatch):
    seen = _spy_admission(monkeypatch)
    monkeypatch.setattr(proxy, "configured_downstream_headers", lambda *_args, **_kwargs: {})
    response = asyncio.run(proxy._submit_video_graph(
        workflow={}, auth_context=object(), workflow_name="w", requested_model="m", public_model="m", prompt="p",
        width=1, height=1, length=1, seed_val=1, seconds_val=1.0, actual_seconds=1.0, remixed_from_video_id=None,
        duration_adjustment_reason=None, admission_intent=MADE_UP))
    assert seen == [MADE_UP] and response.status_code == 503


def test_the_video_rows_keep_route_policy_and_execution_intents_apart():
    """Route policy keeps its own values: edit's policy is video.remix and extend's is video.generate."""
    from gateway import route_registry
    records = route_registry.protocol_frontend_records(proxy_config=proxy.config)
    items = {item.get("intent"): item for record in records for item in record.get("registered_routes") or ()
             if item.get("translator") and str(item.get("intent", "")).startswith("videos.")}
    policy = {intent: (item.get("runtime_policy") or {}).get("intent") for intent, item in items.items()}
    assert policy["videos.edit"] == "video.remix" and policy["videos.extend"] == "video.generate"
    assert items["videos.edit"]["execution_intents"] == {"admission": "video.edit"}
    assert items["videos.extend"]["execution_intents"] == {"admission": "video.extend", "receipt": "video.extend"}
    assert items["videos.remix"]["execution_intents"] == {"admission": "video.remix"}


def test_videos_create_is_the_one_video_handler_that_still_spells_its_intents():
    """Its registered row has no translator, so the mount holds no row to read; this pins what stays spelled."""
    create = _body(proxy._proxy_graph_video_create)
    assert _spelled_intents(proxy._proxy_graph_video_create) == ["video.generate"]  # the receipt it starts
    assigned = [node.value.value for node in ast.walk(create) if isinstance(node, ast.Assign)
                and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["admission_intent"]]
    assert assigned == ["video.generate"]
    document = yaml.safe_load((ROOT / "registry" / "protocol_frontends.yml").read_text(encoding="utf-8"))
    row = next(item for record in document["records"] for item in record.get("registered_routes") or ()
               if item.get("intent") == "videos.create")
    assert "translator" not in row and "execution_intents" not in row


def test_the_shared_video_helpers_spell_no_intent_and_take_the_callers():
    for function in (proxy._submit_video_graph, proxy._submit_video_chain):
        assert _spelled_intents(function) == []
        assert "admission_intent" in inspect.signature(function).parameters


def test_every_caller_of_the_shared_video_helpers_passes_its_own_admission_intent():
    for function in (proxy._proxy_graph_video_create, proxy._submit_video_chain, proxy.proxy_graph_video_extend):
        calls = [node for node in ast.walk(_body(function)) if isinstance(node, ast.Call)
                 and getattr(node.func, "id", "") in {"_submit_video_graph", "_submit_video_chain"}]
        for call in calls:
            keyword = {k.arg: k.value for k in call.keywords}.get("admission_intent")
            assert keyword is not None and not isinstance(keyword, ast.Constant), (function.__name__, call.lineno)
    # extend hands its row's admission on; the lambda that holds the call is inside the handler body.
    assert "require_admission" in inspect.getsource(inspect.unwrap(proxy.proxy_graph_video_extend))


# --- the non-leak sites that fit no reviewed category stay counted ---------------------------------------------

def test_the_realtime_resource_class_the_chain_asset_kind_and_the_operation_keys_stay_unreviewed():
    """Three sites checked against tests/domain_literal_reviewed.yml's categories (media_type, engine_node_input,
    wire_enum) and left out of it: the resource class a realtime route admits under (an admission choice, not a
    published value), the resolver mode ``kind="video"`` the chain passes (an internal video/image switch, not
    the published assets[].kind), and the operation table keys (the code side of the registry rows' ``intent``,
    not in the route graph or OpenAPI bytes). A broad proxy.py allowance for these words would also excuse handlers."""
    document = yaml.safe_load((ROOT / "tests" / "domain_literal_reviewed.yml").read_text(encoding="utf-8"))
    allowed = {entry["literal"] for entry in document["sites"] if entry["file"] == "proxy.py"}
    assert not allowed & {"audio", "video", "audio.speech", "audio.transcriptions", "audio.translations",
                          "audio.generate", "audio.models", "audio.realtime"}


def test_graph_admission_refuses_an_intent_without_a_modality_row(monkeypatch):
    from gateway import media_job_intents
    monkeypatch.delitem(media_job_intents.MODALITY_BY_INTENT, MADE_UP, raising=False)
    with pytest.raises(ValueError, match="media job intent 'frobnicate.widget' has no row in media_job_intents.yml"):
        proxy._graph_media_admission(MADE_UP, auth_context=object())
