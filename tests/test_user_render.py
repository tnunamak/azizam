"""Tests for the normal-user gateway portal renderer."""

from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from shutil import which
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.user_render import render_user_portal  # noqa: E402


class UserPortalRenderTests(unittest.TestCase):
    def test_renders_a_full_normal_user_page(self) -> None:
        html = render_user_portal(
            public_base_url="https://ai.vivid.fish",
            oidc_enabled=True,
            wordmark=("vivid", "fish"),
        )
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("</html>", html)
        self.assertIn("AI Gateway", html)
        self.assertIn("Continue with vivid.fish ID", html)
        self.assertIn('/login?return_to=/', html)
        # /v1/models survives in the reveal sheet's runnable "prove it works"
        # curl; /gateway/v1/capabilities is fetched by the "what works" health
        # card. The old paste-a-key checker's sessionStorage handling is gone.
        self.assertIn("/v1/models", html)
        self.assertIn("/gateway/v1/capabilities", html)
        self.assertNotIn("sessionStorage", html)

    def test_uses_normal_user_language(self) -> None:
        html = render_user_portal()
        self.assertNotIn("Check an API key", html)
        self.assertNotIn("Base URL", html)
        self.assertNotIn(
            "Gateway reachable. Use this for OpenAI-compatible SDKs and tools.",
            html,
        )
        self.assertNotIn("Use your local AI stack", html)
        self.assertNotIn("Failure Meanings", html)
        self.assertNotIn("ANTHROPIC_BASE_URL", html)
        self.assertNotIn("OpenAI-compatible client", html)
        self.assertNotIn("Mint runtime key", html)
        self.assertNotIn("Copy this API key now", html)

    def test_does_not_render_real_api_key_material(self) -> None:
        html = render_user_portal()
        self.assertNotIn("sk-secret", html)
        self.assertNotIn("local-key-store", html)
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
        # Non-admins see no Profile/permission chrome on this calm surface — it's
        # operator vocabulary they can't act on. The calm entry point is a title.
        self.assertNotIn("account permissions", html)
        self.assertIn("Your gateway", html)
        # The key value is masked Stripe/OpenAI-style: scheme + dots + last 4.
        self.assertIn("opk_ •••• abcd", html)
        self.assertIn("base-url-display", html)
        self.assertIn("copy-base-url", html)
        self.assertNotIn("Check another key", html)
        self.assertNotIn('id="runtime-key"', html)
        self.assertIn("data-mint-owned-key", html)
        self.assertIn("Create key", html)
        # The calm Home (design 03): a key is a tappable row that opens the
        # detail sheet — destructive actions and "view access" live IN the
        # sheet, never as buttons on the surface.
        self.assertNotIn("data-revoke-owned-key", html)
        self.assertNotIn(">Delete key<", html)
        self.assertNotIn(">View access<", html)
        # The key row carries the open-detail hook and its label, not surface buttons.
        self.assertIn('data-view-owned-key="cred_1"', html)
        self.assertIn("key-row-name", html)
        # "+ New key" is a quiet disclosure, and removing an App is tucked behind
        # its own disclosure (no red "Delete App" button on the surface).
        self.assertIn("key-create", html)
        self.assertNotIn(">Delete App<", html)
        self.assertIn("Remove this App", html)
        self.assertIn("data-delete-owned-app", html)
        self.assertIn("Create one App for each tool or script", html)
        # The per-card tutorial sentence is cut for normal users (cognitive
        # cost paid every view); the name + permission badge carry the meaning.
        self.assertNotIn("Use this for one tool, workflow, or script", html)
        self.assertNotIn("Create one App per client", html)
        self.assertIn("Copy key", html)
        # Reveal-once copy + a runnable first call with the key in a Bearer header.
        self.assertIn("shown once and cannot be retrieved", html)
        self.assertIn("Prove it works", html)
        self.assertIn("Authorization: Bearer", html)
        self.assertNotIn("Copy this API key now", html)
        self.assertIn("key-row", html)
        self.assertIn("/me/api/credentials/", html)
        self.assertNotIn("location.reload", html)
        self.assertNotIn('href="/admin"', html)
        self.assertNotIn(">Discovery<", html)
        self.assertNotIn(">Health<", html)
        self.assertNotIn("hash", html)

    def test_no_runtime_key_checker_on_user_surface(self) -> None:
        # The paste-a-key "Check an API key" checker (and its opk_/oak_ jargon)
        # was operator/debug tooling on the user surface — fully removed. Key
        # verification is the reveal-sheet probe; inspection is the key-detail
        # sheet.
        html = render_user_portal()
        self.assertNotIn("Check an API key", html)
        self.assertNotIn('startsWith("oak_")', html)
        self.assertNotIn("admin automation keys are for /admin/api", html)
        self.assertNotIn('id="runtime-key"', html)
        self.assertNotIn("function checkAccess", html)

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
        # The admin path is the /admin nav link in the header — no "Admin manages
        # permission Profiles" jargon sentence on the calm Home surface anymore.
        self.assertNotIn("permission Profiles", html)
        self.assertNotIn("Signed in as", html)
        self.assertIn("Create one App for each tool or script", html)
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
        self.assertIn("Create one App for each tool or script", html)
        self.assertIn("Open WebUI, Cline, render pipeline", html)
        self.assertIn("<button type=\"submit\">Create App</button>", html)
        self.assertIn("data-create-owned-app", html)
        self.assertIn("Create your first App to get an", html)
        self.assertIn("x-ai-gateway-csrf", html)

    def test_base_url_area_keeps_copy_and_openai_setup_compact(self) -> None:
        html = render_user_portal(
            public_base_url="https://gw.example",
            account={
                "authenticated": True,
                "user": {"email": "user@example.com", "role": "none"},
                "apps": [],
            },
        )
        self.assertIn('id="openai-snippet" class="snippet"', html)
        # Base-URL card shows just the base URL; the real key + runnable call is
        # revealed once at key creation, so no dead "opk_..." placeholder here.
        self.assertIn(
            'export OPENAI_BASE_URL="\' + baseUrl + \'/v1"',
            html,
        )
        self.assertNotIn('OPENAI_API_KEY="opk_..."', html)
        self.assertNotIn('curl "$OPENAI_BASE_URL/models"', html)
        self.assertNotIn("Connection settings", html)
        self.assertIn("min-width: 6.25rem;", html)
        # The base-card content column carries the overflow guard so a long
        # base URL can't push the card wider than its grid track.
        self.assertIn(".base-card > div, .shell-main > section", html)
        self.assertIn("min-width: 0;", html)
        self.assertIn("white-space: nowrap;", html)
        self.assertIn('button.textContent = "Copied";', html)
        self.assertIn('button.textContent = "Failed";', html)
        self.assertIn("button.dataset.copyLabel", html)

    def test_transient_messages_and_background_do_not_shift_or_tile(self) -> None:
        html = render_user_portal(
            account={
                "authenticated": True,
                "user": {"email": "user@example.com", "role": "none"},
                "apps": [],
            },
        )
        self.assertIn("background-repeat: no-repeat;", html)
        self.assertIn("background-size: 100% 38rem, 100% 34rem, auto;", html)
        self.assertIn('id="account-toast" class="toast"', html)
        self.assertIn("toastTimer = window.setTimeout", html)
        self.assertIn(".message:empty { display: none; }", html)
        self.assertNotIn(".message:empty { visibility: hidden; }", html)

    def test_capability_describe_helpers_are_plain_language(self) -> None:
        # The capability/model describers (reused by the key-detail sheet) speak
        # plain language, not jargon. The old paste-a-key "access checker" and
        # its #access-list section are GONE — operator/debug tooling that had no
        # place on the user surface.
        html = render_user_portal()
        self.assertIn("function humanizeId(value", html)
        self.assertIn("function intentDescription(intent)", html)
        self.assertIn("Generate music, loops, or sound effects.", html)
        # The paste-a-key checker's MARKUP is gone from the user surface.
        self.assertNotIn('id="access-list"', html)
        self.assertNotIn('id="access-section"', html)
        self.assertNotIn("Check an API key", html)
        self.assertNotIn('id="runtime-key"', html)
        # No noisy raw-field badges in the describers.
        self.assertNotIn("<h3>Capabilities</h3>", html)
        self.assertNotIn('text: model.object || "model"', html)

    def test_home_health_card_folds_in_capabilities(self) -> None:
        # "What works" folds into the health card (design: one card answers
        # is-it-up + what-can-it-do), not a separate page/tab. It's a tappable
        # <details> that lazily loads /gateway/v1/capabilities and groups by
        # modality. No phantom #caps anchor/route.
        html = render_user_portal(
            account={
                "authenticated": True,
                "user": {"email": "user@example.com", "role": "none"},
                "apps": [],
            },
        )
        self.assertIn('<details class="vf-card health-card" id="gateway-health">', html)
        self.assertIn('id="health-caps"', html)
        self.assertIn("async function loadCapabilities()", html)
        self.assertIn('fetch("/gateway/v1/capabilities"', html)
        self.assertIn("groupAccessByModality(caps, [])", html)
        # Honest: declares "capabilities", and an empty state points at Admin.
        self.assertIn("No capabilities are declared yet", html)
        self.assertNotIn('href="/#caps"', html)

    def test_home_has_owner_scoped_recent_activity_and_receipt_sheet(self) -> None:
        # Recent feed loads from the owner-scoped /me/api/activity; rows open the
        # addressable receipt detail sheet (/r/:id). Honest empty state, no
        # fabricated rows.
        html = render_user_portal(
            account={
                "authenticated": True,
                "user": {"email": "user@example.com", "role": "none"},
                "apps": [],
            },
        )
        self.assertIn('id="recent-activity"', html)
        self.assertIn(">Recent activity</h2>", html)
        self.assertIn('accountJson("/me/api/activity?limit=5")', html)
        self.assertIn("No calls yet", html)
        # Receipt detail = an addressable sheet bound to /r/:id.
        self.assertIn('id="receipt-sheet"', html)
        self.assertIn("async function openReceiptSheet(requestId", html)
        self.assertIn('"/me/api/receipts/"', html)
        self.assertIn('history.pushState({}, "", "/r/"', html)
        # Back/cold-start deep-link handling.
        self.assertIn('location.pathname.indexOf("/r/") === 0', html)
        # No fabricated activity rows in the static HTML.
        self.assertNotIn("482ms", html)
        self.assertNotIn("chat.completions</", html)

    def test_client_rendered_key_row_is_an_accessible_button(self) -> None:
        # The client-rendered key row (after a create/delete refresh) is a
        # <button> with an accessible name that opens the detail sheet — where
        # the revoke control, clearly labelled "Revoke this key", lives.
        html = render_user_portal()
        self.assertIn('row.setAttribute("aria-label", "Open " + label);', html)
        self.assertIn('row.dataset.viewOwnedKey = text(credential.id);', html)
        self.assertIn('revoke.textContent = "Revoke this key";', html)
        # The old surface delete-key button is gone from the client renderer.
        self.assertNotIn(
            'del.setAttribute("aria-label", "Delete API key " + title.textContent);',
            html,
        )

    def test_key_detail_sheet_summarizes_by_modality_not_a_wall(self) -> None:
        # The key-detail sheet (design 07) leads with a calm grouped summary —
        # a plain sentence + modality pills with counts — and tucks the full
        # per-model list behind a "Show all N items" disclosure, instead of
        # dumping every backend model as a card. This keeps the duplicate
        # capability/model entries from reading as a repetitive wall.
        html = render_user_portal()
        self.assertIn("function groupAccessByModality(caps, models)", html)
        self.assertIn("function modalityKey(item)", html)
        self.assertIn("key-detail-sentence", html)
        self.assertIn("key-detail-pills", html)
        self.assertIn('summary.textContent = "Show all " + total', html)
        # Still honest about the missing per-key activity data.
        self.assertIn("Per-key activity isn't recorded yet.", html)
        # The full-wall default is gone: items live inside the disclosure.
        self.assertIn("key-detail-all", html)

    def test_signed_out_landing_uses_shared_shell_and_clear_hero(self) -> None:
        html = render_user_portal(oidc_enabled=True, wordmark=("vivid", "fish"))
        # Shared shell chrome.
        self.assertIn('class="shell-header"', html)
        self.assertIn('class="shell-main"', html)
        self.assertIn('aria-label="AI Gateway home"', html)
        self.assertIn(">AI Gateway</span>", html)
        # Signed-out chrome has no inert section nav; operator-only links stay hidden.
        self.assertNotIn('class="shell-nav"', html)
        self.assertNotIn('href="/status"', html)
        self.assertNotIn('href="/admin"', html)
        self.assertNotIn('id="runtime-key"', html)
        self.assertNotIn("No key is pasted in this tab.", html)
        self.assertNotIn('id="copy-base-url"', html)
        # The signed-out door is maximally thin: no paste-a-key access section,
        # no operator tooling — just the hero and one sign-in path.
        self.assertNotIn('id="access-section"', html)
        self.assertNotIn('id="access-list"', html)
        # Hero copy points the user at the thin sign-in path.
        self.assertIn("Sign in to use the gateway.", html)
        self.assertIn("Your vivid.fish account controls", html)
        self.assertIn("media workflows", html)
        # No Sign out affordance when not signed in.
        self.assertNotIn(">Sign out</a>", html)

    def test_minted_key_is_revealed_in_a_sheet(self) -> None:
        # The reveal lands in a sheet (bottom-sheet mobile / dialog desktop),
        # opened by showMintedKey and dismissed by Done — not an inline block.
        html = render_user_portal(
            oidc_enabled=True,
            account={
                "authenticated": True,
                "user": {"email": "u@example.com", "role": "none"},
                "apps": [],
            },
        )
        self.assertIn('id="key-reveal-sheet"', html)
        self.assertIn('class="vf-sheet-backdrop"', html)
        self.assertIn('id="key-reveal-body"', html)
        self.assertIn(">Save this key</h2>", html)
        self.assertIn("data-close-key-reveal", html)
        # The reveal logic targets the sheet body and opens the sheet.
        self.assertIn("openKeyRevealSheet()", html)
        self.assertIn("closeKeyRevealSheet()", html)
        # The runnable-curl reveal content is still built (unchanged behavior).
        self.assertIn("shown once and cannot be retrieved", html)
        self.assertIn("Authorization: Bearer", html)

    def test_destructive_actions_use_the_shared_confirmation_sheet(self) -> None:
        html = render_user_portal(
            account={
                "authenticated": True,
                "user": {"email": "user@example.com", "role": "none"},
                "apps": [],
            },
        )
        # Native browser confirms block without offering the portal's sheet
        # semantics, so both destructive flows use one accessible sheet.
        self.assertNotIn("con" + "firm(", html)
        self.assertIn('id="destructive-confirm-sheet"', html)
        self.assertIn('class="vf-sheet-backdrop"', html)
        self.assertIn('role="dialog" aria-modal="true"', html)
        self.assertIn('aria-describedby="destructive-confirm-description"', html)
        self.assertIn("data-cancel-destructive-confirm", html)
        self.assertIn("data-confirm-destructive-action", html)
        self.assertIn("function openDestructiveConfirmation", html)
        self.assertIn("function closeDestructiveConfirmation", html)
        self.assertIn("makeDestructiveBackgroundInert", html)
        self.assertIn("restoreDestructiveBackground", html)
        self.assertIn("element.inert = true", html)
        self.assertIn('element.setAttribute("aria-hidden", "true")', html)
        self.assertIn('event.key === "Tab"', html)
        self.assertIn("cancel.focus()", html)
        self.assertIn('event.key === "Escape"', html)
        self.assertIn("target === sheet", html)
        self.assertIn('status.textContent = "Working…"', html)
        self.assertIn("await state.mutation()", html)
        self.assertIn("await state.refresh()", html)
        self.assertIn("focusAccountStatus()", html)
        self.assertIn('confirmLabel: "Revoke key"', html)
        self.assertIn('confirmLabel: "Delete App"', html)
        # The page only refreshes after the DELETE resolves; it never removes an
        # App or key optimistically before a successful server response.
        self.assertIn("await accountJson(", html)
        self.assertIn("await refreshAccount();", html)
        self.assertIn(".confirm-actions > button { flex: 1 1 10rem; }", html)

    @unittest.skipUnless(which("node"), "requires the local Node JavaScript runtime")
    def test_destructive_confirmation_control_flow_is_executable(self) -> None:
        """Run the rendered confirmation code against a deterministic tiny DOM.

        This exercises the actual client functions, rather than treating source
        substrings as proof of focus containment or post-commit behavior.
        """
        html = render_user_portal(
            account={
                "authenticated": True,
                "user": {"email": "user@example.com", "role": "none"},
                "apps": [],
            },
        )
        node_test = r'''
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const html = fs.readFileSync(process.argv[1], "utf8");
const start = html.indexOf("  let destructiveConfirmation = null;");
const end = html.indexOf("\n\n  function badge", start);
assert(start >= 0 && end > start, "confirmation code was not rendered");

class Element {
  constructor(tagName = "DIV") {
    this.tagName = tagName;
    this.hidden = false;
    this.disabled = false;
    this.inert = false;
    this.isConnected = true;
    this.attributes = new Map();
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  focus() { document.activeElement = this; }
  closest() { return null; }
}
class HTMLButtonElement extends Element { constructor() { super("BUTTON"); } }
globalThis.HTMLElement = Element;
globalThis.HTMLButtonElement = HTMLButtonElement;

const header = new Element("HEADER");
const main = new Element("MAIN");
const tabbar = new Element("NAV");
const sheet = new Element("DIV");
const script = new Element("SCRIPT");
const cancel = new HTMLButtonElement();
const confirm = new HTMLButtonElement();
sheet.querySelector = (selector) => selector.includes("cancel") ? cancel : selector.includes("confirm") ? confirm : null;
sheet.querySelectorAll = () => [cancel, confirm].filter((element) => !element.disabled);
sheet.contains = (element) => element === sheet || element === cancel || element === confirm;
const heading = new Element();
const detail = new Element();
const confirmStatus = new Element();
const toast = new Element();
const elements = {
  "destructive-confirm-sheet": sheet,
  "destructive-confirm-title": heading,
  "destructive-confirm-description": detail,
  "destructive-confirm-status": confirmStatus,
  "account-toast": toast,
};
const listeners = {};
globalThis.document = {
  activeElement: null,
  body: { children: [header, main, tabbar, sheet, script] },
  getElementById: (id) => elements[id] || null,
  addEventListener: (type, listener) => { listeners[type] = listener; },
};
globalThis.$ = (id) => document.getElementById(id);
const messages = [];
globalThis.setAccountMessage = (message, kind) => messages.push({ message, kind });
let source = html.slice(start, end);
source += "\nglobalThis.confirmationTestApi = { openDestructiveConfirmation, closeDestructiveConfirmation, submitDestructiveConfirmation, state: () => destructiveConfirmation };";
vm.runInThisContext(source, { filename: "rendered-confirmation.js" });
const api = globalThis.confirmationTestApi;
;(async () => {
const trigger = new HTMLButtonElement();
const open = (overrides = {}) => api.openDestructiveConfirmation({
  title: "Delete app?", description: "Irreversible.", confirmLabel: "Delete App",
  trigger, mutation: async () => {}, refresh: async () => {},
  successMessage: "Deleted.", refreshFailureMessage: "Deleted, refresh failed.",
  ...overrides,
});
const key = (key, shiftKey = false) => {
  let prevented = false;
  listeners.keydown({ key, shiftKey, preventDefault: () => { prevented = true; } });
  return prevented;
};

// Open: portal/background (including a nested key-detail sheet in main) is inert,
// and the least-destructive action is focused.
open();
assert.equal(sheet.hidden, false);
assert.equal(main.inert, true);
assert.equal(main.getAttribute("aria-hidden"), "true");
assert.equal(document.activeElement, cancel);
confirm.focus();
assert.equal(key("Tab"), true);
assert.equal(document.activeElement, cancel);
assert.equal(key("Tab", true), true);
assert.equal(document.activeElement, confirm);
key("Escape");
assert.equal(sheet.hidden, true);
assert.equal(main.inert, false);
assert.equal(main.getAttribute("aria-hidden"), null);
assert.equal(document.activeElement, trigger);

// A failed, uncommitted mutation returns focus to its still-live invoker.
open({ mutation: async () => { throw new Error("request failed"); } });
await api.submitDestructiveConfirmation();
assert.equal(api.state(), null);
assert.equal(sheet.hidden, true);
assert.equal(document.activeElement, trigger);
assert.equal(messages.at(-1).kind, "bad");

// Pending work ignores Escape/backdrop dismissal; a committed delete stays closed
// and cannot become a second destructive retry when refresh fails.
let resolveMutation;
open({
  mutation: () => new Promise((resolve) => { resolveMutation = resolve; }),
  refresh: async () => { throw new Error("refresh failed"); },
});
const pending = api.submitDestructiveConfirmation();
key("Escape");
assert.equal(sheet.hidden, false);
resolveMutation();
await pending;
assert.equal(api.state(), null);
assert.equal(sheet.hidden, true);
assert.equal(confirm.disabled, true);
assert.equal(document.activeElement, toast);
assert.equal(messages.at(-1).kind, "warn");

// If refresh succeeds after the invoker has disappeared, focus still reaches the
// durable account status instead of a detached deleted control.
trigger.isConnected = false;
open({ successMessage: "Deleted." });
await api.submitDestructiveConfirmation();
assert.equal(api.state(), null);
assert.equal(document.activeElement, toast);
assert.equal(messages.at(-1).kind, "good");
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
'''
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "portal.html"
            html_path.write_text(html, encoding="utf-8")
            result = subprocess.run(
                ["node", "-e", node_test, str(html_path)],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_sign_in_door_is_a_warm_two_option_landing(self) -> None:
        # OIDC on + wordmark: invite pill, an "or" divider between the two
        # options, full-width actions, and the brand wordmark in copy.
        branded = render_user_portal(
            oidc_enabled=True, owner_exists=True, wordmark=("vivid", "fish")
        )
        self.assertIn('class="hero hero-door"', branded)
        self.assertIn(
            '<span class="vf-pill vf-pill-accent">invite only</span>', branded
        )
        self.assertIn('<div class="hero-or">', branded)
        self.assertIn("Continue with vivid.fish ID", branded)

        # OIDC off + no wordmark: single action (no divider), neutral copy,
        # and no user-facing deployment-brand leak.
        neutral = render_user_portal(oidc_enabled=False, owner_exists=True)
        self.assertNotIn('<div class="hero-or">', neutral)
        self.assertIn("invite only", neutral)
        self.assertIn("Your AI Gateway account controls", neutral)
        self.assertNotIn("vivid.fish account", neutral)
        self.assertNotIn("Continue with vivid", neutral)

    def test_signed_out_without_oidc_offers_setup_not_a_dead_end(self) -> None:
        # Regression: with OIDC unconfigured the door must still offer a real
        # next step (setup, where local-owner/admin-key live), never a dead
        # "OIDC login not configured" chip.
        fresh = render_user_portal(oidc_enabled=False, owner_exists=False)
        self.assertNotIn("OIDC login not configured", fresh)
        self.assertIn('href="/setup"', fresh)
        self.assertIn("Set up this gateway", fresh)
        # A fresh gateway signals "first run" in plain language (no jargon).
        self.assertIn("This gateway is brand new", fresh)

        claimed = render_user_portal(oidc_enabled=False, owner_exists=True)
        self.assertNotIn("OIDC login not configured", claimed)
        self.assertIn('href="/setup"', claimed)
        self.assertIn(">Sign in</a>", claimed)
        # The "Sign in" button stands alone — no operator jargon about identity
        # providers / OIDC at the door (cognitive cost with no user value).
        self.assertNotIn("No identity provider is configured", claimed)
        self.assertNotIn("OIDC", claimed)

        # The thin signed-out surface is preserved: no runtime-key tooling and
        # no operator nav leak into the cold door.
        for html in (fresh, claimed):
            self.assertNotIn('id="runtime-key"', html)
            self.assertNotIn('class="shell-nav"', html)
            self.assertNotIn('href="/admin"', html)

    def test_home_auto_mints_first_key_and_offers_runnable_curl(self) -> None:
        # Stripe-shaped first run: the portal auto-mints the first key for a
        # personal App with no keys yet, and reveals it once with a runnable
        # curl. The wiring (auto-mint guard + reveal) must be present.
        html = render_user_portal(
            oidc_enabled=True,
            account={
                "authenticated": True,
                "user": {"email": "u@example.com", "role": "owner"},
                "apps": [
                    {
                        "id": "app_1",
                        "kind": "personal",
                        "name": "u personal access",
                        "can_mint_keys": True,
                        "active_credentials": 0,
                        "credentials": [],
                    }
                ],
            },
        )
        self.assertIn("autoMintFirstKey", html)
        self.assertIn("personalAppWithNoKey", html)
        # Only fires when there are zero active keys (idempotent guard).
        self.assertIn("active === 0", html)
        self.assertIn("Prove it works", html)

    def test_no_deployment_brand_leaks_without_wordmark(self) -> None:
        # A generic install (no wordmark) must not render Tim's deployment
        # brand in user-facing copy. (Internal CSS comments, the storage-key
        # name, and the public_base_url default are not user-facing chrome.)
        signed_out = render_user_portal(oidc_enabled=True)
        self.assertNotIn("vivid.fish account", signed_out)
        self.assertNotIn("Continue with vivid.fish ID", signed_out)
        self.assertNotIn('class="eyebrow">ai.vivid.fish', signed_out)
        self.assertIn("Continue with AI Gateway ID", signed_out)
        self.assertIn("Your AI Gateway account controls", signed_out)

    def test_signed_out_with_oidc_keeps_setup_fallback(self) -> None:
        # OIDC is the primary path, but operators without an IdP account can
        # still reach the local-owner/admin-key routes via setup.
        html = render_user_portal(
            oidc_enabled=True, owner_exists=True, wordmark=("vivid", "fish")
        )
        self.assertIn("Continue with vivid.fish ID", html)
        self.assertIn('href="/setup"', html)
        self.assertNotIn("OIDC login not configured", html)

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
        # The apps section heading ("Your apps" — uppercased by the brand
        # .vf-section-title) anchors the surface; the kicker counts keys.
        self.assertIn("Your apps", html)
        self.assertIn("Create one App for each tool or script", html)
        self.assertIn("Base URL", html)
        # The surface stays thin — no how-to narration, no operator jargon.
        self.assertNotIn("Tap any key to see what it can do", html)
        self.assertNotIn("Profile editor", html)
        self.assertNotIn("Which profile", html)
        self.assertNotIn("permission Profiles", html)
        self.assertNotIn("policy matrix", html)
        self.assertNotIn("Chat only", html)
        self.assertNotIn("Full runtime access", html)


if __name__ == "__main__":
    unittest.main()
