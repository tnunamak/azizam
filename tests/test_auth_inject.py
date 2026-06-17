"""Tests for the generic, data-driven downstream auth injection.

apply_auth_inject replaced the hardcoded apply_tabby_auth (the last
`if service == "tabby"` auth branch). These tests pin the exact behaviors the
old code had — path-conditional admin/api keys, env fallback, first-match-wins —
plus the generic mechanism, all driven by `auth_inject` DATA with no engine name.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


# Mirrors the seeded tabby auth_inject (proxy_config.yml) — the data that
# reproduces the old apply_tabby_auth behavior.
TABBY_RULES = [
    {"paths": ["/v1/models", "/v1/model/list"], "header": "x-admin-key",
     "secret": "admin_key", "secret_env": "TABBY_ADMIN_KEY"},
    {"header": "x-api-key", "secret": "api_key", "secret_env": "TABBY_API_KEY"},
]


class TestApplyAuthInject(unittest.TestCase):
    def _inject(self, service_cfg, path):
        headers: dict = {}
        proxy.apply_auth_inject(headers, service_cfg, path)
        return headers

    # -- behavior parity with the old apply_tabby_auth ----------------------

    def test_model_list_path_gets_admin_key_from_identity_secret(self):
        cfg = {"auth_inject": TABBY_RULES, "admin_key": "ADM", "api_key": "API"}
        self.assertEqual(self._inject(cfg, "/v1/models"), {"x-admin-key": "ADM"})
        self.assertEqual(self._inject(cfg, "/v1/model/list"), {"x-admin-key": "ADM"})

    def test_non_model_list_path_gets_api_key(self):
        cfg = {"auth_inject": TABBY_RULES, "admin_key": "ADM", "api_key": "API"}
        self.assertEqual(
            self._inject(cfg, "/v1/chat/completions"), {"x-api-key": "API"}
        )

    def test_first_match_wins_admin_rule_short_circuits(self):
        # On an admin path, the api-key rule must NOT also fire.
        cfg = {"auth_inject": TABBY_RULES, "admin_key": "ADM", "api_key": "API"}
        out = self._inject(cfg, "/v1/models")
        self.assertNotIn("x-api-key", out)

    # -- env fallback (the old default=API_KEY/ADMIN_KEY behavior, now data) -

    def test_env_fallback_when_identity_has_no_secret(self):
        cfg = {"auth_inject": TABBY_RULES}  # no per-identity admin_key/api_key
        with patch.dict(os.environ, {"TABBY_ADMIN_KEY": "ENV_ADM", "TABBY_API_KEY": "ENV_API"}):
            self.assertEqual(self._inject(cfg, "/v1/models"), {"x-admin-key": "ENV_ADM"})
            self.assertEqual(self._inject(cfg, "/v1/chat"), {"x-api-key": "ENV_API"})

    def test_identity_secret_takes_precedence_over_env(self):
        cfg = {"auth_inject": TABBY_RULES, "api_key": "FROM_IDENTITY"}
        with patch.dict(os.environ, {"TABBY_API_KEY": "FROM_ENV"}):
            self.assertEqual(self._inject(cfg, "/v1/chat"), {"x-api-key": "FROM_IDENTITY"})

    def test_admin_rule_skipped_when_no_admin_secret_falls_through_to_api(self):
        # Old behavior: needs_admin but no admin key -> fall through to api key.
        cfg = {"auth_inject": TABBY_RULES, "api_key": "API"}
        with patch.dict(os.environ, {"TABBY_ADMIN_KEY": "", "TABBY_API_KEY": ""}, clear=False):
            # admin path, admin secret empty -> first rule yields no value, second matches
            out = self._inject(cfg, "/v1/models")
        self.assertEqual(out, {"x-api-key": "API"})

    # -- generic mechanism / guards -----------------------------------------

    def test_no_auth_inject_is_noop(self):
        self.assertEqual(self._inject({}, "/v1/models"), {})
        self.assertEqual(self._inject({"auth_inject": None}, "/v1/models"), {})

    def test_rule_without_paths_matches_all(self):
        cfg = {"auth_inject": [{"header": "x-key", "secret_env": "K"}]}
        with patch.dict(os.environ, {"K": "v"}):
            self.assertEqual(self._inject(cfg, "/anything"), {"x-key": "v"})
            self.assertEqual(self._inject(cfg, "/v1/models"), {"x-key": "v"})

    def test_rule_missing_header_is_skipped(self):
        cfg = {"auth_inject": [{"secret_env": "K"}, {"header": "x-ok", "secret_env": "K"}]}
        with patch.dict(os.environ, {"K": "v"}):
            self.assertEqual(self._inject(cfg, "/x"), {"x-ok": "v"})

    def test_empty_secret_does_not_inject_header(self):
        cfg = {"auth_inject": [{"header": "x-key", "secret_env": "MISSING_VAR"}]}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MISSING_VAR", None)
            self.assertEqual(self._inject(cfg, "/x"), {})

    def test_no_engine_name_required(self):
        # The mechanism is generic: a made-up backend with its own rules works.
        cfg = {"auth_inject": [{"header": "authorization", "secret_env": "MY_VLLM_KEY"}]}
        with patch.dict(os.environ, {"MY_VLLM_KEY": "Bearer xyz"}):
            self.assertEqual(self._inject(cfg, "/v1/chat"), {"authorization": "Bearer xyz"})


if __name__ == "__main__":
    unittest.main()
