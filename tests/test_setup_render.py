"""Tests for the first-run setup page renderer."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.setup_render import render_setup_page  # noqa: E402


class SetupRenderTests(unittest.TestCase):
    def test_oidc_enabled_no_owner_shows_claim_button(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=True,
        )
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("</html>", html)
        self.assertIn("Claim this gateway", html)
        # The primary action links to /login with /setup as the return target.
        self.assertIn('href="/login?return_to=/setup"', html)
        self.assertIn("Sign in to claim owner", html)
        # The "first allowed OIDC login becomes owner" promise is on the page.
        self.assertIn("first allowed OIDC login", html)
        # No "Setup complete" copy yet.
        self.assertNotIn("Setup complete", html)
        # OIDC has a small recovery hatch so a broken IdP is not a dead end.
        self.assertIn("OIDC not working?", html)

    def test_oidc_enabled_can_show_admin_key_recovery_form(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=True,
            can_claim_with_admin_key=True,
        )
        self.assertIn("OIDC not working?", html)
        self.assertIn("generate-admin-key owner-cli", html)
        self.assertIn('method="post" action="/setup/claim"', html)

    def test_oidc_disabled_no_owner_explains_what_to_configure(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=False,
            can_claim_with_admin_key=True,
        )
        self.assertIn("Claim this gateway from this machine", html)
        self.assertIn("generate-admin-key owner-cli", html)
        self.assertIn("oak_ admin key", html)
        # No primary OIDC sign-in CTA when OIDC is disabled.
        self.assertNotIn('href="/login?return_to=/setup"', html)
        self.assertNotIn("Sign in to claim owner", html)
        self.assertIn('method="post" action="/setup/claim"', html)
        self.assertIn('placeholder="oak_..."', html)

    def test_oidc_disabled_without_session_key_explains_missing_bootstrap_secret(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=False,
        )
        self.assertIn("AI_GATEWAY_ADMIN_SESSION_KEY", html)
        self.assertNotIn('action="/setup/claim"', html)

    def test_owner_exists_shows_setup_complete_with_links(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=True,
        )
        self.assertIn("Setup complete", html)
        self.assertIn("This gateway has an owner", html)
        self.assertIn('href="/"', html)
        self.assertIn('href="/admin"', html)
        # The claim CTA isn't shown anymore.
        self.assertNotIn("Sign in to claim owner", html)
        # A small recovery hatch remains so OIDC failures are not dead ends.
        self.assertIn("OIDC not working?", html)

    def test_claim_error_opens_recovery_details_on_complete_state(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=True,
            can_claim_with_admin_key=True,
            claim_error="That key was rejected.",
        )
        self.assertIn('<details class="recovery" open>', html)
        self.assertIn("That key was rejected.", html)

    def test_owner_exists_without_oidc_can_still_open_browser_session_with_admin_key(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=False,
            can_claim_with_admin_key=True,
        )
        self.assertIn("Setup complete", html)
        self.assertIn("Use an existing admin key", html)
        self.assertIn('method="post" action="/setup/claim"', html)

    def test_admin_signed_in_sees_actor_pill_and_sign_out(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=True,
            is_admin_signed_in=True,
            actor_label="owner@example.com",
            logout_url="/logout",
        )
        self.assertIn("owner@example.com", html)
        self.assertIn("Sign out", html)
        self.assertIn('href="/logout"', html)
        # Admin nav surfaces /admin link in shared chrome.
        self.assertIn('href="/admin"', html)

    def test_unauthenticated_view_omits_sign_out(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=True,
        )
        self.assertNotIn("Sign out", html)

    def test_login_url_is_html_escaped(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=True,
            login_url="/login?return_to=/setup&x=\"evil\"",
        )
        self.assertNotIn('x="evil"', html)
        self.assertIn("&quot;evil&quot;", html)

    def test_actor_label_is_html_escaped(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=True,
            is_admin_signed_in=True,
            actor_label="<script>alert(1)</script>",
            logout_url="/logout",
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_no_private_data_leaks_in_html(self) -> None:
        # Render with the most-loaded inputs the route can produce and verify
        # there's no secret-shaped material in the output.
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=True,
            is_admin_signed_in=True,
            actor_label="owner@example.com",
            logout_url="/logout",
        )
        for needle in ("opk_", "sk-", "client_secret", "signing_key", "Bearer "):
            self.assertNotIn(needle, html)


if __name__ == "__main__":
    unittest.main()
