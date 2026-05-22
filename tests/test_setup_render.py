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
        self.assertIn("oak_ admin key", html)

    def test_oidc_enabled_can_show_admin_key_recovery_form(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=True,
            can_claim_with_admin_key=True,
        )
        # The recovery disclosure surfaces the host-side command and the paste
        # form, but it's behind a <details> and never the primary action.
        self.assertIn("generate-admin-key owner-cli", html)
        self.assertIn('method="post" action="/setup/claim"', html)
        self.assertIn("<summary>OIDC not working?", html)

    def test_oidc_disabled_no_owner_offers_local_owner_create_form(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=False,
        )
        # The primary action is creating the local owner account, not pasting
        # an admin key from a CLI recipe.
        self.assertIn("Create the local owner account", html)
        self.assertIn('action="/setup/local-owner"', html)
        self.assertIn('name="email"', html)
        self.assertIn('name="password"', html)
        self.assertIn('name="password_confirm"', html)
        # No primary OIDC sign-in CTA when OIDC is disabled.
        self.assertNotIn('href="/login?return_to=/setup"', html)
        self.assertNotIn("Sign in to claim owner", html)
        # The CLI recipe is still reachable, but only inside the recovery
        # disclosure — no more "run one host-side command" framing.
        self.assertNotIn("run one host-side command", html)
        self.assertNotIn("Run one ", html)

    def test_oidc_disabled_no_owner_offers_admin_key_recovery_when_signing_key_present(
        self,
    ) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=False,
            can_claim_with_admin_key=True,
        )
        self.assertIn("Create the local owner account", html)
        # Recovery disclosure is present but secondary.
        self.assertIn("generate-admin-key owner-cli", html)
        self.assertIn('method="post" action="/setup/claim"', html)
        # Even with the admin-key path available, the create form remains the
        # primary action above the recovery details.
        create_pos = html.index('action="/setup/local-owner"')
        recovery_pos = html.index('action="/setup/claim"')
        self.assertLess(create_pos, recovery_pos)

    def test_oidc_disabled_no_owner_without_signing_key_hides_admin_key_form(
        self,
    ) -> None:
        # When the gateway has no signing key at all, the admin-key paste form
        # can't be honored, so we hide it. The local-owner create form is still
        # the primary path.
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=False,
        )
        self.assertIn("Create the local owner account", html)
        self.assertNotIn('action="/setup/claim"', html)
        # No leftover "set this env var" copy from the legacy admin-key path.
        self.assertNotIn("AI_GATEWAY_ADMIN_SESSION_KEY", html)

    def test_create_error_surfaces_above_form_with_prefill(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=False,
            create_error="Passwords did not match.",
            create_email="owner@example.com",
        )
        self.assertIn("Passwords did not match.", html)
        self.assertIn('value="owner@example.com"', html)

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

    def test_owner_exists_without_oidc_shows_local_signin_form(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=False,
        )
        self.assertIn("Setup complete", html)
        # Returning operators sign in with the same credentials they set on
        # first run, not with an oak_ admin key.
        self.assertIn('action="/setup/local-login"', html)
        self.assertIn('name="email"', html)
        self.assertIn('name="password"', html)

    def test_owner_exists_without_oidc_keeps_admin_key_recovery_available(
        self,
    ) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=False,
            can_claim_with_admin_key=True,
        )
        # Both the primary local sign-in form and the recovery admin-key form
        # are reachable; the local form is primary.
        self.assertIn('action="/setup/local-login"', html)
        self.assertIn('action="/setup/claim"', html)
        local_pos = html.index('action="/setup/local-login"')
        recovery_pos = html.index('action="/setup/claim"')
        self.assertLess(local_pos, recovery_pos)

    def test_login_error_surfaces_above_signin_form_with_prefill(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=False,
            login_error="Wrong email or password.",
            login_email="owner@example.com",
        )
        self.assertIn("Wrong email or password.", html)
        self.assertIn('value="owner@example.com"', html)

    def test_admin_signed_in_sees_actor_pill_and_sign_out(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=True,
            is_admin_signed_in=True,
            actor_label="owner@example.com",
            logout_url="/logout",
            csrf_token="csrf-token",
            doctor_items=[
                {"label": "Owner/Admin", "state": "ok", "detail": "ready"},
                {"label": "Runtime App key", "state": "warn", "detail": "create one"},
            ],
        )
        self.assertIn("owner@example.com", html)
        self.assertIn("Sign out", html)
        self.assertIn('href="/logout"', html)
        # Admin nav surfaces /admin link in shared chrome.
        self.assertIn('href="/admin"', html)
        self.assertIn("Connect a client", html)
        self.assertIn('action="/setup/first-app"', html)
        self.assertIn('value="csrf-token"', html)
        self.assertIn("Gateway checks", html)
        self.assertIn("Runtime App key", html)

    def test_one_time_app_key_renders_only_when_supplied(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=True,
            oidc_enabled=True,
            is_admin_signed_in=True,
            actor_label="owner@example.com",
            one_time_key={"api_key": "opk_test_secret", "app_name": "first-app"},
            public_base_url="https://ai.example",
        )
        self.assertIn("opk_test_secret", html)
        self.assertIn('export OPENAI_BASE_URL="https://ai.example/openai/v1"', html)
        self.assertIn("This value is shown once", html)

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

    def test_prefill_email_is_html_escaped(self) -> None:
        html = render_setup_page(
            owner_or_admin_exists=False,
            oidc_enabled=False,
            create_email='"><script>alert(1)</script>',
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
