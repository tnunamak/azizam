"""Ollama native API <-> canonical chat-completions translator.

Pure module. No FastAPI, no httpx, no ``proxy.py`` imports.

Covers the pure translation subset of the Ollama API surface:

* ``POST /api/chat``  → canonical chat-completions request, and back
* ``POST /api/generate`` → canonical chat-completions request, and back
* ``POST /api/embed`` / ``POST /api/embeddings`` → canonical embeddings
  request, and back
* ``GET /api/tags``    → response helper from a list of gateway model
  summaries
* ``POST /api/show`` / ``GET /api/ps`` / ``GET /api/version`` helper shapes

Backend model management belongs in follow-up modules;
``unsupported_operation_error`` is the SDK-shaped error used everywhere we
need to refuse cleanly.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


# Clients compare this to semver floors, so it must parse as a version. The
# number is the Ollama API revision the translators were checked against.
DEFAULT_VERSION = "0.12.6+vivid-fish"


class OllamaTranslationError(ValueError):
    """Raised when an Ollama request cannot be translated."""

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status

    @property
    def error_body(self) -> dict[str, Any]:
        return {"error": str(self)}


def unsupported_operation_error(
    operation: str, *, detail: str | None = None
) -> dict[str, Any]:
    """Ollama-shaped error envelope. Ollama uses ``{"error": "..."}``."""
    message = f"unsupported operation: {operation}"
    if detail:
        message = f"{message}: {detail}"
    return {"error": message}


# ---------------------------------------------------------------------------
# /api/chat
# ---------------------------------------------------------------------------


def is_load_request(body: Any, *, endpoint: str) -> bool:
    """True for the empty chat/generate request that only loads or unloads.

    Ollama clients send it to warm a model. ``messages: []`` is the chat form
    and a missing or empty ``prompt`` is the generate form.
    """
    if not isinstance(body, dict):
        return False
    if endpoint == "chat":
        return body.get("messages") == []
    return not body.get("prompt") and not body.get("images")


def load_response(
    model: str, *, endpoint: str, keep_alive: Any = None
) -> dict[str, Any]:
    """Response for a load or unload request. The gateway keeps no per-client
    model state, so this only reports what Ollama would report."""
    unload = _keep_alive_is_zero(keep_alive)
    if endpoint == "chat":
        out: dict[str, Any] = {
            "model": model,
            "created_at": _iso_utc_now(),
            "message": {"role": "assistant", "content": ""},
            "done_reason": "unload" if unload else "load",
            "done": True,
        }
        return out
    out = {
        "model": model,
        "created_at": _iso_utc_now(),
        "response": "",
        "done": True,
    }
    if unload:
        out["done_reason"] = "unload"
    return out


def _keep_alive_is_zero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        stripped = value.strip().lower()
        return stripped in {"0", "0s", "0m", "0h"}
    return False


def thinking_requested(body: Any) -> bool:
    """True when the caller set ``think`` to true or to a level."""
    think = body.get("think") if isinstance(body, dict) else None
    return think is True or isinstance(think, str) and think.lower() in {
        "low",
        "medium",
        "high",
        "max",
    }


def _apply_think(canonical: dict[str, Any], think: Any) -> None:
    if think is None:
        return
    if think is True:
        return
    if think is False:
        canonical["reasoning_effort"] = "minimal"
        return
    if isinstance(think, str) and think.lower() in {"low", "medium", "high", "max"}:
        canonical["reasoning_effort"] = (
            "high" if think.lower() == "max" else think.lower()
        )
        return
    raise OllamaTranslationError(
        "'think' must be a boolean or one of low, medium, high, max"
    )


def chat_request_to_canonical(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Ollama ``/api/chat`` request to canonical shape."""
    if not isinstance(body, dict):
        raise OllamaTranslationError("request body must be a JSON object")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise OllamaTranslationError("'model' is required")
    model = _request_model(model)

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise OllamaTranslationError("'messages' must be a non-empty list")

    canonical_messages: list[dict[str, Any]] = []
    # Ollama tool results name the tool, not the call. Remember the calls the
    # assistant made so each result gets the matching OpenAI tool_call_id.
    pending_calls: list[tuple[str, str]] = []
    for i, msg in enumerate(raw_messages):
        if not isinstance(msg, dict):
            raise OllamaTranslationError(f"messages[{i}] must be an object")
        role = msg.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            raise OllamaTranslationError(
                f"messages[{i}].role must be one of system|user|assistant|tool"
            )
        content = msg.get("content", "")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise OllamaTranslationError(
                f"messages[{i}].content must be a string"
            )
        out_msg: dict[str, Any] = {
            "role": role,
            "content": _content_with_images(
                content,
                msg.get("images"),
                f"messages[{i}].images",
            ),
        }
        if role == "assistant" and msg.get("tool_calls"):
            out_msg["tool_calls"] = _history_tool_calls(
                msg["tool_calls"], i, pending_calls
            )
            if not content:
                out_msg["content"] = None
        elif role == "tool":
            out_msg["tool_call_id"] = _match_tool_call_id(
                msg.get("tool_name"), pending_calls
            )
        canonical_messages.append(out_msg)

    canonical: dict[str, Any] = {
        "model": model,
        "messages": canonical_messages,
        "stream": _stream_flag(body),
    }

    _apply_options(canonical, body.get("options"))
    _apply_format(canonical, body.get("format"))
    _apply_think(canonical, body.get("think"))
    if canonical["stream"]:
        # Ask for the usage chunk so the final object can report token counts.
        canonical["stream_options"] = {"include_usage": True}

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        canonical["tools"] = [_translate_tool_decl(t) for t in tools]

    return canonical


def _request_model(model: str) -> str:
    """Ollama treats ``name`` and ``name:latest`` as the same model."""
    return model.removesuffix(":latest") or model


def _stream_flag(body: Mapping[str, Any]) -> bool:
    # Ollama streams unless the caller sets ``stream`` to false.
    value = body.get("stream")
    return True if value is None else bool(value)


def _history_tool_calls(
    calls: Any, index: int, pending: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    if not isinstance(calls, list):
        raise OllamaTranslationError(f"messages[{index}].tool_calls must be a list")
    out: list[dict[str, Any]] = []
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            raise OllamaTranslationError(
                f"messages[{index}].tool_calls[].function.name is required"
            )
        arguments = fn.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, sort_keys=True)
        call_id = f"call_{uuid.uuid4().hex[:24]}"
        pending.append((str(fn["name"]), call_id))
        out.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": fn["name"], "arguments": arguments},
            }
        )
    return out


def _match_tool_call_id(tool_name: Any, pending: list[tuple[str, str]]) -> str:
    for position, (name, call_id) in enumerate(pending):
        if tool_name in (None, "") or name == tool_name:
            del pending[position]
            return call_id
    return f"call_{uuid.uuid4().hex[:24]}"


def canonical_response_to_chat(
    canonical: dict[str, Any],
    *,
    request_model: str | None = None,
    thinking: bool = False,
    started_ns: int | None = None,
) -> dict[str, Any]:
    """Translate canonical chat-completions response to Ollama ``/api/chat``."""
    choices = canonical.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    finish_reason = first.get("finish_reason")
    has_tools = bool(message.get("tool_calls"))
    text, reasoning = _split_answer_and_reasoning(
        message, thinking=thinking, finish_reason=finish_reason, has_tools=has_tools
    )

    out_message: dict[str, Any] = {
        "role": message.get("role") or "assistant",
        "content": text,
    }
    if thinking and reasoning:
        out_message["thinking"] = reasoning

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        out_message["tool_calls"] = [
            _openai_tool_call_to_ollama(tc) for tc in tool_calls
        ]

    response = {
        "model": canonical.get("model") or request_model or "",
        "created_at": _iso_utc_now(),
        "message": out_message,
        "done": True,
        "done_reason": _finish_reason_to_done_reason(finish_reason),
    }
    _apply_final_stats(response, canonical, started_ns)
    return response


# ---------------------------------------------------------------------------
# /api/generate
# ---------------------------------------------------------------------------


def generate_request_to_canonical(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Ollama ``/api/generate`` request to canonical shape."""
    if not isinstance(body, dict):
        raise OllamaTranslationError("request body must be a JSON object")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise OllamaTranslationError("'model' is required")
    model = _request_model(model)

    prompt = body.get("prompt", "")
    if not isinstance(prompt, str):
        raise OllamaTranslationError("'prompt' must be a string")
    if body.get("suffix"):
        raise OllamaTranslationError(
            "unsupported operation: 'suffix' (fill-in-the-middle) is not available "
            "through this frontend",
        )
    if body.get("template"):
        raise OllamaTranslationError(
            "unsupported operation: 'template' overrides are not available "
            "through this frontend",
        )

    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    messages.append(
        {
            "role": "user",
            "content": _content_with_images(prompt, body.get("images"), "'images'"),
        }
    )

    canonical: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": _stream_flag(body),
    }
    _apply_options(canonical, body.get("options"))
    _apply_format(canonical, body.get("format"))
    _apply_think(canonical, body.get("think"))
    if canonical["stream"]:
        # Ask for the usage chunk so the final object can report token counts.
        canonical["stream_options"] = {"include_usage": True}

    if body.get("raw"):
        # `raw` disables Ollama's prompt templating; the canonical layer
        # cannot promise that downstream backends honor it. Record the
        # caller's intent so routing can decide what to do.
        canonical["__ollama_raw__"] = True

    return canonical


def canonical_response_to_generate(
    canonical: dict[str, Any],
    *,
    request_model: str | None = None,
    thinking: bool = False,
    started_ns: int | None = None,
) -> dict[str, Any]:
    """Translate canonical chat-completions response to ``/api/generate``."""
    choices = canonical.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    finish_reason = first.get("finish_reason")
    text, reasoning = _split_answer_and_reasoning(
        message,
        thinking=thinking,
        finish_reason=finish_reason,
        has_tools=False,
    )

    response: dict[str, Any] = {
        "model": canonical.get("model") or request_model or "",
        "created_at": _iso_utc_now(),
        "response": text,
        "done": True,
        "done_reason": _finish_reason_to_done_reason(finish_reason),
    }
    if thinking and reasoning:
        response["thinking"] = reasoning
    _apply_final_stats(response, canonical, started_ns)
    return response


class OllamaStreamTranslator:
    """Stateful OpenAI chat stream -> Ollama NDJSON objects.

    Feed decoded chat-completions chunks and write each returned object as one
    JSON line. Call ``finish()`` when the upstream stream ends. OpenAI sends
    usage after the finish chunk, and splits one tool call over many chunks.
    Ollama sends a tool call whole and puts usage on the last object. This
    class holds both back until ``finish()``.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        request_model: str | None = None,
        thinking: bool = False,
    ) -> None:
        if endpoint not in ("chat", "generate"):
            raise ValueError("endpoint must be 'chat' or 'generate'")
        self.endpoint = endpoint
        self.model = request_model or ""
        self.thinking = thinking
        self.started_ns = time.monotonic_ns()
        self._tool_calls: dict[Any, dict[str, Any]] = {}
        self._reasoning: list[str] = []
        self._emitted_text = False
        self._finish_reason: Any = None
        self._usage: dict[str, Any] = {}
        self._timings: dict[str, Any] = {}
        self.finished = False

    def _object(self, *, text: str = "", thinking: str = "") -> dict[str, Any]:
        obj: dict[str, Any] = {
            "model": self.model,
            "created_at": _iso_utc_now(),
        }
        if self.endpoint == "chat":
            message: dict[str, Any] = {"role": "assistant", "content": text}
            if thinking:
                message["thinking"] = thinking
            obj["message"] = message
        else:
            obj["response"] = text
            if thinking:
                obj["thinking"] = thinking
        obj["done"] = False
        return obj

    def feed(self, chunk: Mapping[str, Any]) -> list[dict[str, Any]]:
        if self.finished or not isinstance(chunk, Mapping):
            return []
        self.model = chunk.get("model") or self.model
        if isinstance(chunk.get("usage"), Mapping):
            self._usage = dict(chunk["usage"])
        if isinstance(chunk.get("timings"), Mapping):
            self._timings = dict(chunk["timings"])

        choice = _first_choice(chunk)
        delta = choice.get("delta") or {}
        out: list[dict[str, Any]] = []

        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            if self.thinking:
                out.append(self._object(thinking=reasoning))
            else:
                self._reasoning.append(reasoning)

        text = delta.get("content")
        if isinstance(text, str) and text:
            self._emitted_text = True
            out.append(self._object(text=text))

        for call in delta.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            slot = self._tool_calls.setdefault(
                call.get("index", 0), {"name": "", "arguments": ""}
            )
            fn = call.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            arguments = fn.get("arguments")
            if isinstance(arguments, str):
                slot["arguments"] += arguments
            elif isinstance(arguments, dict):
                slot["arguments"] = json.dumps(arguments)

        if choice.get("finish_reason") is not None:
            self._finish_reason = choice["finish_reason"]
        return out

    def finish(self) -> list[dict[str, Any]]:
        if self.finished:
            return []
        self.finished = True
        out: list[dict[str, Any]] = []

        if self._tool_calls:
            tool_obj = self._object()
            calls = [
                _openai_tool_call_to_ollama(
                    {"function": {"name": v["name"], "arguments": v["arguments"]}}
                )
                for _, v in sorted(
                    self._tool_calls.items(), key=lambda kv: str(kv[0])
                )
            ]
            if self.endpoint == "chat":
                tool_obj["message"]["tool_calls"] = calls
                out.append(tool_obj)
        elif (
            self._reasoning
            and not self._emitted_text
            and not self.thinking
            and self._finish_reason in (None, "stop")
        ):
            # Same rule as the non-streaming path: reasoning is an answer
            # only when the model stopped on its own and said nothing else.
            fallback = _visible_text_from_reasoning("".join(self._reasoning))
            if fallback:
                out.append(self._object(text=fallback))

        final = self._object()
        final["done"] = True
        final["done_reason"] = _finish_reason_to_done_reason(self._finish_reason)
        _apply_final_stats(
            final,
            {"usage": self._usage, "timings": self._timings},
            self.started_ns,
        )
        out.append(final)
        return out


# ---------------------------------------------------------------------------
# /api/embed and /api/embeddings
# ---------------------------------------------------------------------------


def embed_request_to_canonical(body: dict[str, Any]) -> dict[str, Any]:
    """Translate current Ollama ``/api/embed`` to OpenAI embeddings shape."""
    if not isinstance(body, dict):
        raise OllamaTranslationError("request body must be a JSON object")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise OllamaTranslationError("'model' is required")
    model = _request_model(model)

    input_value = body.get("input")
    if isinstance(input_value, str):
        canonical_input: str | list[str] = input_value
    elif (
        isinstance(input_value, list)
        and all(isinstance(item, str) for item in input_value)
    ):
        canonical_input = input_value
    else:
        raise OllamaTranslationError("'input' must be a string or list of strings")

    canonical: dict[str, Any] = {
        "model": model,
        "input": canonical_input,
    }
    # ``truncate``, ``keep_alive`` and ``options`` only tune a local runtime.
    # Real clients send them on every call, so they are accepted and ignored.
    dimensions = body.get("dimensions")
    if isinstance(dimensions, int) and not isinstance(dimensions, bool) and dimensions > 0:
        canonical["dimensions"] = dimensions
    return canonical


def embeddings_request_to_canonical(body: dict[str, Any]) -> dict[str, Any]:
    """Translate legacy Ollama ``/api/embeddings`` to OpenAI embeddings shape."""
    if not isinstance(body, dict):
        raise OllamaTranslationError("request body must be a JSON object")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise OllamaTranslationError("'model' is required")
    model = _request_model(model)

    prompt = body.get("prompt")
    if not isinstance(prompt, str):
        raise OllamaTranslationError("'prompt' must be a string")

    return {
        "model": model,
        "input": prompt,
    }


def canonical_response_to_embed(
    canonical: dict[str, Any], *, request_model: str | None = None
) -> dict[str, Any]:
    """Translate OpenAI embeddings response to current Ollama ``/api/embed``."""
    rows = _embedding_rows(canonical)
    usage = canonical.get("usage") or {}
    return {
        "model": canonical.get("model") or request_model or "",
        "embeddings": [row.get("embedding") or [] for row in rows],
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_count": int(usage.get("prompt_tokens") or 0),
    }


def canonical_response_to_embeddings(
    canonical: dict[str, Any], *, request_model: str | None = None
) -> dict[str, Any]:
    """Translate OpenAI embeddings response to legacy Ollama shape."""
    rows = _embedding_rows(canonical)
    first = rows[0] if rows else {}
    return {
        "embedding": first.get("embedding") or [],
    }


# ---------------------------------------------------------------------------
# Model-management response helpers
# ---------------------------------------------------------------------------


def tags_response_from_model_summaries(
    summaries: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build an ``/api/tags`` response envelope from gateway model summaries.

    Each summary should be a mapping with at least an ``id`` key. Optional
    fields used when present:

    * ``family`` / ``parameter_size`` / ``quantization_level`` → details
    * ``size`` (bytes) and ``digest`` → top-level fields
    * ``modified_at`` (ISO-8601 string) → top-level field
    """
    models: list[dict[str, Any]] = []
    for entry in summaries:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("id") or entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        details = _ollama_model_details(entry)
        models.append(
            {
                "name": name,
                "model": name,
                "modified_at": entry.get("modified_at") or _iso_utc_now(),
                "size": int(entry.get("size") or 0),
                "digest": entry.get("digest") or "",
                "details": details,
            }
        )
    return {"models": models}


def show_response_from_model_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Build an ``/api/show`` response from a configured model summary."""
    if not isinstance(summary, Mapping):
        raise OllamaTranslationError("model summary must be an object")
    name = _model_name(summary)
    if not name:
        raise OllamaTranslationError("model summary requires an id or name")

    details = _ollama_model_details(summary)
    model_info = summary.get("model_info")
    if not isinstance(model_info, Mapping):
        model_info = {
            "general.architecture": details["family"],
            "general.file_type": details["format"],
            "general.parameter_count": summary.get("parameter_count", 0),
            "general.quantization_version": details["quantization_level"],
        }

    model_info = dict(model_info)
    context_window = _positive_int(
        summary.get("context_window")
        or summary.get("context_length")
        or summary.get("max_context_tokens")
        or summary.get("n_ctx")
    )
    if context_window is not None:
        has_context_length = any(
            str(key).lower() == "context_length"
            or str(key).lower().endswith(".context_length")
            for key in model_info
        )
        if not has_context_length:
            architecture = str(
                model_info.get("general.architecture") or details.get("family") or "general"
            ).strip() or "general"
            model_info[f"{architecture}.context_length"] = context_window

    parameters = str(summary.get("parameters") or "")
    if context_window is not None and "num_ctx" not in parameters.split():
        parameters = (parameters.rstrip() + f"\nnum_ctx {context_window}").lstrip()

    return {
        "license": summary.get("license", ""),
        "modelfile": summary.get("modelfile") or f"FROM {name}",
        "parameters": parameters,
        "template": summary.get("template", ""),
        "details": details,
        "model_info": model_info,
        "capabilities": _ollama_capabilities(summary, name),
    }


def _ollama_capabilities(summary: Mapping[str, Any], name: str) -> list[str]:
    """Ollama capability names. Clients use them to enable tools and images.

    Every gateway chat model takes images, tools and reasoning. Embedding
    models are the only exception and are found by name.
    """
    explicit = summary.get("capabilities")
    if isinstance(explicit, list) and all(isinstance(c, str) for c in explicit) and any(
        c in ("embedding", "vision", "tools", "thinking") for c in explicit
    ):
        return list(explicit)
    if "embed" in name.lower():
        return ["embedding"]
    return ["completion", "vision", "tools", "thinking"]


def ps_response_from_model_summaries(
    summaries: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build an ``/api/ps`` response from loaded gateway model summaries."""
    models: list[dict[str, Any]] = []
    for entry in summaries:
        if not isinstance(entry, Mapping):
            continue
        name = _model_name(entry)
        if not name:
            continue
        models.append(
            {
                "name": name,
                "model": name,
                "size": int(entry.get("size") or 0),
                "digest": entry.get("digest") or "",
                "details": _ollama_model_details(entry),
                "expires_at": entry.get("expires_at") or _iso_utc_now(),
                "size_vram": int(entry.get("size_vram") or entry.get("size") or 0),
            }
        )
    return {"models": models}


def version_response(version: str | None = None) -> dict[str, Any]:
    """Build an Ollama-compatible ``/api/version`` response."""
    return {"version": version or DEFAULT_VERSION}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_OLLAMA_TO_OPENAI_OPTIONS = {
    "temperature": "temperature",
    "top_p": "top_p",
    "seed": "seed",
    "stop": "stop",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    # Sampling controls the llama.cpp backend takes under the same name.
    "top_k": "top_k",
    "min_p": "min_p",
    "typical_p": "typical_p",
    "repeat_penalty": "repeat_penalty",
    "repeat_last_n": "repeat_last_n",
    "mirostat": "mirostat",
    "mirostat_tau": "mirostat_tau",
    "mirostat_eta": "mirostat_eta",
}

_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)

# Ollama options that size a local runtime (context, GPU layers, threads).
# The gateway owns the backend, so these are accepted and ignored, as Ollama
# itself ignores options it cannot apply. Unknown options are ignored too.


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _apply_options(canonical: dict[str, Any], options: Any) -> None:
    if not isinstance(options, dict):
        return
    for key, value in options.items():
        if key == "num_predict":
            # -1 and -2 mean "no limit" and "fill the context" in Ollama.
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                canonical["max_tokens"] = value
            continue
        target = _OLLAMA_TO_OPENAI_OPTIONS.get(key)
        if target is not None and value is not None:
            canonical[target] = value


def _apply_format(canonical: dict[str, Any], fmt: Any) -> None:
    if fmt is None:
        return
    if fmt == "json":
        canonical["response_format"] = {"type": "json_object"}
        return
    if isinstance(fmt, dict):
        canonical["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "ollama_format", "schema": fmt},
        }
        return
    raise OllamaTranslationError("'format' must be 'json' or a JSON schema object")


def _content_with_images(
    content: str,
    images: Any,
    field: str,
) -> str | list[dict[str, Any]]:
    if images is None or images == []:
        return content
    if not isinstance(images, list):
        raise OllamaTranslationError(f"{field} must be a list of base64 strings")

    blocks: list[dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for i, image in enumerate(images):
        if not isinstance(image, str) or not image:
            raise OllamaTranslationError(
                f"{field}[{i}] must be a non-empty base64 string"
            )
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": _data_url_from_ollama_image(image)},
            }
        )
    return blocks


def _data_url_from_ollama_image(image: str) -> str:
    if image.startswith("data:image/"):
        return image
    return f"data:{_guess_image_media_type(image)};base64,{image}"


def _guess_image_media_type(image: str) -> str:
    import base64
    import binascii

    try:
        head = base64.b64decode(image[:64], validate=False)
    except (binascii.Error, ValueError):
        return "image/jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    for signature, media_type in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return media_type
    return "image/jpeg"


def _translate_tool_decl(tool: Any) -> dict[str, Any]:
    # Ollama already speaks OpenAI tool-decl shape; pass through after
    # light validation so callers can't smuggle unexpected fields.
    if not isinstance(tool, dict):
        raise OllamaTranslationError("each tool must be an object")
    if tool.get("type") != "function":
        raise OllamaTranslationError("tool.type must be 'function'")
    fn = tool.get("function")
    if not isinstance(fn, dict) or not fn.get("name"):
        raise OllamaTranslationError("tool.function.name is required")
    return {
        "type": "function",
        "function": {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {
                "type": "object",
                "properties": {},
            },
        },
    }


def _openai_tool_call_to_ollama(call: Any) -> dict[str, Any]:
    if not isinstance(call, dict):
        return {}
    fn = call.get("function") or {}
    raw_args = fn.get("arguments")
    args: Any = {}
    if isinstance(raw_args, str) and raw_args:
        import json

        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {"__raw__": raw_args}
    elif isinstance(raw_args, dict):
        args = raw_args
    return {
        "function": {
            "name": fn.get("name", ""),
            "arguments": args,
        }
    }


def _first_choice(canonical: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = canonical.get("choices") or []
    first = choices[0] if choices else {}
    return first if isinstance(first, Mapping) else {}


def _visible_text_from_reasoning(reasoning: str) -> str:
    lower = reasoning.lower()
    close = lower.rfind("</think>")
    if close >= 0:
        return reasoning[close + len("</think>") :].strip()
    return reasoning


def _split_answer_and_reasoning(
    message: Mapping[str, Any],
    *,
    thinking: bool,
    finish_reason: Any,
    has_tools: bool,
) -> tuple[str, str]:
    """Return ``(answer, reasoning)`` for one chat-completions message.

    Reasoning is shown only when the caller asked for ``think``. Otherwise it
    is an answer only when the model stopped on its own and said nothing
    else; a length stop means the reasoning was cut off.
    """
    content = message.get("content")
    answer = content if isinstance(content, str) else ""
    reasoning = message.get("reasoning_content")
    reasoning = reasoning if isinstance(reasoning, str) else ""
    if not answer and reasoning and not thinking and not has_tools:
        if finish_reason in (None, "stop"):
            answer = _visible_text_from_reasoning(reasoning)
    return answer, reasoning


def _apply_final_stats(
    response: dict[str, Any], canonical: Mapping[str, Any], started_ns: int | None
) -> None:
    """Add the token counts and nanosecond durations Ollama puts on a final object."""
    usage = canonical.get("usage") or {}
    timings = canonical.get("timings") or {}
    response["total_duration"] = (
        max(0, time.monotonic_ns() - started_ns) if started_ns is not None else 0
    )
    response["load_duration"] = 0
    response["prompt_eval_count"] = int(usage.get("prompt_tokens") or 0)
    response["prompt_eval_duration"] = int(float(timings.get("prompt_ms") or 0) * 1e6)
    response["eval_count"] = int(usage.get("completion_tokens") or 0)
    response["eval_duration"] = int(float(timings.get("predicted_ms") or 0) * 1e6)


def _embedding_rows(canonical: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = canonical.get("data") or []
    if not isinstance(rows, list):
        return []
    valid = [row for row in rows if isinstance(row, Mapping)]
    return sorted(valid, key=lambda row: int(row.get("index") or 0))


def _model_name(entry: Mapping[str, Any]) -> str:
    name = entry.get("id") or entry.get("name") or entry.get("model")
    return name if isinstance(name, str) and name else ""


def _ollama_model_details(entry: Mapping[str, Any]) -> dict[str, Any]:
    family = entry.get("family", "")
    return {
        "parent_model": entry.get("parent_model", ""),
        "format": entry.get("format", "gguf"),
        "family": family,
        "families": entry.get("families") or ([family] if family else []),
        "parameter_size": entry.get("parameter_size", ""),
        "quantization_level": entry.get("quantization_level", ""),
    }


def _finish_reason_to_done_reason(finish_reason: Any) -> str:
    if finish_reason == "length":
        return "length"
    if finish_reason in (None, "stop", "tool_calls"):
        return "stop"
    return str(finish_reason)


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_request_summary(body: dict[str, Any], *, endpoint: str) -> dict[str, Any]:
    """Metadata-only summary of an Ollama request — no prompt content."""
    if not isinstance(body, dict):
        return {"valid": False, "endpoint": endpoint}
    if endpoint == "chat":
        messages = body.get("messages") or []
        return {
            "endpoint": endpoint,
            "model": body.get("model"),
            "message_count": len(messages) if isinstance(messages, list) else 0,
            "stream": bool(body.get("stream", False)),
            "has_tools": bool(body.get("tools")),
            "received_at": int(time.time()),
        }
    if endpoint == "generate":
        return {
            "endpoint": endpoint,
            "model": body.get("model"),
            "has_system": bool(body.get("system")),
            "has_prompt": bool(body.get("prompt")),
            "stream": bool(body.get("stream", False)),
            "raw": bool(body.get("raw", False)),
            "received_at": int(time.time()),
        }
    return {"endpoint": endpoint, "model": body.get("model")}
