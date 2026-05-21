"""Anthropic Python SDK probe.

Skipped unless ``anthropic`` is installed and the harness env is set.

Install with::

    /home/tnunamak/applications/openai-proxy/venv/bin/pip install \
        -r tests/compat/python/requirements-anthropic.txt
"""

from __future__ import annotations

import os
import unittest

from tests.compat.harness import ConfigError, load_config

try:  # pragma: no cover - optional dependency
    import anthropic as _anthropic  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _anthropic = None


TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAF"
    "gwJ/l6p7YQAAAABJRU5ErkJggg=="
)


@unittest.skipUnless(_anthropic is not None, "anthropic SDK not installed")
@unittest.skipUnless(
    os.environ.get("OPENAI_PROXY_BASE_URL"),
    "OPENAI_PROXY_BASE_URL not set; live SDK probe disabled",
)
class TestAnthropicPythonSDK(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.cfg = load_config()
        except ConfigError as exc:  # pragma: no cover - env-dependent
            self.skipTest(str(exc))
        assert _anthropic is not None
        self.client = _anthropic.Anthropic(
            base_url=self.cfg.anthropic_base_url(),
            api_key=self.cfg.api_key or "anonymous",
            timeout=self.cfg.request_timeout_s,
        )

    def test_client_construct(self) -> None:
        self.assertTrue(hasattr(self.client, "messages"))
        self.assertTrue(hasattr(self.client, "models"))

    def test_models_list(self) -> None:
        models = self.client.models.list(limit=20)
        data = getattr(models, "data", None)
        self.assertIsInstance(data, list)

    def test_messages_count_tokens(self) -> None:
        result = self.client.messages.count_tokens(
            model=self._model(),
            messages=[{"role": "user", "content": "compatibility probe"}],
        )
        input_tokens = getattr(result, "input_tokens", None)
        self.assertIsInstance(input_tokens, int)
        self.assertGreater(input_tokens, 0)

    def test_messages_count_tokens_image_block(self) -> None:
        result = self.client.messages.count_tokens(
            model=self._model(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        self._tiny_png_image_block(),
                        {"type": "text", "text": "compatibility probe"},
                    ],
                }
            ],
        )
        input_tokens = getattr(result, "input_tokens", None)
        self.assertIsInstance(input_tokens, int)
        self.assertGreater(input_tokens, 0)

    def test_messages_create(self) -> None:
        self._skip_without_live_llm()
        message = self.client.messages.create(
            model=self._model(),
            max_tokens=8,
            messages=[{"role": "user", "content": "compatibility probe"}],
        )
        self.assertEqual(getattr(message, "type", None), "message")
        content = getattr(message, "content", None)
        self.assertIsInstance(content, list)
        self.assertGreater(len(content), 0)
        first = content[0]
        text = getattr(first, "text", None)
        if text is None and isinstance(first, dict):
            text = first.get("text")
        self.assertIsInstance(text, str)
        self.assertGreater(len(text), 0)

    def test_messages_create_image_block(self) -> None:
        self._skip_without_live_vision()
        message = self.client.messages.create(
            model=self._model(),
            max_tokens=8,
            messages=[
                {
                    "role": "user",
                    "content": [
                        self._tiny_png_image_block(),
                        {"type": "text", "text": "Describe this image in one word."},
                    ],
                }
            ],
        )
        self.assertEqual(getattr(message, "type", None), "message")
        content = getattr(message, "content", None)
        self.assertIsInstance(content, list)
        self.assertGreater(len(content), 0)

    def test_messages_streaming(self) -> None:
        self._skip_without_live_llm()
        text_chunks: list[str] = []
        with self.client.messages.stream(
            model=self._model(),
            max_tokens=8,
            messages=[{"role": "user", "content": "compatibility probe"}],
        ) as stream:
            for text in stream.text_stream:
                if text:
                    text_chunks.append(str(text))
                if len("".join(text_chunks)) >= 1:
                    break
        self.assertGreater(len("".join(text_chunks)), 0)

    def _model(self) -> str:
        return os.environ.get("OPENAI_PROXY_SMOKE_ANTHROPIC_MODEL", "default")

    def _skip_without_live_llm(self) -> None:
        if os.environ.get("OPENAI_PROXY_SMOKE_LIVE_LLM") != "1":
            self.skipTest("OPENAI_PROXY_SMOKE_LIVE_LLM not set; live LLM probe disabled")

    def _skip_without_live_vision(self) -> None:
        if os.environ.get("OPENAI_PROXY_SMOKE_LIVE_VISION") != "1":
            self.skipTest(
                "OPENAI_PROXY_SMOKE_LIVE_VISION not set; live vision probe disabled"
            )

    def _tiny_png_image_block(self) -> dict[str, object]:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": TINY_PNG_BASE64,
            },
        }


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
