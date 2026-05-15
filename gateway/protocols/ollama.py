"""Ollama native API <-> canonical chat-completions translator.

Pure module. No FastAPI, no httpx, no ``proxy.py`` imports.

Covers the non-streaming subset of the Ollama API surface:

* ``POST /api/chat``  → canonical chat-completions request, and back
* ``POST /api/generate`` → canonical chat-completions request, and back
* ``GET /api/tags``    → response helper from a list of gateway model
  summaries

Streaming and `/api/embeddings` translation belong in follow-up modules;
``unsupported_operation_error`` is the SDK-shaped error used everywhere we
need to refuse cleanly.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


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


def chat_request_to_canonical(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Ollama ``/api/chat`` request to canonical shape."""
    if not isinstance(body, dict):
        raise OllamaTranslationError("request body must be a JSON object")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise OllamaTranslationError("'model' is required")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise OllamaTranslationError("'messages' must be a non-empty list")

    canonical_messages: list[dict[str, Any]] = []
    for i, msg in enumerate(raw_messages):
        if not isinstance(msg, dict):
            raise OllamaTranslationError(f"messages[{i}] must be an object")
        role = msg.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            raise OllamaTranslationError(
                f"messages[{i}].role must be one of system|user|assistant|tool"
            )
        content = msg.get("content", "")
        if not isinstance(content, str):
            raise OllamaTranslationError(
                f"messages[{i}].content must be a string"
            )
        if msg.get("images"):
            # Vision passthrough is a separate slice; refuse explicitly
            # rather than silently dropping the modality.
            raise OllamaTranslationError(
                f"messages[{i}].images is not yet supported by the translator"
            )
        canonical_messages.append({"role": role, "content": content})

    canonical: dict[str, Any] = {
        "model": model,
        "messages": canonical_messages,
        "stream": bool(body.get("stream", False)),
    }

    _apply_options(canonical, body.get("options"))
    _apply_format(canonical, body.get("format"))

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        canonical["tools"] = [_translate_tool_decl(t) for t in tools]

    return canonical


def canonical_response_to_chat(
    canonical: dict[str, Any], *, request_model: str | None = None
) -> dict[str, Any]:
    """Translate canonical chat-completions response to Ollama ``/api/chat``."""
    choices = canonical.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}

    out_message: dict[str, Any] = {
        "role": message.get("role") or "assistant",
        "content": message.get("content") or "",
    }

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        out_message["tool_calls"] = [
            _openai_tool_call_to_ollama(tc) for tc in tool_calls
        ]

    finish_reason = first.get("finish_reason")
    done_reason = _finish_reason_to_done_reason(finish_reason)

    usage = canonical.get("usage") or {}
    response = {
        "model": canonical.get("model") or request_model or "",
        "created_at": _iso_utc_now(),
        "message": out_message,
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": int(usage.get("prompt_tokens") or 0),
        "eval_count": int(usage.get("completion_tokens") or 0),
    }
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

    prompt = body.get("prompt", "")
    if not isinstance(prompt, str):
        raise OllamaTranslationError("'prompt' must be a string")

    if body.get("images"):
        raise OllamaTranslationError(
            "'images' on /api/generate is not yet supported by the translator"
        )

    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    canonical: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }
    _apply_options(canonical, body.get("options"))
    _apply_format(canonical, body.get("format"))

    if body.get("raw"):
        # `raw` disables Ollama's prompt templating; the canonical layer
        # cannot promise that downstream backends honor it. Record the
        # caller's intent so routing can decide what to do.
        canonical["__ollama_raw__"] = True

    return canonical


def canonical_response_to_generate(
    canonical: dict[str, Any], *, request_model: str | None = None
) -> dict[str, Any]:
    """Translate canonical chat-completions response to ``/api/generate``."""
    choices = canonical.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    text = message.get("content") or ""

    finish_reason = first.get("finish_reason")
    done_reason = _finish_reason_to_done_reason(finish_reason)
    usage = canonical.get("usage") or {}

    return {
        "model": canonical.get("model") or request_model or "",
        "created_at": _iso_utc_now(),
        "response": text,
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": int(usage.get("prompt_tokens") or 0),
        "eval_count": int(usage.get("completion_tokens") or 0),
    }


# ---------------------------------------------------------------------------
# /api/tags
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
        details = {
            "format": entry.get("format", "gguf"),
            "family": entry.get("family", ""),
            "families": entry.get("families") or (
                [entry["family"]] if entry.get("family") else []
            ),
            "parameter_size": entry.get("parameter_size", ""),
            "quantization_level": entry.get("quantization_level", ""),
        }
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_OLLAMA_TO_OPENAI_OPTIONS = {
    "temperature": "temperature",
    "top_p": "top_p",
    "num_predict": "max_tokens",
    "seed": "seed",
    "stop": "stop",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
}

# Ollama option keys we do not yet translate. The canonical request will
# carry an annotation so the router can decide whether to refuse.
_OLLAMA_OPTIONS_NATIVE_ONLY = frozenset(
    {
        "num_ctx",
        "num_keep",
        "num_batch",
        "num_gpu",
        "main_gpu",
        "low_vram",
        "f16_kv",
        "logits_all",
        "vocab_only",
        "use_mmap",
        "use_mlock",
        "num_thread",
        "mirostat",
        "mirostat_tau",
        "mirostat_eta",
        "repeat_last_n",
        "repeat_penalty",
        "tfs_z",
        "top_k",
        "typical_p",
        "min_p",
    }
)


def _apply_options(canonical: dict[str, Any], options: Any) -> None:
    if not isinstance(options, dict):
        return
    native: dict[str, Any] = {}
    for key, value in options.items():
        target = _OLLAMA_TO_OPENAI_OPTIONS.get(key)
        if target is not None:
            canonical[target] = value
        elif key in _OLLAMA_OPTIONS_NATIVE_ONLY:
            native[key] = value
    if native:
        canonical["__ollama_options__"] = native


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


def _finish_reason_to_done_reason(finish_reason: Any) -> str:
    if finish_reason == "length":
        return "length"
    if finish_reason == "tool_calls":
        return "tool_calls"
    if finish_reason in (None, "stop"):
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
