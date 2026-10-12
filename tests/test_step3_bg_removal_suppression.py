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

import asyncio
import json
from unittest.mock import patch

import httpx

import proxy
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


class TestBackgroundRemovalOutboundPayload:
    """Capture the request the production helper actually sends to the LLM."""

    def test_outbound_payload_uses_kwarg_only_suppression(self) -> None:
        prompt = "A professional logo for a tech startup"
        sent: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            decision = {"remove_background": True, "confidence": 0.9, "reason": "logo"}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(decision)}}]})

        real_client = httpx.AsyncClient

        def client(*_args, **kwargs):
            return real_client(transport=httpx.MockTransport(respond), timeout=kwargs.get("timeout"))

        async def active_backend() -> str:
            return "tabby"

        async def model_for_host(_host: str) -> str:
            return "bg-decider-model"

        auth = proxy.AuthContext(identity=proxy.AuthIdentity(name="p", key_hash="sha256:p"))
        with patch.object(proxy, "IMAGE_BACKGROUND_REMOVAL_ENABLED", True), \
             patch.object(proxy, "get_active_backend", active_backend), \
             patch.dict(proxy.LLM_BACKEND_HOSTS, {"tabby": "http://127.0.0.1:9"}), \
             patch.object(proxy, "effective_llm_model_for_host", model_for_host), \
             patch.object(proxy.httpx, "AsyncClient", client):
            decision = asyncio.run(proxy.decide_image_background_removal(prompt, auth_context=auth))

        assert (decision.remove_background, decision.source) == (True, "llm")
        assert len(sent) == 1
        assert sent[0].method == "POST"
        assert str(sent[0].url) == "http://127.0.0.1:9/v1/chat/completions"
        payload = json.loads(sent[0].content)
        assert set(payload) == {
            "model", "messages", "temperature", "max_tokens", "response_format", "chat_template_kwargs",
        }
        # The kwarg is the only suppression mechanism; spelled out here so a
        # renamed constant cannot keep this assertion passing.
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        # Dict equality alone accepts 0 == False; the JSON value must be a real boolean.
        assert payload["chat_template_kwargs"]["enable_thinking"] is False
        for message in payload["messages"]:
            assert QWEN3_NO_THINK_HINT not in message["content"], message
        system, user = payload["messages"]
        assert system["role"] == "system"
        assert system["content"] == (
            "Decide whether an image generation request should have its "
            "background removed and returned as a transparent PNG. "
            "Use the user's intent, not keyword matching. Return only JSON "
            "with keys remove_background boolean, confidence number 0..1, "
            "and reason string. Choose true for reusable assets such as "
            "logos, stickers, product cutouts, icons, isolated objects for "
            "commerce, or explicitly transparent-background intent. Choose "
            "false for portraits, landscapes, scenes, paintings, context-rich "
            "images, and ordinary photos where the environment is part of the result."
        )
        assert user == {"role": "user", "content": prompt}
        assert payload["model"] == "bg-decider-model"
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 160
        assert payload["response_format"] == {"type": "json_object"}
