"""Server-rendered first-run setup page for the AI Gateway.

The gateway is web-first: a new self-hoster starts the service, opens
``/setup`` in a browser, and either claims the first owner via OIDC or
sees what they still need to configure. ``render_setup_page`` is a pure
function that returns HTML for the first-run states:

* OIDC is configured and no owner exists -> primary action that links to
  ``/login?return_to=/setup`` and copy that calls out that the first
  allowed OIDC login becomes owner.
* OIDC is not configured and no owner exists -> shows the local admin-key
  claim path: run the host-side ``generate-admin-key`` command, paste the
  printed ``oak_`` key, and receive a signed browser session.
* An owner/admin already exists -> setup-complete card with links to the
  account portal and the admin dashboard, a sign-out affordance when signed
  in as admin, and an admin-key sign-in form when OIDC is unavailable.

No I/O, no framework imports. Callers compose the returned string into a
response. Chrome (header, fonts, layout tokens) is shared with ``/`` and
``/admin`` via :mod:`gateway.gateway_shell`.
"""

from __future__ import annotations

from typing import Any, Mapping

from gateway.gateway_shell import (
    SHELL_CSS,
    esc,
    render_shell_header,
)

__all__ = ["render_setup_page"]


_PAGE_CSS = """
  .shell-main {
    max-width: 720px;
    gap: var(--space-5);
  }
  .setup-card {
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


def _claim_card_oidc(
    *,
    login_url: str,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
) -> str:
    return (
        '<section class="setup-card hero" aria-labelledby="claim-heading">'
        '<p class="eyebrow">First-run setup</p>'
        '<h2 id="claim-heading">Claim this gateway</h2>'
        "<p>No owner has signed in yet. The <strong>first allowed OIDC login "
        "becomes the owner</strong> of this gateway and can invite others, "
        "create Apps, and manage permission Profiles from the admin dashboard.</p>"
        '<div class="actions">'
        f'<a class="pill primary" href="{esc(login_url)}">'
        "Sign in to claim owner</a>"
        "</div>"
        + _oidc_recovery_details(
            claim_error=claim_error,
            can_claim_with_admin_key=can_claim_with_admin_key,
        )
        + "</section>"
    )


def _admin_key_claim_form(
    *,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
) -> str:
    if not can_claim_with_admin_key:
        return (
            '<div class="recovery">'
            "<p>Browser admin sessions need "
            "<code>admin.session_signing_key</code> or "
            "<code>AI_GATEWAY_ADMIN_SESSION_KEY</code>. Set that secret, "
            "restart, then return here to claim the browser session.</p>"
            "</div>"
        )
    error_html = (
        f'<p class="setup-error" role="alert">{esc(claim_error)}</p>'
        if claim_error
        else ""
    )
    return (
        error_html
        + '<form class="setup-form" method="post" action="/setup/claim">'
        "<label><span>Admin key</span>"
        '<input name="admin_key" type="password" autocomplete="off" '
        'spellcheck="false" placeholder="oak_..." required></label>'
        "<button type=\"submit\">Open dashboard</button>"
        "</form>"
    )


def _oidc_recovery_details(
    *,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
) -> str:
    open_attr = " open" if claim_error else ""
    return (
        f'<details class="recovery"{open_attr}>'
        "<summary>OIDC not working?</summary>"
        "<p>Run the host-side admin-key command and paste the printed "
        "<code>oak_...</code> key here to open a browser admin session.</p>"
        "<pre>./venv/bin/python proxy.py generate-admin-key owner-cli</pre>"
        + _admin_key_claim_form(
            claim_error=claim_error,
            can_claim_with_admin_key=can_claim_with_admin_key,
        )
        + "</details>"
    )


def _claim_card_no_oidc(
    *,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
) -> str:
    return (
        '<section class="setup-card hero" aria-labelledby="claim-heading">'
        '<p class="eyebrow">First-run setup</p>'
        '<h2 id="claim-heading">Claim this gateway from this machine</h2>'
        "<p>No owner has signed in yet, and OIDC is not configured. Run one "
        "host-side command to mint an <strong>oak_ admin key</strong>, paste "
        "it here, and the browser will receive an owner/admin session.</p>"
        '<ul class="checklist" aria-label="Setup checklist">'
        '<li class="todo"><span class="mark" aria-hidden="true">○</span>'
        "<span>On the gateway host, run "
        "<code>./venv/bin/python proxy.py generate-admin-key owner-cli</code>.</span></li>"
        '<li class="todo"><span class="mark" aria-hidden="true">○</span>'
        "<span>Paste the printed <code>oak_...</code> value below. It is shown once.</span></li>"
        '<li class="todo"><span class="mark" aria-hidden="true">○</span>'
        "<span>Use the admin dashboard to create Apps, Profiles, and runtime keys.</span></li>"
        "</ul>"
        + _admin_key_claim_form(
            claim_error=claim_error,
            can_claim_with_admin_key=can_claim_with_admin_key,
        )
        + "</section>"
    )


def _recovery_details() -> str:
    return (
        '<details class="recovery">'
        "<summary>Headless or recovery setup (advanced)</summary>"
        "<p>If you cannot run OIDC, you can mint a service-account admin "
        "key from the command line. This is the no-OIDC claim path and the "
        "recovery/headless automation path; OIDC deployments should normally "
        "claim owner by signing in.</p>"
        "<pre>python proxy.py generate-admin-key &lt;service-account-name&gt; "
        "[role]</pre>"
        '<p class="meta">The printed <code>oak_</code> key is an admin key, '
        "not a runtime App key. Use <code>opk_</code> keys only for SDKs "
        "and local AI clients.</p>"
        "</details>"
    )


def _complete_card(
    *,
    is_admin_signed_in: bool,
    actor_label: str | None,
    oidc_enabled: bool,
    claim_error: str | None,
    can_claim_with_admin_key: bool,
) -> str:
    if is_admin_signed_in and actor_label:
        signed_in_line = (
            f"<p>Signed in as <strong>{esc(actor_label)}</strong>.</p>"
        )
    elif oidc_enabled:
        signed_in_line = (
            "<p>Sign in with your OIDC account to manage Apps and Profiles.</p>"
            + _oidc_recovery_details(
                claim_error=claim_error,
                can_claim_with_admin_key=can_claim_with_admin_key,
            )
        )
    else:
        signed_in_line = (
            "<p>OIDC is not configured. Use an existing admin key to open a "
            "browser admin session.</p>"
            + _admin_key_claim_form(
                claim_error=claim_error,
                can_claim_with_admin_key=can_claim_with_admin_key,
            )
        )
    return (
        '<section class="setup-card hero" aria-labelledby="complete-heading">'
        '<p class="eyebrow">Setup complete</p>'
        '<h2 id="complete-heading">This gateway has an owner</h2>'
        "<p>An owner or admin already claimed this gateway. Use the user "
        "portal to mint App keys, or the admin dashboard to manage shared "
        "Apps, Profiles, and access policy.</p>"
        + signed_in_line
        + '<div class="actions">'
        '<a class="pill primary" href="/">Open user portal</a>'
        '<a class="pill secondary" href="/admin">Open admin dashboard</a>'
        "</div>"
        "</section>"
    )


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
        '<p class="eyebrow">Setup doctor</p>'
        '<h2 id="doctor-heading">Gateway checks</h2>'
        '<ul class="doctor-list">'
        + "".join(rows)
        + "</ul>"
        "</section>"
    )


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
        base = esc(public_base_url.rstrip("/") + "/openai/v1")
        secret_html = (
            '<div class="setup-secret" role="status">'
            f"<p><strong>{app_name}</strong> has a new runtime key. This value is shown once.</p>"
            f"<code>{key}</code>"
            '<div class="setup-snippet">'
            f'export OPENAI_BASE_URL="{base}"<br>'
            f'export OPENAI_API_KEY="{key}"'
            "</div>"
            "</div>"
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
        '<p class="eyebrow">First useful key</p>'
        '<h2 id="first-app-heading">Connect a client</h2>'
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
    )


def render_setup_page(
    *,
    title: str = "AI Gateway setup",
    owner_or_admin_exists: bool,
    oidc_enabled: bool,
    login_url: str = "/login?return_to=/setup",
    is_admin_signed_in: bool = False,
    actor_label: str | None = None,
    logout_url: str | None = None,
    claim_error: str | None = None,
    can_claim_with_admin_key: bool = False,
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
        admin-key claim form.
      * ``owner_or_admin_exists=True`` -> setup-complete card. When the
        viewer is not signed in and OIDC is unavailable, the admin-key claim
        form remains available so the dashboard is not a dead end.

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
            else _claim_card_no_oidc(
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
        current="",
        show_account=is_admin_signed_in,
        show_admin=is_admin_signed_in,
        show_status=is_admin_signed_in,
        actor_label=actor_label if is_admin_signed_in else None,
        logout_url=logout_url if is_admin_signed_in else None,
    )

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f"<style>{SHELL_CSS}{_PAGE_CSS}</style>"
        "</head><body>"
        + header_html
        + '<main class="shell-main">'
        + body
        + "</main></body></html>"
    )
