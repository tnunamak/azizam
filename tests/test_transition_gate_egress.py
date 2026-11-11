"""T1.1 egress guard: in a transition-enabled residency domain, no request bytes go to a service
unless the sending task holds an unreleased serving lease for that domain.

Every factory in the gateway that builds a connection, client or URL for a request send calls
``_require_egress_lease``. This file checks that three ways:

* one parametrized table over every guarded factory: refused with no send, allowed with a lease,
  unchanged for a domain that is not enabled;
* the async sites (protocol runtime, protocol jobs, decision and mesh hooks);
* a static sweep over proxy.py, so a new connection or client factory fails until a reviewer
  puts it in the guarded or the exempt list below, with a reason.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import proxy
from gateway import transition_gate
from gateway.engine_protocol import dispatch as protocol_dispatch, jobs as protocol_jobs
from gateway.transition_gate import GateStore, TransitionUnavailable

ENABLED = {"enabled": True, "activation": "pass_through"}
SERVICES = ("comfyui", "faces", "stt-a", "tts-a", "tts-host", "native-a", "llm-a", "realtime", "rerank-a")


def _record(service: str, enabled: bool) -> dict[str, Any]:
    return {"enabled": True, "host": "127.0.0.1", "port": 9,
            "residency": {"domain": f"{service}-domain", **({"transition": dict(ENABLED)} if enabled else {})}}


@pytest.fixture
def domains(monkeypatch, tmp_path):
    """Every service id above in its own enabled pass-through domain, gate open, no lease held."""
    def configure(enabled: bool = True) -> None:
        backends = {**(proxy.config.get("backends") or {}), **{name: _record(name, enabled) for name in SERVICES}}
        monkeypatch.setitem(proxy.config, "backends", backends)

    configure(True)
    store = GateStore(tmp_path / "transition.db")
    store.load()
    monkeypatch.setattr(proxy, "TRANSITION_GATES", store)
    monkeypatch.setattr(proxy, "_swap_drain", proxy._new_swap_drain())
    token = transition_gate._EGRESS_LEASES.set(())
    yield SimpleNamespace(configure=configure, drain=proxy._swap_drain)
    transition_gate._EGRESS_LEASES.reset(token)


# One row per guarded factory: (service id, how to call it). The call builds a connection, client
# or URL for a send; it must refuse before it returns anything.
STT = SimpleNamespace(service_id="stt-a", host="http://127.0.0.1:9", timeout_seconds=5.0)
FACTORIES = {
    "graph_connection": ("comfyui", lambda: proxy._graph_connection(capability=proxy._GRAPH_CAPABILITY)),
    "graph_session": ("comfyui", lambda: proxy._graph(object(), proxy._GRAPH_CAPABILITY)),
    "tts_connection": ("tts-a", lambda: proxy._tts_connection("http://127.0.0.1:9", service_id="tts-a")),
    "stt_connection": ("stt-a", lambda: proxy._stt_connection(STT)),
    "identity_face_client": ("faces", lambda: proxy._identity_face_client()),
    "native_engine_base": ("native-a", lambda: proxy._native_engine_base({"service": "native-a", "host": "http://127.0.0.1:9"})),
    "inference_connection": ("llm-a", lambda: proxy._inference_connection(
        "llm-a", None, "/v1/chat/completions", fallback_address="http://127.0.0.1:9", fallback_timeout=5.0,
        db_path=Path("unused.db"), env=lambda name: None)),
}


@pytest.mark.parametrize("name", FACTORIES)
def test_a_guarded_factory_refuses_an_enabled_domain_without_a_lease(domains, name) -> None:
    service, call = FACTORIES[name]
    with pytest.raises(TransitionUnavailable) as refused:
        call()
    assert refused.value.reason == "path_not_leased"
    assert refused.value.domain == f"{service}-domain"
    assert domains.drain.in_flight(service) == 0, "a refusal takes no lease"


@pytest.mark.parametrize("name", FACTORIES)
def test_a_guarded_factory_proceeds_under_a_lease_and_refuses_again_after_it(domains, name) -> None:
    service, call = FACTORIES[name]
    lease = domains.drain.lease(service)
    try:
        call()  # no TransitionUnavailable
    finally:
        lease.release()
    with pytest.raises(TransitionUnavailable):
        call()


@pytest.mark.parametrize("name", FACTORIES)
def test_a_lease_of_another_domain_does_not_open_the_path(domains, name) -> None:
    service, call = FACTORIES[name]
    other = next(item for item in SERVICES if item != service)
    lease = domains.drain.lease(other)
    try:
        with pytest.raises(TransitionUnavailable):
            call()
    finally:
        lease.release()


@pytest.mark.parametrize("name", FACTORIES)
def test_a_domain_that_is_not_enabled_is_never_refused(domains, name) -> None:
    domains.configure(False)
    _service, call = FACTORIES[name]
    call()  # nothing held, nothing raised


def test_a_probe_connection_to_an_enabled_domain_is_not_refused(domains) -> None:
    """A health read, a metadata read and a message-only address are exempt: they carry no request bytes."""
    proxy._graph_connection(capability=proxy._GRAPH_CAPABILITY, probe=True)
    proxy._graph(object(), proxy._GRAPH_CAPABILITY, probe=True)


def test_a_lease_does_not_leak_to_a_task_that_started_before_it(domains) -> None:
    async def scenario() -> None:
        before = asyncio.create_task(asyncio.sleep(0, result="x"))
        lease = domains.drain.lease("comfyui")
        try:
            proxy._graph_connection(capability=proxy._GRAPH_CAPABILITY)
            async def later() -> None:
                with pytest.raises(TransitionUnavailable):
                    proxy._tts_connection("http://127.0.0.1:9", service_id="tts-a")
            await asyncio.create_task(later())
            await before
        finally:
            lease.release()

    asyncio.run(scenario())


# -- the async sites ---------------------------------------------------------------------------------


def test_the_decision_hook_answers_a_gateway_shaped_refusal_and_allows_a_lease(domains) -> None:
    refusal = proxy._egress_refusal_response("llm-a")
    assert refusal is not None and refusal.status_code == 503
    assert json.loads(refusal.body)["error"]["code"] == "transition_unsupported"
    assert refusal.headers.get("Retry-After")
    lease = domains.drain.lease("llm-a")
    try:
        assert proxy._egress_refusal_response("llm-a") is None
    finally:
        lease.release()


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append("invoke")
        return "sent"

    async def submit(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("submit")
        raise AssertionError("the job was submitted")


def test_the_protocol_runtime_checks_before_the_send(domains) -> None:
    runtime = protocol_dispatch.Runtime.__new__(protocol_dispatch.Runtime)
    adapter = _Adapter()
    asked: list[int] = []

    def egress() -> None:
        asked.append(1)
        proxy._require_egress_lease("llm-a")

    runtime.adapter, runtime._egress = adapter, egress
    runtime.connection_for = lambda op, **kwargs: object()
    runtime.request = lambda op, body: object()
    with pytest.raises(TransitionUnavailable):
        asyncio.run(runtime.invoke("generate", {}))
    assert adapter.calls == [] and asked == [1]

    async def with_lease() -> str:
        lease = domains.drain.lease("llm-a")
        try:
            return await runtime.invoke("generate", {})
        finally:
            lease.release()

    assert asyncio.run(with_lease()) == "sent"


def test_the_protocol_job_loop_checks_before_any_engine_call(domains) -> None:
    adapter = _Adapter()
    runtime = SimpleNamespace(adapter=adapter, require_egress=lambda: proxy._require_egress_lease("llm-a"),
                              connection_for=lambda op: pytest.fail("a connection was built"))
    with pytest.raises(TransitionUnavailable):
        asyncio.run(protocol_jobs.run(runtime, "generate", {}, job_id="job"))
    assert adapter.calls == []


# -- the static sweep --------------------------------------------------------------------------------

_SOURCE = Path(proxy.__file__).read_text()
_GUARD_CALLS = {"_require_egress_lease", "require_egress_lease", "_egress_refusal_response", "_inference_connection",
                "_graph_connection", "_graph", "_tts_connection", "_stt_connection", "_identity_face_client",
                "_native_engine_base"}
_BUILDERS = {"AdapterConnection", "engine_egress.async_client", "engine_egress.client", "httpx.AsyncClient", "httpx.Client", "websockets.connect",
             "aiohttp.ClientSession", "urllib.request.urlopen"}

# A function that builds a client or connection and calls none of the guards must be named here, with why.
EXEMPT = {
    "_engine_probe_json_sync": "health read",
    "_get_probe_json": "health read",
    "probe_backend": "health read",
    "_probe_service_resource_fact": "resource fact read",
    "_models_catalog_health_evidence": "health read",
    "_forward_chat_completions": "loopback client to the gateway itself; the inner hop is guarded",
    "_forward_embeddings": "loopback client to the gateway itself; the inner hop is guarded",
    "_mcp_dispatch_call": "loopback client to the gateway itself",
    "_mcp_internal_call": "loopback client to the gateway itself",
    "_operator_stt_transcribe": "loopback client to the gateway itself",
    "_init_cloudflare_ip_ranges": "not an engine",
    "_oidc_exchange_code": "not an engine",
    "_provider_discovery": "not an engine",
    "refresh_openai_model_aliases": "not an engine",
}
# These build a bare httpx client that carries no connection. Every send they make goes through a
# ``_graph(client)`` helper, which is guarded: the test below checks they never call a verb on it.
BARE_CLIENT = {
    "_identity_run_workflow", "proxy_graph_image_request", "proxy_graph_video_list",
    "proxy_graph_video_status", "_proxy_offset_inpaint_tile_request",
}


def _call_name(node: ast.Call) -> str:
    target, parts = node.func, []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _functions() -> list[ast.AST]:
    """The outermost functions: a nested helper belongs to the function that defines it."""
    found: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(child)
            else:
                visit(child)
    visit(ast.parse(_SOURCE))
    return found


def test_every_client_or_connection_factory_in_proxy_is_guarded_or_reviewed() -> None:
    unclassified = []
    for function in _functions():
        calls = [_call_name(node) for node in ast.walk(function) if isinstance(node, ast.Call)]
        if not any(name in _BUILDERS for name in calls):
            continue
        if _GUARD_CALLS.intersection(calls) or function.name in EXEMPT or function.name in BARE_CLIENT:
            continue
        unclassified.append(f"{function.name} (line {function.lineno})")
    assert not unclassified, (
        "These functions build an engine client or connection and call no egress guard. Guard them with "
        f"_require_egress_lease, or list them in EXEMPT or BARE_CLIENT with a reason: {unclassified}")


def test_every_adapter_connection_is_built_by_a_guarded_factory() -> None:
    """The strong form: a bare ``AdapterConnection(...)`` is where an address and timeouts become a send path."""
    for function in _functions():
        calls = [_call_name(node) for node in ast.walk(function) if isinstance(node, ast.Call)]
        if "AdapterConnection" in calls:
            assert "_require_egress_lease" in calls or "require_egress_lease" in calls, (
                f"{function.name} builds an AdapterConnection without the egress guard")


def test_a_bare_client_function_never_sends_through_the_client_itself() -> None:
    for function in _functions():
        if function.name not in BARE_CLIENT:
            continue
        for node in ast.walk(function):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "client"):
                pytest.fail(f"{function.name} calls client.{node.func.attr} at line {node.lineno}: "
                            "send through a guarded _graph(client) helper, or guard this function")


def test_the_exempt_lists_name_functions_that_exist() -> None:
    names = {function.name for function in _functions()}
    assert (set(EXEMPT) | BARE_CLIENT) <= names, "a stale entry in the exempt lists"
