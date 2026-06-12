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
        images = msg.get("images")
        canonical_messages.append(
            {
                "role": role,
                "content": _content_with_images(
                    content,
                    images,
                    f"messages[{i}].images",
                ),
            }
        )

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
        "content": _visible_text_from_message(message),
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


def canonical_stream_chunk_to_chat(
    canonical: dict[str, Any], *, request_model: str | None = None
) -> dict[str, Any]:
    """Translate a canonical chat-completions stream chunk to ``/api/chat``."""
    choice = _first_choice(canonical)
    delta = choice.get("delta") or {}
    out_message: dict[str, Any] = {
        "role": delta.get("role") or "assistant",
        "content": _visible_text_from_message(delta),
    }

    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        out_message["tool_calls"] = [
            _openai_tool_call_to_ollama(tc) for tc in tool_calls
        ]

    finish_reason = choice.get("finish_reason")
    done = finish_reason is not None
    response: dict[str, Any] = {
        "model": canonical.get("model") or request_model or "",
        "created_at": _iso_utc_now(),
        "message": out_message,
        "done": done,
    }
    if done:
        response["done_reason"] = _finish_reason_to_done_reason(finish_reason)
        _apply_usage_counts(response, canonical.get("usage") or {})
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
    text = _visible_text_from_message(message)

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


def canonical_stream_chunk_to_generate(
    canonical: dict[str, Any], *, request_model: str | None = None
) -> dict[str, Any]:
    """Translate a canonical chat-completions stream chunk to ``/api/generate``."""
    choice = _first_choice(canonical)
    delta = choice.get("delta") or {}
    finish_reason = choice.get("finish_reason")
    done = finish_reason is not None
    response: dict[str, Any] = {
        "model": canonical.get("model") or request_model or "",
        "created_at": _iso_utc_now(),
        "response": _visible_text_from_message(delta),
        "done": done,
    }
    if done:
        response["done_reason"] = _finish_reason_to_done_reason(finish_reason)
        _apply_usage_counts(response, canonical.get("usage") or {})
    return response


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
    if isinstance(body.get("truncate"), bool):
        canonical["__ollama_truncate__"] = body["truncate"]
    if isinstance(body.get("keep_alive"), (str, int, float)):
        canonical["__ollama_keep_alive__"] = body["keep_alive"]
    if isinstance(body.get("options"), dict) and body["options"]:
        canonical["__ollama_options__"] = dict(body["options"])
    return canonical


def embeddings_request_to_canonical(body: dict[str, Any]) -> dict[str, Any]:
    """Translate legacy Ollama ``/api/embeddings`` to OpenAI embeddings shape."""
    if not isinstance(body, dict):
        raise OllamaTranslationError("request body must be a JSON object")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise OllamaTranslationError("'model' is required")

    prompt = body.get("prompt")
    if not isinstance(prompt, str):
        raise OllamaTranslationError("'prompt' must be a string")

    canonical: dict[str, Any] = {
        "model": model,
        "input": prompt,
    }
    if isinstance(body.get("options"), dict) and body["options"]:
        canonical["__ollama_options__"] = dict(body["options"])
    return canonical


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
        "capabilities": summary.get("capabilities") or ["completion"],
    }


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
    return {"version": version or "openai-proxy"}


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

_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)

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


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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


def _visible_text_from_message(message: Mapping[str, Any]) -> str:
    text = message.get("content")
    if isinstance(text, str) and text:
        return text
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        lower = reasoning.lower()
        close = lower.rfind("</think>")
        if close >= 0:
            visible = reasoning[close + len("</think>") :].strip()
            if visible:
                return visible
        return reasoning
    return ""


def _apply_usage_counts(response: dict[str, Any], usage: Mapping[str, Any]) -> None:
    response["prompt_eval_count"] = int(usage.get("prompt_tokens") or 0)
    response["eval_count"] = int(usage.get("completion_tokens") or 0)


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
        "format": entry.get("format", "gguf"),
        "family": family,
        "families": entry.get("families") or ([family] if family else []),
        "parameter_size": entry.get("parameter_size", ""),
        "quantization_level": entry.get("quantization_level", ""),
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
