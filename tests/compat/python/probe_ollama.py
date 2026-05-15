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
            self.cfg = load_config(allow_anonymous=True)
        except ConfigError as exc:  # pragma: no cover - env-dependent
            self.skipTest(str(exc))
        assert _ollama is not None
        self.client = _ollama.Client(host=self.cfg.ollama_host())

    def test_client_construct(self) -> None:
        self.assertIsNotNone(self.client)
        self.assertTrue(hasattr(self.client, "list"))

    def test_tags_list(self) -> None:
        listing = self.client.list()
        # The Ollama SDK has shifted between dict and pydantic shapes
        # across versions, so we tolerate both.
        models = listing.get("models") if isinstance(listing, dict) else getattr(listing, "models", None)
        self.assertIsNotNone(models)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
