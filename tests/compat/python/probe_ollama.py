"""Ollama Python SDK probe.

Skipped unless ``ollama`` is installed and the harness env is set.

Install with::

    /home/tnunamak/applications/openai-proxy/venv/bin/pip install \
        -r tests/compat/python/requirements-ollama.txt
"""

from __future__ import annotations

import os
import unittest

from tests.compat.harness import ConfigError, load_config

try:  # pragma: no cover - optional dependency
    import ollama as _ollama  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _ollama = None


@unittest.skipUnless(_ollama is not None, "ollama SDK not installed")
@unittest.skipUnless(
    os.environ.get("OPENAI_PROXY_BASE_URL"),
    "OPENAI_PROXY_BASE_URL not set; live SDK probe disabled",
)
class TestOllamaPythonSDK(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.cfg = load_config()
        except ConfigError as exc:  # pragma: no cover - env-dependent
            self.skipTest(str(exc))
        assert _ollama is not None
        self.client = _ollama.Client(
            host=self.cfg.ollama_host(),
            headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            timeout=self.cfg.request_timeout_s,
        )

    def test_client_construct(self) -> None:
        self.assertIsNotNone(self.client)
        self.assertTrue(hasattr(self.client, "list"))
        self.assertTrue(hasattr(self.client, "embed"))
        self.assertTrue(hasattr(self.client, "embeddings"))

    def test_tags_list(self) -> None:
        listing = self.client.list()
        # The Ollama SDK has shifted between dict and pydantic shapes
        # across versions, so we tolerate both.
        models = listing.get("models") if isinstance(listing, dict) else getattr(listing, "models", None)
        self.assertIsNotNone(models)

    def test_embed_current_route(self) -> None:
        result = self.client.embed(
            model=os.environ.get("OPENAI_PROXY_SMOKE_OLLAMA_EMBED_MODEL", "nomic-embed-text-v1.5"),
            input="compatibility probe",
        )
        embeddings = result.get("embeddings") if isinstance(result, dict) else getattr(result, "embeddings", None)
        self.assertIsInstance(embeddings, list)
        self.assertGreater(len(embeddings), 0)
        self.assertIsInstance(embeddings[0], list)
        self.assertGreater(len(embeddings[0]), 0)
        self.assertIsInstance(embeddings[0][0], float)

    def test_embeddings_legacy_route(self) -> None:
        result = self.client.embeddings(
            model=os.environ.get("OPENAI_PROXY_SMOKE_OLLAMA_EMBED_MODEL", "nomic-embed-text-v1.5"),
            prompt="compatibility probe",
        )
        embedding = result.get("embedding") if isinstance(result, dict) else getattr(result, "embedding", None)
        self.assertIsInstance(embedding, list)
        self.assertGreater(len(embedding), 0)
        self.assertIsInstance(embedding[0], float)

    def test_show_model(self) -> None:
        result = self.client.show(self._model())
        details = _field(result, "details")
        self.assertIsNotNone(details)

    def test_ps_list(self) -> None:
        result = self.client.ps()
        models = _field(result, "models")
        self.assertIsInstance(models, list)

    def test_chat_nonstream(self) -> None:
        self._skip_without_live_llm()
        result = self.client.chat(
            model=self._model(),
            messages=[{"role": "user", "content": "compatibility probe"}],
            stream=False,
            options={"num_predict": 8},
        )
        message = _field(result, "message")
        self.assertIsNotNone(message)
        content = _field(message, "content")
        self.assertIsInstance(content, str)

    def test_chat_stream(self) -> None:
        self._skip_without_live_llm()
        chunks = self.client.chat(
            model=self._model(),
            messages=[{"role": "user", "content": "compatibility probe"}],
            stream=True,
            options={"num_predict": 8},
        )
        text = ""
        done_seen = False
        for chunk in chunks:
            message = _field(chunk, "message")
            if message is not None:
                text += str(_field(message, "content") or "")
            done_seen = bool(_field(chunk, "done") or done_seen)
            if text and done_seen:
                break
        self.assertGreater(len(text), 0)
        self.assertTrue(done_seen)

    def test_generate_nonstream(self) -> None:
        self._skip_without_live_llm()
        result = self.client.generate(
            model=self._model(),
            prompt="compatibility probe",
            stream=False,
            options={"num_predict": 8},
        )
        response = _field(result, "response")
        self.assertIsInstance(response, str)

    def test_generate_stream(self) -> None:
        self._skip_without_live_llm()
        chunks = self.client.generate(
            model=self._model(),
            prompt="compatibility probe",
            stream=True,
            options={"num_predict": 8},
        )
        text = ""
        done_seen = False
        for chunk in chunks:
            text += str(_field(chunk, "response") or "")
            done_seen = bool(_field(chunk, "done") or done_seen)
            if text and done_seen:
                break
        self.assertGreater(len(text), 0)
        self.assertTrue(done_seen)

    def _model(self) -> str:
        return os.environ.get("OPENAI_PROXY_SMOKE_OLLAMA_MODEL", "default")

    def _skip_without_live_llm(self) -> None:
        if os.environ.get("OPENAI_PROXY_SMOKE_LIVE_LLM") != "1":
            self.skipTest("OPENAI_PROXY_SMOKE_LIVE_LLM not set; live LLM probe disabled")


def _field(value, name: str):
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
