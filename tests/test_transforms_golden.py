"""Golden-replay harness for the transform engine (Step 0 + Step 1).

PURPOSE
-------
Step 0 — Empty-pipeline no-op proof.
    Running any representative request/response body through an empty transform
    pipeline must produce byte-identical output (deep-equal dicts).  This is
    the correctness gate that every subsequent migration step must also pass for
    all bodies it does NOT intend to change.

Step 1 — voxtral-voices bit-identical proof.
    Every key in VOICE_MAP + one unmapped voice routed through the
    voxtral-voices transform must produce the IDENTICAL downstream `voice`
    value as the old ``VOICE_MAP.get(requested_voice, DEFAULT_VOICE)`` did.

The harness is intentionally shape-stable so later increments can add more
fixture bodies without changing the invariant assertions.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from gateway.transforms import (
    REGISTRY,
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


# ---------------------------------------------------------------------------
# Step 1 — voxtral-voices bit-identical proof
# ---------------------------------------------------------------------------

# Hard-coded ground truth for the TTS voice map (OpenAI-named canonical voices,
# legacy aliases, language presets).  Kept independent of gateway/transforms.py
# so an accidental edit there fails here.  Canonical-voice rules live in
# tests/test_tts_voice_catalog.py.
_LEGACY_VOICE_MAP: dict[str, str] = {
    # Canonical OpenAI voices
    **{v: v for v in (
        "alloy", "ash", "ballad", "cedar", "coral", "echo", "fable",
        "marin", "nova", "onyx", "sage", "shimmer", "verse",
    )},
    # Legacy aliases -> closest canonical voice
    "neutral_female": "alloy",
    "casual_female": "coral",
    "cheerful_female": "shimmer",
    "neutral_male": "ash",
    "casual_male": "echo",
    "sophia": "alloy",
    "isabella": "shimmer",
    "lily": "coral",
    "juniper": "coral",
    "evan": "echo",
    "adam": "ash",
    # Language voice-design presets (pass through)
    **{f"{lang}_{g}": f"{lang}_{g}" for lang in ("ar", "de", "es", "fr", "hi", "it", "nl", "pt") for g in ("female", "male")},
}
_LEGACY_DEFAULT_VOICE = "alloy"


def _legacy_map_voice(requested_voice: str) -> str:
    """Old VOICE_MAP.get() logic from proxy.py:3398-3399."""
    return _LEGACY_VOICE_MAP.get(requested_voice, _LEGACY_DEFAULT_VOICE)


def _transform_map_voice(requested_voice: str) -> str:
    """New path: run voxtral-voices transform on a minimal TTS body."""
    body = {"voice": requested_voice}
    result = run_request_pipeline(["voxtral-voices"], body)
    return result["voice"]


@pytest.mark.parametrize("voice_key", sorted(_LEGACY_VOICE_MAP.keys()))
def test_voxtral_voices_matches_legacy_for_all_known_keys(voice_key: str) -> None:
    """Every VOICE_MAP key through the transform yields the same downstream voice."""
    legacy = _legacy_map_voice(voice_key)
    new = _transform_map_voice(voice_key)
    assert new == legacy, (
        f"voxtral-voices mismatch for voice={voice_key!r}: "
        f"legacy={legacy!r} transform={new!r}"
    )


def test_voxtral_voices_unmapped_yields_default() -> None:
    """An unmapped voice name still yields the default voice (alloy)."""
    unmapped = "completely_unknown_voice"
    legacy = _legacy_map_voice(unmapped)
    new = _transform_map_voice(unmapped)
    assert legacy == _LEGACY_DEFAULT_VOICE
    assert new == _LEGACY_DEFAULT_VOICE
    assert new == legacy


def test_voxtral_voices_empty_string_yields_default() -> None:
    """Empty string (the body.get('voice', '').lower() case) yields default."""
    # The application site does: requested_voice = body_json.get("voice", "").lower()
    # An empty string is not in VOICE_MAP, so must fall back to DEFAULT_VOICE.
    legacy = _legacy_map_voice("")
    new = _transform_map_voice("")
    assert new == legacy == _LEGACY_DEFAULT_VOICE


def test_voxtral_voices_is_registered_in_registry() -> None:
    """voxtral-voices must be resolvable from the module-level REGISTRY."""
    primitives = REGISTRY.resolve("voxtral-voices")
    assert len(primitives) >= 1


def test_voxtral_voices_does_not_touch_other_fields() -> None:
    """The transform must not modify any fields besides 'voice'."""
    body = {
        "model": "tts-1",
        "input": "Hello",
        "voice": "alloy",
        "response_format": "wav",
    }
    original_without_voice = {k: v for k, v in body.items() if k != "voice"}
    result = run_request_pipeline(["voxtral-voices"], copy.deepcopy(body))
    result_without_voice = {k: v for k, v in result.items() if k != "voice"}
    assert result_without_voice == original_without_voice


def test_voxtral_voices_noop_on_response_phase() -> None:
    """voxtral-voices is request-phase; running it through response pipeline is a no-op."""
    body = {"voice": "alloy", "model": "tts-1"}
    original = copy.deepcopy(body)
    result = run_response_pipeline(["voxtral-voices"], copy.deepcopy(body))
    assert result == original, "voxtral-voices must not mutate response-phase bodies"
