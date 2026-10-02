"""Anthropic Messages <-> canonical chat-completions translator.

This module is intentionally pure. It does not import FastAPI, httpx, or
``proxy.py``. It exposes:

* :func:`messages_request_to_canonical` — turn an Anthropic ``/v1/messages``
  request body into an OpenAI chat-completions-shaped canonical request.
* :func:`canonical_response_to_messages` — turn an OpenAI chat-completions
  response into an Anthropic Messages response.
* :func:`unsupported_feature_error` — Anthropic SDK-shaped error envelope.
* :func:`invalid_request_error` — Anthropic SDK-shaped validation error.

The canonical request shape is the subset of OpenAI chat-completions that
the gateway already speaks internally::

    {
      "model": str,
      "messages": [{"role": str, "content": str | list[block]}, ...],
      "max_tokens": int | None,
      "temperature": float | None,
      "top_p": float | None,
      "stop": list[str] | None,
      "stream": bool,
      "reasoning_effort": "minimal",
      "tools": list[...] | None,
      "tool_choice": ... | None,
      "metadata": dict | None,
    }

Conservative posture for unsupported features:

* Anthropic image content blocks are translated into OpenAI-compatible
  multimodal chat content blocks and routed by the normal chat path.
* Tool use is translated structurally; we do not invent shapes the
  underlying chat-completions backend does not accept.
* Anthropic-only knobs (``top_k``, ``thinking`` blocks, ``cache_control``)
  are recorded as ignored fields and surfaced via
  :func:`unsupported_feature_error` when the caller asked for them
  explicitly.
"""

from __future__ import annotations

import json
import time
import uuid
from math import ceil
from typing import Any, Iterable

ANTHROPIC_DEFAULT_STOP_REASON = "end_turn"
_TOOL_USE_STOP_REASON = "tool_use"
_MAX_TOKENS_STOP_REASON = "max_tokens"
_STOP_SEQUENCE_STOP_REASON = "stop_sequence"

_UNSUPPORTED_BLOCK_TYPES = frozenset({"document"})
# Prior-turn reasoning that clients echo back. Local models cannot use a
# signed thinking block, so the translator drops it.
_DROPPED_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


class AnthropicTranslationError(ValueError):
    """Raised when an Anthropic request cannot be translated.

    Carries an ``error_body`` mapping shaped like the Anthropic SDK's
    error envelope so the calling route can pass it straight back to the
    client without extra wrapping.
    """

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "invalid_request_error",
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.http_status = http_status

    @property
    def error_body(self) -> dict[str, Any]:
        return {
            "type": "error",
            "error": {"type": self.error_type, "message": str(self)},
        }


def unsupported_feature_error(
    feature: str, *, detail: str | None = None
) -> dict[str, Any]:
    """SDK-shaped Anthropic error envelope for an unsupported feature.

    The Anthropic SDK validates errors against a small enum of types;
    ``invalid_request_error`` is the safest mapping for "we don't support
    this".
    """
    message = f"Unsupported Anthropic feature: {feature}"
    if detail:
        message = f"{message}: {detail}"
    return {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": message},
    }


def invalid_request_error(message: str) -> dict[str, Any]:
    return {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": message},
    }


def thinking_requested(body: Any) -> bool:
    """True when the caller asked for visible ``thinking`` blocks.

    ``display: "omitted"`` (sent by Claude Code) asks the model to think but
    not return the text, so no thinking block is produced.
    """
    thinking = body.get("thinking") if isinstance(body, dict) else None
    return (
        isinstance(thinking, dict)
        and thinking.get("type") in ("enabled", "adaptive")
        and thinking.get("display") != "omitted"
    )


_OUTPUT_EFFORTS = {"low": "low", "medium": "medium", "high": "high", "max": "high"}


def _reasoning_effort_for_thinking(thinking: Any, output_config: Any = None) -> str:
    """Map the Anthropic ``thinking`` parameter to a chat ``reasoning_effort``."""
    if thinking is None:
        return "minimal"
    if not isinstance(thinking, dict):
        raise AnthropicTranslationError("'thinking' must be an object")
    kind = thinking.get("type")
    if kind == "disabled":
        return "minimal"
    if kind == "adaptive":
        effort = output_config.get("effort") if isinstance(output_config, dict) else None
        return _OUTPUT_EFFORTS.get(effort, "medium")
    if kind == "enabled":
        budget = thinking.get("budget_tokens")
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise AnthropicTranslationError(
                "'thinking.budget_tokens' must be a positive integer when "
                "thinking is enabled"
            )
        if budget <= 2048:
            return "low"
        if budget <= 8192:
            return "medium"
        return "high"
    raise AnthropicTranslationError(
        "'thinking.type' must be 'enabled', 'disabled' or 'adaptive'"
    )


def estimate_count_tokens(body: dict[str, Any]) -> dict[str, Any]:
    """Return an Anthropic-shaped best-effort token count for a Messages body."""
    canonical = messages_request_to_canonical(
        {**body, "max_tokens": body.get("max_tokens") or 1}
    )
    total_chars = 0
    for message in canonical.get("messages") or []:
        if not isinstance(message, dict):
            continue
        total_chars += len(str(message.get("role") or ""))
        content = message.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        else:
            total_chars += len(
                json.dumps(content, separators=(",", ":"), sort_keys=True)
            )

    tools = canonical.get("tools")
    if tools:
        total_chars += len(json.dumps(tools, separators=(",", ":"), sort_keys=True))

    # Four chars/token is a conservative, tokenizer-free approximation commonly
    # used for English text. Keep a non-empty request at one token.
    return {"input_tokens": max(1, ceil(total_chars / 4))}


def messages_request_to_canonical(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic Messages request to the canonical shape.

    Raises :class:`AnthropicTranslationError` for malformed inputs or for
    features we deliberately refuse to fake.
    """
    if not isinstance(body, dict):
        raise AnthropicTranslationError("request body must be a JSON object")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise AnthropicTranslationError("'model' is required")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise AnthropicTranslationError("'messages' must be a non-empty list")

    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        raise AnthropicTranslationError(
            "'max_tokens' is required by the Anthropic Messages API"
        )
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise AnthropicTranslationError(
            "'max_tokens' must be a positive integer"
        )
    reasoning_effort = _reasoning_effort_for_thinking(
        body.get("thinking"), body.get("output_config")
    )

    canonical_messages: list[dict[str, Any]] = []

    system_parts: list[str] = []
    system = body.get("system")
    if isinstance(system, str) and system:
        system_parts.append(system)
    elif isinstance(system, list):
        merged = _flatten_text_blocks(system, where="system")
        if merged:
            system_parts.append(merged)

    for index, msg in enumerate(raw_messages):
        if isinstance(msg, dict) and msg.get("role") == "system":
            # Current Claude Code sends system-role entries inside ``messages``.
            # Chat templates only allow a system message first, so they join
            # the system prompt.
            content = msg.get("content")
            text = (
                content
                if isinstance(content, str)
                else _flatten_text_blocks(content, where=f"messages[{index}].content")
                if isinstance(content, list)
                else ""
            )
            if text:
                system_parts.append(text)
            continue
        canonical_messages.extend(_translate_message(msg, index=index))
    if system_parts:
        canonical_messages.insert(
            0, {"role": "system", "content": "\n\n".join(system_parts)}
        )
    if not any(m["role"] != "system" for m in canonical_messages):
        raise AnthropicTranslationError("'messages' needs at least one user message")

    canonical: dict[str, Any] = {
        "model": model,
        "messages": canonical_messages,
        "max_tokens": max_tokens,
        "stream": bool(body.get("stream", False)),
        # Anthropic Messages does not expose local chain-of-thought unless the
        # caller enables ``thinking``. Otherwise ask the gateway chat path to
        # suppress local reasoning-model chatter.
        "reasoning_effort": reasoning_effort,
    }

    if canonical["stream"]:
        # Ask for the usage chunk so message_delta can report token counts.
        canonical["stream_options"] = {"include_usage": True}
    if "temperature" in body:
        canonical["temperature"] = body["temperature"]
    if "top_p" in body:
        canonical["top_p"] = body["top_p"]
    if isinstance(body.get("top_k"), int) and not isinstance(body["top_k"], bool):
        canonical["top_k"] = body["top_k"]
    stop_sequences = body.get("stop_sequences")
    if isinstance(stop_sequences, list) and stop_sequences:
        canonical["stop"] = list(stop_sequences)

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        canonical["tools"] = [_translate_tool_decl(t) for t in tools]

    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        canonical["tool_choice"] = _translate_tool_choice(tool_choice)
        if (
            isinstance(tool_choice, dict)
            and tool_choice.get("disable_parallel_tool_use") is True
        ):
            canonical["parallel_tool_calls"] = False

    metadata = body.get("metadata")
    if isinstance(metadata, dict) and metadata:
        # Anthropic only documents `user_id` here today. Carry through only
        # the safe subset; do NOT echo raw prompt content into metadata.
        user_id = metadata.get("user_id")
        if isinstance(user_id, str) and user_id:
            canonical["user"] = user_id

    return canonical


def canonical_response_to_messages(
    canonical: dict[str, Any],
    *,
    request_model: str | None = None,
    thinking: bool = False,
) -> dict[str, Any]:
    """Translate an OpenAI chat-completions response to Anthropic Messages.

    ``canonical`` is expected to follow the OpenAI chat-completions
    response envelope. Missing optional fields are tolerated; missing
    required fields produce a best-effort response with empty content.
    ``thinking`` is true when the caller enabled extended thinking; local
    reasoning is then returned as a ``thinking`` block. Otherwise it is not
    shown to the caller.
    """
    if not isinstance(canonical, dict):
        raise AnthropicTranslationError(
            "canonical response must be a JSON object",
            error_type="api_error",
            http_status=502,
        )

    choices = canonical.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    finish_reason = first.get("finish_reason")

    content_blocks: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content")
    if thinking and isinstance(reasoning, str) and reasoning.strip():
        content_blocks.append(
            {"type": "thinking", "thinking": reasoning, "signature": ""}
        )
    text = _visible_text_from_local_reasoning(message.get("content"))
    if (
        (text is None or text == "")
        and not thinking
        and isinstance(reasoning, str)
        and finish_reason in (None, "stop")
    ):
        # Some local backends put the whole answer in reasoning_content. Use
        # it only when the model finished on its own; a length stop means the
        # reasoning was cut off and is not an answer.
        text = _visible_text_from_local_reasoning(reasoning)
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            block = _translate_tool_call_to_block(call)
            if block is not None:
                content_blocks.append(block)

    stop_reason = _finish_reason_to_stop_reason(finish_reason, content_blocks)

    usage = canonical.get("usage") or {}
    anthropic_usage = {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
    }

    response_id = canonical.get("id") or f"msg_{uuid.uuid4().hex}"
    if not str(response_id).startswith("msg_"):
        response_id = f"msg_{response_id}"

    return {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "model": canonical.get("model") or request_model or "",
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


def canonical_stream_to_messages_sse(
    canonical_stream: Iterable[Any],
    *,
    request_model: str | None = None,
    thinking: bool = False,
) -> list[str]:
    """Translate OpenAI chat-completions stream chunks to Anthropic SSE frames.

    Accepts either decoded ``chat.completion.chunk`` dictionaries or raw
    OpenAI SSE ``data: ...`` lines. Transport code can pass each upstream
    event through this pure helper without depending on FastAPI/httpx.
    """

    translator = MessagesStreamTranslator(
        request_model=request_model, thinking=thinking
    )
    frames: list[str] = []

    for raw_event in canonical_stream:
        frames.extend(translator.feed(raw_event))
    frames.extend(translator.finish())

    return frames


class MessagesStreamTranslator:
    """Stateful OpenAI chat stream -> Anthropic Messages SSE translator.

    Transport code feeds decoded OpenAI chat-completions chunks (or raw
    ``data: ...`` lines) as they arrive and yields the returned frames
    immediately. It calls ``finish()`` once the upstream stream ends. The
    closing frames wait for ``finish()`` so that the usage chunk, which
    OpenAI-style streams send after the finish chunk, reaches ``message_delta``.
    ``finish()`` is idempotent.
    """

    def __init__(
        self, *, request_model: str | None = None, thinking: bool = False
    ) -> None:
        self.response_id: str | None = None
        self.model = request_model or ""
        self.thinking = thinking
        self.finished = False
        self.suppressing_local_reasoning = False
        self.local_reasoning_prefix_buffer = ""
        self._next_index = 0
        self._open_kind: str | None = None
        self._tool_blocks: dict[Any, int] = {}
        self._emitted_text = False
        self._emitted_tool = False
        self._reasoning_buffer: list[str] = []
        self._finish_reason: Any = None
        self._usage: dict[str, Any] = {}

    # -- frame helpers -----------------------------------------------------

    def _message_start(self) -> str:
        return _anthropic_sse_frame(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.response_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def _close_block(self) -> list[str]:
        if self._open_kind is None:
            return []
        frames: list[str] = []
        index = self._next_index - 1
        if self._open_kind == "thinking":
            frames.append(
                _anthropic_sse_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "signature_delta", "signature": ""},
                    },
                )
            )
        frames.append(
            _anthropic_sse_frame(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        )
        self._open_kind = None
        return frames

    def _open_block(self, kind: str, block: dict[str, Any]) -> list[str]:
        frames = self._close_block()
        frames.append(
            _anthropic_sse_frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self._next_index,
                    "content_block": block,
                },
            )
        )
        self._next_index += 1
        self._open_kind = kind
        return frames

    def _delta(self, delta: dict[str, Any]) -> str:
        return _anthropic_sse_frame(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self._next_index - 1,
                "delta": delta,
            },
        )

    def _text_frames(self, text: str) -> list[str]:
        frames: list[str] = []
        if self._open_kind != "text":
            frames.extend(self._open_block("text", {"type": "text", "text": ""}))
        frames.append(self._delta({"type": "text_delta", "text": text}))
        self._emitted_text = True
        return frames

    # -- upstream chunks ---------------------------------------------------

    def feed(self, raw_event: Any) -> list[str]:
        if self.finished:
            return []

        chunk = _coerce_openai_stream_chunk(raw_event)
        if chunk is None:
            return []

        frames: list[str] = []
        if self.response_id is None:
            self.response_id = _messages_response_id(chunk.get("id"))
            self.model = chunk.get("model") or self.model
            frames.append(self._message_start())

        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            self._usage = chunk["usage"]

        choices = chunk.get("choices") or []
        first = choices[0] if choices else {}
        delta = first.get("delta") or {}

        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            if self.thinking:
                if self._open_kind != "thinking":
                    frames.extend(
                        self._open_block(
                            "thinking",
                            {"type": "thinking", "thinking": "", "signature": ""},
                        )
                    )
                frames.append(
                    self._delta({"type": "thinking_delta", "thinking": reasoning})
                )
            else:
                self._reasoning_buffer.append(reasoning)

        text = self._visible_text_delta(delta.get("content"))
        if isinstance(text, str) and text:
            frames.extend(self._text_frames(text))

        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                frames.extend(self._tool_call_frames(call))

        if first.get("finish_reason") is not None:
            self._finish_reason = first["finish_reason"]
        return frames

    def _tool_call_frames(self, call: Any) -> list[str]:
        if not isinstance(call, dict):
            return []
        key = call.get("index", 0)
        fn = call.get("function") or {}
        frames: list[str] = []
        if key not in self._tool_blocks:
            frames.extend(
                self._open_block(
                    "tool_use",
                    {
                        "type": "tool_use",
                        "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                        "name": fn.get("name") or "",
                        "input": {},
                    },
                )
            )
            self._tool_blocks[key] = self._next_index - 1
            self._emitted_tool = True
        arguments = fn.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, separators=(",", ":"))
        if isinstance(arguments, str) and arguments:
            frames.append(
                self._delta({"type": "input_json_delta", "partial_json": arguments})
            )
        return frames

    def finish(self, *, finish_reason: Any = None) -> list[str]:
        if self.finished:
            return []
        self.finished = True
        if finish_reason is not None:
            self._finish_reason = finish_reason
        frames: list[str] = []
        if self.response_id is None:
            self.response_id = _messages_response_id(None)
            frames.append(self._message_start())

        if (
            self._reasoning_buffer
            and not self._emitted_text
            and not self._emitted_tool
            and self._finish_reason in (None, "stop")
        ):
            # Same rule as the non-streaming path: reasoning is an answer only
            # when the model stopped on its own and said nothing else.
            fallback = _visible_text_from_local_reasoning(
                "".join(self._reasoning_buffer)
            )
            if fallback:
                frames.extend(self._text_frames(fallback))

        frames.extend(self._close_block())
        stop_reason = _finish_reason_to_stop_reason(
            self._finish_reason,
            [{"type": "tool_use"}] if self._emitted_tool else [],
        )
        usage: dict[str, Any] = {
            "output_tokens": int(self._usage.get("completion_tokens") or 0)
        }
        if self._usage.get("prompt_tokens") is not None:
            usage["input_tokens"] = int(self._usage.get("prompt_tokens") or 0)
        frames.append(
            _anthropic_sse_frame(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": usage,
                },
            )
        )
        frames.append(_anthropic_sse_frame("message_stop", {"type": "message_stop"}))
        return frames

    def _visible_text_delta(self, text: Any) -> str | None:
        if not isinstance(text, str):
            return None

        visible_parts: list[str] = []
        remaining = f"{self.local_reasoning_prefix_buffer}{text}"
        self.local_reasoning_prefix_buffer = ""
        while remaining:
            lower = remaining.lower()
            if self.suppressing_local_reasoning:
                close = lower.find("</think>")
                if close < 0:
                    return "".join(visible_parts)
                remaining = remaining[close + len("</think>") :]
                self.suppressing_local_reasoning = False
                continue

            open_tag = lower.find("<think")
            if open_tag < 0:
                partial = _think_open_prefix_suffix_length(lower)
                if partial:
                    visible_parts.append(remaining[:-partial])
                    self.local_reasoning_prefix_buffer = remaining[-partial:]
                    break
                visible_parts.append(remaining)
                break

            visible_parts.append(remaining[:open_tag])
            tag_end = lower.find(">", open_tag)
            if tag_end < 0:
                self.suppressing_local_reasoning = True
                break
            remaining = remaining[tag_end + 1 :]
            self.suppressing_local_reasoning = True

        return "".join(visible_parts)


def _coerce_openai_stream_chunk(raw_event: Any) -> dict[str, Any] | None:
    if isinstance(raw_event, dict):
        return raw_event
    if isinstance(raw_event, bytes):
        raw_event = raw_event.decode("utf-8")
    if not isinstance(raw_event, str):
        return None

    line = raw_event.strip()
    if not line:
        return None
    if line.startswith("data:"):
        line = line.removeprefix("data:").strip()
    if line == "[DONE]":
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _messages_response_id(raw_id: Any) -> str:
    response_id = raw_id or f"msg_{uuid.uuid4().hex}"
    if not str(response_id).startswith("msg_"):
        return f"msg_{response_id}"
    return str(response_id)


def _anthropic_sse_frame(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _think_open_prefix_suffix_length(text: str) -> int:
    marker = "<think"
    max_length = min(len(marker) - 1, len(text))
    for length in range(max_length, 0, -1):
        if marker.startswith(text[-length:]):
            return length
    return 0


def _visible_text_from_local_reasoning(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    lower = text.lower()
    has_think_marker = "<think" in lower or "</think>" in lower
    close = lower.rfind("</think>")
    if close >= 0:
        visible = text[close + len("</think>") :].strip()
        if visible:
            return visible
    if has_think_marker:
        return ""
    return text


def _translate_message(msg: Any, *, index: int) -> list[dict[str, Any]]:
    if not isinstance(msg, dict):
        raise AnthropicTranslationError(
            f"messages[{index}] must be an object"
        )
    role = msg.get("role")
    if role not in ("user", "assistant"):
        raise AnthropicTranslationError(
            f"messages[{index}].role must be 'user' or 'assistant'"
        )
    content = msg.get("content")

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        raise AnthropicTranslationError(
            f"messages[{index}].content must be a string or a list of blocks"
        )

    text_parts: list[str] = []
    content_parts: list[dict[str, Any]] = []
    tool_use_messages: list[dict[str, Any]] = []
    tool_result_messages: list[dict[str, Any]] = []

    for block_index, block in enumerate(content):
        if not isinstance(block, dict):
            raise AnthropicTranslationError(
                f"messages[{index}].content[{block_index}] must be an object"
            )
        btype = block.get("type")
        if btype in _DROPPED_BLOCK_TYPES:
            continue
        if btype in _UNSUPPORTED_BLOCK_TYPES:
            raise AnthropicTranslationError(
                f"unsupported content block type '{btype}' at "
                f"messages[{index}].content[{block_index}]",
            )
        if btype == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise AnthropicTranslationError(
                    f"messages[{index}].content[{block_index}].text must be a string"
                )
            text_parts.append(text)
            content_parts.append({"type": "text", "text": text})
        elif btype == "image":
            if role != "user":
                raise AnthropicTranslationError(
                    "image content blocks are only supported on user messages"
                )
            content_parts.append(
                _anthropic_image_to_openai(block, index, block_index)
            )
        elif btype == "tool_use":
            tool_use_messages.append(_anthropic_tool_use_to_openai(block, index, block_index))
        elif btype == "tool_result":
            tool_result_messages.append(
                _anthropic_tool_result_to_openai(block, index, block_index)
            )
        else:
            raise AnthropicTranslationError(
                f"unsupported content block type '{btype}' at "
                f"messages[{index}].content[{block_index}]",
            )

    out: list[dict[str, Any]] = []
    joined_text = "\n".join(p for p in text_parts if p)
    # OpenAI requires each ``tool`` message to directly follow the assistant
    # turn that made the call, so tool results go before any user text.
    out.extend(tool_result_messages)
    if role == "assistant" and tool_use_messages:
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        assistant_msg["content"] = joined_text if joined_text else None
        assistant_msg["tool_calls"] = [
            tu["__tool_call__"] for tu in tool_use_messages
        ]
        out.append(assistant_msg)
    elif role == "user" and tool_result_messages and not content_parts:
        # A user turn made entirely of tool_result blocks becomes the
        # OpenAI tool messages above; no empty user message after them.
        pass
    elif role == "user" and any(
        part.get("type") == "image_url" for part in content_parts
    ):
        out.append({"role": role, "content": content_parts})
    elif joined_text or role == "user":
        out.append({"role": role, "content": joined_text})
    return out


def _anthropic_image_to_openai(
    block: dict[str, Any], index: int, block_index: int
) -> dict[str, Any]:
    source = block.get("source")
    where = f"messages[{index}].content[{block_index}].source"
    if not isinstance(source, dict):
        raise AnthropicTranslationError(f"{where} must be an object")

    source_type = source.get("type")
    if source_type == "base64":
        media_type = source.get("media_type")
        data = source.get("data")
        if not isinstance(media_type, str) or not media_type:
            raise AnthropicTranslationError(f"{where}.media_type is required")
        if not isinstance(data, str) or not data:
            raise AnthropicTranslationError(f"{where}.data is required")
        url = f"data:{media_type};base64,{data}"
    elif source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise AnthropicTranslationError(f"{where}.url is required")
    else:
        raise AnthropicTranslationError(
            f"{where}.type must be 'base64' or 'url'"
        )

    return {"type": "image_url", "image_url": {"url": url}}


def _anthropic_tool_use_to_openai(
    block: dict[str, Any], index: int, block_index: int
) -> dict[str, Any]:
    call_id = block.get("id")
    name = block.get("name")
    arguments = block.get("input")
    if not isinstance(call_id, str) or not call_id:
        raise AnthropicTranslationError(
            f"messages[{index}].content[{block_index}].id is required for tool_use"
        )
    if not isinstance(name, str) or not name:
        raise AnthropicTranslationError(
            f"messages[{index}].content[{block_index}].name is required for tool_use"
        )
    import json

    return {
        "__tool_call__": {
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments or {}, sort_keys=True),
            },
        }
    }


def _anthropic_tool_result_to_openai(
    block: dict[str, Any], index: int, block_index: int
) -> dict[str, Any]:
    tool_use_id = block.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise AnthropicTranslationError(
            f"messages[{index}].content[{block_index}].tool_use_id is required"
        )
    raw = block.get("content")
    if isinstance(raw, str):
        content = raw
    elif isinstance(raw, list):
        content = _flatten_text_blocks(
            raw, where=f"messages[{index}].content[{block_index}]"
        )
    else:
        content = ""
    return {
        "role": "tool",
        "tool_call_id": tool_use_id,
        "content": content,
    }


def _flatten_text_blocks(blocks: Iterable[Any], *, where: str) -> str:
    parts: list[str] = []
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise AnthropicTranslationError(
                f"{where}[{i}] must be an object"
            )
        btype = block.get("type")
        if btype in _UNSUPPORTED_BLOCK_TYPES:
            raise AnthropicTranslationError(
                f"unsupported content block type '{btype}' at {where}[{i}]"
            )
        if btype != "text":
            raise AnthropicTranslationError(
                f"only 'text' blocks are supported at {where}[{i}]"
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise AnthropicTranslationError(
                f"{where}[{i}].text must be a string"
            )
        parts.append(text)
    return "\n".join(parts)


def _translate_tool_decl(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        raise AnthropicTranslationError("each tool must be an object")
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise AnthropicTranslationError("tool.name is required")
    if tool.get("type") not in (None, "custom"):
        raise AnthropicTranslationError(
            f"unsupported tool type '{tool.get('type')}' for tool '{name}'; "
            "only custom (client-side) tools are available",
        )
    description = tool.get("description") or ""
    schema = tool.get("input_schema") or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def _translate_tool_choice(choice: Any) -> Any:
    if isinstance(choice, dict):
        ctype = choice.get("type")
        if ctype == "auto":
            return "auto"
        if ctype == "any":
            return "required"
        if ctype == "none":
            return "none"
        if ctype == "tool":
            name = choice.get("name")
            if not isinstance(name, str) or not name:
                raise AnthropicTranslationError(
                    "tool_choice.name is required when type='tool'"
                )
            return {"type": "function", "function": {"name": name}}
    raise AnthropicTranslationError("unsupported tool_choice shape")


def _translate_tool_call_to_block(call: Any) -> dict[str, Any] | None:
    if not isinstance(call, dict):
        return None
    fn = call.get("function") or {}
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_args = fn.get("arguments")
    parsed_args: Any = {}
    if isinstance(raw_args, str) and raw_args:
        import json

        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed_args = {"__raw__": raw_args}
    elif isinstance(raw_args, dict):
        parsed_args = raw_args

    call_id = call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}"
    return {
        "type": "tool_use",
        "id": call_id,
        "name": name,
        "input": parsed_args,
    }


def _finish_reason_to_stop_reason(
    finish_reason: Any, content_blocks: list[dict[str, Any]]
) -> str:
    if any(b.get("type") == "tool_use" for b in content_blocks):
        return _TOOL_USE_STOP_REASON
    if finish_reason == "length":
        return _MAX_TOKENS_STOP_REASON
    if finish_reason == "stop":
        return ANTHROPIC_DEFAULT_STOP_REASON
    if finish_reason == "tool_calls":
        return _TOOL_USE_STOP_REASON
    if finish_reason == "content_filter":
        return ANTHROPIC_DEFAULT_STOP_REASON
    if finish_reason == "stop_sequence":
        return _STOP_SEQUENCE_STOP_REASON
    return ANTHROPIC_DEFAULT_STOP_REASON


def safe_request_summary(body: dict[str, Any]) -> dict[str, Any]:
    """Return a metadata-only summary safe to log.

    Deliberately omits message content, system prompts, tool inputs, and
    tool results so receipts/diagnostics can't accidentally leak prompts.
    """
    if not isinstance(body, dict):
        return {"valid": False}
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    return {
        "model": body.get("model"),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "max_tokens": body.get("max_tokens"),
        "stream": bool(body.get("stream", False)),
        "has_system": bool(body.get("system")),
        "received_at": int(time.time()),
    }
