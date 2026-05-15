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

    def test_client_constructs(self) -> None:
        # Construction alone is a useful smoke probe; live calls live in
        # later slices once the gateway exposes a known Anthropic model.
        self.assertTrue(hasattr(self.client, "messages"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
