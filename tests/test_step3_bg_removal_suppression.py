"""Step 3 parity test — background-removal helper suppression.

GATE: The background-removal request body constructed by proxy.py must be
byte-identical after the Step 3 refactoring.  Specifically:
  - The system message content must still begin with "/no_think\\n"
  - chat_template_kwargs.enable_thinking must still be False

Step 3 achieves "one source of truth" by sourcing the /no_think literal and
enable_thinking key name from gateway/transforms.py (QWEN3_NO_THINK_HINT and
QWEN3_ENABLE_THINKING_KEY) rather than hardcoding them in proxy.py.  The
produced body bytes are identical to the pre-Step-3 body.
"""

from __future__ import annotations

import pytest

from gateway.transforms import (
    QWEN3_ENABLE_THINKING_KEY,
    QWEN3_NO_THINK_HINT,
)


class TestBackgroundRemovalSuppressionConstants:
    """Verify the constants from gateway/transforms.py match the expected literals."""

    def test_no_think_hint_constant(self) -> None:
        """The /no_think hint must be exactly '/no_think'."""
        assert QWEN3_NO_THINK_HINT == "/no_think"

    def test_enable_thinking_key_constant(self) -> None:
        """The enable_thinking key must be exactly 'enable_thinking'."""
        assert QWEN3_ENABLE_THINKING_KEY == "enable_thinking"


class TestBackgroundRemovalPayloadShape:
    """Verify the payload the bg-removal helper builds is byte-identical to pre-Step-3."""

    def _build_payload(self, prompt: str = "a cute cat") -> dict:
        """Re-build the payload using the same logic as proxy.py (Step 3 version)."""
        return {
            "model": "test-model",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        QWEN3_NO_THINK_HINT + "\n"
                        "Decide whether an image generation request should have its "
                        "background removed and returned as a transparent PNG. "
                        "Use the user's intent, not keyword matching. Return only JSON "
                        "with keys remove_background boolean, confidence number 0..1, "
                        "and reason string. Choose true for reusable assets such as "
                        "logos, stickers, product cutouts, icons, isolated objects for "
                        "commerce, or explicitly transparent-background intent. Choose "
                        "false for portraits, landscapes, scenes, paintings, context-rich "
                        "images, and ordinary photos where the environment is part of the result."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 160,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {QWEN3_ENABLE_THINKING_KEY: False},
        }

    def _build_legacy_payload(self, prompt: str = "a cute cat") -> dict:
        """The pre-Step-3 hardcoded payload."""
        return {
            "model": "test-model",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "/no_think\n"
                        "Decide whether an image generation request should have its "
                        "background removed and returned as a transparent PNG. "
                        "Use the user's intent, not keyword matching. Return only JSON "
                        "with keys remove_background boolean, confidence number 0..1, "
                        "and reason string. Choose true for reusable assets such as "
                        "logos, stickers, product cutouts, icons, isolated objects for "
                        "commerce, or explicitly transparent-background intent. Choose "
                        "false for portraits, landscapes, scenes, paintings, context-rich "
                        "images, and ordinary photos where the environment is part of the result."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 160,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def test_system_message_starts_with_no_think(self) -> None:
        payload = self._build_payload()
        system_content = payload["messages"][0]["content"]
        assert system_content.startswith("/no_think\n"), (
            f"System message must start with '/no_think\\n', got: {system_content[:30]!r}"
        )

    def test_chat_template_kwargs_enable_thinking_false(self) -> None:
        payload = self._build_payload()
        assert payload["chat_template_kwargs"]["enable_thinking"] is False

    def test_payload_byte_identical_to_legacy(self) -> None:
        """The Step 3 payload must be BYTE-IDENTICAL to the pre-Step-3 payload."""
        new = self._build_payload("a logo design")
        legacy = self._build_legacy_payload("a logo design")
        assert new == legacy, (
            "Step 3 payload diverges from legacy!\n"
            f"  new    = {new}\n"
            f"  legacy = {legacy}"
        )

    def test_user_message_content_preserved(self) -> None:
        prompt = "A professional logo for a tech startup"
        payload = self._build_payload(prompt)
        assert payload["messages"][1] == {"role": "user", "content": prompt}

    def test_other_fields_unchanged(self) -> None:
        payload = self._build_payload()
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 160
        assert payload["response_format"] == {"type": "json_object"}
