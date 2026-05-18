"""Server-side HTML rendering for the gateway's user-facing landing page.

``render_user_portal`` is intentionally pure. The live app serves the page at
``/`` (with ``/me`` redirecting here as a legacy browser alias) and the
browser uses either the signed OIDC session cookie or the user's pasted
``opk_`` API key to call existing gateway APIs. That keeps the first
useful user surface small: no new authorization model, no duplicated policy,
and no server-side handling of pasted API keys.

The page shares its chrome (header, nav, sign-out affordance) with
``/admin`` via :mod:`gateway.gateway_shell` so the site reads as one app.
"""

from __future__ import annotations

from typing import Any, Mapping

from gateway.gateway_shell import (
    SHELL_CSS,
    can_admin,
    esc,
    json_for_script,
    render_shell_header,
)

__all__ = ["render_user_portal"]


_PAGE_CSS = """
  .lead { color: var(--muted); margin: 0; line-height: 1.55; }
  .card, .app-card, .app-create {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
    align-content: start;
  }
  .card h3, .app-card h3, .app-create h3 { margin: 0; font-size: 1rem; }
  .card p, .app-card p, .app-create p { margin: 0; color: var(--muted); line-height: 1.5; }
  .actions, .key-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
  }
  .hero {
    display: grid;
    gap: var(--space-3);
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-5);
  }
  .hero h2 {
    margin: 0;
    font-size: 1.2rem;
    color: var(--text);
    text-transform: none;
    letter-spacing: 0;
  }
  .hero .actions a, .hero .actions button { font-size: 0.95rem; }
  .row, .key-form {
    display: grid;
    gap: var(--space-2);
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: end;
  }
  .field {
    display: grid;
    gap: var(--space-1);
    min-width: 0;
  }
  .field span {
    color: var(--muted);
    font-size: 0.8rem;
  }
  input {
    background: oklch(14.5% 0.016 250);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 0.65rem 0.8rem;
    min-height: 2.75rem;
    min-width: 0;
  }
  input:focus-visible, button:focus-visible, a:focus-visible {
    outline: 2px solid var(--focus);
    outline-offset: 2px;
  }
  button {
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 0.65rem 1rem;
    min-height: 2.75rem;
    color: var(--accent-ink);
    background: var(--accent);
    font-weight: 650;
    cursor: pointer;
  }
  button.secondary {
    background: var(--panel-2);
    color: var(--text);
    border-color: var(--line);
  }
  button.danger {
    color: var(--bad);
    background: transparent;
    border-color: var(--line);
  }
  button:hover { filter: brightness(1.06); }
  .pill {
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: 999px;
    color: var(--text);
    background: var(--panel-2);
    padding: 0.4rem 0.85rem;
    min-height: 2.5rem;
    font-size: 0.88rem;
  }
  .pill.primary {
    background: var(--accent);
    color: var(--accent-ink);
    border-color: transparent;
    font-weight: 650;
  }
  .pill.primary:hover { text-decoration: none; filter: brightness(1.06); }
  .message {
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: oklch(14.5% 0.016 250);
    color: var(--muted);
    padding: var(--space-3);
    line-height: 1.5;
  }
  .message:empty { display: none; }
  .message.good { color: var(--good); background: var(--good-bg); }
  .message.warn { color: var(--warn); background: var(--warn-bg); }
  .message.bad { color: var(--bad); background: var(--bad-bg); }
  .message .secret {
    display: grid;
    gap: var(--space-2);
  }
  .message .secret code {
    display: block;
    color: var(--text);
    overflow-wrap: anywhere;
  }
  .snippet {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    background: oklch(14.5% 0.016 250);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-3);
    color: oklch(88% 0.05 168);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.86rem;
    line-height: 1.55;
  }
  .apps-list {
    display: grid;
    gap: var(--space-4);
  }
  .app-card-header, .key-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--space-3);
    align-items: start;
  }
  .app-card-title {
    display: grid;
    gap: var(--space-2);
    min-width: 0;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    min-height: 1.6rem;
    padding: 0.18rem 0.55rem;
    border-radius: 999px;
    border: 1px solid var(--line);
    background: var(--panel-2);
    color: var(--muted);
    font-size: 0.78rem;
  }
  .badge.good { color: var(--good); background: var(--good-bg); }
  .badge.bad { color: var(--bad); background: var(--bad-bg); }
  .badge.warn { color: var(--warn); background: var(--warn-bg); }
  .meta { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .list, .key-list { display: grid; gap: var(--space-2); }
  .key-list { border-top: 1px solid var(--line); padding-top: var(--space-2); }
  .key-row { padding: var(--space-2) 0; }
  .key-row + .key-row { border-top: 1px solid var(--line); }
  .key-row h3 { margin: 0; font-size: 0.94rem; }
  .key-row p { margin: 0; color: var(--muted); }
  .key-note, .empty { color: var(--muted); font-size: 0.88rem; }
  .two-col {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr));
    gap: var(--space-4);
  }
  .access-group { display: grid; gap: var(--space-3); align-content: start; }
  .access-group h3 {
    margin: 0;
    color: var(--muted);
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
  @media (max-width: 720px) {
    .row, .key-form, .app-card-header, .key-row { grid-template-columns: 1fr; }
    .key-actions button, .actions button { width: 100%; }
  }
"""


def _badge(text: Any, cls: str = "") -> str:
    return f'<span class="badge {cls}">{esc(text)}</span>'


def _client_account(account: Mapping[str, Any] | None, *, show_permission_details: bool) -> dict[str, Any]:
    if not account:
        return {"authenticated": False, "apps": []}
    user_value = account.get("user")
    user = user_value if isinstance(user_value, Mapping) else {}
    apps: list[dict[str, Any]] = []
    for app_value in account.get("apps", []):
        if not isinstance(app_value, Mapping):
            continue
        app: dict[str, Any] = {
            "id": app_value.get("id"),
            "kind": app_value.get("kind"),
            "name": app_value.get("name"),
            "disabled_at": app_value.get("disabled_at"),
            "can_mint_keys": app_value.get("can_mint_keys"),
            "active_credentials": app_value.get("active_credentials"),
            "created_at": app_value.get("created_at"),
            "credentials": [
                _sanitize_client_credential(credential)
                for credential in app_value.get("credentials", [])
                if isinstance(credential, Mapping)
            ],
        }
        profile = app_value.get("profile")
        if isinstance(profile, Mapping):
            safe_profile = {
                "id": profile.get("id"),
                "missing": bool(profile.get("missing")),
                "inherited": profile.get("inherited"),
            }
            if show_permission_details:
                safe_profile["name"] = profile.get("name")
                safe_profile["policy_id"] = profile.get("policy_id")
            app["profile"] = safe_profile
        apps.append(app)
    return {
        "authenticated": bool(account.get("authenticated")),
        "user": {
            "email": user.get("email"),
            "name": user.get("name"),
            "role": user.get("role"),
        },
        "apps": apps,
    }


def _sanitize_client_credential(credential: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "prefix",
        "display_name",
        "created_at",
        "expires_at",
        "active",
    }
    return {key: credential.get(key) for key in allowed if key in credential}


def _credential_html(record: Mapping[str, Any]) -> str:
    if not record.get("active", True):
        return ""
    credential_id = esc(record.get("id"))
    prefix = esc(record.get("prefix") or "key")
    label = esc(record.get("display_name") or "API key")
    created = esc(record.get("created_at") or "created")
    return (
        '<div class="key-row" data-key-row="' + credential_id + '">'
        "<div>"
        f"<h3>{label}</h3>"
        f"<p>Prefix <code>{prefix}</code> · {created}</p>"
        "</div>"
        '<div class="key-actions">'
        '<button class="secondary" type="button" data-view-owned-key="'
        + credential_id
        + '" data-key-label="'
        + label
        + '">View access</button>'
        '<button class="danger" type="button" data-revoke-owned-key="'
        + credential_id
        + '" aria-label="Delete API key '
        + label
        + '">Delete key</button>'
        "</div>"
        + "</div>"
    )


def _app_html(app: Mapping[str, Any], *, show_permission_details: bool = False) -> str:
    app_id = esc(app.get("id"))
    name = esc(app.get("name") or app_id or "App")
    profile = app.get("profile") if isinstance(app.get("profile"), Mapping) else None
    permission_text = "your account permissions"
    raw_profile_id = str(app.get("profile_id") or "")
    permission_badge = _badge("account permissions")
    if profile:
        raw_profile_id = str(profile.get("id") or raw_profile_id)
        inherited = bool(profile.get("inherited"))
        if profile.get("missing"):
            permission_text = "permissions that are currently unavailable"
            permission_badge = _badge("permissions unavailable", "bad")
        elif show_permission_details:
            permission_text = str(
                profile.get("name")
                or profile.get("policy_id")
                or raw_profile_id
                or "account permissions"
            )
            permission_badge = _badge(
                "inherited permissions" if inherited else "custom permissions"
            )
        elif not inherited:
            permission_text = "custom account permissions"
            permission_badge = _badge("custom permissions")
    credentials = [
        c
        for c in app.get("credentials", [])
        if isinstance(c, Mapping) and c.get("active", True)
    ]
    credential_html = (
        "".join(_credential_html(c) for c in credentials)
        if credentials
        else '<p class="empty">No API keys for this App yet.</p>'
    )
    can_mint = bool(app.get("can_mint_keys"))
    mint_html = (
        '<form class="key-form" data-mint-owned-key="' + app_id + '">'
        '<label class="field"><span>New API key</span>'
        '<input type="text" name="label" placeholder="MacBook, Open WebUI, deploy script" '
        'aria-label="API key label"></label>'
        '<button type="submit">Create key</button>'
        "</form>"
        if can_mint
        else (
            '<p class="empty">This App cannot create keys until its '
            "permissions are available.</p>"
        )
    )
    return (
        '<article class="app-card" data-app-card="' + app_id + '">'
        '<div class="app-card-header">'
        '<div class="app-card-title">'
        f"<h3>{name}</h3>"
        f"<p>Use this App for one tool or workflow. Its keys use {esc(permission_text)}.</p>"
        '<div class="meta">'
        + permission_badge
        + _badge(f"{app.get('active_credentials') or 0} active keys", "good")
        + "</div>"
        "</div>"
        '<button class="danger" type="button" data-delete-owned-app="'
        + app_id
        + '" data-app-name="'
        + name
        + '">Delete App</button>'
        "</div>"
        + mint_html
        + '<div class="key-list" data-key-list="' + app_id + '">'
        + credential_html
        + "</div>"
        + "</article>"
    )


def _signed_out_hero(*, oidc_enabled: bool, login_url: str) -> str:
    primary = (
        f'<a class="pill primary" href="{esc(login_url)}">Sign in with OIDC</a>'
        if oidc_enabled
        else '<span class="pill">OIDC login not configured</span>'
    )
    return (
        '<section aria-labelledby="hero-heading">'
        '<div class="hero">'
        '<h2 id="hero-heading">Start with an account or key</h2>'
        '<p class="lead">Sign in to create Apps and API keys. Paste an '
        "<code>opk_</code> key if you already have one; "
        "the browser stores it in this tab.</p>"
        f'<div class="actions">{primary}</div>'
        "</div>"
        "</section>"
    )


def _signed_in_account(account: Mapping[str, Any]) -> str:
    user_value = account.get("user")
    user = user_value if isinstance(user_value, Mapping) else {}
    user_label = esc(user.get("email") or user.get("name") or "signed-in user")
    apps = [a for a in account.get("apps", []) if isinstance(a, Mapping)]
    is_admin = can_admin(account)
    create_help = (
        "Create one App per tool, workflow, or script. Keys live inside the App."
    )
    create_form = (
        '<form class="app-create" data-create-owned-app>'
        "<h3>New App</h3>"
        f"<p>{create_help}</p>"
        '<div class="row">'
        '<label class="field"><span>App name</span>'
        '<input type="text" name="name" placeholder="Open WebUI, Cline, render pipeline" '
        'aria-label="App name" required maxlength="80"></label>'
        '<button type="submit">Create App</button>'
        "</div>"
        "</form>"
    )
    apps_html = (
        f'<div id="apps-list" class="apps-list">{"".join(_app_html(app, show_permission_details=is_admin) for app in apps)}</div>'
        if apps
        else '<div id="apps-list" class="apps-list"></div>'
    )
    admin_hint = (
        ' <a href="/admin">Admin</a> manages shared Apps and permission Profiles.'
        if is_admin
        else ""
    )
    return (
        '<section aria-labelledby="apps-heading">'
        '<h2 id="apps-heading">Your Apps</h2>'
        '<p class="hint">'
        + f"Signed in as {user_label}. "
        + "Use View access on any key to see what it can do."
        + admin_hint
        + "</p>"
        + '<div id="account-result" class="message"></div>'
        + create_form
        + apps_html
        + "</section>"
    )


def _runtime_key_section(*, signed_in: bool = False) -> str:
    heading = "Check another key" if signed_in else "Check an API key"
    hint = (
        "Use this only for a key that is not listed above."
        if signed_in
        else "Paste an <code>opk_</code> key if you already have one."
    )
    return (
        '<section aria-labelledby="runtime-heading">'
        f'<h2 id="runtime-heading">{heading}</h2>'
        f'<p class="hint">{hint} This only checks the key from this browser tab.</p>'
        '<div class="card">'
        '<div class="row">'
        '<input id="runtime-key" type="password" autocomplete="off" spellcheck="false" '
        'placeholder="opk_..." aria-describedby="connect-result">'
        '<button id="check-key" type="button">Check access</button>'
        '<button id="forget-key" class="secondary" type="button">Clear pasted key</button>'
        "</div>"
        '<div id="connect-result" class="message">'
        "No key is pasted in this tab."
        "</div>"
        "</div>"
        "</section>"
    )


def _settings_section() -> str:
    return (
        '<section aria-labelledby="settings-heading">'
        '<h2 id="settings-heading">Connection settings</h2>'
        '<p class="hint">Point an OpenAI-compatible App or SDK tool at the gateway. '
        "Other protocols use the same base URL with their own paths.</p>"
        '<div id="openai-snippet" class="snippet"></div>'
        "</section>"
    )


def _access_section() -> str:
    return (
        '<section id="access-section" aria-labelledby="access-heading" hidden>'
        '<h2 id="access-heading">Access</h2>'
        '<p class="hint" id="access-summary"></p>'
        '<div class="two-col">'
        '<div id="capabilities-group" class="access-group" hidden>'
        '<h3>Capabilities</h3>'
        '<div id="capabilities" class="list"></div>'
        '</div>'
        '<div id="models-group" class="access-group" hidden>'
        '<h3>Models and workflows</h3>'
        '<div id="models" class="list"></div>'
        '</div>'
        "</div>"
        "</section>"
    )


def render_user_portal(
    *,
    title: str = "AI Gateway",
    public_base_url: str = "https://ai.vivid.fish",
    account: Mapping[str, Any] | None = None,
    oidc_enabled: bool = False,
    login_url: str = "/login?return_to=/",
    logout_url: str = "/me/logout",
    csrf_token: str = "",
) -> str:
    """Return the normal-user HTML portal."""
    base = public_base_url.rstrip("/") or "https://ai.vivid.fish"
    base_json = json_for_script(base)
    csrf_json = json_for_script(csrf_token)
    title_text = esc(title)

    is_signed_in = bool(account and account.get("authenticated"))
    show_admin = can_admin(account)
    account_json = json_for_script(
        _client_account(account, show_permission_details=show_admin)
    )
    actor_label = None
    if is_signed_in and account is not None:
        user_value = account.get("user")
        user = user_value if isinstance(user_value, Mapping) else {}
        actor_label = (
            str(user.get("email") or user.get("name") or "signed in")
            if user
            else "signed in"
        )

    header_html = render_shell_header(
        title=title,
        current="account",
        show_account=show_admin,
        show_admin=show_admin,
        show_status=show_admin,
        actor_label=actor_label,
        logout_url=logout_url if is_signed_in else None,
    )

    if is_signed_in:
        primary_section = _signed_in_account(account or {})
    else:
        primary_section = _signed_out_hero(
            oidc_enabled=oidc_enabled, login_url=login_url
        )

    body = (
        primary_section
        + _runtime_key_section(signed_in=is_signed_in)
        + _settings_section()
        + _access_section()
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_text}</title>
  <style>{SHELL_CSS}{_PAGE_CSS}</style>
</head>
<body>
  {header_html}
  <main class="shell-main">
    {body}
  </main>
  <script>
(() => {{
  const publicBase = {base_json};
  const csrfToken = {csrf_json};
  let accountSnapshot = {account_json};
  const storageKey = "vivid_fish_ai_gateway_runtime_key";
  const $ = (id) => document.getElementById(id);
  const keyInput = $("runtime-key");
  const result = $("connect-result");
  const baseUrl = publicBase.replace(/\\/$/, "");

  $("openai-snippet").textContent = [
    'export OPENAI_BASE_URL="' + baseUrl + '/v1"',
    'export OPENAI_API_KEY="opk_..."',
    "",
    'curl "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY"'
  ].join("\\n");

  function setMessage(text, kind = "") {{
    result.className = "message" + (kind ? " " + kind : "");
    result.textContent = text;
  }}

  function setAccountMessage(text, kind = "") {{
    const accountResult = $("account-result");
    if (!accountResult) return;
    accountResult.className = "message" + (kind ? " " + kind : "");
    accountResult.textContent = text;
  }}

  async function copyText(text, button) {{
    try {{
      await navigator.clipboard.writeText(text);
      if (button) button.textContent = "Copied";
    }} catch (_) {{
      if (button) button.textContent = "Copy failed";
    }}
  }}

  function showMintedKey(key) {{
    const accountResult = $("account-result");
    if (!accountResult) return;
    accountResult.className = "message good";
    accountResult.textContent = "";
    const wrap = document.createElement("div");
    wrap.className = "secret";
    const text = document.createElement("p");
    text.textContent = "Copy this opk_ App API key now. It is only for Apps and SDK tools, and it will not be shown again.";
    const code = document.createElement("code");
    code.textContent = key;
    const actions = document.createElement("div");
    actions.className = "actions";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.textContent = "Copy key";
    copy.addEventListener("click", () => copyText(key, copy));
    actions.append(copy);
    wrap.append(text, code, actions);
    accountResult.append(wrap);
  }}

  function runtimeKey() {{
    return keyInput.value.trim();
  }}

  function hasAdminAutomationKey() {{
    return runtimeKey().toLowerCase().startsWith("oak_");
  }}

  function authHeaders() {{
    return {{ "Authorization": "Bearer " + runtimeKey() }};
  }}

  function updateKeyState() {{
    if (runtimeKey()) {{
      setMessage("A key is pasted in this tab. Check access to inspect it.");
    }}
  }}

  async function getJson(path) {{
    const response = await fetch(path, {{ headers: authHeaders(), cache: "no-store" }});
    const text = await response.text();
    let body = {{}};
    try {{ body = text ? JSON.parse(text) : {{}}; }} catch (_) {{ body = {{ raw: text }}; }}
    if (!response.ok) {{
      const err = body && body.error;
      const message = err && (err.message || err.code) ? (err.message || err.code) : response.statusText;
      throw new Error(response.status + " " + message);
    }}
    return body;
  }}

  async function accountJson(path, options = {{}}) {{
    const method = options.method || "GET";
    const body = options.body;
    const headers = {{
      "x-ai-gateway-user-action": "1",
      "x-ai-gateway-csrf": csrfToken
    }};
    if (body !== undefined) headers["content-type"] = "application/json";
    const response = await fetch(path, {{
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store"
    }});
    const text = await response.text();
    let data = {{}};
    try {{ data = text ? JSON.parse(text) : {{}}; }} catch (_) {{ data = {{ raw: text }}; }}
    if (!response.ok) {{
      const err = data && data.error;
      const message = err && (err.message || err.code) ? (err.message || err.code) : response.statusText;
      throw new Error(response.status + " " + message);
    }}
    return data;
  }}

  function badge(text, cls = "") {{
    const span = document.createElement("span");
    span.className = "badge" + (cls ? " " + cls : "");
    span.textContent = text;
    return span;
  }}

  function card(title, body, badges = []) {{
    const article = document.createElement("article");
    article.className = "card";
    const h = document.createElement("h3");
    h.textContent = title;
    const p = document.createElement("p");
    p.textContent = body;
    const meta = document.createElement("div");
    meta.className = "meta";
    badges.forEach((item) => meta.appendChild(badge(item.text, item.cls)));
    article.append(h, p, meta);
    return article;
  }}

  function text(value, fallback = "") {{
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }}

  function activeCredentials(app) {{
    return (Array.isArray(app.credentials) ? app.credentials : [])
      .filter((credential) => credential && credential.active !== false);
  }}

  function permissionInfo(app) {{
    const profile = app && typeof app.profile === "object" ? app.profile : null;
    if (profile && profile.missing) {{
      return {{ text: "permissions unavailable", badge: "permissions unavailable", cls: "bad" }};
    }}
    if (profile && profile.inherited === false) {{
      return {{ text: "custom account permissions", badge: "custom permissions", cls: "" }};
    }}
    return {{ text: "your account permissions", badge: "account permissions", cls: "" }};
  }}

  function renderCredential(credential) {{
    const row = document.createElement("div");
    row.className = "key-row";
    row.dataset.keyRow = text(credential.id);

    const info = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = text(credential.display_name, "API key");
    const details = document.createElement("p");
    const prefix = document.createElement("code");
    prefix.textContent = text(credential.prefix, "key");
    details.append("Prefix ", prefix, " · " + text(credential.created_at, "created"));
    info.append(title, details);

    const actions = document.createElement("div");
    actions.className = "key-actions";
    const inspect = document.createElement("button");
    inspect.type = "button";
    inspect.className = "secondary";
    inspect.dataset.viewOwnedKey = text(credential.id);
    inspect.dataset.keyLabel = title.textContent;
    inspect.textContent = "View access";
    const del = document.createElement("button");
    del.type = "button";
    del.className = "danger";
    del.dataset.revokeOwnedKey = text(credential.id);
    del.textContent = "Delete key";
    actions.append(inspect, del);

    row.append(info, actions);
    return row;
  }}

  function renderApp(app) {{
    const appId = text(app.id);
    const credentials = activeCredentials(app);
    const permission = permissionInfo(app);
    const article = document.createElement("article");
    article.className = "app-card";
    article.dataset.appCard = appId;

    const header = document.createElement("div");
    header.className = "app-card-header";
    const titleWrap = document.createElement("div");
    titleWrap.className = "app-card-title";
    const title = document.createElement("h3");
    title.textContent = text(app.name, appId || "App");
    const help = document.createElement("p");
    help.textContent = "Use this App for one tool or workflow. Its keys use " + permission.text + ".";
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.append(
      badge(permission.badge, permission.cls),
      badge(credentials.length + " active keys", "good")
    );
    titleWrap.append(title, help, meta);
    const deleteApp = document.createElement("button");
    deleteApp.type = "button";
    deleteApp.className = "danger";
    deleteApp.dataset.deleteOwnedApp = appId;
    deleteApp.dataset.appName = title.textContent;
    deleteApp.textContent = "Delete App";
    header.append(titleWrap);
    if (app.can_delete !== false) header.append(deleteApp);

    const form = document.createElement("form");
    form.className = "key-form";
    form.dataset.mintOwnedKey = appId;
    const label = document.createElement("label");
    label.className = "field";
    const labelText = document.createElement("span");
    labelText.textContent = "New API key";
    const input = document.createElement("input");
    input.type = "text";
    input.name = "label";
    input.placeholder = "MacBook, Open WebUI, deploy script";
    input.setAttribute("aria-label", "API key label");
    label.append(labelText, input);
    const createKey = document.createElement("button");
    createKey.type = "submit";
    createKey.textContent = "Create key";
    form.append(label, createKey);

    const list = document.createElement("div");
    list.className = "key-list";
    list.dataset.keyList = appId;
    if (credentials.length) {{
      credentials.forEach((credential) => list.appendChild(renderCredential(credential)));
    }} else {{
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No API keys for this App yet.";
      list.appendChild(empty);
    }}

    article.append(header);
    if (app.can_mint_keys !== false) article.append(form);
    article.append(list);
    return article;
  }}

  function renderApps(apps) {{
    const root = $("apps-list");
    if (!root) return;
    root.textContent = "";
    const visible = (Array.isArray(apps) ? apps : [])
      .filter((app) => app && !app.disabled_at);
    if (!visible.length) {{
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No Apps yet.";
      root.appendChild(empty);
      return;
    }}
    visible.forEach((app) => root.appendChild(renderApp(app)));
  }}

  async function refreshAccount() {{
    const snapshot = await accountJson("/me/api/session");
    accountSnapshot = snapshot;
    renderApps(snapshot.apps || []);
    return snapshot;
  }}

  function renderCapabilities(payload) {{
    const group = $("capabilities-group");
    const root = $("capabilities");
    if (!root || !group) return 0;
    root.textContent = "";
    const caps = Array.isArray(payload.capabilities) ? payload.capabilities : [];
    if (!caps.length) {{
      group.hidden = true;
      return 0;
    }}
    group.hidden = false;
    caps.forEach((cap) => {{
      const name = cap.workflow || cap.model || cap.intent || "capability";
      const modalities = []
        .concat(cap.input_modalities || [])
        .concat("->")
        .concat(cap.output_modalities || [])
        .join(" ");
      const caveats = Array.isArray(cap.caveats) && cap.caveats.length
        ? " Caveats: " + cap.caveats.join("; ")
        : "";
      root.appendChild(card(
        name,
        (cap.intent || "unknown intent") + " - " + (modalities || "modalities not declared") + caveats,
        [
          {{ text: cap.available === false ? "denied or unavailable" : "available", cls: cap.available === false ? "bad" : "good" }},
          {{ text: cap.execution || "sync" }},
          {{ text: cap.resource_tier || "resource" }},
          {{ text: cap.trust_tier || "trust" }},
          {{ text: cap.exposure || "exposure" }}
        ].concat((cap.tags || []).slice(0, 4).map((tag) => ({{ text: tag }})))
      ));
    }});
    return caps.length;
  }}

  function renderModels(payload) {{
    const group = $("models-group");
    const root = $("models");
    if (!root || !group) return 0;
    root.textContent = "";
    const modelPayload = payload.models && Array.isArray(payload.models.data)
      ? payload.models
      : payload;
    const data = Array.isArray(modelPayload.data) ? modelPayload.data : [];
    if (!data.length) {{
      group.hidden = true;
      return 0;
    }}
    group.hidden = false;
    data.slice(0, 24).forEach((model) => {{
      const id = model.id || model.model || "model";
      root.appendChild(card(id, model.intent || (model.owned_by ? "owned by " + model.owned_by : "OpenAI-compatible"), [
        {{ text: model.object || "model" }},
        {{ text: model.available === false ? "unavailable" : "available", cls: model.available === false ? "warn" : "good" }}
      ]));
    }});
    return data.length;
  }}

  function renderAccess(payload, label) {{
    const accessSection = $("access-section");
    if (accessSection) accessSection.hidden = false;
    const capCount = renderCapabilities(payload);
    const modelCount = renderModels(payload.models || payload);
    const summary = $("access-summary");
    if (summary) {{
      const denied = Number(payload.denied_count || 0);
      const suffix = denied ? " " + denied + " blocked by policy." : "";
      summary.textContent = label + ": " + capCount + " capabilities, " + modelCount + " models/workflows." + suffix;
    }}
    return {{ capCount, modelCount }};
  }}

  async function checkAccess() {{
    if (!runtimeKey()) {{
      setMessage("Paste an opk_ API key first.", "warn");
      return;
    }}
    if (hasAdminAutomationKey()) {{
      setMessage("oak_ admin automation keys are for /admin/api and /status, not runtime access. Paste an opk_ App API key instead.", "bad");
      return;
    }}
    sessionStorage.setItem(storageKey, runtimeKey());
    updateKeyState();
    setMessage("Checking access...");
    try {{
      const [caps, models] = await Promise.all([
        getJson("/gateway/v1/capabilities"),
        getJson("/v1/models")
      ]);
      const counts = renderAccess({{ ...caps, models }}, "Pasted key");
      setMessage("Key works. Loaded " + counts.capCount + " capabilities and " + counts.modelCount + " models/workflows.", "good");
    }} catch (error) {{
      setMessage(error.message || "Access check failed.", "bad");
    }}
  }}

  $("check-key").addEventListener("click", checkAccess);
  $("forget-key").addEventListener("click", () => {{
    sessionStorage.removeItem(storageKey);
    keyInput.value = "";
    setMessage("Cleared the pasted key from this tab.");
  }});
  keyInput.addEventListener("keydown", (event) => {{
    if (event.key === "Enter") checkAccess();
  }});

  keyInput.value = sessionStorage.getItem(storageKey) || "";
  updateKeyState();

  renderApps((accountSnapshot && accountSnapshot.apps) || []);

  document.addEventListener("submit", async (event) => {{
    const createForm = event.target.closest("[data-create-owned-app]");
    const keyForm = event.target.closest("[data-mint-owned-key]");
    if (!createForm && !keyForm) return;
    event.preventDefault();

    if (createForm) {{
      const name = String(new FormData(createForm).get("name") || "").trim();
      if (!name) {{
        setAccountMessage("Name the App before creating it.", "warn");
        return;
      }}
      setAccountMessage("Creating App...");
      try {{
        await accountJson("/me/api/apps", {{ method: "POST", body: {{ name }} }});
        createForm.reset();
        await refreshAccount();
        setAccountMessage("App created.", "good");
      }} catch (error) {{
        setAccountMessage(error.message || "App creation failed.", "bad");
      }}
      return;
    }}

    const appId = keyForm.dataset.mintOwnedKey;
    const label = new FormData(keyForm).get("label") || "";
    setAccountMessage("Creating key...");
    try {{
      const data = await accountJson(
        "/me/api/apps/" + encodeURIComponent(appId) + "/keys",
        {{ method: "POST", body: {{ label }} }}
      );
      const key = data && data.credential && data.credential.api_key;
      if (key) showMintedKey(key);
      keyForm.reset();
      await refreshAccount();
      if (!key) setAccountMessage("Key created.", "good");
    }} catch (error) {{
      setAccountMessage(error.message || "Key creation failed.", "bad");
    }}
  }});

  document.addEventListener("click", async (event) => {{
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;

    const inspect = target.closest("[data-view-owned-key]");
    if (inspect) {{
      const credentialId = inspect.dataset.viewOwnedKey;
      const label = inspect.dataset.keyLabel || "Selected key";
      setAccountMessage("Loading access...");
      try {{
        const payload = await accountJson(
          "/me/api/credentials/" + encodeURIComponent(credentialId) + "/access"
        );
        renderAccess(payload, label);
        setAccountMessage("Access loaded.", "good");
      }} catch (error) {{
        setAccountMessage(error.message || "Access check failed.", "bad");
      }}
      return;
    }}

    const deleteKey = target.closest("[data-revoke-owned-key]");
    if (deleteKey) {{
      const credentialId = deleteKey.dataset.revokeOwnedKey;
      if (!confirm("Delete this API key? Apps using it will lose access immediately.")) return;
      setAccountMessage("Deleting key...");
      try {{
        await accountJson(
          "/me/api/credentials/" + encodeURIComponent(credentialId),
          {{ method: "DELETE" }}
        );
        await refreshAccount();
        setAccountMessage("Key deleted.", "good");
      }} catch (error) {{
        setAccountMessage(error.message || "Key deletion failed.", "bad");
      }}
      return;
    }}

    const deleteApp = target.closest("[data-delete-owned-app]");
    if (deleteApp) {{
      const appId = deleteApp.dataset.deleteOwnedApp;
      const appName = deleteApp.dataset.appName || "this App";
      if (!confirm("Delete " + appName + "? Its API keys will stop working immediately.")) return;
      setAccountMessage("Deleting App...");
      try {{
        await accountJson(
          "/me/api/apps/" + encodeURIComponent(appId),
          {{ method: "DELETE" }}
        );
        await refreshAccount();
        setAccountMessage("App deleted.", "good");
      }} catch (error) {{
        setAccountMessage(error.message || "App deletion failed.", "bad");
      }}
    }}
  }});
}})();
  </script>
</body>
</html>
"""
