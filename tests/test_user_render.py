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
        self.assertIn("Continue with vivid.fish ID", html)
        self.assertIn('/login?return_to=/', html)
        self.assertIn("/gateway/v1/capabilities", html)
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
        self.assertIn("Point an OpenAI-compatible App or SDK tool at the gateway.", html)
        self.assertNotIn("Use your local AI stack", html)
        self.assertNotIn("Failure Meanings", html)
        self.assertNotIn("ANTHROPIC_BASE_URL", html)
        self.assertNotIn("OpenAI-compatible client", html)
        self.assertNotIn("Mint runtime key", html)
        self.assertNotIn("Copy this API key now", html)

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
        self.assertNotIn("Family chat", html)
        self.assertIn("account permissions", html)
        self.assertIn("base-url-display", html)
        self.assertIn("copy-base-url", html)
        self.assertIn("data-mint-owned-key", html)
        self.assertIn("data-revoke-owned-key", html)
        self.assertIn("Create key", html)
        self.assertIn("Delete key", html)
        self.assertIn("Delete App", html)
        self.assertIn("View access", html)
        self.assertIn("Create one App per tool, workflow, or script", html)
        self.assertIn("Its keys stay inside that App", html)
        self.assertNotIn("Create one App per client", html)
        self.assertIn("Copy key", html)
        self.assertIn("Copy this opk_ App API key now", html)
        self.assertIn("only for Apps and SDK tools", html)
        self.assertNotIn("Copy this API key now", html)
        self.assertIn("key-row", html)
        self.assertIn("/me/api/credentials/", html)
        self.assertNotIn("location.reload", html)
        self.assertNotIn('href="/admin"', html)
        self.assertNotIn(">Discovery<", html)
        self.assertNotIn(">Health<", html)
        self.assertNotIn("hash", html)

    def test_runtime_key_check_rejects_admin_automation_keys(self) -> None:
        html = render_user_portal()
        self.assertIn('startsWith("oak_")', html)
        self.assertIn(
            "oak_ admin automation keys are for /admin/api and /status, not runtime access",
            html,
        )
        self.assertIn("Paste an opk_ App API key instead.", html)

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
        self.assertNotIn("role owner", html)
        self.assertIn("permission Profiles", html)
        self.assertIn("Create an App", html)
        self.assertNotIn('<form class="actions" data-request-access>', html)
        self.assertNotIn("<button type=\"submit\">Request access</button>", html)

    def test_signed_in_user_without_apps_can_create_apps(self) -> None:
        html = render_user_portal(
            oidc_enabled=True,
            account={
                "authenticated": True,
                "user": {"email": "new@example.com", "role": "none"},
                "apps": [],
            },
            csrf_token="csrf-token",
        )
        self.assertIn("Create an App", html)
        self.assertIn("Open WebUI, Cline, render pipeline", html)
        self.assertIn("<button type=\"submit\">Create App</button>", html)
        self.assertIn("data-create-owned-app", html)
        self.assertIn("No Apps yet", html)
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
        # The access section is empty before a key check and must stay visually hidden.
        self.assertIn("[hidden] { display: none !important; }", html)
        self.assertIn(
            '<section id="access-section" aria-labelledby="access-heading" hidden>',
            html,
        )
        # Hero copy points the user at the thin sign-in/create-key path.
        self.assertIn("Tim&apos;s gateway. Now yours, too.", html)
        self.assertIn("One OpenAI-compatible base URL", html)
        self.assertIn("create Apps", html)
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

    def test_design_contract_keeps_normal_user_surface_thin(self) -> None:
        html = render_user_portal(
            oidc_enabled=True,
            account={
                "authenticated": True,
                "user": {"email": "thin@example.com", "role": "none"},
                "apps": [],
            },
        )
        self.assertIn("Your Apps", html)
        self.assertIn("Create an App", html)
        self.assertIn("base url", html)
        self.assertIn("View access", html)
        self.assertNotIn("Profile editor", html)
        self.assertNotIn("Which profile", html)
        self.assertNotIn("permission Profiles", html)
        self.assertNotIn("policy matrix", html)
        self.assertNotIn("Chat only", html)
        self.assertNotIn("Full runtime access", html)


if __name__ == "__main__":
    unittest.main()
