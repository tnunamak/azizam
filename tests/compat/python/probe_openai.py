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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
