"""OpenAI Python SDK probe.

Skipped by default unless the optional ``openai`` package is installed
and ``OPENAI_PROXY_BASE_URL``/``OPENAI_PROXY_API_KEY`` are set.

Install with::

    /home/tnunamak/applications/openai-proxy/venv/bin/pip install \
        -r tests/compat/python/requirements-openai.txt

Run with::

    /home/tnunamak/applications/openai-proxy/venv/bin/python \
        -m unittest tests.compat.python.probe_openai -v
"""

from __future__ import annotations

import os
import unittest

from tests.compat.harness import ConfigError, load_config

try:  # pragma: no cover - optional dependency
    import openai as _openai  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only without the SDK
    _openai = None


@unittest.skipUnless(_openai is not None, "openai SDK not installed")
@unittest.skipUnless(
    os.environ.get("OPENAI_PROXY_BASE_URL"),
    "OPENAI_PROXY_BASE_URL not set; live SDK probe disabled",
)
class TestOpenAIPythonSDK(unittest.TestCase):
    """Live SDK probe.

    Each case here should add a corresponding entry in the emitted
    compatibility matrix when wired into ``scripts/compat/run_matrix.py``.
    """

    def setUp(self) -> None:
        try:
            self.cfg = load_config()
        except ConfigError as exc:  # pragma: no cover - env-dependent
            self.skipTest(str(exc))
        assert _openai is not None
        self.client = _openai.OpenAI(
            base_url=self.cfg.openai_base_url(),
            api_key=self.cfg.api_key or "anonymous",
            timeout=self.cfg.request_timeout_s,
        )

    def test_client_construct(self) -> None:
        self.assertTrue(hasattr(self.client, "models"))
        self.assertTrue(hasattr(self.client, "chat"))
        self.assertTrue(hasattr(self.client, "embeddings"))

    def test_models_list(self) -> None:
        models = self.client.models.list()
        self.assertTrue(hasattr(models, "data"))
        self.assertIsInstance(list(models.data), list)

    def test_embeddings_create(self) -> None:
        result = self.client.embeddings.create(
            model=os.environ.get(
                "OPENAI_PROXY_SMOKE_EMBEDDINGS_MODEL",
                "nomic-embed-text-v1.5",
            ),
            input="compatibility probe",
        )
        self.assertEqual(result.object, "list")
        self.assertGreater(len(result.data), 0)
        embedding = result.data[0].embedding
        self.assertIsInstance(embedding, list)
        self.assertGreater(len(embedding), 0)
        self.assertIsInstance(embedding[0], float)

    def _require_live_llm(self) -> None:
        if os.environ.get("OPENAI_PROXY_SMOKE_LIVE_LLM") != "1":
            self.skipTest("OPENAI_PROXY_SMOKE_LIVE_LLM=1 not set; live LLM probe disabled")

    def _chat_model(self) -> str:
        return os.environ.get("OPENAI_PROXY_SMOKE_OPENAI_MODEL", "default")

    def test_chat_completions_create(self) -> None:
        self._require_live_llm()
        result = self.client.chat.completions.create(
            model=self._chat_model(),
            messages=[
                {
                    "role": "user",
                    "content": "Reply with exactly: compatibility probe",
                }
            ],
            temperature=0,
        )
        self.assertGreater(len(result.choices), 0)
        content = result.choices[0].message.content
        self.assertIsInstance(content, str)
        self.assertTrue(content.strip())

    def test_chat_completions_streaming(self) -> None:
        self._require_live_llm()
        chunks = self.client.chat.completions.create(
            model=self._chat_model(),
            messages=[
                {
                    "role": "user",
                    "content": "Reply with exactly: compatibility probe",
                }
            ],
            stream=True,
            temperature=0,
        )
        content_parts = []
        for chunk in chunks:
            self.assertGreater(len(chunk.choices), 0)
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                content_parts.append(content)
        self.assertTrue("".join(content_parts).strip())

    def test_chat_completions_tool_call_envelope(self) -> None:
        """SDK must accept a function-tool request and parse the tool_calls envelope."""
        self._require_live_llm()
        result = self.client.chat.completions.create(
            model=self._chat_model(),
            messages=[
                {
                    "role": "user",
                    "content": "Call the function get_weather for San Francisco.",
                }
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the current weather for a city.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "city": {"type": "string"},
                            },
                            "required": ["city"],
                        },
                    },
                }
            ],
            tool_choice="auto",
            temperature=0,
        )
        # The SDK successfully parsed the response envelope. We don't require the
        # model to actually emit a tool call (that's a model-quality concern),
        # only that when it does, the envelope is OpenAI-shaped.
        choice = result.choices[0]
        tool_calls = getattr(choice.message, "tool_calls", None)
        if tool_calls:
            call = tool_calls[0]
            self.assertEqual(getattr(call, "type", None), "function")
            self.assertTrue(getattr(call, "id", None))
            fn = getattr(call, "function", None)
            self.assertIsNotNone(fn)
            self.assertTrue(getattr(fn, "name", None))
            # SDK requires arguments to be a JSON string.
            self.assertIsInstance(getattr(fn, "arguments", None), str)

    def test_chat_completions_json_response_format(self) -> None:
        """SDK must accept response_format=json_object and parse the result."""
        self._require_live_llm()
        result = self.client.chat.completions.create(
            model=self._chat_model(),
            messages=[
                {
                    "role": "system",
                    "content": "Reply with a JSON object only.",
                },
                {
                    "role": "user",
                    "content": 'Return the JSON object {"ok": true}.',
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = result.choices[0].message.content
        self.assertIsInstance(content, str)
        self.assertTrue(content.strip())

    def test_chat_completions_image_input_block(self) -> None:
        """SDK accepts image_url content blocks; gateway must not reject the shape.

        Vision-only opt-in: text-only local backends would 4xx on the image
        content block and that isn't a gateway-shape failure, so this case
        requires ``OPENAI_PROXY_SMOKE_LIVE_VISION=1`` in addition to the
        normal live LLM gate.
        """
        self._require_live_llm()
        if os.environ.get("OPENAI_PROXY_SMOKE_LIVE_VISION") != "1":
            self.skipTest(
                "OPENAI_PROXY_SMOKE_LIVE_VISION=1 not set; "
                "vision probe disabled (text-only backends would 4xx the image block)"
            )
        # Smallest possible PNG (1x1 transparent), embedded as a data URL so this
        # probe stays dependency-free and never hits the network for the image.
        tiny_png_data_url = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        result = self.client.chat.completions.create(
            model=os.environ.get(
                "OPENAI_PROXY_SMOKE_VISION_MODEL",
                self._chat_model(),
            ),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image in one word."},
                        {
                            "type": "image_url",
                            "image_url": {"url": tiny_png_data_url},
                        },
                    ],
                }
            ],
            temperature=0,
            max_tokens=16,
        )
        # The SDK round-trip succeeded; that proves the gateway accepted the
        # image_url content block shape on the request.
        self.assertGreater(len(result.choices), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
