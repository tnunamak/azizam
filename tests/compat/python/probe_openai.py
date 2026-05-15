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

    def test_models_list(self) -> None:
        models = self.client.models.list()
        self.assertTrue(hasattr(models, "data"))
        self.assertIsInstance(list(models.data), list)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
