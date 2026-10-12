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
from unittest.mock import patch

import proxy
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401
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

    def test_outbound_payload_uses_kwarg_only_suppression(self, fake_upstreams) -> None:
        prompt = "A professional logo for a tech startup"
        fake = fake_upstreams.asgi(load_scenario("openai-compatible/json-decision", model="bg-decider-model"), host="bg.fk.test")

        async def active_backend() -> str:
            return "kobold"

        async def model_for_host(_host: str) -> str:
            return "bg-decider-model"

        auth = proxy.AuthContext(identity=proxy.AuthIdentity(name="p", key_hash="sha256:p"))
        with patch.object(proxy, "IMAGE_BACKGROUND_REMOVAL_ENABLED", True), \
             patch.object(proxy, "get_active_backend", active_backend), \
             patch.dict(proxy.LLM_BACKEND_HOSTS, {"kobold": fake.base_url}), \
             patch.object(proxy, "effective_llm_model_for_host", model_for_host):
            decision = asyncio.run(proxy.decide_image_background_removal(prompt, auth_context=auth))

        assert (decision.remove_background, decision.source) == (True, "llm")
        [sent] = fake.calls("chat")
        assert (sent.method, sent.path) == ("POST", "/v1/chat/completions")
        payload = sent.json
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


class TestBackgroundRemovalGateIsLifecycleData:
    """The active backend qualifies by its swap lifecycle driver, never by its id."""

    def _decide(self, base_url: str, service_id: str, lifecycle: dict) -> "proxy.ImageBackgroundRemovalDecision":
        async def active_backend() -> str:
            return service_id

        async def model_for_host(_host: str) -> str:
            return "bg-decider-model"

        auth = proxy.AuthContext(identity=proxy.AuthIdentity(name="p", key_hash="sha256:p"))
        backends = {**proxy.config.get("backends", {}), service_id: {"lifecycle": lifecycle}}
        with patch.object(proxy, "IMAGE_BACKGROUND_REMOVAL_ENABLED", True), \
             patch.object(proxy, "get_active_backend", active_backend), \
             patch.dict(proxy.config, {"backends": backends}), \
             patch.dict(proxy.LLM_BACKEND_HOSTS, {service_id: base_url}), \
             patch.object(proxy, "effective_llm_model_for_host", model_for_host):
            return asyncio.run(proxy.decide_image_background_removal("A logo", auth_context=auth))

    def test_new_service_id_with_a_swap_driver_is_asked(self, fake_upstreams) -> None:
        fake = fake_upstreams.asgi(load_scenario("openai-compatible/json-decision", model="bg-decider-model"), host="bg2.fk.test")
        decision = self._decide(fake.base_url, "brand-new-llm", {"driver": "systemd", "driver_config": {"unit": "x.service"}})
        assert (decision.remove_background, decision.source) == (True, "llm")

    def test_a_service_without_a_swap_driver_is_not_asked_whatever_its_id(self) -> None:
        # No upstream exists: a request to this address would fail as "llm_error".
        decision = self._decide("http://127.0.0.1:9", "kobold", {"driver": "external"})
        assert decision.source == "llm_unavailable"
