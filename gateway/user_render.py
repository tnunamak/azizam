"""Server-side HTML rendering for the gateway's user-facing landing page.

``render_user_portal`` is intentionally pure. The live app serves the page at
``/`` (with ``/me`` redirecting here as a legacy browser alias) and the
browser uses either the signed OIDC session cookie or the user's pasted
runtime ``opk_`` key to call existing gateway APIs. That keeps the first
useful user surface small: no new authorization model, no duplicated policy,
and no server-side handling of pasted runtime keys.

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
    user_role,
)

__all__ = ["render_user_portal"]


_PAGE_CSS = """
  .lead { color: var(--muted); margin: 0; line-height: 1.55; }
  .card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
    align-content: start;
  }
  .card h3 { margin: 0; font-size: 1rem; }
  .card p { margin: 0; color: var(--muted); line-height: 1.5; }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
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
  .row {
    display: grid;
    gap: var(--space-2);
    grid-template-columns: minmax(0, 1fr) auto auto;
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
  .grid {
    display: grid;
    gap: var(--space-4);
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
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
  .list { display: grid; gap: var(--space-3); }
  .credential-row {
    display: grid;
    gap: var(--space-2);
    padding-top: var(--space-3);
    margin-top: var(--space-3);
    border-top: 1px solid var(--line);
  }
  .credential-row:first-of-type { padding-top: 0; margin-top: 0; border-top: 0; }
  .credential-row h3 { margin: 0; font-size: 0.94rem; }
  .credential-row p { margin: 0; color: var(--muted); }
  .empty { color: var(--muted); }
  .two-col {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
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
    .row { grid-template-columns: 1fr; }
  }
"""


def _badge(text: Any, cls: str = "") -> str:
    return f'<span class="badge {cls}">{esc(text)}</span>'


def _credential_html(record: Mapping[str, Any]) -> str:
    credential_id = esc(record.get("id"))
    prefix = esc(record.get("prefix") or "key")
    label = esc(record.get("display_name") or "Runtime key")
    created = esc(record.get("created_at") or "created")
    active = bool(record.get("active"))
    status = "active" if active else "revoked"
    status_class = "good" if active else "bad"
    action = (
        '<button class="secondary" type="button" data-revoke-owned-key="'
        + credential_id
        + '" aria-label="Revoke runtime key '
        + label
        + '">Revoke</button>'
        if active and credential_id
        else ""
    )
    return (
        '<div class="credential-row">'
        f"<h3>{label}</h3>"
        f"<p><code>{prefix}</code> · {created}</p>"
        '<div class="meta">'
        + _badge(status, status_class)
        + "</div>"
        + (f'<div class="actions">{action}</div>' if action else "")
        + "</div>"
    )


def _app_html(app: Mapping[str, Any]) -> str:
    app_id = esc(app.get("id"))
    name = esc(app.get("name") or app_id or "App")
    kind = esc(app.get("kind") or "app")
    profile = app.get("profile") if isinstance(app.get("profile"), Mapping) else None
    profile_name = "Awaiting operator profile"
    profile_id = app.get("profile_id") or ""
    profile_badge = _badge("profile needed", "warn")
    if profile:
        profile_name = str(profile.get("name") or profile.get("policy_id") or profile_id)
        profile_id = profile.get("id") or profile_id
        profile_badge = _badge(
            "missing profile" if profile.get("missing") else profile_id,
            "bad" if profile.get("missing") else "",
        )
    credentials = [c for c in app.get("credentials", []) if isinstance(c, Mapping)]
    credential_html = (
        "".join(_credential_html(c) for c in credentials)
        if credentials
        else '<p class="empty">No runtime keys for this App yet.</p>'
    )
    can_mint = bool(app.get("can_mint_keys"))
    mint_html = (
        '<form class="actions" data-mint-owned-key="' + app_id + '">'
        '<input type="text" name="label" placeholder="New key label" '
        'aria-label="New runtime key label">'
        '<button type="submit">Mint key</button>'
        "</form>"
        if can_mint
        else (
            '<p class="empty">An operator needs to attach a Profile before '
            "this App can mint runtime keys.</p>"
        )
    )
    return (
        '<article class="card">'
        f"<h3>{name}</h3>"
        f"<p>{kind} App · Profile: {esc(profile_name)}</p>"
        '<div class="meta">'
        + profile_badge
        + _badge(f"{app.get('active_credentials') or 0} active keys", "good")
        + "</div>"
        + mint_html
        + '<div class="list">'
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
        '<p class="lead">Sign in to manage Apps and runtime keys. Paste an '
        "<code>opk_</code> key only if an operator already gave you one; "
        "the browser stores it in this tab.</p>"
        f'<div class="actions">{primary}</div>'
        "</div>"
        "</section>"
    )


def _signed_in_account(account: Mapping[str, Any]) -> str:
    user_value = account.get("user")
    user = user_value if isinstance(user_value, Mapping) else {}
    user_label = esc(user.get("email") or user.get("name") or "signed-in user")
    raw_role = user_role(account)
    role_label = raw_role if raw_role in {"owner", "admin", "superadmin"} else "user"
    apps = [a for a in account.get("apps", []) if isinstance(a, Mapping)]
    is_admin = can_admin(account)
    empty_html = (
        '<article class="card"><h3>No owned Apps</h3>'
        "<p>Create or assign Apps from Admin.</p>"
        '<div class="actions"><a class="pill" href="/admin">Open Admin</a></div>'
        "</article>"
        if is_admin
        else (
            '<article class="card"><h3>No Apps yet</h3>'
            "<p>Request access to create a pending Personal App. An operator "
            "will attach a Profile so you can mint runtime keys.</p>"
            '<form class="actions" data-request-access>'
            '<button type="submit">Request access</button>'
            "</form></article>"
        )
    )
    apps_html = (
        "".join(_app_html(app) for app in apps) if apps else empty_html
    )
    return (
        '<section aria-labelledby="apps-heading">'
        '<h2 id="apps-heading">Apps</h2>'
        '<p class="hint">'
        + f"Signed in as {user_label} · role {esc(role_label)}. "
        + "Apps own runtime keys; revoke a key to cut its access immediately."
        + "</p>"
        '<div id="account-result" class="message"></div>'
        f'<div class="grid">{apps_html}</div>'
        "</section>"
    )


def _runtime_key_section() -> str:
    return (
        '<section aria-labelledby="runtime-heading">'
        '<h2 id="runtime-heading">Check access</h2>'
        '<p class="hint">Paste an <code>opk_</code> key to inspect access for '
        "any App from this browser. The key stays in this tab only.</p>"
        '<div class="card">'
        '<div class="row">'
        '<input id="runtime-key" type="password" autocomplete="off" spellcheck="false" '
        'placeholder="opk_..." aria-describedby="connect-result">'
        '<button id="check-key" type="button">Check access</button>'
        '<button id="forget-key" class="secondary" type="button">Forget</button>'
        "</div>"
        '<div id="connect-result" class="message">'
        "Paste an opk_ key to test access from this browser."
        "</div>"
        "</div>"
        "</section>"
    )


def _settings_section() -> str:
    return (
        '<section aria-labelledby="settings-heading">'
        '<h2 id="settings-heading">Connection settings</h2>'
        '<p class="hint">Point an OpenAI-compatible client at the gateway. '
        "Other protocols use the same base URL with their own paths.</p>"
        '<div id="openai-snippet" class="snippet"></div>'
        "</section>"
    )


def _access_section() -> str:
    return (
        '<section id="access-section" aria-labelledby="access-heading" hidden>'
        '<h2 id="access-heading">Access</h2>'
        '<p class="hint" id="access-empty-hint">Check a key to load capabilities, '
        "models, and recent jobs.</p>"
        '<div class="two-col">'
        '<div class="access-group">'
        '<h3>Capabilities</h3>'
        '<div id="capabilities" class="list"></div>'
        '</div>'
        '<div class="access-group">'
        '<h3>Models</h3>'
        '<div id="models" class="list"></div>'
        '</div>'
        '<div class="access-group">'
        '<h3>Recent Jobs</h3>'
        '<div id="jobs" class="list"></div>'
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
        + _runtime_key_section()
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

  function runtimeKey() {{
    return keyInput.value.trim();
  }}

  function authHeaders() {{
    return {{ "Authorization": "Bearer " + runtimeKey() }};
  }}

  function updateKeyState() {{
    if (runtimeKey()) {{
      setMessage("Key is stored in this browser. Check access to refresh.");
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

  async function postAccountJson(path, body = {{}}) {{
    const response = await fetch(path, {{
      method: "POST",
      headers: {{
        "content-type": "application/json",
        "x-ai-gateway-user-action": "1",
        "x-ai-gateway-csrf": csrfToken
      }},
      body: JSON.stringify(body),
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

  function renderCapabilities(payload) {{
    const root = $("capabilities");
    root.textContent = "";
    const caps = Array.isArray(payload.capabilities) ? payload.capabilities : [];
    if (!caps.length) {{
      root.innerHTML = '<p class="empty">No capabilities were returned for this key.</p>';
      return;
    }}
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
  }}

  function renderModels(payload) {{
    const root = $("models");
    root.textContent = "";
    const data = Array.isArray(payload.data) ? payload.data : [];
    if (!data.length) {{
      root.innerHTML = '<p class="empty">No OpenAI-compatible models were returned.</p>';
      return;
    }}
    data.slice(0, 24).forEach((model) => {{
      const id = model.id || model.model || "model";
      root.appendChild(card(id, model.owned_by ? "owned by " + model.owned_by : "OpenAI-compatible model", [
        {{ text: model.object || "model" }}
      ]));
    }});
  }}

  function renderJobs(payload) {{
    const root = $("jobs");
    root.textContent = "";
    const jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
    if (!jobs.length) {{
      root.innerHTML = '<p class="empty">No recent media jobs are visible to this key.</p>';
      return;
    }}
    jobs.slice(0, 12).forEach((job) => {{
      const status = job.status || "unknown";
      const cls = status === "succeeded" || status === "completed" ? "good" : status === "failed" || status === "canceled" ? "bad" : "warn";
      root.appendChild(card(job.job_id || "media job", job.intent || "media job", [
        {{ text: status, cls }},
        {{ text: job.created_at || "created" }}
      ]));
    }});
  }}

  async function checkAccess() {{
    if (!runtimeKey()) {{
      setMessage("Paste an opk_ runtime key first.", "warn");
      return;
    }}
    sessionStorage.setItem(storageKey, runtimeKey());
    updateKeyState();
    setMessage("Checking access...");
    try {{
      const [caps, models, jobs] = await Promise.all([
        getJson("/gateway/v1/capabilities"),
        getJson("/v1/models"),
        getJson("/gateway/v1/media/jobs?limit=12")
      ]);
      const accessSection = $("access-section");
      if (accessSection) accessSection.hidden = false;
      const emptyHint = $("access-empty-hint");
      if (emptyHint) emptyHint.style.display = "none";
      renderCapabilities(caps);
      renderModels(models);
      renderJobs(jobs);
      const capCount = Array.isArray(caps.capabilities) ? caps.capabilities.length : 0;
      const modelCount = Array.isArray(models.data) ? models.data.length : 0;
      const jobCount = Array.isArray(jobs.jobs) ? jobs.jobs.length : 0;
      setMessage("Key works. Loaded " + capCount + " capabilities, " + modelCount + " models, " + jobCount + " recent jobs.", "good");
    }} catch (error) {{
      setMessage(error.message || "Access check failed.", "bad");
    }}
  }}

  $("check-key").addEventListener("click", checkAccess);
  $("forget-key").addEventListener("click", () => {{
    sessionStorage.removeItem(storageKey);
    keyInput.value = "";
    setMessage("Forgot the key in this tab.");
  }});
  keyInput.addEventListener("keydown", (event) => {{
    if (event.key === "Enter") checkAccess();
  }});

  keyInput.value = sessionStorage.getItem(storageKey) || "";
  updateKeyState();

  document.querySelector("[data-request-access]")?.addEventListener("submit", async (event) => {{
    event.preventDefault();
    setAccountMessage("Requesting access...");
    try {{
      await postAccountJson("/me/api/access-request", {{}});
      setAccountMessage("Access request recorded. Reloading account view...", "good");
      setTimeout(() => location.reload(), 800);
    }} catch (error) {{
      setAccountMessage(error.message || "Access request failed.", "bad");
    }}
  }});

  document.querySelectorAll("[data-mint-owned-key]").forEach((form) => {{
    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const appId = form.dataset.mintOwnedKey;
      const label = new FormData(form).get("label") || "";
      setAccountMessage("Minting key...");
      try {{
        const data = await postAccountJson("/me/api/apps/" + encodeURIComponent(appId) + "/keys", {{ label }});
        const key = data && data.credential && data.credential.api_key;
        setAccountMessage(key ? "New key: " + key : "Key minted. Reload to see the credential list.", "good");
      }} catch (error) {{
        setAccountMessage(error.message || "Key mint failed.", "bad");
      }}
    }});
  }});

  document.querySelectorAll("[data-revoke-owned-key]").forEach((button) => {{
    button.addEventListener("click", async () => {{
      const credentialId = button.dataset.revokeOwnedKey;
      setAccountMessage("Revoking key...");
      try {{
        await postAccountJson("/me/api/credentials/" + encodeURIComponent(credentialId) + "/revoke", {{}});
        setAccountMessage("Key revoked. Reloading account view...", "good");
        setTimeout(() => location.reload(), 800);
      }} catch (error) {{
        setAccountMessage(error.message || "Key revoke failed.", "bad");
      }}
    }});
  }});
}})();
  </script>
</body>
</html>
"""
