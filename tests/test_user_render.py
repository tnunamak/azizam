"""Tests for the normal-user gateway portal renderer."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.user_render import render_user_portal  # noqa: E402


class UserPortalRenderTests(unittest.TestCase):
    def test_renders_a_full_normal_user_page(self) -> None:
        html = render_user_portal(public_base_url="https://ai.vivid.fish")
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("</html>", html)
        self.assertIn("My AI Gateway", html)
        self.assertIn("/gateway/v1/capabilities", html)
        self.assertIn("/gateway/v1/media/jobs", html)
        self.assertIn("/v1/models", html)
        self.assertIn("OPENAI_BASE_URL", html)
        self.assertIn("ANTHROPIC_BASE_URL", html)
        self.assertIn("opk_...", html)

    def test_uses_normal_user_language(self) -> None:
        html = render_user_portal()
        self.assertIn("Effective Access", html)
        self.assertIn("Permissions are controlled by the operator", html)
        self.assertNotIn("Create App", html)
        self.assertNotIn("Mint runtime key", html)
        self.assertNotIn("/admin/api", html)

    def test_does_not_render_real_api_key_material(self) -> None:
        html = render_user_portal()
        self.assertNotIn("sk-secret", html)
        self.assertNotIn("proxy_api_keys.yml", html)
        self.assertNotIn("opk_real", html)
        self.assertNotIn("Bearer opk_secret", html)

    def test_html_escapes_untrusted_values(self) -> None:
        html = render_user_portal(
            title="<script>alert('title')</script>",
            public_base_url="https://gw.example</script><script>alert('base')</script>",
        )
        self.assertNotIn("<script>alert('title')", html)
        self.assertNotIn("<script>alert('base')", html)
        self.assertNotIn("</script><script>", html)
        self.assertIn("&lt;script&gt;alert", html)
        self.assertIn("\\u003c/script\\u003e", html)


if __name__ == "__main__":
    unittest.main()
