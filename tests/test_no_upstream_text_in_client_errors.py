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


# ---------------------------------------------------------------------------------------------------------------------
# Relays inside translators and stream rewriters (decision D45)
#
# A translator that builds a dict, an event or a message from the fields of an upstream's ``error`` object relays the
# upstream's words even when no response is built from a body (the Responses translator copied ``error.message``,
# ``error.code`` and ``error.param`` of a chat chunk into its SSE error event and its stored final response). The scan below
# lists every read of ``message``, ``code``, ``type``, ``param``, ``detail`` or ``reason`` from a value taken from an
# ``error`` key, in every production module that handles replies (``gateway/**``, ``proxy.py``, ``responses_translator.py``).
# A read passes only when listed here with the reason it does not reach a caller; a value that went through
# ``client_errors`` is clean.
# ---------------------------------------------------------------------------------------------------------------------

ERROR_TEXT_FIELDS = {"message", "code", "type", "param", "detail", "reason", "msg", "error_description"}

ALLOWED_READS: dict[str, str] = {
    "gateway/client_errors.py::rewrite_stream_errors": "the helper itself: it reads the fields it replaces",
    "gateway/decisions_api.py::_systemone_body_for_client": "the local decision service is dialed with no credential (decision_routing posts no headers but a deadline), so it has none to echo",
    "gateway/mcp_server.py::_normalize_job": "the payload is the gateway's own media job answer, whose error sentence is fixed copy (proxy._media_job_failure)",
    "gateway/mcp_server.py::register_mcp_routes": "the code of the gateway's own JSON-RPC reply",
    "gateway/mcp_server.py::dispatch": "the code of the gateway's own JSON-RPC reply",
    "gateway/resource_admission_http.py::admission_protocol_body": "the body is the gateway's own projected admission refusal",
    "gateway/workflow_call.py::error_from": "the reply is the gateway's own route answer, built by gateway.client_errors",
    "proxy.py::_speech_fallback_reason": "reads the code to decide a fallback; nothing is sent on",
    "proxy.py::_stt_resource_limit_response": "the body is the gateway's own refusal; the message is replaced by the gateway's projected reason",
    "proxy.py::_generate_images_with_adaptive_batching": "the text is lowercased to decide whether to retry; it is not sent on",
    "proxy.py::_resolve_gateway_media_url": "reads the gateway's own signed-asset response",
    "proxy.py::_admission_refusal_message": "reads the gateway's own admission refusal",
    "proxy.py::_identity_run_workflow": "reads the gateway's own graph error response (built by gateway.client_errors)",
    "proxy.py::_image_response_error": "reads the gateway's own image response, built by gateway.client_errors",
    "proxy.py::_media_job_failure": "the code is kept only from a fixed list (_JOB_KNOWN_ERROR_CODES); the sentence is fixed copy",
    "proxy.py::_drain_streaming_upstream_error": "compares the code to find the gateway's own admission refusal; the message goes through client_errors",
    "proxy.py::_upstream_admission_response": "compares the code to find the gateway's own admission refusal",
    "proxy.py::_response_error_message": "reads the gateway's own policy refusals (an engine's reply goes through _upstream_reply_message)",
    "proxy.py::_normalize_litellm_error_envelope": "fills a missing type; the envelope then goes through client_errors.upstream_error_response",
}

_CLEAN_CALLS = ("client_errors.",)


def _key_read(node: ast.AST) -> tuple[str, ast.AST] | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args \
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value, node.func.value
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value, node.value
    return None


def _reads_error_key(node: ast.AST) -> bool:
    return any((read := _key_read(inner)) is not None and read[0] == "error" for inner in ast.walk(node))


def _from_clean_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and taint.chain(node.func).startswith(_CLEAN_CALLS)


def error_field_reads(trees: list[tuple[str, ast.Module]]) -> set[str]:
    """``file::function`` of every function that reads an error text field from a value taken from an ``error`` key."""
    found: set[str] = set()
    for relative, tree in trees:
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            derived: set[str] = set()
            clean: set[str] = set()
            for node in ast.walk(function):
                if isinstance(node, ast.Assign):
                    names = {target.id for target in node.targets if isinstance(target, ast.Name)}
                    if _from_clean_call(node.value):
                        clean |= names
                    elif _reads_error_key(node.value):
                        derived |= names
            for node in ast.walk(function):
                read = _key_read(node)
                if read is None or read[0] not in ERROR_TEXT_FIELDS:
                    continue
                receiver = read[1]
                if isinstance(receiver, ast.Name) and receiver.id in clean:
                    continue
                if _reads_error_key(receiver) or (isinstance(receiver, ast.Name) and receiver.id in derived):
                    found.add(f"{relative}::{function.name}")
    return found


def reply_handling_modules() -> list[tuple[str, ast.Module]]:
    modules = list(taint.gateway_modules())
    translator = taint.ROOT / "responses_translator.py"
    modules.append((translator.name, ast.parse(translator.read_text(encoding="utf-8"))))
    return modules


def test_no_translator_or_rewriter_builds_text_from_an_upstream_error_object():
    found = error_field_reads(reply_handling_modules())
    new = sorted(found - set(ALLOWED_READS))
    assert not new, ("a function reads an error text field (message, code, type, param, detail, reason) of an upstream's error "
                     "object: pass it through gateway.client_errors.error_fields / stream_error, or list it in ALLOWED_READS "
                     f"with the reason it does not reach a caller: {new}")


def test_the_allowed_reads_hold_no_stale_entry():
    stale = sorted(set(ALLOWED_READS) - error_field_reads(reply_handling_modules()))
    assert not stale, f"ALLOWED_READS lists a function that no longer reads an error field: {stale}"


def test_the_read_scan_finds_the_translator_relay_it_is_meant_to_find():
    relay = '''
def handle_chunk(chunk, fail):
    if chunk.get("error") is not None:
        error = chunk["error"]
        return fail(str(error.get("message") or "failed"), code=str(error.get("code")), param=error.get("param"))
def inline(payload):
    return {"message": payload["error"]["message"]}
def through_helper(chunk):
    fields = client_errors.stream_error(chunk["error"])
    return fields["message"]
def unrelated(payload):
    return payload.get("message")
'''
    assert error_field_reads([("x.py", ast.parse(relay))]) == {"x.py::handle_chunk", "x.py::inline"}
