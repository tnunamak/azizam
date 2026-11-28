"""Responses-plan wire format, HTTP transport and model discovery.

Inputs are provider configuration, request values and an injected client. Policy,
connection storage and receipt ownership belong to callers.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from gateway import transport_budgets
from gateway.reasoning_efforts import capped_effort
from gateway.subscription_kinds import Refusal, SubscriptionKind
from gateway.provider_protocols import RequestError, DiscoveryError, ReplyDecoder

if TYPE_CHECKING:
    from gateway.provider_connections import Credential

_CHAT_FIELDS = frozenset({"model", "messages", "stream", "stream_options", "tools", "tool_choice",
                          "parallel_tool_calls", "response_format", "reasoning_effort", "n", "prompt_cache_key"})
_TRANSLATOR = transport_budgets.section("plan_translator")   # registry/transport_budgets.yml, read at import
_STREAM_BUFFER_LIMIT = _TRANSLATOR["stream_buffer_bytes"]
_MAX_ITEMS = _TRANSLATOR["max_items"]
_LEAST_EFFORT = _TRANSLATOR["least_effort"]
_TURN_OPENING_ROLES = frozenset({"user", "developer", "system"})
_HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}")


# ---------------------------------------------------------------- request checks and translation

def _check_responses_request(body: Mapping[str, Any], kind: SubscriptionKind) -> dict[str, Any]:
    """The upstream payload for a Responses request, or a named 400 for a field the plan endpoint rejects."""
    for key in body:
        if key in kind.unsupported_fields:
            raise RequestError("plan_unsupported_parameter", f"{key} is not supported for plan models.", param=key)
    if body.get("store") not in (None, False):
        raise RequestError("plan_store_unsupported", "store must be false for plan models.", param="store")
    items = body.get("input")
    if not isinstance(items, list) or not items:
        raise RequestError("plan_input_required", "Plan models need input as a list of items.", param="input")
    for item in items:
        if isinstance(item, Mapping) and item.get("role") == "system" and item.get("type", "message") == "message":
            raise RequestError("plan_system_message_unsupported",
                                   "Use instructions or a developer message instead of a system message.", param="input")
    return dict(body)


def _check_reasoning_effort(payload: Mapping[str, Any], advertised: list[str]) -> None:
    """An effort the model does not advertise is a named 400, never dropped. A model that advertises none is not checked."""
    reasoning = payload.get("reasoning")
    if reasoning is None:
        return
    effort = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
    if not isinstance(reasoning, Mapping) or (effort is not None and not isinstance(effort, str)):
        raise RequestError("plan_unsupported_reasoning_effort", "reasoning must be an object with a text effort.",
                               param="reasoning")
    if effort and advertised and effort not in advertised:
        raise RequestError(
            "plan_unsupported_reasoning_effort",
            f"This model does not support the reasoning effort {effort!r}. It supports: {', '.join(advertised)}.",
            param="reasoning.effort")


def _raise_least_effort(payload: dict[str, Any], advertised: list[str]) -> bool:
    """A chat caller's "minimal" effort (the Anthropic route sends it when thinking is off) on a model that does not
    advertise it becomes the model's lowest advertised effort. Any other unsupported effort stays a named 400."""
    reasoning = payload.get("reasoning")
    if (advertised and isinstance(reasoning, Mapping) and reasoning.get("effort") == _LEAST_EFFORT
            and _LEAST_EFFORT not in advertised):
        payload["reasoning"] = {**reasoning, "effort": advertised[0]}
        return True
    return False


def _derive_cache_key(kind: SubscriptionKind, namespace: str, payload: Mapping[str, Any]) -> str:
    """A prompt cache key that stays the same for one conversation: it hashes this connection and the conversation's
    start (the instructions and the input items up to the first user message), which later turns repeat unchanged."""
    start: list[Any] = []
    for item in payload.get("input") or []:
        start.append(item)
        if isinstance(item, Mapping) and item.get("role") == "user":
            break
    material = json.dumps([namespace, payload.get("instructions"), start], sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    prefix = str(kind.request.get("cache_key_prefix") or "azizam-plan")
    return f"{prefix}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _with_request_defaults(payload: Mapping[str, Any], kind: SubscriptionKind, namespace: str) -> dict[str, Any]:
    """What every plan request carries, as Codex CLI's request builder sends it: ``include`` holds the kind's entries
    (the client's own stay), and the client's ``prompt_cache_key`` is kept or one is derived."""
    out = dict(payload)
    wanted = [str(item) for item in kind.request.get("include") or ()]
    sent = [item for item in out.get("include") or [] if isinstance(item, str)]
    out["include"] = [*wanted, *[item for item in sent if item not in wanted]]
    if not isinstance(out.get("prompt_cache_key"), str) or not out["prompt_cache_key"]:
        out["prompt_cache_key"] = _derive_cache_key(kind, namespace, out)
    return out


def _text_of(content: Any, *, param: str) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            else:
                raise RequestError("plan_unsupported_content", "Only text is supported here.", param=param)
        return "".join(parts)
    raise RequestError("plan_unsupported_content", "Message content must be text.", param=param)


def _user_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts: list[dict[str, Any]] = []
    for part in content if isinstance(content, list) else []:
        kind = part.get("type") if isinstance(part, Mapping) else None
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append({"type": "input_text", "text": part["text"]})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, Mapping) else image
            if not isinstance(url, str) or not url:
                raise RequestError("plan_unsupported_content", "An image needs an address.", param="messages")
            parts.append({"type": "input_image", "image_url": url})
        else:
            raise RequestError("plan_unsupported_content", f"A {kind or 'this'} content part is not supported.", param="messages")
    if not parts:
        raise RequestError("plan_unsupported_content", "A user message needs content.", param="messages")
    return parts


def _chat_to_responses(body: Mapping[str, Any], kind: SubscriptionKind, slug: str) -> dict[str, Any]:
    """Translate a Chat Completions request to the Responses payload the plan endpoint takes."""
    for key in body:
        if key not in _CHAT_FIELDS:
            raise RequestError("plan_unsupported_parameter", f"{key} is not supported for plan models.", param=key)
    if body.get("n") not in (None, 1):
        raise RequestError("plan_unsupported_parameter", "n must be 1 for plan models.", param="n")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RequestError("plan_input_required", "messages must be a non-empty list.", param="messages")
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise RequestError("plan_input_required", "Each message must be an object.", param="messages")
        role = message.get("role")
        if role in ("system", "developer"):
            instructions.append(_text_of(message.get("content"), param="messages"))
        elif role == "user":
            items.append({"type": "message", "role": "user", "content": _user_parts(message.get("content"))})
        elif role == "assistant":
            text = _text_of(message.get("content"), param="messages")
            if text:
                items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, Mapping) else None
                if not isinstance(function, Mapping) or not isinstance(call.get("id"), str):
                    raise RequestError("plan_input_required", "A tool call needs an id and a function.", param="messages")
                items.append({"type": "function_call", "call_id": call["id"], "name": function.get("name"),
                              "arguments": function.get("arguments") or "{}"})
        elif role == "tool":
            content = message.get("content")
            has_image = isinstance(content, list) and any(
                isinstance(part, Mapping) and part.get("type") == "image_url" for part in content)
            # A function_call_output may carry input_text and input_image items, as Codex sends them.
            output = _user_parts(content) if has_image else _text_of(content, param="messages")
            items.append({"type": "function_call_output", "call_id": message.get("tool_call_id"), "output": output})
        else:
            raise RequestError("plan_input_required", f"The role {role!r} is not supported.", param="messages")
    if not items:
        raise RequestError("plan_input_required", "messages need at least one user, assistant or tool message.", param="messages")
    payload: dict[str, Any] = {"model": slug, "input": items, "store": False, "stream": True}
    if instructions:
        payload["instructions"] = "\n\n".join(text for text in instructions if text)
    tools = body.get("tools")
    if tools:
        payload["tools"] = [_chat_tool(tool) for tool in tools]
    choice = body.get("tool_choice")
    if isinstance(choice, Mapping):
        function = choice.get("function")
        payload["tool_choice"] = {"type": "function", "name": function.get("name")} if isinstance(function, Mapping) else choice
    elif choice is not None:
        payload["tool_choice"] = choice
    if "parallel_tool_calls" in body:
        payload["parallel_tool_calls"] = body["parallel_tool_calls"]
    if body.get("reasoning_effort"):
        payload["reasoning"] = {"effort": body["reasoning_effort"]}
    if isinstance(body.get("prompt_cache_key"), str) and body["prompt_cache_key"]:
        payload["prompt_cache_key"] = body["prompt_cache_key"]
    fmt = body.get("response_format")
    if isinstance(fmt, Mapping) and fmt.get("type") == "json_object":
        payload["text"] = {"format": {"type": "json_object"}}
    elif isinstance(fmt, Mapping) and fmt.get("type") == "json_schema":
        schema = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), Mapping) else {}
        payload["text"] = {"format": {"type": "json_schema", "name": schema.get("name") or "response",
                                      "schema": schema.get("schema") or {}, "strict": bool(schema.get("strict"))}}
    for key in kind.unsupported_fields & set(payload):
        raise RequestError("plan_unsupported_parameter", f"{key} is not supported for plan models.", param=key)
    return payload


def _chat_tool(tool: Any) -> dict[str, Any]:
    function = tool.get("function") if isinstance(tool, Mapping) else None
    if not isinstance(tool, Mapping) or tool.get("type") != "function" or not isinstance(function, Mapping):
        raise RequestError("plan_unsupported_parameter", "Only function tools are supported for plan models.", param="tools")
    out = {"type": "function", "name": function.get("name"), "parameters": function.get("parameters") or {"type": "object"}}
    if function.get("description"):
        out["description"] = function["description"]
    if "strict" in function:
        out["strict"] = function["strict"]
    return out


# --------------------------------------------------------------------------- events

class _SseEvents:
    """Split an SSE byte stream into (type, object) events, bounded in memory."""

    def __init__(self) -> None:
        self._pending = b""

    def feed(self, chunk: bytes) -> list[tuple[str, dict[str, Any]]]:
        self._pending += chunk.replace(b"\r\n", b"\n")
        if len(self._pending) > _STREAM_BUFFER_LIMIT:
            self._pending = b""
            return [("error", {"error": {"code": "plan_stream_too_large"}})]
        events: list[tuple[str, dict[str, Any]]] = []
        while b"\n\n" in self._pending:
            block, self._pending = self._pending.split(b"\n\n", 1)
            data = b"".join(line[5:].strip() for line in block.split(b"\n") if line.startswith(b"data:"))
            if not data or data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except (ValueError, UnicodeError):
                continue
            if isinstance(obj, dict):
                events.append((str(obj.get("type") or ""), obj))
        return events


class _Outcome:
    """What the upstream said happened: its terminal event, usage and (on failure) error code."""

    def __init__(self) -> None:
        self.terminal: str | None = None
        self.response: dict[str, Any] | None = None
        self.usage: dict[str, Any] | None = None
        self.error_code: str | None = None
        self.items: list[dict[str, Any]] = []

    def final_response(self) -> dict[str, Any]:
        """The terminal response; its output is the items finished along the way when the event left it empty."""
        response = dict(self.response or {})
        if not response.get("output") and self.items:
            response["output"] = list(self.items)
        return response

    def observe(self, event_type: str, event: Mapping[str, Any]) -> None:
        if event_type in ("response.completed", "response.failed", "response.incomplete"):
            self.terminal = event_type
            response = event.get("response")
            self.response = dict(response) if isinstance(response, Mapping) else {}
            usage = self.response.get("usage")
            self.usage = dict(usage) if isinstance(usage, Mapping) else None
            error = self.response.get("error")
            if isinstance(error, Mapping):
                self.error_code = error.get("code") if isinstance(error.get("code"), str) else None
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, Mapping) and len(self.items) < _MAX_ITEMS:
                self.items.append(dict(item))
        elif event_type == "error":
            self.terminal = "error"
            error = event.get("error") if isinstance(event.get("error"), Mapping) else event
            self.error_code = error.get("code") if isinstance(error.get("code"), str) else None


def _code_of_reply(body: Any) -> str | None:
    """The upstream code of a failed reply, which may use ``error`` or ``detail`` (the docs allow either)."""
    if not isinstance(body, Mapping):
        return None
    for key in ("error", "detail"):
        value = body.get(key)
        if isinstance(value, Mapping) and isinstance(value.get("code"), str):
            return value["code"]
        if isinstance(value, str):
            return value
    return body.get("code") if isinstance(body.get("code"), str) else None


# ---------------------------------------------------------------- responses -> chat

def _chat_id() -> str:
    return "chatcmpl-" + secrets.token_hex(12)


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _details(usage: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = usage.get(key)
    return value if isinstance(value, Mapping) else {}


def usage_facts(usage: Mapping[str, Any]) -> dict[str, int]:
    """The upstream's own counts for a receipt: input, cached input, output, reasoning and cache-write tokens.

    A count the upstream did not report, or reported as something other than a whole number, is absent, never 0.
    Input includes cached reads and cache writes; output includes reasoning."""
    inputs, outputs = _details(usage, "input_tokens_details"), _details(usage, "output_tokens_details")
    reported = (("input_tokens", usage.get("input_tokens")), ("cached_input_tokens", inputs.get("cached_tokens")),
                ("output_tokens", usage.get("output_tokens")), ("reasoning_tokens", outputs.get("reasoning_tokens")),
                ("cache_write_tokens", inputs.get("cache_creation_tokens")))
    return {name: value for name, value in reported if type(value) is int and value >= 0}


def _chat_usage(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Responses usage as Chat Completions usage, counts unchanged."""
    if not usage:
        return None
    inputs, outputs = _details(usage, "input_tokens_details"), _details(usage, "output_tokens_details")
    prompt, completion = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    prompt_details = {"cached_tokens": _count(inputs.get("cached_tokens"))}
    if "cache_creation_tokens" in inputs:  # the Claude plan names its cache writes; the ChatGPT plan does not
        prompt_details["cache_creation_tokens"] = _count(inputs["cache_creation_tokens"])
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": _count(usage.get("total_tokens")) or prompt + completion,
            "prompt_tokens_details": prompt_details,
            "completion_tokens_details": {"reasoning_tokens": _count(outputs.get("reasoning_tokens"))}}


def _responses_to_chat(response: Mapping[str, Any], *, model: str, created: int | None = None) -> dict[str, Any]:
    """A completed Response as a Chat Completion."""
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, Mapping) and part.get("type") == "output_text":
                    text.append(str(part.get("text") or ""))
        elif item.get("type") == "function_call":
            calls.append({"id": item.get("call_id") or item.get("id"), "type": "function",
                          "function": {"name": item.get("name"), "arguments": item.get("arguments") or "{}"}})
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) if text or not calls else None}
    if calls:
        message["tool_calls"] = calls
    incomplete = response.get("status") == "incomplete"
    finish = "tool_calls" if calls else "length" if incomplete else "stop"
    body: dict[str, Any] = {"id": _chat_id(), "object": "chat.completion", "created": created or int(time.time()),
                            "model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    usage = _chat_usage(response.get("usage"))
    if usage:
        body["usage"] = usage
    return body


class _ChatStream:
    """Turn Responses events into Chat Completion chunks."""

    def __init__(self, *, model: str, include_usage: bool) -> None:
        self.id, self.model, self.created = _chat_id(), model, int(time.time())
        self._include_usage = include_usage
        self._started = False
        self._tools: dict[str, int] = {}

    def _chunk(self, delta: Mapping[str, Any], finish: str | None = None, **extra: Any) -> bytes:
        body = {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish}], **extra}
        return b"data: " + json.dumps(body, separators=(",", ":")).encode() + b"\n\n"

    def _open(self) -> list[bytes]:
        if self._started:
            return []
        self._started = True
        return [self._chunk({"role": "assistant", "content": ""})]

    def translate(self, event_type: str, event: Mapping[str, Any]) -> list[bytes]:
        out: list[bytes] = []
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            out += self._open() + [self._chunk({"content": event["delta"]})]
        elif event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "function_call":
                index = len(self._tools)
                self._tools[str(item.get("id") or event.get("output_index"))] = index
                out += self._open() + [self._chunk({"tool_calls": [{
                    "index": index, "id": item.get("call_id") or item.get("id"), "type": "function",
                    "function": {"name": item.get("name"), "arguments": ""}}]})]
        elif event_type == "response.function_call_arguments.delta":
            index = self._tools.get(str(event.get("item_id")))
            if index is not None and isinstance(event.get("delta"), str):
                out.append(self._chunk({"tool_calls": [{"index": index, "function": {"arguments": event["delta"]}}]}))
        elif event_type in ("response.completed", "response.incomplete"):
            response = event.get("response") if isinstance(event.get("response"), Mapping) else {}
            finish = "tool_calls" if self._tools else "length" if event_type == "response.incomplete" else "stop"
            out += self._open() + [self._chunk({}, finish)]
            usage = _chat_usage(response.get("usage"))
            if self._include_usage and usage:
                out.append(b"data: " + json.dumps({"id": self.id, "object": "chat.completion.chunk",
                                                    "created": self.created, "model": self.model, "choices": [],
                                                    "usage": usage}, separators=(",", ":")).encode() + b"\n\n")
        return out


def _stream_error_event(refusal: Refusal) -> bytes:
    return b"data: " + json.dumps({"error": {"code": refusal.code, "message": refusal.message, "type": "plan_refusal"}},
                                  separators=(",", ":")).encode() + b"\n\n"


async def discover_models(kind: SubscriptionKind, credential: Credential, client_context) -> dict[str, list[str]]:
    upstream = kind.upstream
    try:
        async with client_context() as client:
            response = await client.get(str(upstream["base_url"]).rstrip("/") + str(upstream["models_path"]),
                                        headers={"Authorization": f"Bearer {credential.value}"})
        body = response.json()
    except (httpx.HTTPError, ValueError):
        raise DiscoveryError(502, "models_unavailable", "The model list could not be read. Try again shortly.") from None
    if response.status_code != 200 or not isinstance(body, Mapping):
        raise DiscoveryError(502, "models_unavailable", "The model list could not be read. Try again shortly.")
    rows = body.get(str(upstream["models_items_path"])) or body.get("data") or []
    slugs: dict[str, list[str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        visible = row.get(str(upstream["models_visibility_path"]), upstream["models_visible_value"])
        slug = row.get(str(upstream["models_id_path"]))
        if visible == upstream["models_visible_value"] and isinstance(slug, str) and slug and slug not in slugs:
            slugs[slug] = _efforts_of(upstream, row)
    return slugs


def _efforts_of(upstream: Mapping[str, Any], row: Mapping[str, Any]) -> list[str]:
    levels = row.get(str(upstream.get("models_efforts_path") or ""))
    key = str(upstream.get("models_effort_key") or "effort")
    out: list[str] = []
    for level in levels if isinstance(levels, list) else []:
        value = level.get(key) if isinstance(level, Mapping) else level
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


def validate(config: Mapping[str, Any], *, source: Any) -> None:
    upstream = config.get("upstream")
    keys = ("base_url", "models_path", "models_items_path", "models_id_path", "models_visibility_path",
            "models_visible_value", "responses_path")
    if not isinstance(upstream, Mapping) or not all(upstream.get(key) for key in keys):
        raise ValueError(f"{source}: upstream needs {', '.join(keys)}")
    if urlsplit(str(upstream["base_url"])).scheme != "https":
        raise ValueError(f"{source}: upstream.base_url must be an https address")
    request = config.get("request")
    header = request.get("turn_state_header") if isinstance(request, Mapping) else None
    if header is not None and not (isinstance(header, str) and _HEADER_NAME.fullmatch(header)):
        raise ValueError(f"{source}: request.turn_state_header must be an HTTP header name")


def _cache_affinity_headers(kind: SubscriptionKind, payload: Mapping[str, Any]) -> dict[str, str]:
    """The kind's cache-affinity headers, each set to the request's prompt cache key (none when the key is absent)."""
    key = payload.get("prompt_cache_key")
    if not isinstance(key, str) or not key:
        return {}
    return {str(name): key for name in kind.request.get("cache_affinity_headers") or ()}


def turn_state_header(kind: SubscriptionKind) -> str | None:
    """The header that carries the kind's turn state, both ways; none when the kind's row names none."""
    name = kind.request.get("turn_state_header")
    return name if isinstance(name, str) and name else None


def _is_tool_result(item: Any) -> bool:
    # function_call_output, custom_tool_call_output, computer_call_output and the other *_output items a client returns.
    return isinstance(item, Mapping) and str(item.get("type") or "").endswith("_output")


def continues_turn(items: list[Any], start: int) -> bool:
    """Whether a request continues the turn of the conversation's previous request: its last input item is a tool result,
    and no item added since that request (from ``start``) is a user, developer or system message, which begins a turn.
    A chat tool message arrives here as a function_call_output item."""
    if not items or not _is_tool_result(items[-1]):
        return False
    return not any(isinstance(item, Mapping) and item.get("role") in _TURN_OPENING_ROLES for item in items[start:])


async def send(kind: SubscriptionKind, payload: Mapping[str, Any], credential: Credential, new_client,
               extra_headers: Mapping[str, str] | None = None):
    """Send once. The caller owns retry policy and closes a successful transport."""
    url = str(kind.upstream["base_url"]).rstrip("/") + str(kind.upstream["responses_path"])
    client = new_client(kind)
    try:
        request = client.build_request("POST", url, json=payload, headers={
            **_cache_affinity_headers(kind, payload), **(extra_headers or {}),
            "Authorization": f"Bearer {credential.value}", "Accept": "text/event-stream"})
        return client, await client.send(request, stream=True)
    except BaseException:
        await client.aclose()
        raise


def classify_error(kind: SubscriptionKind, status: int, raw: bytes) -> Refusal:
    try:
        reply = json.loads(raw)
    except (ValueError, UnicodeError):
        reply = None
    return kind.refusal_for(status, _code_of_reply(reply))


def check_request(surface: str, body: Mapping[str, Any], kind: SubscriptionKind, slug: str) -> dict[str, Any]:
    return _check_responses_request(body, kind) if surface == "responses" else _chat_to_responses(body, kind, slug)


def apply_model_facts(surface: str, payload: dict[str, Any], advertised: list[str],
                      body: Mapping[str, Any], *, top_effort: str | None = None) -> Mapping[str, Any]:
    metadata: dict[str, Any] = {}
    reasoning = payload.get("reasoning")
    requested = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
    lowered = capped_effort(requested, advertised, top_effort)
    if lowered is not None:
        payload["reasoning"] = {**reasoning, "effort": lowered}
        metadata["plan_effort_lowered_from"] = requested
    if surface == "chat" and _raise_least_effort(payload, advertised):
        metadata["plan_effort_raised_from"] = _LEAST_EFFORT
    _check_reasoning_effort(payload, advertised)
    return metadata


def prepare(payload: Mapping[str, Any], kind: SubscriptionKind, namespace: str, slug: str, *,
            caller: str = "") -> dict[str, Any]:
    """The upstream payload. The cache key is the connection's, not the caller's (``caller`` is not used here)."""
    return _with_request_defaults({**payload, "model": slug, "store": False, "stream": True}, kind, namespace)


class _Reply:
    def __init__(self, surface: str, model: str, body: Mapping[str, Any]) -> None:
        self._surface, self._model = surface, model
        self._outcome, self._splitter = _Outcome(), _SseEvents()
        self.committed = False
        self.event_seen = False
        options = body.get("stream_options")
        include_usage = bool(options.get("include_usage")) if isinstance(options, Mapping) else False
        self.translated = surface == "chat"
        self._translator = _ChatStream(model=model, include_usage=include_usage) if self.translated and body.get("stream") else None
        self.done = b"data: [DONE]\n\n" if self.translated else b""

    @property
    def usage(self):
        return self._outcome.usage

    @property
    def terminal(self) -> bool:
        return self._outcome.terminal is not None

    @property
    def successful(self) -> bool:
        return self._outcome.terminal in ("response.completed", "response.incomplete")

    @property
    def has_response(self) -> bool:
        return self._outcome.response is not None

    def feed(self, chunk: bytes) -> list[bytes]:
        events = self._splitter.feed(chunk)
        for event_type, event in events:
            self.event_seen = True
            if event_type not in {"response.failed", "error"}:
                self.committed = True
            self._outcome.observe(event_type, event)
        if self._translator is None:
            return [chunk]
        return [piece for event_type, event in events for piece in self._translator.translate(event_type, event)]

    def quota_rejected(self, kind: SubscriptionKind) -> bool:
        return not self.committed and _mapped_quota(kind, self._outcome.error_code)

    def failure(self, kind: SubscriptionKind, *, streaming: bool) -> Refusal:
        if streaming and not self.terminal:
            return Refusal(502, "plan_stream_interrupted", "The plan service ended the stream early.")
        return kind.refusal_for_failed(self._outcome.error_code)

    def error_event(self, refusal: Refusal) -> bytes:
        return _stream_error_event(refusal)

    def result(self) -> Mapping[str, Any]:
        response = self._outcome.final_response()
        return response if self._surface == "responses" else _responses_to_chat(response, model=self._model)


def open_reply(surface: str, model: str, body: Mapping[str, Any]) -> ReplyDecoder:
    return _Reply(surface, model, body)


def _mapped_quota(kind: SubscriptionKind, code: str | None) -> bool:
    # A status-only 429 never establishes that execution did not begin.
    return bool(code and ((code in kind.errors and kind.errors[code].status == 429)
                          or kind.failed_codes.get(code) == 429))


def quota_rejection(kind: SubscriptionKind, status: int, raw: bytes) -> bool:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return False
    return status >= 400 and _mapped_quota(kind, _code_of_reply(value))
