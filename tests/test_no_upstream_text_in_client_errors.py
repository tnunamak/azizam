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
def reply_text(reply):
    return reply.read_text()
async def j(client, url):
    resp = await client.post(url)
    return Response(content=json.dumps({"error": {"message": f"upload failed: {reply_text(resp)[:200]}"}}), status_code=502)
async def k(client, url):
    resp = await client.post(url)
    return Response(content=json.dumps({"error": {"message": "upload failed: " + resp.content.decode()}}), status_code=502)
async def l(client, url):
    resp = await client.post(url)
    return Response(content=json.dumps({"error": {"message": f"upload failed: {resp.status}"}}), status_code=502)
async def m(client, url):
    try:
        await client.post(url)
    except httpx.HTTPError as exc:
        return Response(content=json.dumps({"error": {"message": f"upload failed: {str(exc)}"}}), status_code=502)
'''
    assert taint.scan_source(source, is_client_sink, traceback_is_sink=False, bare_exception=False) == {"a", "b", "d", "f", "g", "j", "k", "m"}


def test_the_production_helpers_that_return_a_response_body_are_known():
    """The review of r9 found a 502 built from ``_graph_reply_text(resp)``: a wrapper hid ``.read_text()`` from the scan."""
    assert taint.helpers_returning_a_body(ast.parse("def wrap(reply):\n    return reply.read_text()\n")) == {"wrap"}
    assert taint.helpers_returning_a_body(ast.parse("def own(path):\n    return 'x'\n")) == set()


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


# ---------------------------------------------------------------------------------------------------------------------
# Responses built from an upstream's status, content type or Retry-After (decision D45, round 12)
#
# ``relays`` above finds ``Response(content=x.content, status_code=x.status_code)``. A relay can also take its body from a
# name (``Response(upstream_content, status_code=upstream_status)``), so these scans look at the other two values an
# upstream chooses: the status it answered with, and the header it wrote. A response whose status is an upstream's passes
# only when its function sends an error status (>= 400) through ``client_errors`` first; the list below says which function
# does. A ``Content-Type`` or ``Retry-After`` an upstream wrote reaches a caller only through ``client_errors``.
# ---------------------------------------------------------------------------------------------------------------------

RESPONSE_CALLS = {"Response", "JSONResponse", "PlainTextResponse", "HTMLResponse", "StreamingResponse"}
UPSTREAM_RECEIVERS = taint.REPLY_NAMES | {"upstream"}

# "file::function": the response takes the upstream's status; why an error status never reaches it that way.
ALLOWED_STATUS_RELAYS: dict[str, str] = {
    "gateway/decision_provider_execution.py::openai": "an upstream status >= 400 returns client_errors.upstream_error_response first",
    "gateway/provider_execution.py::execute_paid_litellm": "an upstream status >= 400 is answered with gateway.providers errors or a fixed body first; the "
                                                          "stream branch takes only 2xx",
    "proxy.py::_proxy_tts_once": "a status outside 2xx returns a fixed TTS error first",
    "proxy.py::_stt_response_from_upstream": "a status >= 400 returns client_errors.upstream_error_response first",
    "proxy.py::_upstream_admission_response": "the body is the gateway's own admission refusal, re-shaped for a protocol; Retry-After goes through client_errors",
    "proxy.py::_forward_current_loaded_openai_request": "a stream is relayed only below 400; an error reply goes through _engine_error_response",
    "proxy.py::_forward_litellm_proxy_request": "a stream is relayed only below 400; an error reply goes through _engine_error_response",
    "proxy.py::_try_protocol_request": "the body is rebuilt from the validated public shape; an engine error raises and goes through _protocol_failure",
    "proxy.py::_forward_internal_litellm_local_upstream": "the internal hop the model router calls with the lifecycle key; the router's own reply to a caller goes "
                                                        "through _engine_error_response",
    "proxy.py::_forward_provider_native": "an upstream status >= 400 returns client_errors.upstream_error_response first",
}

# "file::function": a Content-Type taken from an upstream without ``client_errors.safe_content_type``.
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "proxy.py::_forward_provider_native": "the native lane relays the engine's success headers as they are (an owner-only lane); an error is rebuilt",
}
ALLOWED_RETRY_AFTER: dict[str, str] = {
    "gateway/engine_admin.py::slot": "the gateway's own rate limit, in seconds",
    "gateway/engine_switch.py::__init__": "the gateway's own wait, in seconds",
    "gateway/me_make.py::_error": "an integer the gateway chose (PublicFailure.retry_after)",
    "gateway/resource_admission_http.py::admission_http_response": "an integer the admission decision computed",
    "proxy.py::_swap_deferred_response": "an integer the swap controller computed",
    "proxy.py::_service_availability_response": "an integer from the owner's service override",
}


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_upstream_status(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in {"status_code", "status"} and _root_name(node) in UPSTREAM_RECEIVERS


def _functions(trees):
    for relative, tree in trees:
        for function in ast.walk(tree):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield relative, function


def status_relays_in(trees) -> set[str]:
    """``file::function`` of each response that takes its status from an upstream reply (an attribute of ``resp``,
    ``response``, ``reply``, ``upstream``, or a name assigned from one, or an ``upstream_*`` name)."""
    found: set[str] = set()
    for relative, function in _functions(trees):
        names = {target.id for node in ast.walk(function) if isinstance(node, ast.Assign) and _is_upstream_status(node.value)
                 for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and taint.call_name(node) in RESPONSE_CALLS):
                continue
            status = next((keyword.value for keyword in node.keywords if keyword.arg == "status_code"), None)
            if _is_upstream_status(status) or (isinstance(status, ast.Name) and (status.id in names or status.id.startswith("upstream_"))):
                found.add(f"{relative}::{function.name}")
    return found


def unsafe_content_types_in(trees) -> set[str]:
    """``file::function`` of each response whose ``media_type`` is an upstream's ``content-type`` header as it came."""
    found: set[str] = set()
    for relative, function in _functions(trees):
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and taint.call_name(node) in RESPONSE_CALLS):
                continue
            media = next((keyword.value for keyword in node.keywords if keyword.arg == "media_type"), None)
            text = ast.unparse(media).lower() if media is not None else ""
            if "content-type" in text and "safe_content_type" not in text:
                found.add(f"{relative}::{function.name}")
    return found


def retry_after_copies_in(trees) -> set[str]:
    """``file::function`` of each ``Retry-After`` header set to a value that is not a literal and not the number
    ``client_errors.retry_after`` returned (a name assigned from it)."""
    def header_name(key) -> bool:
        return isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.lower() == "retry-after"
    found: set[str] = set()
    for relative, function in _functions(trees):
        clean = {target.id for node in ast.walk(function) if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                 and taint.chain(node.value.func).endswith("retry_after") for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(function):
            values = []
            if isinstance(node, ast.Dict):
                values = [value for key, value in zip(node.keys, node.values) if key is not None and header_name(key)]
            elif isinstance(node, ast.Assign):
                values = [node.value for target in node.targets if isinstance(target, ast.Subscript) and header_name(target.slice)]
            for value in values:
                if isinstance(value, ast.Constant) or (isinstance(value, ast.Name) and value.id in clean):
                    continue
                # OpenAPI describes a header with a schema object, not a wire value.
                if isinstance(value, ast.Dict) and any(
                    isinstance(key, ast.Constant) and key.value == "schema" for key in value.keys
                ):
                    continue
                found.add(f"{relative}::{function.name}")
    return found


def test_a_response_with_an_upstream_status_is_known_to_send_errors_through_the_helpers():
    new = sorted(status_relays_in(taint.gateway_modules()) - set(ALLOWED_STATUS_RELAYS))
    assert not new, ("a response takes the status of an upstream reply: send a status >= 400 through client_errors "
                     f"(upstream_error_response) before it and list the function in ALLOWED_STATUS_RELAYS: {new}")
    assert not sorted(set(ALLOWED_STATUS_RELAYS) - status_relays_in(taint.gateway_modules()))


def test_a_content_type_an_upstream_wrote_is_cut_to_its_media_type():
    found = unsafe_content_types_in(taint.gateway_modules())
    assert not sorted(found - set(ALLOWED_CONTENT_TYPES)), \
        f"a response takes the Content-Type of an upstream as it came; use client_errors.safe_content_type: {sorted(found - set(ALLOWED_CONTENT_TYPES))}"
    assert not sorted(set(ALLOWED_CONTENT_TYPES) - found)


def test_a_retry_after_is_a_number_the_gateway_wrote():
    found = retry_after_copies_in(taint.gateway_modules())
    assert not sorted(found - set(ALLOWED_RETRY_AFTER)), \
        f"a Retry-After is set from a value that did not go through client_errors.retry_after: {sorted(found - set(ALLOWED_RETRY_AFTER))}"
    assert not sorted(set(ALLOWED_RETRY_AFTER) - found)


def test_the_status_content_type_and_retry_after_scans_find_what_they_are_meant_to_find():
    source = '''
async def relay_by_name(client, url):
    resp = await client.get(url)
    upstream_status = resp.status_code
    return Response(resp_body, status_code=upstream_status, media_type="x")
async def relay_by_attribute(client, url):
    resp = await client.get(url)
    return JSONResponse({}, status_code=resp.status_code)
async def own_status(client, url):
    return Response("x", status_code=502)
async def raw_type(client, url):
    resp = await client.get(url)
    return Response("x", status_code=200, media_type=resp.headers.get("content-type", "application/json"))
async def cut_type(client, url):
    resp = await client.get(url)
    return Response("x", status_code=200, media_type=client_errors.safe_content_type(resp.headers.get("content-type")))
async def raw_retry(error):
    return JSONResponse({}, headers={"retry-after": error.retry_after})
async def number_retry(error):
    seconds = client_errors.retry_after(error.retry_after)
    return JSONResponse({}, headers={"retry-after": seconds})
async def fixed_retry(error):
    out = {}
    out["Retry-After"] = "10"
    return out
def schema_retry():
    return {"headers": {"Retry-After": {"description": "Seconds before retrying.", "schema": {"type": "integer"}}}}
'''
    trees = [("x.py", ast.parse(source))]
    assert status_relays_in(trees) == {"x.py::relay_by_name", "x.py::relay_by_attribute"}
    assert unsafe_content_types_in(trees) == {"x.py::raw_type"}
    assert retry_after_copies_in(trees) == {"x.py::raw_retry"}
