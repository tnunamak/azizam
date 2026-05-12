"""Translation layer between OpenAI Responses API and Chat Completions API.

The proxy's downstream backends (llama-server, TabbyAPI) speak Chat
Completions. We expose the newer Responses API by translating at the proxy
boundary.

Mapping summary (Responses -> Chat Completions, on request):
  input (str | list[item])      -> messages (list of role/content dicts)
  instructions (str)            -> prepended `system` message
  previous_response_id          -> walked through responses_store and the
                                   prior conversation is reconstructed as
                                   leading messages
  tools[type=function]          -> tools[type=function]  (shape differs)
  tools[type=hosted]            -> 400 not_supported_error
  tool_choice                   -> tool_choice (compatible shapes pass through)
  parallel_tool_calls           -> parallel_tool_calls
  text.format.type=text         -> (drop)
  text.format.type=json_object  -> response_format={"type": "json_object"}
  text.format.type=json_schema  -> response_format={"type": "json_schema", ...}
  max_output_tokens             -> max_tokens
  temperature/top_p/metadata    -> pass through
  reasoning.effort=minimal      -> append "/no_think" to system prompt
  reasoning.effort=low|med|high -> no-op
  stream                        -> stream (event format differs; see translate_stream)

Mapping back (Chat Completions -> Responses, on response):
  choices[0].message.content    -> output: [ {type: message, role: assistant,
                                              content: [{type: output_text, text: ...}]} ]
  choices[0].message.tool_calls -> output: [ {type: function_call, ...} ]
  usage                         -> usage (input_tokens / output_tokens / total_tokens)
  finish_reason=length          -> status=incomplete, incomplete_details

Notes:
- We do NOT implement server-side tool LOOP. If the model emits a tool call,
  the client receives it and is expected to chain via previous_response_id +
  function_call_output input items.
- Hosted tools (web_search, file_search, code_interpreter, computer_use,
  computer_use_preview, image_generation, mcp, file_search_preview,
  web_search_preview, web_search_2025_*, code_interpreter_preview, shell,
  apply_patch) are rejected.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Optional


HOSTED_TOOL_TYPES = {
    "web_search",
    "web_search_preview",
    "web_search_2025_06_24",
    "web_search_2025_08_25",
    "file_search",
    "file_search_preview",
    "code_interpreter",
    "code_interpreter_preview",
    "computer_use",
    "computer_use_preview",
    "image_generation",
    "mcp",
    "shell",
    "local_shell",
    "apply_patch",
}


class ResponsesTranslationError(Exception):
    """400-class error raised during translation."""

    def __init__(self, message: str, code: str = "invalid_request_error", param: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.param = param


def new_response_id() -> str:
    """OpenAI uses 'resp_' + hex. Mimic the prefix; randomness is fine."""
    return "resp_" + secrets.token_hex(24)


def _coerce_content_to_text(content: Any) -> str:
    """Flatten a Responses content array (list of parts) or a string into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                # Responses content part types: input_text, output_text, input_image,
                # input_file, refusal. We extract text from text-bearing types and
                # drop the rest (vision support is on the downstream backend's roadmap,
                # not the translator's).
                t = c.get("type")
                if t in ("input_text", "output_text", "text"):
                    txt = c.get("text", "")
                    if isinstance(txt, str):
                        parts.append(txt)
                elif t == "refusal":
                    refusal = c.get("refusal", "")
                    if isinstance(refusal, str):
                        parts.append(refusal)
        return "".join(parts)
    if isinstance(content, dict):
        # Single-part dict.
        return _coerce_content_to_text([content])
    return str(content)


def _input_item_to_messages(item: dict) -> list[dict]:
    """Map one Responses input item to one or more Chat Completions messages.

    Responses input items include:
      - message (role + content)
      - function_call (assistant tool call)
      - function_call_output (tool result, role=tool)
      - reasoning, refusal, image_generation_call, etc. -> dropped
    """
    if not isinstance(item, dict):
        return []
    itype = item.get("type")
    # "message" is the common case. Easy-input messages without an explicit
    # type still have role + content and should be treated as messages.
    if itype == "message" or (itype is None and "role" in item):
        role = item.get("role") or "user"
        # Chat Completions uses "system" not "developer". Map developer->system.
        if role == "developer":
            role = "system"
        text = _coerce_content_to_text(item.get("content"))
        return [{"role": role, "content": text}]
    if itype == "function_call":
        # Responses assistant tool call -> Chat assistant message with tool_calls.
        call_id = item.get("call_id") or item.get("id") or f"call_{secrets.token_hex(8)}"
        return [{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "") or "",
                },
            }],
        }]
    if itype == "function_call_output":
        call_id = item.get("call_id") or item.get("id") or ""
        output = item.get("output", "")
        if not isinstance(output, str):
            try:
                output = json.dumps(output)
            except Exception:
                output = str(output)
        return [{
            "role": "tool",
            "tool_call_id": call_id,
            "content": output,
        }]
    # Reasoning / refusal / hosted-tool calls / hosted-tool outputs are not
    # useful when replaying through Chat Completions. Skip them.
    return []


def _input_to_messages(input_value: Any) -> list[dict]:
    """Top-level Responses `input` -> list of Chat Completions messages."""
    if input_value is None:
        return []
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if isinstance(input_value, list):
        messages: list[dict] = []
        for item in input_value:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            else:
                messages.extend(_input_item_to_messages(item))
        return messages
    raise ResponsesTranslationError(
        "Field 'input' must be a string or array of input items.",
        param="input",
    )


def _translate_tools(tools: Any) -> list[dict]:
    """Translate Responses `tools` -> Chat Completions `tools`.

    Hosted tools are rejected. Function tools map cleanly.
    """
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ResponsesTranslationError(
            "Field 'tools' must be an array.",
            param="tools",
        )
    out: list[dict] = []
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ResponsesTranslationError(
                f"tools[{i}] must be an object.",
                param=f"tools[{i}]",
            )
        ttype = tool.get("type")
        if ttype in HOSTED_TOOL_TYPES:
            raise ResponsesTranslationError(
                f"Hosted tool '{ttype}' is not supported by this proxy. "
                f"Only `type: 'function'` tools are accepted; the proxy backs "
                f"onto a local llama-server which has no first-party search, "
                f"file search, code interpreter, or computer-use surface.",
                code="hosted_tool_not_supported",
                param=f"tools[{i}].type",
            )
        if ttype == "function":
            # Responses shape (flat):
            #   {type, name, description, parameters, strict}
            # Chat Completions shape (nested):
            #   {type: function, function: {name, description, parameters, strict}}
            name = tool.get("name")
            if not name:
                # Some clients nest function metadata under "function" already.
                inner = tool.get("function") if isinstance(tool.get("function"), dict) else None
                if inner:
                    name = inner.get("name")
                    out.append({"type": "function", "function": inner})
                    continue
                raise ResponsesTranslationError(
                    f"tools[{i}].name is required for function tools.",
                    param=f"tools[{i}].name",
                )
            fn: dict = {"name": name}
            if "description" in tool:
                fn["description"] = tool["description"]
            if "parameters" in tool:
                fn["parameters"] = tool["parameters"]
            if "strict" in tool:
                fn["strict"] = tool["strict"]
            out.append({"type": "function", "function": fn})
            continue
        if ttype == "custom":
            # Custom (non-JSON-schema) tools — pass through name only.
            name = tool.get("name", "")
            out.append({"type": "function", "function": {"name": name, "description": tool.get("description", "")}})
            continue
        raise ResponsesTranslationError(
            f"Unknown tool type '{ttype}' at tools[{i}].",
            param=f"tools[{i}].type",
        )
    return out


def _translate_tool_choice(tc: Any) -> Any:
    """Translate Responses tool_choice -> Chat Completions tool_choice.

    Most shapes are identical: "auto", "none", "required", or
    {type: function, name: "..."} (Responses) / {type: function, function: {name: "..."}} (Chat).
    """
    if tc is None:
        return None
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "function":
            name = tc.get("name") or (tc.get("function") or {}).get("name")
            if not name:
                return "auto"
            return {"type": "function", "function": {"name": name}}
        if t in ("allowed_tools",):
            # Best-effort: collapse to "auto".
            return "auto"
        return "auto"
    return "auto"


def _translate_text_format(text_cfg: Any) -> Optional[dict]:
    """Translate Responses `text.format` -> Chat Completions `response_format`."""
    if not isinstance(text_cfg, dict):
        return None
    fmt = text_cfg.get("format")
    if not isinstance(fmt, dict):
        return None
    ftype = fmt.get("type")
    if ftype == "text":
        return None
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "json_schema":
        # Responses shape: {type: json_schema, name, schema, strict, description}
        # Chat Completions shape: {type: json_schema, json_schema: {name, schema, strict, description}}
        js: dict = {}
        for k in ("name", "schema", "strict", "description"):
            if k in fmt:
                js[k] = fmt[k]
        if "name" not in js:
            js["name"] = "response"
        return {"type": "json_schema", "json_schema": js}
    return None


def _apply_reasoning_effort_messages(messages: list[dict], reasoning: Any) -> list[dict]:
    """Honor reasoning.effort=minimal by appending /no_think to the system
    prompt. Llama-server's Qwen3 chat template additionally honors a
    `chat_template_kwargs.enable_thinking=false` flag (set in
    `_apply_reasoning_effort_kwargs`); we set the system-prompt hint as a
    belt-and-suspenders fallback for backends that ignore the kwarg."""
    if not isinstance(reasoning, dict):
        return messages
    effort = reasoning.get("effort")
    if effort != "minimal":
        return messages
    hint = "/no_think"
    out = list(messages)
    for i, m in enumerate(out):
        if m.get("role") == "system":
            existing = m.get("content") or ""
            if hint not in existing:
                out[i] = {**m, "content": (existing + ("\n" if existing else "") + hint)}
            return out
    out.insert(0, {"role": "system", "content": hint})
    return out


def _apply_reasoning_effort_kwargs(cc: dict, reasoning: Any) -> None:
    """If reasoning.effort=minimal, request that the chat template suppress
    thinking via the llama-server-recognized `chat_template_kwargs.enable_thinking`
    flag. Other backends will ignore unknown fields."""
    if not isinstance(reasoning, dict):
        return
    if reasoning.get("effort") != "minimal":
        return
    cc.setdefault("chat_template_kwargs", {})
    if isinstance(cc["chat_template_kwargs"], dict):
        cc["chat_template_kwargs"].setdefault("enable_thinking", False)


def to_chat_completions(
    req: dict,
    *,
    prior_messages: Optional[list[dict]] = None,
) -> dict:
    """Translate a Responses request body to a Chat Completions request body.

    `prior_messages` is the reconstructed conversation history from
    previous_response_id (already in Chat Completions shape). It is prepended
    to the new messages.
    """
    if not isinstance(req, dict):
        raise ResponsesTranslationError("Request body must be a JSON object.")

    messages: list[dict] = []
    if prior_messages:
        messages.extend(prior_messages)

    # `instructions` is the Responses-specific system message. It overrides
    # any system message inherited from the prior chain (per the OpenAI docs:
    # "instructions from a previous response will not be carried over").
    instructions = req.get("instructions")
    if isinstance(instructions, str) and instructions:
        # Drop any leading system messages inherited from prior_messages so the
        # new instructions take precedence.
        messages = [m for m in messages if m.get("role") != "system"]
        messages.insert(0, {"role": "system", "content": instructions})
    elif isinstance(instructions, list):
        # Less common: instructions can be a list of input items in newer
        # Stainless schemas. Flatten to text.
        text = _coerce_content_to_text(instructions)
        if text:
            messages = [m for m in messages if m.get("role") != "system"]
            messages.insert(0, {"role": "system", "content": text})

    messages.extend(_input_to_messages(req.get("input")))

    # reasoning.effort=minimal hint (system-prompt + chat_template_kwargs)
    messages = _apply_reasoning_effort_messages(messages, req.get("reasoning"))

    cc: dict = {
        "model": req.get("model") or "default",
        "messages": messages,
    }

    # Streaming
    if req.get("stream"):
        cc["stream"] = True

    # Sampling
    for k_in, k_out in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("max_output_tokens", "max_tokens"),
        ("parallel_tool_calls", "parallel_tool_calls"),
        ("seed", "seed"),
        ("metadata", "metadata"),
        ("user", "user"),
    ):
        if k_in in req and req[k_in] is not None:
            cc[k_out] = req[k_in]

    # Tools
    tools = _translate_tools(req.get("tools"))
    if tools:
        cc["tools"] = tools

    tc = _translate_tool_choice(req.get("tool_choice"))
    if tc is not None:
        cc["tool_choice"] = tc

    # text.format -> response_format
    rf = _translate_text_format(req.get("text"))
    if rf is not None:
        cc["response_format"] = rf

    # stop sequences (Responses doesn't have a direct field, but some
    # clients pass it).
    if "stop" in req:
        cc["stop"] = req["stop"]

    # Backend hint for thinking-capable models when reasoning.effort=minimal.
    _apply_reasoning_effort_kwargs(cc, req.get("reasoning"))

    return cc


def _build_output_items_from_message(msg: dict) -> list[dict]:
    """Map a Chat Completions assistant message to Responses output items.

    llama-server returns reasoning in a separate `reasoning_content` field
    (vs OpenAI's first-party hosted reasoning models which produce reasoning
    inline). Surface it as a Responses `reasoning` output item so clients
    that inspect the output array see the model actually thought, even when
    `content` is empty (e.g. truncated at max_output_tokens during thinking).
    """
    items: list[dict] = []
    reasoning_content = msg.get("reasoning_content") or msg.get("reasoning")
    content = msg.get("content")
    tool_calls = msg.get("tool_calls") or []
    if isinstance(reasoning_content, str) and reasoning_content:
        items.append({
            "id": "rs_" + secrets.token_hex(12),
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": reasoning_content}],
        })
    if content:
        text = content if isinstance(content, str) else _coerce_content_to_text(content)
        items.append({
            "id": "msg_" + secrets.token_hex(12),
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        items.append({
            "id": "fc_" + secrets.token_hex(12),
            "type": "function_call",
            "status": "completed",
            "call_id": tc.get("id") or ("call_" + secrets.token_hex(8)),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "") or "",
        })
    return items


def _flatten_output_text(output: list[dict]) -> str:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    txt = c.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
    return "".join(parts)


def _translate_usage(usage: Any) -> dict:
    """Chat Completions usage -> Responses usage."""
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    tt = int(usage.get("total_tokens", pt + ct) or 0)
    return {
        "input_tokens": pt,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": ct,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": tt,
    }


def from_chat_completions(
    cc_resp: dict,
    *,
    response_id: str,
    created_at: int,
    original_request: dict,
) -> dict:
    """Translate a Chat Completions response body to a Responses response body."""
    choices = cc_resp.get("choices") or []
    first = choices[0] if choices else {}
    msg = first.get("message") or {}
    output = _build_output_items_from_message(msg)

    finish_reason = first.get("finish_reason")
    status = "completed"
    incomplete_details = None
    if finish_reason == "length":
        status = "incomplete"
        incomplete_details = {"reason": "max_output_tokens"}
    elif finish_reason == "content_filter":
        status = "incomplete"
        incomplete_details = {"reason": "content_filter"}

    model = cc_resp.get("model") or original_request.get("model") or "default"

    out: dict = {
        "id": response_id,
        "object": "response",
        "created_at": int(created_at),
        "status": status,
        "error": None,
        "incomplete_details": incomplete_details,
        "instructions": original_request.get("instructions"),
        "max_output_tokens": original_request.get("max_output_tokens"),
        "model": model,
        "output": output,
        "output_text": _flatten_output_text(output),
        "parallel_tool_calls": original_request.get("parallel_tool_calls", True),
        "previous_response_id": original_request.get("previous_response_id"),
        "reasoning": original_request.get("reasoning") or {"effort": None, "summary": None},
        "store": original_request.get("store", True) if original_request.get("store") is not False else False,
        "temperature": original_request.get("temperature"),
        "text": original_request.get("text") or {"format": {"type": "text"}},
        "tool_choice": original_request.get("tool_choice") or "auto",
        "tools": original_request.get("tools") or [],
        "top_p": original_request.get("top_p"),
        "truncation": original_request.get("truncation") or "disabled",
        "usage": _translate_usage(cc_resp.get("usage")),
        "user": original_request.get("user"),
        "metadata": original_request.get("metadata") or {},
    }
    return out


def stored_chain_to_messages(chain: list[dict]) -> list[dict]:
    """Given a chain of stored response rows (oldest first), reconstruct
    the Chat Completions `messages` array that would re-create the
    conversation context for the next call.

    For each row we re-translate its original `input` to messages and append
    the assistant output as message(s)/tool-call(s).
    """
    messages: list[dict] = []
    for row in chain:
        req = row.get("request_json") or {}
        resp = row.get("response_json") or {}

        # Inputs for this turn — but DO NOT re-prepend `instructions`; only
        # the latest call's `instructions` (or current call's) should apply.
        # We still need to walk input items as user/tool messages.
        turn_messages = _input_to_messages(req.get("input"))
        messages.extend(turn_messages)

        # Assistant output from this turn.
        for item in resp.get("output") or []:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message":
                text = _coerce_content_to_text(item.get("content"))
                messages.append({"role": "assistant", "content": text})
            elif itype == "function_call":
                call_id = item.get("call_id") or item.get("id") or f"call_{secrets.token_hex(8)}"
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "") or "",
                        },
                    }],
                })
    return messages


# -------- Streaming translation --------------------------------------------

def sse_pack(event: str, data: dict) -> bytes:
    """Format one SSE event."""
    return (f"event: {event}\n" + f"data: {json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")


def make_skeleton_response(
    *,
    response_id: str,
    created_at: int,
    original_request: dict,
    status: str,
) -> dict:
    """Build a Responses object with empty output, used in `response.created`
    and `response.in_progress` events."""
    model = original_request.get("model") or "default"
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(created_at),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": original_request.get("instructions"),
        "max_output_tokens": original_request.get("max_output_tokens"),
        "model": model,
        "output": [],
        "output_text": "",
        "parallel_tool_calls": original_request.get("parallel_tool_calls", True),
        "previous_response_id": original_request.get("previous_response_id"),
        "reasoning": original_request.get("reasoning") or {"effort": None, "summary": None},
        "store": original_request.get("store", True) if original_request.get("store") is not False else False,
        "temperature": original_request.get("temperature"),
        "text": original_request.get("text") or {"format": {"type": "text"}},
        "tool_choice": original_request.get("tool_choice") or "auto",
        "tools": original_request.get("tools") or [],
        "top_p": original_request.get("top_p"),
        "truncation": original_request.get("truncation") or "disabled",
        "usage": None,
        "user": original_request.get("user"),
        "metadata": original_request.get("metadata") or {},
    }


class StreamTranslator:
    """Consume Chat Completions SSE chunks; emit Responses SSE events.

    Usage:
        st = StreamTranslator(response_id=..., created_at=..., original_request=...)
        yield from st.start()              # emits response.created + response.in_progress
        for cc_line in upstream_sse_lines:
            for evt in st.feed(cc_line):
                yield evt
        yield from st.finish()              # emits remaining done events + response.completed

    The translator tracks output-item indexes so deltas can reference them.
    """

    def __init__(self, *, response_id: str, created_at: int, original_request: dict):
        self.response_id = response_id
        self.created_at = int(created_at)
        self.original_request = original_request
        self._seq = 0
        self._started = False
        self._buf = ""

        # Output bookkeeping
        self._message_index: Optional[int] = None
        self._message_id: Optional[str] = None
        self._message_text: list[str] = []
        # Tool calls keyed by their streaming index (Chat Completions convention).
        self._tool_calls: dict[int, dict] = {}
        # Output ordering — list of (kind, key) so we can serialize at the end.
        self._output_order: list[tuple] = []

        self._finish_reason: Optional[str] = None
        self._usage: Optional[dict] = None
        self._model_seen: Optional[str] = None

        self._done_sent = False

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def start(self) -> list[bytes]:
        """Emit the `response.created` and `response.in_progress` events."""
        if self._started:
            return []
        self._started = True
        skel = make_skeleton_response(
            response_id=self.response_id,
            created_at=self.created_at,
            original_request=self.original_request,
            status="in_progress",
        )
        ev1 = sse_pack("response.created", {
            "type": "response.created",
            "sequence_number": self._next_seq(),
            "response": skel,
        })
        ev2 = sse_pack("response.in_progress", {
            "type": "response.in_progress",
            "sequence_number": self._next_seq(),
            "response": skel,
        })
        return [ev1, ev2]

    def feed(self, line_bytes: bytes) -> list[bytes]:
        """Feed one byte chunk from the upstream SSE stream. Returns
        zero-or-more translated Responses SSE events."""
        try:
            text = line_bytes.decode("utf-8", errors="replace")
        except Exception:
            return []
        self._buf += text

        events: list[bytes] = []
        # Process complete SSE messages (terminated by blank line).
        while "\n\n" in self._buf:
            raw, self._buf = self._buf.split("\n\n", 1)
            data_lines: list[str] = []
            for ln in raw.split("\n"):
                if ln.startswith("data:"):
                    data_lines.append(ln[5:].lstrip())
            if not data_lines:
                continue
            payload = "\n".join(data_lines).strip()
            if payload == "[DONE]":
                # Wait for finish() to emit response.completed.
                continue
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            events.extend(self._handle_cc_chunk(chunk))
        return events

    def _ensure_message_item(self) -> tuple[list[bytes], int, str]:
        out_events: list[bytes] = []
        if self._message_index is None:
            idx = len(self._output_order)
            mid = "msg_" + secrets.token_hex(12)
            self._message_index = idx
            self._message_id = mid
            self._output_order.append(("message", idx))
            item = {
                "id": mid,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
            out_events.append(sse_pack("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": idx,
                "item": item,
            }))
            out_events.append(sse_pack("response.content_part.added", {
                "type": "response.content_part.added",
                "sequence_number": self._next_seq(),
                "output_index": idx,
                "item_id": mid,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }))
        return out_events, self._message_index, self._message_id  # type: ignore[return-value]

    def _ensure_tool_call_item(self, tc_index: int, tc_id: Optional[str], name: Optional[str]) -> tuple[list[bytes], dict]:
        out_events: list[bytes] = []
        if tc_index not in self._tool_calls:
            order_idx = len(self._output_order)
            fc_id = "fc_" + secrets.token_hex(12)
            call_id = tc_id or ("call_" + secrets.token_hex(8))
            entry = {
                "order_index": order_idx,
                "item_id": fc_id,
                "call_id": call_id,
                "name": name or "",
                "arguments": "",
            }
            self._tool_calls[tc_index] = entry
            self._output_order.append(("function_call", tc_index))
            item = {
                "id": fc_id,
                "type": "function_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": name or "",
                "arguments": "",
            }
            out_events.append(sse_pack("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": order_idx,
                "item": item,
            }))
        else:
            entry = self._tool_calls[tc_index]
            # Late-arriving id/name should fill in.
            if tc_id and not entry["call_id"].startswith("call_"):
                entry["call_id"] = tc_id
            elif tc_id:
                entry["call_id"] = tc_id
            if name and not entry["name"]:
                entry["name"] = name
        return out_events, self._tool_calls[tc_index]

    def _handle_cc_chunk(self, chunk: dict) -> list[bytes]:
        events: list[bytes] = []
        if not isinstance(chunk, dict):
            return events
        if "model" in chunk and isinstance(chunk["model"], str):
            self._model_seen = chunk["model"]
        if isinstance(chunk.get("usage"), dict):
            self._usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            return events
        choice = choices[0]
        if not isinstance(choice, dict):
            return events
        delta = choice.get("delta") or {}
        # Text delta
        content = delta.get("content")
        if isinstance(content, str) and content:
            ev, idx, mid = self._ensure_message_item()
            events.extend(ev)
            self._message_text.append(content)
            events.append(sse_pack("response.output_text.delta", {
                "type": "response.output_text.delta",
                "sequence_number": self._next_seq(),
                "item_id": mid,
                "output_index": idx,
                "content_index": 0,
                "delta": content,
                "logprobs": [],
            }))
        # Tool-call deltas (streaming function-call args).
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_index = tc.get("index", 0)
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                name = fn.get("name")
                args_chunk = fn.get("arguments") or ""
                ev, entry = self._ensure_tool_call_item(tc_index, tc_id, name)
                events.extend(ev)
                if args_chunk:
                    entry["arguments"] += args_chunk
                    events.append(sse_pack("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "sequence_number": self._next_seq(),
                        "item_id": entry["item_id"],
                        "output_index": entry["order_index"],
                        "delta": args_chunk,
                    }))
        fr = choice.get("finish_reason")
        if fr:
            self._finish_reason = fr
        return events

    def finish(self) -> list[bytes]:
        """Emit terminal events + response.completed."""
        if self._done_sent:
            return []
        self._done_sent = True
        events: list[bytes] = []

        # Close message item if any.
        if self._message_index is not None and self._message_id is not None:
            full_text = "".join(self._message_text)
            events.append(sse_pack("response.output_text.done", {
                "type": "response.output_text.done",
                "sequence_number": self._next_seq(),
                "item_id": self._message_id,
                "output_index": self._message_index,
                "content_index": 0,
                "text": full_text,
                "logprobs": [],
            }))
            events.append(sse_pack("response.content_part.done", {
                "type": "response.content_part.done",
                "sequence_number": self._next_seq(),
                "output_index": self._message_index,
                "item_id": self._message_id,
                "content_index": 0,
                "part": {"type": "output_text", "text": full_text, "annotations": []},
            }))
            events.append(sse_pack("response.output_item.done", {
                "type": "response.output_item.done",
                "sequence_number": self._next_seq(),
                "output_index": self._message_index,
                "item": {
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": full_text, "annotations": []}],
                },
            }))

        # Close tool-call items.
        for tc_index, entry in self._tool_calls.items():
            events.append(sse_pack("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "sequence_number": self._next_seq(),
                "item_id": entry["item_id"],
                "output_index": entry["order_index"],
                "arguments": entry["arguments"],
            }))
            events.append(sse_pack("response.output_item.done", {
                "type": "response.output_item.done",
                "sequence_number": self._next_seq(),
                "output_index": entry["order_index"],
                "item": {
                    "id": entry["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "call_id": entry["call_id"],
                    "name": entry["name"],
                    "arguments": entry["arguments"],
                },
            }))

        # Final response.completed
        final = self.build_final_response()
        events.append(sse_pack("response.completed", {
            "type": "response.completed",
            "sequence_number": self._next_seq(),
            "response": final,
        }))
        return events

    def build_final_response(self) -> dict:
        """Build the full Responses object that the streaming summary represents.
        Used for the final `response.completed` event AND for persisting to
        responses_store."""
        # Reassemble output in insertion order.
        output: list[dict] = []
        full_text = "".join(self._message_text)
        for kind, key in self._output_order:
            if kind == "message":
                if self._message_id is None:
                    continue
                output.append({
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": full_text, "annotations": []}],
                })
            elif kind == "function_call":
                entry = self._tool_calls.get(key)
                if not entry:
                    continue
                output.append({
                    "id": entry["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "call_id": entry["call_id"],
                    "name": entry["name"],
                    "arguments": entry["arguments"],
                })

        status = "completed"
        incomplete_details = None
        if self._finish_reason == "length":
            status = "incomplete"
            incomplete_details = {"reason": "max_output_tokens"}
        elif self._finish_reason == "content_filter":
            status = "incomplete"
            incomplete_details = {"reason": "content_filter"}

        model = self._model_seen or self.original_request.get("model") or "default"
        usage = _translate_usage(self._usage) if self._usage else {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        }
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "error": None,
            "incomplete_details": incomplete_details,
            "instructions": self.original_request.get("instructions"),
            "max_output_tokens": self.original_request.get("max_output_tokens"),
            "model": model,
            "output": output,
            "output_text": full_text,
            "parallel_tool_calls": self.original_request.get("parallel_tool_calls", True),
            "previous_response_id": self.original_request.get("previous_response_id"),
            "reasoning": self.original_request.get("reasoning") or {"effort": None, "summary": None},
            "store": self.original_request.get("store", True) if self.original_request.get("store") is not False else False,
            "temperature": self.original_request.get("temperature"),
            "text": self.original_request.get("text") or {"format": {"type": "text"}},
            "tool_choice": self.original_request.get("tool_choice") or "auto",
            "tools": self.original_request.get("tools") or [],
            "top_p": self.original_request.get("top_p"),
            "truncation": self.original_request.get("truncation") or "disabled",
            "usage": usage,
            "user": self.original_request.get("user"),
            "metadata": self.original_request.get("metadata") or {},
        }
