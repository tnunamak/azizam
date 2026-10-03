"""Server-rendered first-run setup page for the AI Gateway.

The gateway is web-first: a new self-hoster starts the service, opens
``/setup`` in a browser, and creates the first owner directly in the
browser. ``render_setup_page`` is a pure function that returns HTML for
the first-run states:

* OIDC is configured and no owner exists -> primary action that links to
  ``/login?return_to=/setup`` and copy that calls out that the first
  allowed OIDC login becomes owner.
* OIDC is not configured and no owner exists -> primary action is a
  short "create the local owner account" form (email + password). After
  submitting it the operator has an owner session and can mint Apps and
  manage the gateway from the browser.
* An owner/admin already exists -> setup-complete card with links to the
  account portal and the admin dashboard, a sign-out affordance when signed
  in as admin, and a local owner sign-in form when OIDC is unavailable.

Every state offers a small "Recovery / advanced" disclosure that exposes
the host-side ``generate-admin-key`` recipe — the same flow still works
for SSH/headless recovery, it's just not the primary first-run path.

No I/O, no framework imports. Callers compose the returned string into a
response. Chrome (header, fonts, layout tokens) is shared with ``/`` and
``/admin`` via :mod:`gateway.gateway_shell`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from gateway.gateway_shell import (
    SHELL_CSS,
    esc,
    render_shell_header,
)

__all__ = ["render_setup_page"]

_PRODUCT_NAME = json.loads(Path(__file__).with_name("product_identity.json").read_text())["name"]


_PAGE_CSS = """
  .shell-main {
    max-width: 720px;
    gap: var(--space-5);
  }
  .setup-card {
    min-width: 0;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: clamp(var(--space-4), 4vw, var(--space-6));
    display: grid;
    gap: var(--space-4);
  }
  .setup-card.hero {
    background:
      linear-gradient(180deg, rgba(123,196,200,0.07), rgba(123,196,200,0.015)),
      var(--panel);
    border-color: var(--line-strong);
  }
  .setup-card h2 {
    margin: 0;
    font-size: 1.75rem;
    line-height: 1.15;
    color: var(--text);
  }
  .setup-card p {
    margin: 0;
    color: var(--muted);
    line-height: 1.55;
  }
  .setup-card p strong { color: var(--text); }
  .setup-card .eyebrow {
    color: var(--soft);
    font-size: 0.74rem;
    font-weight: 750;
    letter-spacing: 0.16em;
    margin: 0;
    text-transform: uppercase;
  }
  .setup-card .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: var(--radius-pill);
    color: var(--text);
    background: var(--panel-2);
    padding: 0.45rem 0.95rem;
    min-height: 2.5rem;
    font-size: 0.92rem;
  }
  .pill.primary {
    background: var(--accent);
    color: var(--accent-ink);
    border-color: transparent;
    font-weight: 650;
  }
  .pill.primary:hover { text-decoration: none; filter: brightness(1.06); }
  .pill.secondary {
    background: var(--panel-2);
    color: var(--text);
  }
  .checklist {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .checklist li {
    display: grid;
    grid-template-columns: 1.25rem 1fr;
    gap: var(--space-2);
    align-items: start;
    color: var(--muted);
    line-height: 1.5;
  }
  .checklist li.ok { color: var(--good); }
  .checklist li.todo { color: var(--muted); }
  .checklist .mark {
    display: inline-block;
    width: 1rem;
    text-align: center;
    color: inherit;
  }
  .recovery {
    background: var(--panel-2);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    padding: var(--space-3);
    display: grid;
    gap: var(--space-2);
  }
  .recovery summary {
    cursor: pointer;
    color: var(--muted);
    font-size: 0.92rem;
  }
  .recovery[open] summary { color: var(--text); }
  .recovery code {
    display: inline-block;
    background: color-mix(in srgb, var(--bg) 60%, transparent);
    padding: 0.15rem 0.4rem;
    border-radius: var(--radius-sm);
    color: var(--text);
  }
  .recovery pre {
    min-width: 0;
    max-width: 100%;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    margin: 0;
    padding: var(--space-3);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--bg) 60%, transparent);
    color: var(--text);
    font: 0.85rem/1.5 var(--font-mono);
    overflow-x: auto;
  }
  .meta {
    color: var(--soft);
    font-size: 0.85rem;
  }
  .setup-form {
    display: grid;
    gap: var(--space-2);
    margin: 0;
  }
  .setup-form label {
    display: grid;
    gap: var(--space-1);
    color: var(--muted);
    font-size: 0.88rem;
  }
  .setup-form input {
    box-sizing: border-box;
    min-width: 0;
    width: 100%;
    min-height: 2.75rem;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--bg) 66%, transparent);
    color: var(--text);
    padding: 0.65rem 0.8rem;
  }
  .setup-form button {
    width: fit-content;
    min-height: 2.75rem;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    padding: 0.65rem 1rem;
    font-weight: 650;
    cursor: pointer;
  }
  .setup-error {
    border: 1px solid color-mix(in srgb, var(--bad) 42%, var(--line));
    border-radius: var(--radius-sm);
    background: var(--bad-bg);
    color: var(--bad);
    padding: var(--space-3);
  }
  .setup-columns {
    display: grid;
    gap: var(--space-4);
  }
  .setup-card h3 {
    margin: 0;
    font-size: 1.05rem;
  }
  .setup-secret {
    display: grid;
    gap: var(--space-2);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--good) 10%, var(--panel-2));
    padding: var(--space-3);
  }
  .setup-secret code, .setup-snippet {
    display: block;
    overflow-wrap: anywhere;
    color: var(--text);
  }
  .setup-snippet {
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--bg) 62%, transparent);
    padding: var(--space-3);
    font: 0.85rem/1.5 var(--font-mono);
  }
  .setup-secret-warn { margin: 0; color: var(--good); line-height: 1.5; }
  .setup-secret-sub {
    margin: var(--space-1) 0 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .setup-copy-block {
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--bg) 62%, transparent);
    overflow: hidden;
  }
  .setup-copy-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--line);
  }
  .setup-copy-label { color: var(--muted); font-size: 0.78rem; font-weight: 600; }
  .setup-copy-btn {
    min-height: 1.9rem;
    padding: 0.2rem 0.7rem;
    border-radius: var(--radius-pill);
    border: 1px solid var(--line);
    background: var(--panel-2);
    color: var(--accent);
    font-size: 0.78rem;
    cursor: pointer;
  }
  .setup-copy-code {
    margin: 0;
    padding: var(--space-3);
    font: 0.82rem/1.55 var(--font-mono);
    color: var(--text);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .doctor-list {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .doctor-list li {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--space-2);
    align-items: start;
    color: var(--muted);
  }
  .doctor-dot {
    width: 0.65rem;
    height: 0.65rem;
    margin-top: 0.45rem;
    border-radius: var(--radius-pill);
    background: var(--soft);
  }
  .doctor-list li.ok .doctor-dot { background: var(--good); }
  .doctor-list li.warn .doctor-dot { background: var(--warn); }
  .doctor-list li.bad .doctor-dot { background: var(--bad); }
  .doctor-list strong {
    display: block;
    color: var(--text);
    font-weight: 650;
  }
  @media (max-width: 540px) {
    .setup-card { padding: var(--space-4); }
    .setup-card h2 { font-size: 1.45rem; }
  }
"""

# Retained native POST responses share the public React presentation. These
# same-origin font assets are emitted from the pinned Geist dependency.
_PUBLIC_SETUP_CSS = """
  @font-face {
    font-family: Geist; font-style: normal; font-display: swap;
    font-weight: 100 900;
    src: url('/ui/_next/static/media/Geist-Variable.0b78ff37.woff2') format('woff2');
  }
  @font-face {
    font-family: 'Geist Mono'; font-style: normal; font-display: swap;
    font-weight: 100 900;
    src: url('/ui/_next/static/media/GeistMono-Variable.723e11e5.woff2') format('woff2');
  }
  html.setup-public {
    color-scheme: light;
    --bg: #ffffff; --panel: #ffffff; --panel-2: #f4f4f5;
    --line: #e4e4e7; --line-strong: #d4d4d8;
    --text: #18181b; --muted: #65656e; --soft: #65656e;
    --accent: #087f75; --accent-ink: #ffffff; --link: #087f75;
    --bad: #b42335; --bad-bg: #fcecef;
    --font-sans: 'Geist', sans-serif;
    --font-serif: var(--font-sans);
    --font-mono: 'Geist Mono', monospace;
    --radius: 0.75rem; --radius-sm: 0.5rem;
  }
  html.setup-public.dark {
    color-scheme: dark;
    --bg: #18181b; --panel: #202023; --panel-2: #27272a;
    --line: #343438; --line-strong: #52525b;
    --text: #fafafa; --muted: #a1a1aa; --soft: #a1a1aa;
    --accent: #69d8c7; --accent-ink: #103b35; --link: #69d8c7;
    --bad: #ff91a1; --bad-bg: #40272e;
  }
  .setup-public body {
    background: var(--bg); font: 14px/1.5 var(--font-sans);
    -webkit-font-smoothing: antialiased;
  }
  .setup-public .shell-header {
    position: static; background: var(--bg); border: 0; backdrop-filter: none;
  }
  .setup-public .shell-header-inner {
    max-width: none; padding: 16px 20px; justify-content: space-between;
  }
  .setup-public .shell-brand { gap: 8px; font-size: 14px; min-height: 44px; }
  .setup-public .shell-brand-dot { box-shadow: none; }
  .setup-public .shell-main {
    width: 100%; max-width: 552px; padding: 48px 20px; margin: 0 auto;
  }
  .setup-public .setup-card, .setup-public .setup-card.hero {
    background: var(--panel); border-color: var(--line); padding: 24px;
    gap: 24px; box-shadow: 0 1px 3px rgb(0 0 0 / 5%);
  }
  .setup-public .setup-card h1, .setup-public .setup-card h2 {
    margin: 0; font: 600 20px/1.3 var(--font-sans); color: var(--text);
  }
  .setup-public .setup-card p { line-height: 1.5; }
  .setup-public .setup-card .eyebrow {
    font: 500 12px/1.4 var(--font-sans); letter-spacing: 0;
    text-transform: none;
  }
  .setup-public .setup-form { gap: 16px; }
  .setup-public .setup-form label {
    color: var(--text); font-size: 14px; font-weight: 500; gap: 8px;
  }
  .setup-public .setup-form input {
    background: transparent; border-color: var(--line-strong); height: 44px;
    font-weight: 400; padding: 8px 12px;
  }
  .setup-public .setup-form button {
    width: 100%; padding: 8px 16px; border-radius: 0.5rem;
    font-size: 14px; font-weight: 500;
  }
  .setup-public .pill {
    min-height: 44px; border-radius: 0.5rem; font-size: 14px; font-weight: 500;
  }
  .setup-public .recovery { background: transparent; padding: 16px 0 0; border: 0; border-top: 1px solid var(--line); }
  .setup-public .recovery summary { min-height: 44px; padding: 12px 0; font-size: 14px; font-weight: 500; }
  .setup-public .recovery pre { background: var(--panel-2); font-size: 12px; padding: 12px; }
  .setup-public .recovery code { background: var(--panel-2); font-size: 12px; overflow-wrap: anywhere; }
  .setup-public .setup-error { background: var(--bad-bg); color: var(--bad); font-size: 14px; }
  .setup-public :focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
  .setup-theme-toggle { width: 44px; height: 44px; background: transparent; border: 0; border-radius: 0.5rem; cursor: pointer; }
  .setup-theme-toggle:hover { background: var(--panel-2); }
  .setup-theme-toggle svg { width: 20px; height: 20px; vertical-align: middle; }
  .setup-theme-toggle .moon { display: none; }
  .dark .setup-theme-toggle .sun { display: none; }
  .dark .setup-theme-toggle .moon { display: inline; }
  @media (max-width: 540px) {
    .setup-public .shell-main { padding-top: 32px; }
  }
"""

_PUBLIC_THEME_SCRIPT = """
<script>
(function () {
  function prefersDark() { return window.matchMedia('(prefers-color-scheme: dark)').matches; }
  var theme;
  try { theme = localStorage.getItem('theme'); } catch (e) {}
  document.documentElement.classList.toggle('dark', theme === 'dark' || (theme !== 'light' && prefersDark()));
  document.addEventListener('click', function (event) {
    if (!event.target.closest('[data-setup-theme]')) return;
    var dark = document.documentElement.classList.toggle('dark');
    try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch (e) {}
  });
}());
</script>
"""


# ---------------------------------------------------------------------------
# Local owner forms: create (first-run) and sign-in (returning operator)
# ---------------------------------------------------------------------------


def _local_owner_create_form(
    *,
    create_error: str | None,
    prefill_email: str | None = None,
) -> str:
    error_html = (
        f'<p class="setup-error" role="alert">{esc(create_error)}</p>'
        if create_error
        else ""
    )
    email_value = esc(prefill_email or "")
    return (
        error_html
        + '<form class="setup-form" method="post" action="/setup/local-owner">'
        "<label><span>Email or username</span>"
        f'<input name="email" type="text" autocomplete="username" '
        f'spellcheck="false" required value="{email_value}"></label>'
        "<label><span>Password</span>"
        '<input name="password" type="password" autocomplete="new-password" '
        "required></label>"
        '<label><span>Confirm password</span>'
        '<input name="password_confirm" type="password" '
        'autocomplete="new-password" required></label>'
        '<button type="submit">Create owner and continue</button>'
        "</form>"
    )


def _local_owner_signin_form(
    *,
    login_error: str | None,
    prefill_email: str | None = None,
) -> str:
    error_html = (
        f'<p class="setup-error" role="alert">{esc(login_error)}</p>'
        if login_error
        else ""
    )
    email_value = esc(prefill_email or "")
    return (
        error_html
        + '<form class="setup-form" method="post" action="/setup/local-login">'
        "<label><span>Email or username</span>"
        f'<input name="email" type="text" autocomplete="username" '
        f'spellcheck="false" required value="{email_value}"></label>'
        '<label><span>Password</span>'
        '<input name="password" type="password" '
        'autocomplete="current-password" required></label>'
        '<button type="submit">Sign in</button>'
        "</form>"
    )


# ---------------------------------------------------------------------------
# Recovery disclosure (admin-key paste) — always advanced, never primary
# ---------------------------------------------------------------------------


def _admin_key_claim_form(
    *,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
) -> str:
    if not can_claim_with_admin_key:
        return ""
    error_html = (
        f'<p class="setup-error" role="alert">{esc(claim_error)}</p>'
        if claim_error
        else ""
    )
    return (
        error_html
        + '<form class="setup-form" method="post" action="/setup/claim">'
        "<label><span>Admin key (recovery)</span>"
        '<input name="admin_key" type="password" autocomplete="off" '
        'spellcheck="false" placeholder="oak_..." required></label>'
        '<button type="submit">Open dashboard</button>'
        "</form>"
    )


def _recovery_admin_key_details(
    *,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
    summary: str = "Recovery: paste an admin key",
) -> str:
    open_attr = " open" if claim_error else ""
    return (
        f'<details class="recovery"{open_attr}>'
        f"<summary>{esc(summary)}</summary>"
        "<p>Advanced recovery for SSH or headless hosts — most people never "
        "need this. The sign-in above is the normal way in. If you are locked "
        "out at the console, run this on the host to mint a one-time admin key "
        "and paste it here:</p>"
        "<pre>./venv/bin/python proxy.py generate-admin-key owner-cli</pre>"
        + _admin_key_claim_form(
            claim_error=claim_error,
            can_claim_with_admin_key=can_claim_with_admin_key,
        )
        + "</details>"
    )


# ---------------------------------------------------------------------------
# Claim cards: OIDC and no-OIDC variants for the no-owner state
# ---------------------------------------------------------------------------


def _claim_card_oidc(
    *,
    login_url: str,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
) -> str:
    return (
        '<section class="setup-card hero" aria-labelledby="claim-heading">'
        '<h2 id="claim-heading">Claim this gateway</h2>'
        "<p>No owner has signed in yet. The <strong>first allowed OIDC login "
        "becomes the owner</strong> of this gateway and can invite others, "
        "create Apps, and manage permission Profiles from the admin dashboard.</p>"
        '<div class="actions">'
        f'<a class="pill primary" href="{esc(login_url)}">'
        "Sign in to claim owner</a>"
        "</div>"
        + _recovery_admin_key_details(
            claim_error=claim_error,
            can_claim_with_admin_key=can_claim_with_admin_key,
            summary="OIDC not working? Use an admin key",
        )
        + "</section>"
    )


def _claim_card_local_owner(
    *,
    create_error: str | None,
    create_email: str | None,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
) -> str:
    return (
        '<section class="setup-card hero" aria-labelledby="claim-heading">'
        '<h2 id="claim-heading">Create the local owner account</h2>'
        "<p>No owner has signed in yet, and OIDC is not configured. Pick a "
        "sign-in name (an email or any username) and a password to claim this "
        "gateway. You can configure OIDC later from the admin dashboard.</p>"
        '<ul class="checklist" aria-label="Setup checklist">'
        '<li class="todo"><span class="mark" aria-hidden="true">○</span>'
        "<span>Create the local owner account below.</span></li>"
        '<li class="todo"><span class="mark" aria-hidden="true">○</span>'
        "<span>Land in your portal with a working API key, minted for "
        "you.</span></li>"
        '<li class="todo"><span class="mark" aria-hidden="true">○</span>'
        "<span>Open the admin dashboard to configure OIDC, Profiles, and "
        "shared Apps.</span></li>"
        "</ul>"
        + _local_owner_create_form(
            create_error=create_error,
            prefill_email=create_email,
        )
        + _recovery_admin_key_details(
            claim_error=claim_error,
            can_claim_with_admin_key=can_claim_with_admin_key,
        )
        + "</section>"
    )


# ---------------------------------------------------------------------------
# Setup-complete card (owner already exists)
# ---------------------------------------------------------------------------


def _complete_card(
    *,
    is_admin_signed_in: bool,
    actor_label: str | None,
    oidc_enabled: bool,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
    login_error: str | None,
    login_email: str | None,
) -> str:
    # A signed-in owner who LANDS on /setup is redirected to the portal (see the
    # GET /setup handler), so this branch is only reached mid-operation — a POST
    # re-render (e.g. a first-app error) or direct recovery. In that context the
    # user is already doing operator work, so there's no hero to greet them: a
    # marketing "you own this gateway" card would be pure cognitive overhead.
    # Emit nothing here and let the operator tool cards stand on their own.
    if is_admin_signed_in:
        return ""
    if oidc_enabled:
        signed_in_line = (
            "<p>Sign in with your OIDC account to manage Apps and Profiles.</p>"
            + _recovery_admin_key_details(
                claim_error=claim_error,
                can_claim_with_admin_key=can_claim_with_admin_key,
                summary="OIDC not working? Use an admin key",
            )
        )
    else:
        signed_in_line = (
            "<p>Sign in with your local owner account to manage Apps and "
            "Profiles.</p>"
            + _local_owner_signin_form(
                login_error=login_error,
                prefill_email=login_email,
            )
            + _recovery_admin_key_details(
                claim_error=claim_error,
                can_claim_with_admin_key=can_claim_with_admin_key,
            )
        )
    return (
        '<section class="setup-card hero" aria-labelledby="complete-heading">'
        '<h2 id="complete-heading">This gateway has an owner</h2>'
        # No paragraph narrating the two buttons below it — the heading + the
        # labeled buttons + the sign-in line carry it.
        + signed_in_line
        + '<div class="actions">'
        '<a class="pill primary" href="/">Open user portal</a>'
        '<a class="pill secondary" href="/admin">Open admin dashboard</a>'
        "</div>"
        "</section>"
    )


# ---------------------------------------------------------------------------
# Doctor / first-app cards (unchanged behaviorally)
# ---------------------------------------------------------------------------


def _setup_doctor_card(
    *,
    doctor_items: list[Mapping[str, Any]],
) -> str:
    if not doctor_items:
        return ""
    rows = []
    for item in doctor_items:
        state = str(item.get("state") or "warn")
        if state not in {"ok", "warn", "bad"}:
            state = "warn"
        label = esc(item.get("label") or "Check")
        detail = esc(item.get("detail") or "")
        rows.append(
            f'<li class="{state}"><span class="doctor-dot" aria-hidden="true"></span>'
            f"<span><strong>{label}</strong>{detail}</span></li>"
        )
    return (
        '<section class="setup-card" aria-labelledby="doctor-heading">'
        '<p class="eyebrow">Operator checks</p>'
        '<h2 id="doctor-heading">Is the gateway wired up?</h2>'
        '<p class="meta">Backends and config — for diagnosing a gateway that '
        "isn't responding.</p>"
        '<ul class="doctor-list">'
        + "".join(rows)
        + "</ul>"
        "</section>"
    )


def _setup_copy_block(label: str, value: str) -> str:
    """A titled code block with its own Copy button (server-rendered).

    The raw value is carried in a data attribute so a tiny inline copy script
    can put it on the clipboard without re-reading the (wrapped) text node.
    """
    return (
        '<div class="setup-copy-block">'
        '<div class="setup-copy-head">'
        f'<span class="setup-copy-label">{esc(label)}</span>'
        '<button type="button" class="setup-copy-btn" '
        f'data-copy="{esc(value)}">Copy</button>'
        "</div>"
        f'<pre class="setup-copy-code">{esc(value)}</pre>'
        "</div>"
    )


# Minimal clipboard helper. The setup page is otherwise script-free; this only
# wires the Copy buttons in the one-time key reveal.
_SETUP_COPY_SCRIPT = """
<script>
(() => {
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".setup-copy-btn");
    if (!btn) return;
    const value = btn.getAttribute("data-copy") || "";
    const original = btn.textContent;
    try {
      await navigator.clipboard.writeText(value);
      btn.textContent = "Copied";
    } catch (_) {
      btn.textContent = "Copy failed";
    }
    window.setTimeout(() => { btn.textContent = original; }, 1300);
  });
})();
</script>
"""


def _first_app_card(
    *,
    csrf_token: str,
    public_base_url: str,
    app_count: int,
    active_key_count: int,
    personal_app_name: str | None,
    personal_active_key_count: int,
    app_error: str | None,
    one_time_key: Mapping[str, Any] | None,
) -> str:
    secret_html = ""
    if one_time_key:
        key = esc(one_time_key.get("api_key") or "")
        app_name = esc(one_time_key.get("app_name") or "Setup App")
        base = public_base_url.rstrip("/") + "/v1"
        base_esc = esc(base)
        curl_cmd = f'curl {base_esc}/models -H "Authorization: Bearer {key}"'
        env_block = (
            f'export OPENAI_BASE_URL="{base_esc}"\n'
            f'export OPENAI_API_KEY="{key}"'
        )
        secret_html = (
            '<div class="setup-secret" role="status">'
            f"<p class=\"setup-secret-warn\"><strong>{app_name}</strong> has a new "
            "key. Copy it now — it is shown once and cannot be retrieved "
            "again. If you lose it, mint a new one.</p>"
            + _setup_copy_block("Your API key", key)
            + '<p class="setup-secret-sub">Prove it works:</p>'
            + _setup_copy_block("Run this", curl_cmd)
            + _setup_copy_block(
                "Or set env for OpenAI-compatible SDKs", env_block
            )
            + "</div>"
        )
    error_html = (
        f'<p class="setup-error" role="alert">{esc(app_error)}</p>'
        if app_error
        else ""
    )
    if personal_app_name:
        target_count = personal_active_key_count
        lead = (
            f"Your personal App is {personal_app_name}. Mint another key only "
            "when a new tool or script needs its own boundary."
            if target_count
            else f"Your personal App is {personal_app_name}. Mint its first "
            "runtime key to connect an SDK or local client."
        )
        count_line = f"Personal App keys: {target_count}. Active runtime keys: {active_key_count}."
        app_name_field = ""
    else:
        target_count = active_key_count
        lead = (
            "You already have a runtime key. Create another only when a new tool "
            "or script needs its own boundary."
            if target_count
            else "Create one personal App for the first tool you want to connect. "
            "The App gets one runtime key and inherits the default Profile."
        )
        count_line = f"Apps: {app_count}. Active runtime keys: {active_key_count}."
        app_name_field = (
            '<label><span>App name</span>'
            '<input name="app_name" value="first-app" maxlength="80" required></label>'
        )
    button = "Mint another key" if target_count else "Mint first App key"
    return (
        '<section class="setup-card" aria-labelledby="first-app-heading">'
        '<p class="eyebrow">Operator key</p>'
        '<h2 id="first-app-heading">Mint a key from the console</h2>'
        f"<p>{esc(lead)}</p>"
        f"<p class=\"meta\">{esc(count_line)}</p>"
        + secret_html
        + error_html
        + '<form class="setup-form" method="post" action="/setup/first-app">'
        + f'<input type="hidden" name="csrf_token" value="{esc(csrf_token)}">'
        + app_name_field
        + '<label><span>Key label</span>'
        + '<input name="key_label" value="Setup key" maxlength="80" required></label>'
        + f"<button type=\"submit\">{esc(button)}</button>"
        "</form>"
        "</section>"
        + (_SETUP_COPY_SCRIPT if one_time_key else "")
    )


def render_setup_page(
    *,
    title: str = "AI Gateway setup",
    brand: str = "AI Gateway",
    wordmark: tuple[str, str] | None = None,
    owner_or_admin_exists: bool,
    oidc_enabled: bool,
    login_url: str = "/login?return_to=/setup",
    is_admin_signed_in: bool = False,
    actor_label: str | None = None,
    logout_url: str | None = None,
    claim_error: str | None = None,
    can_claim_with_admin_key: bool = False,
    create_error: str | None = None,
    create_email: str | None = None,
    login_error: str | None = None,
    login_email: str | None = None,
    csrf_token: str = "",
    public_base_url: str = "http://127.0.0.1:5000",
    app_count: int = 0,
    active_key_count: int = 0,
    personal_app_name: str | None = None,
    personal_active_key_count: int = 0,
    app_error: str | None = None,
    one_time_key: Mapping[str, Any] | None = None,
    doctor_items: list[Mapping[str, Any]] | None = None,
) -> str:
    """Return the first-run setup HTML for the gateway.

    States:
      * ``owner_or_admin_exists=False`` + ``oidc_enabled=True`` -> claim card
        with the OIDC sign-in primary action.
      * ``owner_or_admin_exists=False`` + ``oidc_enabled=False`` -> local
        owner create form (email + password) as the primary action; the
        admin-key paste is demoted to a recovery disclosure.
      * ``owner_or_admin_exists=True`` -> setup-complete card. When the
        viewer is not signed in and OIDC is unavailable, the local owner
        sign-in form remains available so the dashboard is not a dead end.

    When the viewer is signed in as an admin/owner, the shared header
    surfaces "Admin" and a Sign out link.
    """

    if not owner_or_admin_exists:
        body = (
            _claim_card_oidc(
                login_url=login_url,
                claim_error=claim_error,
                can_claim_with_admin_key=can_claim_with_admin_key,
            )
            if oidc_enabled
            else _claim_card_local_owner(
                create_error=create_error,
                create_email=create_email,
                claim_error=claim_error,
                can_claim_with_admin_key=can_claim_with_admin_key,
            )
        )
    else:
        body = _complete_card(
            is_admin_signed_in=is_admin_signed_in,
            actor_label=actor_label,
            oidc_enabled=oidc_enabled,
            claim_error=claim_error,
            can_claim_with_admin_key=can_claim_with_admin_key,
            login_error=login_error,
            login_email=login_email,
        )
        if is_admin_signed_in:
            body += (
                '<div class="setup-columns">'
                + _first_app_card(
                    csrf_token=csrf_token,
                    public_base_url=public_base_url,
                    app_count=app_count,
                    active_key_count=active_key_count,
                    personal_app_name=personal_app_name,
                    personal_active_key_count=personal_active_key_count,
                    app_error=app_error,
                    one_time_key=one_time_key,
                )
                + _setup_doctor_card(doctor_items=doctor_items or [])
                + "</div>"
            )

    header_html = render_shell_header(
        title=title,
        brand=brand,
        wordmark=wordmark,
        current="",
        show_account=is_admin_signed_in,
        show_admin=is_admin_signed_in,
        show_status=is_admin_signed_in,
        actor_label=actor_label if is_admin_signed_in else None,
        logout_url=logout_url if is_admin_signed_in else None,
    )

    public_presentation = not is_admin_signed_in
    if public_presentation:
        # The native forms retain their routes, values and validation messages;
        # only their surrounding public chrome changes.
        body = body.replace("<h2 ", "<h1 ", 1).replace("</h2>", "</h1>", 1)
        header_html = (
            '<header class="shell-header"><div class="shell-header-inner">'
            f'<a class="shell-brand" href="/" aria-label="{esc(_PRODUCT_NAME)} home">'
            '<svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" '
            'fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M6.5 12c.94-3.46 4.94-6 8.5-6 3.56 0 6.06 2.54 7 6-.94 3.47-3.44 6-7 6s-7.56-2.53-8.5-6Z"/>'
            '<path d="M18 12v.5M16 17.93a9.77 9.77 0 0 1 0-11.86M7 10.67C7 8 5.58 5.97 2.73 5.5c-1 1.5-1 5 .23 6.5-1.24 1.5-1.24 5-.23 6.5C5.58 18.03 7 16 7 13.33"/>'
            '<path d="M10.46 7.26C10.2 5.88 9.17 4.24 8 3h5.8a2 2 0 0 1 1.98 1.67l.23 1.4M10.46 16.74C10.2 18.12 9.17 19.76 8 21h5.8a2 2 0 0 0 1.98-1.67l.23-1.4"/></svg>'
            f'{esc(_PRODUCT_NAME)}</a>'
            '<button class="setup-theme-toggle" type="button" '
            'data-setup-theme aria-label="Toggle theme">'
            '<svg class="sun" aria-hidden="true" viewBox="0 0 24 24" '
            'fill="none" stroke="currentColor" stroke-width="2">'
            '<circle cx="12" cy="12" r="4"/>'
            '<path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg>'
            '<svg class="moon" aria-hidden="true" viewBox="0 0 24 24" '
            'fill="none" stroke="currentColor" stroke-width="2">'
            '<path d="M20.5 13A9 9 0 0 1 11 3.5 9 9 0 1 0 20.5 13Z"/></svg>'
            '</button></div></header>'
        )
    presentation_css = _PUBLIC_SETUP_CSS if public_presentation else ""
    theme_script = _PUBLIC_THEME_SCRIPT if public_presentation else ""
    html_class = ' class="setup-public"' if public_presentation else ""

    return (
        "<!doctype html>"
        f'<html lang="en"{html_class}><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f"<style>{SHELL_CSS}{_PAGE_CSS}{presentation_css}</style>"
        + theme_script
        + "</head><body>"
        + header_html
        + '<main class="shell-main">'
        + body
        + "</main></body></html>"
    )
