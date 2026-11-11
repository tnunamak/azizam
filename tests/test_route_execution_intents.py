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
], ids=lambda function: function.__name__)
def test_no_handler_spells_the_intent_it_admits_or_receipts(function):
    assert _spelled_intents(function) == []
    assert "intents" in inspect.signature(inspect.unwrap(function)).parameters


def test_the_ast_check_catches_a_spelled_intent():
    async def regressed(request, auth_context, *, intents):
        _dispatch_admission_response("image.generate", auth_context=auth_context)  # noqa: F821
        await media_ingestion.start_receipt(request, auth_context, intent="audio.generate")  # noqa: F821

    assert _spelled_intents(regressed) == ["image.generate", "audio.generate"]
