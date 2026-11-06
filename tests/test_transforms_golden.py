"""Golden-replay harness for the transform engine (Step 0 + Step 1).

PURPOSE
-------
Step 0 — Empty-pipeline no-op proof.
    Running any representative request/response body through an empty transform
    pipeline must produce byte-identical output (deep-equal dicts).  This is
    the correctness gate that every subsequent migration step must also pass for
    all bodies it does NOT intend to change.

Speech voices are engine data now; tests/test_engine_p2_differential.py proves
the requests they produce.

The harness is intentionally shape-stable so later increments can add more
fixture bodies without changing the invariant assertions.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from gateway.transforms import (
    TransformContext,
    TransformRegistry,
    run_request_pipeline,
    run_response_pipeline,
)

# ---------------------------------------------------------------------------
# Fixtures — representative bodies the gateway currently processes
# ---------------------------------------------------------------------------

# A typical chat-completion request body
CHAT_REQUEST: dict[str, Any] = {
    "model": "qwen3",
    "messages": [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ],
    "temperature": 0.7,
    "stream": True,
}

# A typical chat-completion response (non-streaming)
CHAT_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-abc",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hi there!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# A TTS request body
TTS_REQUEST: dict[str, Any] = {
    "model": "tts-1",
    "input": "Hello world",
    "voice": "alloy",
    "response_format": "wav",
}

REPRESENTATIVE_BODIES: list[tuple[str, dict[str, Any]]] = [
    ("chat_request", CHAT_REQUEST),
    ("chat_response", CHAT_RESPONSE),
    ("tts_request", TTS_REQUEST),
]

# ---------------------------------------------------------------------------
# Step 0 — Empty-pipeline no-op proof
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,body", REPRESENTATIVE_BODIES)
def test_empty_request_pipeline_is_noop(label: str, body: dict[str, Any]) -> None:
    """Running an empty pipeline must return byte-identical (deep-equal) output."""
    original = copy.deepcopy(body)
    result = run_request_pipeline([], copy.deepcopy(body))
    assert result == original, f"[{label}] Empty request pipeline mutated body"


@pytest.mark.parametrize("label,body", REPRESENTATIVE_BODIES)
def test_empty_response_pipeline_is_noop(label: str, body: dict[str, Any]) -> None:
    """Running an empty pipeline on response path must return byte-identical output."""
    original = copy.deepcopy(body)
    result = run_response_pipeline([], copy.deepcopy(body))
    assert result == original, f"[{label}] Empty response pipeline mutated body"
