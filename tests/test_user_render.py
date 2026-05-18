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
        html = render_user_portal(
            public_base_url="https://ai.vivid.fish",
            oidc_enabled=True,
        )
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("</html>", html)
        self.assertIn("AI Gateway", html)
        self.assertIn("Sign in with OIDC", html)
        self.assertIn('/login?return_to=/', html)
        self.assertIn("/gateway/v1/capabilities", html)
        self.assertIn("/gateway/v1/media/jobs", html)
        self.assertIn("/v1/models", html)
        self.assertIn("OPENAI_BASE_URL", html)
        self.assertIn("opk_...", html)
        self.assertIn("sessionStorage", html)
        self.assertNotIn("localStorage", html)

    def test_uses_normal_user_language(self) -> None:
        html = render_user_portal()
        self.assertIn("Check access", html)
        self.assertIn("Connection settings", html)
        self.assertIn("Access", html)
        self.assertNotIn("Use your local AI stack", html)
        self.assertNotIn("Failure Meanings", html)
        self.assertNotIn("ANTHROPIC_BASE_URL", html)
        self.assertNotIn("Create App", html)
        self.assertNotIn("Mint runtime key", html)
        self.assertNotIn("/admin/api", html)

    def test_does_not_render_real_api_key_material(self) -> None:
        html = render_user_portal()
        self.assertNotIn("sk-secret", html)
        self.assertNotIn("proxy_api_keys.yml", html)
        self.assertNotIn("opk_real", html)
        self.assertNotIn("Bearer opk_secret", html)

    def test_renders_signed_in_owned_apps_without_secrets(self) -> None:
        html = render_user_portal(
            oidc_enabled=True,
            account={
                "authenticated": True,
                "user": {"email": "user@example.com", "role": "none"},
                "apps": [
                    {
                        "id": "app_1",
                        "kind": "personal",
                        "name": "user-laptop",
                        "profile_id": "prof_1",
                        "profile": {
                            "id": "prof_1",
                            "name": "Family chat",
                            "missing": False,
                        },
                        "can_mint_keys": True,
                        "active_credentials": 1,
                        "credentials": [
                            {
                                "id": "cred_1",
                                "prefix": "opk_live_abcd",
                                "display_name": "Laptop",
                                "active": True,
                            }
                        ],
                    }
                ],
            },
        )
        self.assertIn("user@example.com", html)
        self.assertIn("user-laptop", html)
        self.assertIn("Family chat", html)
        self.assertIn("data-mint-owned-key", html)
        self.assertIn("data-revoke-owned-key", html)
        self.assertIn("credential-row", html)
        self.assertNotIn('href="/admin"', html)
        self.assertNotIn(">Discovery<", html)
        self.assertNotIn(">Health<", html)
        self.assertNotIn("hash", html)

    def test_signed_in_admin_gets_admin_nav_from_user_portal(self) -> None:
        html = render_user_portal(
            oidc_enabled=True,
            account={
                "authenticated": True,
                "user": {"email": "owner@example.com", "role": "owner"},
                "apps": [],
            },
        )
        self.assertIn('href="/admin"', html)
        self.assertIn(">Admin<", html)
        self.assertIn('href="/status">Status</a>', html)
        self.assertIn("role owner", html)
        self.assertIn("No owned Apps", html)
        self.assertNotIn('<form class="actions" data-request-access>', html)
        self.assertNotIn("<button type=\"submit\">Request access</button>", html)

    def test_signed_in_user_without_apps_can_request_access(self) -> None:
        html = render_user_portal(
            oidc_enabled=True,
            account={
                "authenticated": True,
                "user": {"email": "new@example.com", "role": "none"},
                "apps": [],
            },
            csrf_token="csrf-token",
        )
        self.assertIn("Request access", html)
        self.assertIn("data-request-access", html)
        self.assertIn("x-ai-gateway-csrf", html)

    def test_signed_out_landing_uses_shared_shell_and_clear_hero(self) -> None:
        html = render_user_portal(oidc_enabled=True)
        # Shared shell chrome.
        self.assertIn('class="shell-header"', html)
        self.assertIn('class="shell-main"', html)
        self.assertIn('aria-label="AI Gateway home"', html)
        self.assertIn(">AI Gateway</span>", html)
        # Signed-out chrome has no inert section nav; operator-only links stay hidden.
        self.assertNotIn('class="shell-nav"', html)
        self.assertNotIn('href="/status"', html)
        self.assertNotIn('href="/admin"', html)
        # Hero copy points the user at the two ways to start.
        self.assertIn("Start with an account or key", html)
        self.assertIn("opk_", html)
        # No Sign out affordance when not signed in.
        self.assertNotIn(">Sign out</a>", html)

    def test_signed_in_user_sees_logout_affordance_in_shell(self) -> None:
        html = render_user_portal(
            oidc_enabled=True,
            account={
                "authenticated": True,
                "user": {"email": "user@example.com", "role": "none"},
                "apps": [],
            },
        )
        self.assertIn('href="/me/logout">Sign out</a>', html)
        self.assertNotIn('href="/">Account</a>', html)
        self.assertNotIn('href="/status"', html)
        self.assertNotIn('href="/admin"', html)
        # The actor email surfaces in the header chrome.
        self.assertIn("user@example.com", html)

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
