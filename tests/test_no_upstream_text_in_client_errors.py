"""An error response to an API caller is built from the gateway's own words (decision D44).

An upstream (an engine, a provider, a sidecar, an identity provider) echoes what it was sent, so a rejected upload can come
back as "bad key sk-..." and an HTTP client error names the URL it dialed. Relaying that text to the caller hands over a
credential the gateway holds. ``gateway/client_errors.py`` builds every such response from a fixed public reason, the
gateway's error code and the request id; ``upstream_error_response`` passes an engine's error on with its status, machine
code and ``Retry-After`` and none of its words.

This test finds every call that builds a response or an error for a caller (``Response``, ``JSONResponse``,
``HTTPException``, and any call whose name says error, failure, refusal or denial) whose arguments hold text derived from an
upstream: a response body, a subprocess output, or the text of an exception caught around a call to an upstream. It uses
the same checker as ``tests/test_no_upstream_text_in_logs.py`` (``tests/support/upstream_taint.py``).

It also checks the relays by shape: a function that sends a response through ``Response(content=<name>.content/.text/...,
status_code=<name>.status_code)`` without the error helpers is the verbatim relay of an upstream's reply, error statuses
included.
"""

from __future__ import annotations

import ast
import re

from tests.support import upstream_taint as taint

SINK_PATTERN = re.compile(r"error|failure|refus|denied|unavailable_response", re.IGNORECASE)
# Calls that are not builders of an error for a caller although their names say so.
BUILTIN_ERRORS = {"RuntimeError", "ValueError", "KeyError", "TypeError", "OSError", "IOError", "AssertionError", "LookupError",
                  "NotImplementedError", "Exception", "BaseException", "SystemExit", "StopIteration", "TimeoutError"}
NOT_SINKS = BUILTIN_ERRORS | {"describe_error", "client_error", "upstream_error_response", "format_error", "add_error", "_failure_text",
             "registry_error_text", "error_codes", "unavailable_message", "_graph_error_message", "execution_error_message"}

# "file::function": why the text cannot carry a credential an upstream was sent.
ALLOWED: dict[str, str] = {
    "gateway/engine_check.py::_run_check": "the owner's engine check quotes what the server said to the owner; the key is scrubbed from every "
                                           "spelling and an administrator on the owner's stored key sees nothing (guarded_fetch, WITHHELD_TEXT)",
    "gateway/engine_protocol/node_graph.py::NodeGraphAdapter.submit": "the text goes into the error's `excerpt`, which no str, repr or log line shows",
    "gateway/workflow_call.py::error_from": "the reply parsed is the gateway's own route answer, already built by gateway.client_errors",
    "proxy.py::create_response": "`upstream` is the gateway's own /v1/chat/completions answer (an engine's words are already replaced there)",
    "proxy.py::_handle_rerank": "RerankUpstreamError and the build_response ValueError carry the fixed sentences of gateway/rerank.py",
    "proxy.py::_iter_openai_sse_json": "the stream is the gateway's own internal chat stream, whose error events gateway.client_errors rewrites",
}


RESPONSE_NAMES = {"Response", "JSONResponse", "PlainTextResponse", "HTMLResponse", "StreamingResponse"}


def is_client_sink(call: ast.Call, info: taint.ModuleInfo) -> bool:
    """A builder of an error for a caller. A response constructor counts when it says a failing status; the success body of
    a proxied reply is not an error (``relays`` finds the ones that pass an upstream's own status on)."""
    name = taint.call_name(call)
    if name in NOT_SINKS or taint.is_log_call(call, info):
        return False
    if name in RESPONSE_NAMES:
        status = next((keyword.value for keyword in call.keywords if keyword.arg == "status_code"), None)
        return isinstance(status, ast.Constant) and isinstance(status.value, int) and status.value >= 400
    return name == "HTTPException" or bool(SINK_PATTERN.search(name))


def discover() -> list[tuple[str, int, str]]:
    return taint.discover(is_client_sink, traceback_is_sink=False, bare_exception=False)


def relays() -> list[tuple[str, int]]:
    """``Response(content=x.content|x.text|x.raw, status_code=x.status_code|x.status)`` in a function: an upstream's reply
    sent on as it is, with its status."""
    found: list[tuple[str, int]] = []
    for relative, tree in taint.gateway_modules():
        if relative == "gateway/client_errors.py":
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and taint.call_name(node) in {"Response", "StreamingResponse"}):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            status, content = keywords.get("status_code"), keywords.get("content")
            if isinstance(status, ast.Attribute) and status.attr in {"status_code", "status"} \
                    and isinstance(content, ast.Attribute) and content.attr in {"content", "text", "raw", "body"}:
                found.append((relative, node.lineno))
    return found


def test_no_error_response_takes_text_an_upstream_wrote():
    bare = sorted({(key, line, what) for key, line, what in discover() if key not in ALLOWED})
    assert not bare, "error responses built from upstream text (use gateway.client_errors):\n" + "\n".join(
        f"  {key} line {line}: {what}" for key, line, what in bare)


def test_the_allowed_list_holds_no_stale_entry():
    keys = {key for key, _, _ in discover()}
    assert not [key for key in ALLOWED if key not in keys]


def test_no_function_relays_an_upstream_reply_with_its_status():
    found = relays()
    assert not found, f"a response built from an upstream's body and status, which relays its error replies: {found}"


def test_the_checker_finds_what_it_is_meant_to_find():
    source = '''
import httpx
from starlette.responses import Response
async def a(client, url):
    resp = await client.get(url)
    return Response(content=resp.text, status_code=502)
async def b(client, url):
    try:
        await client.get(url)
    except Exception as e:
        return openai_error_response(f"proxy error: {e}", status_code=502)
async def c(client, url):
    try:
        await client.get(url)
    except Exception as e:
        return openai_error_response("proxy error", status_code=502)
async def d(client, url):
    resp = await client.get(url)
    message = resp.json()["error"]["message"]
    return JSONResponse({"error": {"message": f"upload failed: {message}"}}, status_code=502)
async def e(client, url):
    resp = await client.get(url)
    return client_error(502, "upload_failed")
async def f(client, url):
    try:
        await client.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
async def g(client, url):
    try:
        await client.get(url)
    except Exception as exc:
        reason = f"failed: {exc}"
        return _tts_error(reason, 502)
async def h(client, url):
    try:
        await client.get(url)
    except Exception as exc:
        return _tts_error(type(exc).__name__, 502)
async def i(client, url):
    resp = await client.get(url)
    return upstream_error_response(resp.status_code, resp.content, resp.headers)
'''
    assert taint.scan_source(source, is_client_sink, traceback_is_sink=False, bare_exception=False) == {"a", "b", "d", "f", "g"}
