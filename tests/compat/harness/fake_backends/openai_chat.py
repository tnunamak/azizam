"""Canonical OpenAI Chat Completions response shapes.

The envelopes here match the public schema documented at
https://platform.openai.com/docs/api-reference/chat. They are intended
for unit tests that verify the gateway emits the right keys without
calling a real backend.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Iterable, List


def fake_chat_completion(
    *,
    model: str,
    content: str,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
) -> Dict[str, Any]:
    """Build a non-streaming Chat Completions response payload."""

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def fake_chat_completion_chunk(
    *,
    model: str,
    delta: Dict[str, Any],
    finish_reason: str | None = None,
    chunk_id: str | None = None,
    created: int | None = None,
) -> Dict[str, Any]:
    """Build a single SSE chunk for streaming Chat Completions."""

    return {
        "id": chunk_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": created if created is not None else int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def fake_chat_completion_stream(
    *,
    model: str,
    tokens: Iterable[str],
) -> List[Dict[str, Any]]:
    """Build the full chunk sequence for a streamed assistant message.

    Mirrors the canonical OpenAI sequence: an opening role delta, one
    chunk per token, and a final ``finish_reason=stop`` chunk with an
    empty delta.
    """

    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    chunks: List[Dict[str, Any]] = [
        fake_chat_completion_chunk(
            model=model,
            delta={"role": "assistant"},
            chunk_id=chunk_id,
            created=created,
        )
    ]
    for token in tokens:
        chunks.append(
            fake_chat_completion_chunk(
                model=model,
                delta={"content": token},
                chunk_id=chunk_id,
                created=created,
            )
        )
    chunks.append(
        fake_chat_completion_chunk(
            model=model,
            delta={},
            finish_reason="stop",
            chunk_id=chunk_id,
            created=created,
        )
    )
    return chunks
