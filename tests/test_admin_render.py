"""Tests for the admin dashboard HTML renderer."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.admin_render import render_admin_dashboard  # noqa: E402


def _snapshot() -> dict:
    return {
        "actor": {"subject": "u_owner", "email": "owner@example.com"},
        "counts": {
            "users": 1,
            "apps": 2,
            "profiles": 1,
            "credentials": 2,
            "active_credentials": 1,
        },
        "apps": [
            {
                "id": "app_1",
                "name": "brother-laptop",
                "owner": "u_owner",
                "profile_id": "default-local",
                "created_at": "2026-05-17T00:00:00Z",
            },
            {
                "id": "app_2",
                "name": "kitchen-tablet",
                "owner_user_id": "u_owner",
                "default_profile_id": "kiosk",
                "created_at": "2026-05-17T00:01:00Z",
            },
        ],
        "users": [
            {
                "id": "u_owner",
                "email": "owner@example.com",
                "name": "Owner",
                "roles": ["admin"],
            },
        ],
        "profiles": [
            {
                "id": "default-local",
                "label": "Default local",
                "protocols": ["openai"],
                "workflows": [],
                "status_visibility": "basic",
            },
        ],
        "credentials": [
            {
                "id": "cred_1",
                "app_id": "app_1",
                "label": "first key",
                "created_at": "2026-05-17T00:02:00Z",
                "revoked_at": None,
            },
            {
                "id": "cred_2",
                "app_id": "app_2",
                "label": "tablet key",
                "created_at": "2026-05-17T00:03:00Z",
                "revoked_at": "2026-05-17T01:00:00Z",
            },
        ],
        "audit": [
            {
                "at": "2026-05-17T00:00:00Z",
                "actor": "u_owner",
                "action": "app.create",
                "target": "app_1",
            },
        ],
        "discovery_links": [
            {"label": "Gateway metadata", "href": "/.well-known/ai-gateway"},
            {"label": "Admin overview", "href": "/admin/api/overview"},
        ],
    }


class AdminRenderHappyPathTests(unittest.TestCase):
    def test_renders_a_full_html_page(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("</html>", html)
        self.assertIn("AI Gateway Admin", html)

    def test_uses_App_terminology_not_Client(self) -> None:
        html = render_admin_dashboard(_snapshot())
        # Data plane noun is "App". The dashboard must mention it.
        self.assertIn("Create App", html)
        # Make sure the dashboard never names the data-plane "Client".
        # Lowercase mentions ("clients") would also fail this assertion.
        self.assertNotRegex(
            html,
            r"\bclients?\b",
        )

    def test_includes_create_app_and_mint_key_flow(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("Create App", html)
        self.assertIn("Create key for existing App", html)
        self.assertIn("https://ai.vivid.fish/v1", html)
        self.assertIn('action="/admin/api/apps"', html)
        self.assertIn("x-ai-gateway-admin-action", html)
        self.assertIn("x-ai-gateway-csrf", html)
        # Mint-key form must reference the key creation endpoint template.
        self.assertIn("/admin/api/apps/{app_id}/keys", html)
        self.assertIn("Revoke", html)
        self.assertIn("/admin/api/credentials/", html)

    def test_includes_create_profile_flow(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("Create reusable access policy", html)
        self.assertIn('action="/admin/api/profiles"', html)
        self.assertIn("data-create-profile", html)
        self.assertIn("name=\"capabilities\"", html)
        self.assertIn("name=\"model_access\"", html)
        self.assertIn("name=\"advanced_json\"", html)
        self.assertIn("Allow cloud fallback", html)
        self.assertIn("Allow native passthrough", html)

    def test_create_profile_offers_librechat_preset_and_simple_options(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn('<option value="family_chat">Family chat</option>', html)
        self.assertIn('<option value="coding_agents">Coding agents</option>', html)
        self.assertIn('name="profile_preset"', html)
        self.assertIn('value="openai" checked> OpenAI-compatible', html)
        self.assertIn('value="chat" checked> Chat', html)
        self.assertIn('value="images"> Images', html)
        self.assertIn("Recommended safe defaults", html)
        self.assertIn("profilePresets={family_chat:", html)
        self.assertNotIn('placeholder="protocols: openai, anthropic"', html)

    def test_create_profile_keeps_advanced_json_in_optional_disclosure(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("<summary>Advanced policy details</summary>", html)
        self.assertIn('name="advanced_json"', html)
        self.assertIn("</details>", html)

    def test_service_access_flow_renders_above_generic_forms(self) -> None:
        html = render_admin_dashboard(_snapshot())
        service_index = html.index("data-create-service-access")
        app_index = html.index("data-create-app")
        profile_index = html.index("data-create-profile")
        self.assertLess(service_index, app_index)
        self.assertLess(service_index, profile_index)
        self.assertIn("Grant service access", html)
        self.assertIn('name="service_name"', html)
        self.assertIn("OpenAI-compatible chat", html)
        self.assertIn('value="librechat.vivid.fish"', html)
        self.assertIn('name="mint_key" checked', html)
        self.assertIn("<summary>Advanced policy fields</summary>", html)
        self.assertIn('aria-label="Raw service access policy JSON"', html)

    def test_service_access_flow_builds_librechat_profile_and_service_app(
        self,
    ) -> None:
        html = render_admin_dashboard(_snapshot())
        branch = html[html.index("if(serviceForm)") : html.index("if(profileForm)")]
        self.assertIn("template=accessTemplates", branch)
        self.assertIn("protocols:template.protocols", branch)
        self.assertIn("intents:template.intents", branch)
        self.assertIn("exposure:['default']", branch)
        self.assertIn("trust_tiers:['stable']", branch)
        self.assertIn("resource_tiers:['interactive']", branch)
        self.assertIn("serviceSlug", branch)
        self.assertIn("kind:'service'", branch)
        self.assertIn("owner_service_account_name:owner", branch)
        self.assertIn("storedProfileId=(createdProfile.profile&&createdProfile.profile.id)", branch)
        self.assertIn("profile_id:storedProfileId", branch)

    def test_service_access_flow_calls_profile_app_key_endpoints(self) -> None:
        html = render_admin_dashboard(_snapshot())
        branch = html[html.index("if(serviceForm)") : html.index("if(profileForm)")]
        self.assertIn("createdProfile=await json(f.action,profileBody)", branch)
        self.assertIn("createdApp=await json(f.dataset.appAction,appBody)", branch)
        self.assertIn("f.dataset.keyActionTemplate.replace('{app_id}'", branch)
        self.assertIn("createdKey=await json(url,keyBody)", branch)
        self.assertIn('action="/admin/api/profiles"', html)
        self.assertIn('data-app-action="/admin/api/apps"', html)
        self.assertIn('data-key-action-template="/admin/api/apps/{app_id}/keys"', html)

    def test_create_app_can_choose_existing_profile_and_clear_ownership(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("Service-owned", html)
        self.assertIn("Personal/user-owned", html)
        self.assertIn("No user owner (service-owned)", html)
        self.assertIn('aria-label="Existing Profile for App"', html)
        self.assertIn('<option value="default-local">Default local (default-local)</option>', html)

    def test_includes_policy_simulation_flow(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("Simulate access", html)
        self.assertIn('action="/admin/api/policy/simulate"', html)
        self.assertIn("data-simulate-policy", html)
        self.assertIn("name=\"capability_tags\"", html)
        self.assertIn("Requires cloud fallback", html)

    def test_includes_app_and_user_records(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("brother-laptop", html)
        self.assertIn("kitchen-tablet", html)
        self.assertIn("owner@example.com", html)

    def test_app_profile_lookup_uses_durable_profile_id(self) -> None:
        snapshot = _snapshot()
        snapshot["profiles"] = [
            {
                "id": "prof_db_1",
                "label": "Chat profile",
                "profile": {
                    "id": "policy-chat",
                    "protocols": ["openai.chat"],
                },
            }
        ]
        snapshot["apps"] = [snapshot["apps"][0]]
        snapshot["apps"][0]["profile_id"] = "prof_db_1"
        html = render_admin_dashboard(snapshot)
        self.assertIn("prof_db_1", html)
        self.assertNotIn("profile missing", html)

    def test_personal_app_without_profile_inherits_default_profile(self) -> None:
        snapshot = _snapshot()
        snapshot["apps"] = [
            {
                "id": "app_pending",
                "kind": "personal",
                "name": "new-user-access",
                "owner_user_id": "u_owner",
                "created_at": "2026-05-17T00:01:00Z",
            }
        ]
        snapshot["credentials"] = []
        html = render_admin_dashboard(snapshot)
        self.assertNotIn("needs profile", html)
        self.assertNotIn("profile required", html)
        self.assertNotIn("new-user-access has no Profile yet.", html)
        self.assertIn("default-local", html)
        self.assertIn("protocols openai", html)

    def test_credentials_table_marks_revoked_status(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("cred_1", html)
        self.assertIn("cred_2", html)
        # active and revoked badges.
        self.assertIn("active", html)
        self.assertIn("revoked", html)

    def test_omits_discovery_links_from_clean_admin_chrome(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertNotIn('href="/.well-known/ai-gateway"', html)
        self.assertNotIn('href="/admin/api/overview"', html)
        self.assertNotIn(">Admin links<", html)
        self.assertIn('href="/">Account</a>', html)
        self.assertIn('href="/status">Status</a>', html)

    def test_admin_chrome_marks_admin_as_active_and_offers_sign_out(self) -> None:
        html = render_admin_dashboard(_snapshot())
        # The admin link must be visible in the shared header and marked active.
        self.assertRegex(html, r'<a [^>]*href="/admin"[^>]*aria-current="page"')
        # A visible Sign out affordance is required on every admin page.
        self.assertIn('href="/me/logout">Sign out</a>', html)
        # Header shares the same brand chrome as the user portal.
        self.assertIn('class="shell-header"', html)
        self.assertIn('class="shell-main"', html)
        self.assertIn('aria-label="AI Gateway home"', html)
        self.assertIn(">AI Gateway</span>", html)
        # The signed-in actor is still surfaced in the header.
        self.assertIn("owner@example.com", html)

    def test_admin_mutations_update_without_page_reload(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertNotIn("location.reload", html)
        self.assertNotIn("window.location", html)
        self.assertIn("refreshMain", html)
        self.assertIn("DOMParser", html)
        key_form_branch = html[
            html.index("if(keyForm)") : html.index("document.addEventListener('click'")
        ]
        self.assertIn("await refreshMain();show(data)", key_form_branch)
        profile_form_branch = html[html.index("if(profileForm)") : html.index("if(simForm)")]
        app_form_branch = html[html.index("if(appForm)") : html.index("if(keyForm)")]
        service_form_branch = html[
            html.index("if(serviceForm)") : html.index("if(profileForm)")
        ]
        self.assertIn("await refreshMain();show(data)", profile_form_branch)
        self.assertIn("await refreshMain();show(data)", app_form_branch)
        self.assertIn("await refreshMain();show(data)", service_form_branch)

    def test_admin_logout_url_is_configurable(self) -> None:
        html = render_admin_dashboard(
            _snapshot(),
            logout_url="/custom/logout",
        )
        self.assertIn('href="/custom/logout">Sign out</a>', html)


class AdminRenderSecurityTests(unittest.TestCase):
    def test_does_not_leak_raw_api_key_even_if_snapshot_includes_one(
        self,
    ) -> None:
        snapshot = _snapshot()
        # A buggy caller might pass api_key inside a list record. The
        # renderer must never surface it.
        snapshot["credentials"][0]["api_key"] = "sk-secret-leak"
        html = render_admin_dashboard(snapshot)
        self.assertNotIn("sk-secret-leak", html)

    def test_html_escapes_untrusted_strings(self) -> None:
        snapshot = _snapshot()
        snapshot["apps"][0]["name"] = "<script>alert('xss')</script>"
        snapshot["profiles"][0]["protocols"] = ["<script>alert('p')</script>"]
        html = render_admin_dashboard(snapshot)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert", html)

    def test_renders_empty_snapshot_without_crashing(self) -> None:
        html = render_admin_dashboard({})
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("No Apps yet", html)
        self.assertIn('href="#grant-service-access"', html)
        self.assertIn("Grant access", html)
        self.assertIn("Create an App before minting a runtime key.", html)
        self.assertNotIn("Mint runtime key", html)
        self.assertNotIn(">Policy test<", html)
        self.assertNotIn("No users yet", html)
        self.assertNotIn("No audit entries yet", html)
        self.assertNotIn(">Users<", html)
        self.assertNotIn(">Audit<", html)


class AdminRenderCustomEndpointsTests(unittest.TestCase):
    def test_custom_action_urls_are_respected(self) -> None:
        html = render_admin_dashboard(
            _snapshot(),
            create_app_action="/custom/admin/apps",
            create_profile_action="/custom/admin/profiles",
            create_key_action_template="/custom/admin/apps/{app_id}/keys",
            policy_simulate_action="/custom/admin/simulate",
        )
        self.assertIn('action="/custom/admin/apps"', html)
        self.assertIn('action="/custom/admin/profiles"', html)
        self.assertIn("/custom/admin/apps/{app_id}/keys", html)
        self.assertIn('action="/custom/admin/simulate"', html)

    def test_counts_are_derived_when_omitted(self) -> None:
        snapshot = _snapshot()
        snapshot.pop("counts")
        html = render_admin_dashboard(snapshot)
        # 2 apps in the fixture.
        self.assertRegex(html, r'<strong>2</strong> Apps')


if __name__ == "__main__":
    unittest.main()
