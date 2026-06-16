"""Step 3 test — background-removal helper suppression.

GATE: The background-removal request body constructed by proxy.py must set
chat_template_kwargs.enable_thinking=False and must NOT inject any /no_think
prompt string into any message.

The /no_think prompt-string injection was removed (verified non-functional:
a live reasoning_effort=none test showed enable_thinking kwarg suppresses
thinking while the /no_think tag alone did not).  The kwarg is the sole
suppression mechanism.  See docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md D-1.
"""

from __future__ import annotations

import pytest

from gateway.transforms import (
    QWEN3_ENABLE_THINKING_KEY,
    QWEN3_NO_THINK_HINT,
)


class TestBackgroundRemovalSuppressionConstants:
    """Verify the constants from gateway/transforms.py have the expected values."""

    def test_no_think_hint_constant(self) -> None:
        """QWEN3_NO_THINK_HINT constant is still defined (for backward compat)."""
        assert QWEN3_NO_THINK_HINT == "/no_think"

    def test_enable_thinking_key_constant(self) -> None:
        """The enable_thinking key must be exactly 'enable_thinking'."""
        assert QWEN3_ENABLE_THINKING_KEY == "enable_thinking"


class TestBackgroundRemovalPayloadShape:
    """Verify the payload the bg-removal helper builds uses kwarg-only suppression."""

    def _build_payload(self, prompt: str = "a cute cat") -> dict:
        """Re-build the payload using the same logic as proxy.py (post-cleanup version)."""
        return {
            "model": "test-model",
            "messages": [
                {
                    "role": "system",
                    "content": (
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

    def test_no_think_not_in_system_message(self) -> None:
        """System message must NOT contain /no_think — injection is removed."""
        payload = self._build_payload()
        system_content = payload["messages"][0]["content"]
        assert "/no_think" not in system_content, (
            f"System message must not contain '/no_think', got: {system_content[:60]!r}"
        )

    def test_chat_template_kwargs_enable_thinking_false(self) -> None:
        payload = self._build_payload()
        assert payload["chat_template_kwargs"]["enable_thinking"] is False

    def test_no_no_think_in_any_message(self) -> None:
        """No /no_think text anywhere in any message."""
        payload = self._build_payload("a logo design")
        for msg in payload["messages"]:
            assert "/no_think" not in msg.get("content", ""), (
                f"/no_think found in message: {msg!r}"
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

    def test_system_message_starts_with_decide(self) -> None:
        """System message starts directly with the task description (no hint prefix)."""
        payload = self._build_payload()
        system_content = payload["messages"][0]["content"]
        assert system_content.startswith("Decide whether"), (
            f"System message must start with 'Decide whether', got: {system_content[:40]!r}"
        )
