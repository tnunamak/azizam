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
        "actor": {
            "subject": "u_owner",
            "email": "owner@example.com",
            "roles": ["owner"],
        },
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
        self.assertIn("Create App API key", html)
        self.assertIn('aria-label="App for App API key"', html)
        self.assertNotIn("Create runtime key", html)
        self.assertNotIn('aria-label="App for runtime key"', html)
        self.assertIn("Create admin automation key", html)
        self.assertIn("oak_", html)
        self.assertIn("/admin/api/admin-keys", html)
        self.assertIn('action="/admin/api/apps"', html)
        self.assertIn("x-ai-gateway-admin-action", html)
        self.assertIn("x-ai-gateway-csrf", html)
        # Mint-key form must reference the key creation endpoint template.
        self.assertIn("/admin/api/apps/{app_id}/keys", html)
        self.assertIn("Revoke", html)
        self.assertIn("/admin/api/credentials/", html)

    def test_includes_create_profile_flow(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("Create Profile", html)
        self.assertIn(
            "Profiles are reusable permission templates assigned by admins to people and shared Apps.",
            html,
        )
        self.assertIn('action="/admin/api/profiles"', html)
        self.assertIn("data-create-profile", html)
        self.assertIn("name=\"profile_preset\"", html)
        self.assertIn("name=\"advanced_json\"", html)
        self.assertIn("Allow cloud fallback", html)
        self.assertIn("Allow native passthrough", html)

    def test_create_profile_offers_policy_templates_and_simple_options(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn(
            'name="profile_preset" value="chat_only" checked',
            html,
        )
        self.assertIn('name="profile_preset" value="chat_embeddings"', html)
        self.assertIn('name="profile_preset" value="media_workflows"', html)
        self.assertIn('name="profile_preset" value="full_runtime"', html)
        self.assertIn("stable local language models", html)
        self.assertIn("trusted power users and internal agents", html)
        self.assertIn("Blank · advanced", html)
        self.assertIn("min-width: 0;", html)
        self.assertIn(".shell-main section", html)
        self.assertIn("minmax(min(15rem, 100%), 1fr)", html)
        self.assertIn(".choice-grid, .template-strip { grid-template-columns: 1fr; }", html)
        self.assertIn('name="profile_preset"', html)
        self.assertIn('value="openai" checked> OpenAI-compatible', html)
        self.assertIn("policyTemplates={chat_only:", html)
        self.assertIn("full_runtime:{label:'Full runtime access'", html)
        self.assertNotIn('<select name="profile_preset"', html)
        self.assertNotIn("chat · completions · stable", html)
        self.assertNotIn("Existing Profiles:", html)
        self.assertNotIn("All local and stable", html)
        self.assertNotIn('placeholder="protocols: openai, anthropic"', html)

    def test_create_profile_keeps_advanced_json_in_optional_disclosure(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("<summary>Advanced policy details</summary>", html)
        self.assertIn('name="advanced_json"', html)
        self.assertIn("</details>", html)

    def test_shared_access_flow_renders_above_generic_forms(self) -> None:
        html = render_admin_dashboard(_snapshot())
        shared_index = html.index("data-create-shared-access")
        app_index = html.index("data-create-app")
        profile_index = html.index("data-create-profile")
        self.assertLess(shared_index, app_index)
        self.assertLess(shared_index, profile_index)
        self.assertIn("Create App API key", html)
        self.assertIn('name="app_name"', html)
        self.assertIn('placeholder="App name, e.g. LibreChat"', html)
        self.assertIn('name="policy_template" value="chat_only" checked', html)
        self.assertIn('name="policy_template" value="full_runtime"', html)
        self.assertIn("Create App API key", html)
        self.assertNotIn('value="librechat.vivid.fish"', html)
        self.assertNotIn('name="mint_key"', html)
        self.assertNotIn("Advanced policy fields", html)
        self.assertNotIn("Raw shared access policy JSON", html)

    def test_shared_access_flow_builds_librechat_profile_and_shared_app(
        self,
    ) -> None:
        html = render_admin_dashboard(_snapshot())
        branch = html[html.index("if(sharedAccessForm)") : html.index("if(profileForm)")]
        self.assertIn("buildProfile(templateId", branch)
        self.assertIn("policyTemplates[templateId]", branch)
        self.assertIn("appSlug", branch)
        self.assertIn("kind:'service'", branch)
        self.assertIn("owner_service_account_name:appName", branch)
        self.assertIn("storedProfileId=(createdProfile.profile&&createdProfile.profile.id)", branch)
        self.assertIn("profile_id:storedProfileId", branch)

    def test_shared_access_flow_calls_profile_app_key_endpoints(self) -> None:
        html = render_admin_dashboard(_snapshot())
        branch = html[html.index("if(sharedAccessForm)") : html.index("if(profileForm)")]
        self.assertIn("createdProfile=await json(f.action,profileBody)", branch)
        self.assertIn("createdApp=await json(f.dataset.appAction,appBody)", branch)
        self.assertIn("f.dataset.keyActionTemplate.replace('{app_id}'", branch)
        self.assertIn("createdKey=await json(url,keyBody)", branch)
        self.assertIn('action="/admin/api/profiles"', html)
        self.assertIn('data-app-action="/admin/api/apps"', html)
        self.assertIn('data-key-action-template="/admin/api/apps/{app_id}/keys"', html)

    def test_admin_automation_key_flow_is_separate_from_runtime_keys(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("Create admin automation key", html)
        self.assertIn("data-create-admin-key", html)
        self.assertIn('action="/admin/api/admin-keys"', html)
        self.assertIn("It is not an OpenAI-compatible runtime key.", html)
        self.assertIn("Admin automation", html)
        branch = html[html.index("if(adminKeyForm)") : html.index("if(profileForm)")]
        self.assertIn("body={name:fd.get('name'),role:fd.get('role')||'admin'}", branch)
        self.assertIn("json(f.action,body)", branch)
        self.assertIn("showSecret(data,'Admin automation key created", branch)
        self.assertIn("admin-keys", html)
        self.assertIn('<option value="owner">Owner</option>', html)

    def test_admin_automation_owner_role_is_disabled_for_non_owner(self) -> None:
        snapshot = _snapshot()
        snapshot["actor"]["roles"] = ["admin"]
        html = render_admin_dashboard(snapshot)
        self.assertIn('<option value="owner" disabled>Owner (owner role required)</option>', html)

    def test_create_app_can_choose_existing_profile_and_clear_ownership(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn("Shared App", html)
        self.assertIn("Personal/user-owned", html)
        self.assertIn("No user owner (shared App)", html)
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
        self.assertIn('href="/">Home</a>', html)
        self.assertIn('href="/status">Status</a>', html)

    def test_admin_dashboard_uses_operator_design_contract(self) -> None:
        html = render_admin_dashboard(_snapshot())
        self.assertIn(">Operating</h1>", html)
        self.assertIn('id="access"', html)
        self.assertIn('id="shared-apps"', html)
        self.assertIn(">Shared Apps</h2>", html)
        self.assertIn('id="profiles"', html)
        self.assertIn('id="debug-access"', html)
        self.assertIn('id="data-views"', html)
        self.assertIn("Profiles stay here as operator tools", html)
        self.assertIn("users never choose them", html)
        self.assertIn("opk_</code>&nbsp;runtime App keys", html)
        self.assertIn("oak_</code>&nbsp;admin automation", html)

    def test_profile_creation_is_template_first(self) -> None:
        html = render_admin_dashboard(_snapshot())
        profile_section = html[html.index('id="profiles"') : html.index('id="debug-access"')]
        self.assertIn("Start from a template", profile_section)
        self.assertIn('class="template-strip"', profile_section)
        self.assertLess(
            profile_section.index('class="template-strip"'),
            profile_section.index("<summary>Advanced policy details</summary>"),
        )

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
        self.assertNotIn("Copy failed", html)
        self.assertIn("button.dataset.copyLabel", html)
        self.assertIn("refreshMain", html)
        self.assertIn("DOMParser", html)
        key_form_branch = html[
            html.index("if(keyForm)") : html.index("document.addEventListener('click'")
        ]
        self.assertIn("await refreshMain();showSecret(data", key_form_branch)
        profile_form_branch = html[html.index("if(profileForm)") : html.index("if(simForm)")]
        app_form_branch = html[html.index("if(appForm)") : html.index("if(keyForm)")]
        shared_access_branch = html[
            html.index("if(sharedAccessForm)") : html.index("if(profileForm)")
        ]
        self.assertIn("await refreshMain();show(data)", profile_form_branch)
        self.assertIn("await refreshMain();show(data)", app_form_branch)
        self.assertIn("await refreshMain();showSecret(data", shared_access_branch)

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
        self.assertIn('href="#create-api-key"', html)
        self.assertIn("Create API key", html)
        self.assertNotIn("Create an App before minting a runtime key.", html)
        self.assertNotIn("Mint runtime key", html)
        self.assertNotIn(">Policy test<", html)
        self.assertNotIn(">Overview<", html)
        self.assertNotIn(">Needs attention<", html)
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
            create_admin_key_action="/custom/admin/admin-keys",
            policy_simulate_action="/custom/admin/simulate",
        )
        self.assertIn('action="/custom/admin/apps"', html)
        self.assertIn('action="/custom/admin/profiles"', html)
        self.assertIn("/custom/admin/apps/{app_id}/keys", html)
        self.assertIn('action="/custom/admin/admin-keys"', html)
        self.assertIn('action="/custom/admin/simulate"', html)

    def test_overview_counts_are_not_rendered(self) -> None:
        snapshot = _snapshot()
        snapshot.pop("counts")
        html = render_admin_dashboard(snapshot)
        self.assertNotIn(">Overview<", html)
        self.assertNotRegex(html, r"<strong>2</strong> Apps")


if __name__ == "__main__":
    unittest.main()
