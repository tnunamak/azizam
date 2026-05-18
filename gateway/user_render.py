"""Server-side HTML rendering for the normal-user gateway portal.

``render_user_portal`` is intentionally pure. The live app serves the page at
``/me`` and the browser uses either the signed OIDC session cookie or the
user's pasted runtime ``opk_`` key to call existing gateway APIs. That keeps
the first useful user surface small: no new authorization model, no duplicated
policy, and no server-side handling of pasted runtime keys.
"""

from __future__ import annotations

import html
import json
from typing import Any, Mapping

__all__ = ["render_user_portal"]


_USER_PORTAL_CSS = """
  :root {
    color-scheme: dark;
    --bg: oklch(16% 0.018 245);
    --panel: oklch(21% 0.02 245);
    --panel-2: oklch(25% 0.024 245);
    --line: oklch(35% 0.03 245);
    --text: oklch(93% 0.018 245);
    --muted: oklch(73% 0.034 245);
    --soft: oklch(62% 0.04 245);
    --accent: oklch(78% 0.15 168);
    --accent-ink: oklch(15% 0.028 168);
    --warn: oklch(83% 0.13 74);
    --warn-bg: oklch(27% 0.065 74);
    --bad: oklch(80% 0.13 28);
    --bad-bg: oklch(27% 0.062 28);
    --good: oklch(82% 0.13 150);
    --good-bg: oklch(26% 0.056 150);
    --radius: 8px;
    --space-1: 0.25rem;
    --space-2: 0.5rem;
    --space-3: 0.75rem;
    --space-4: 1rem;
    --space-6: 1.5rem;
    --space-8: 2rem;
    --space-12: 3rem;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  }
  button, input { font: inherit; }
  header {
    min-height: 4rem;
    padding: var(--space-4) clamp(var(--space-4), 3vw, var(--space-8));
    border-bottom: 1px solid var(--line);
    display: flex;
    justify-content: space-between;
    gap: var(--space-4);
    align-items: center;
    background: oklch(17.5% 0.02 245);
    position: sticky;
    top: 0;
    z-index: 2;
  }
  header h1 { margin: 0; font-size: 1rem; letter-spacing: 0; }
  header nav { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  a {
    color: oklch(82% 0.11 168);
    text-decoration: none;
  }
  header a, .pill {
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: 999px;
    color: var(--muted);
    padding: 0.35rem 0.65rem;
    min-height: 2.75rem;
    font-size: 0.84rem;
    background: var(--panel);
  }
  main {
    width: min(980px, calc(100% - clamp(2rem, 6vw, 4rem)));
    margin: 0 auto;
    padding: var(--space-6) 0 var(--space-12);
    display: grid;
    gap: var(--space-6);
  }
  .label {
    color: var(--muted);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  section {
    border-top: 1px solid var(--line);
    padding-top: var(--space-6);
  }
  section h2 {
    margin: 0 0 var(--space-3);
    font-size: 0.9rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .grid {
    display: grid;
    gap: var(--space-4);
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  }
  .flow, .card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-4);
  }
  .flow {
    display: grid;
    gap: var(--space-3);
  }
  .row {
    display: grid;
    gap: var(--space-2);
    grid-template-columns: minmax(0, 1fr) auto auto;
  }
  input {
    background: oklch(14.5% 0.016 245);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 0.72rem 0.8rem;
    min-height: 2.75rem;
    min-width: 0;
  }
  input:focus-visible, button:focus-visible, a:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  button {
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 0.72rem 0.9rem;
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
  .message {
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: oklch(14.5% 0.016 245);
    color: var(--muted);
    padding: var(--space-3);
    line-height: 1.45;
  }
  .message:empty { display: none; }
  .message.good { color: var(--good); background: var(--good-bg); }
  .message.warn { color: var(--warn); background: var(--warn-bg); }
  .message.bad { color: var(--bad); background: var(--bad-bg); }
  .snippet {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    background: oklch(14.5% 0.016 245);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-3);
    color: oklch(88% 0.05 168);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.86rem;
    line-height: 1.55;
  }
  .card {
    display: grid;
    gap: var(--space-3);
    align-content: start;
  }
  .card h3 {
    margin: 0;
    font-size: 1rem;
    letter-spacing: 0;
  }
  .card .actions {
    display: grid;
    gap: var(--space-2);
  }
  .card p {
    color: var(--muted);
    line-height: 1.45;
    margin: 0;
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .badge {
    display: inline-flex;
    align-items: center;
    min-height: 1.6rem;
    padding: 0.18rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--line);
    background: var(--panel-2);
    color: var(--muted);
    font-size: 0.78rem;
  }
  .badge.good { color: var(--good); background: var(--good-bg); }
  .badge.bad { color: var(--bad); background: var(--bad-bg); }
  .badge.warn { color: var(--warn); background: var(--warn-bg); }
  .list {
    display: grid;
    gap: var(--space-3);
  }
  .credential-row {
    display: grid;
    gap: var(--space-2);
    padding: var(--space-3) 0 0;
    border-top: 1px solid var(--line);
  }
  .credential-row:first-child { padding-top: 0; border-top: 0; }
  .credential-row h3 {
    margin: 0;
    font-size: 0.94rem;
    letter-spacing: 0;
  }
  .credential-row p { margin: 0; }
  .empty { color: var(--muted); font-style: italic; }
  .two-col {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: var(--space-4);
  }
  .access-group {
    display: grid;
    gap: var(--space-3);
    align-content: start;
  }
  .access-group h3 {
    margin: 0;
    color: var(--muted);
    font-size: 0.86rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  @media (max-width: 820px) {
    header { align-items: flex-start; flex-direction: column; }
    main { width: min(100% - 2rem, 980px); }
    .row { grid-template-columns: 1fr; }
  }
"""


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _json_for_script(value: str) -> str:
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _account_badge(text: Any, cls: str = "") -> str:
    return f'<span class="badge {cls}">{_esc(text)}</span>'


def _user_role(account: Mapping[str, Any] | None) -> str:
    if not account or not account.get("authenticated"):
        return "none"
    user = account.get("user") if isinstance(account.get("user"), Mapping) else {}
    return str(user.get("role") or "none")


def _can_admin(account: Mapping[str, Any] | None) -> bool:
    return _user_role(account) in {"owner", "admin", "superadmin"}


def _header_nav(account: Mapping[str, Any] | None) -> str:
    links = []
    if _can_admin(account):
        links.append(("/admin", "Admin"))
    return "".join(
        f'<a href="{_esc(href)}">{_esc(label)}</a>'
        for href, label in links
    )


def _credential_html(record: Mapping[str, Any]) -> str:
    credential_id = _esc(record.get("id"))
    prefix = _esc(record.get("prefix") or "key")
    label = _esc(record.get("display_name") or "Runtime key")
    created = _esc(record.get("created_at") or "created")
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
        + _account_badge(status, status_class)
        + "</div>"
        + (f'<div class="actions">{action}</div>' if action else "")
        + "</div>"
    )


def _app_html(app: Mapping[str, Any]) -> str:
    app_id = _esc(app.get("id"))
    name = _esc(app.get("name") or app_id or "App")
    kind = _esc(app.get("kind") or "app")
    profile = app.get("profile") if isinstance(app.get("profile"), Mapping) else None
    profile_name = "Awaiting operator profile"
    profile_id = app.get("profile_id") or ""
    profile_badge = _account_badge("profile needed", "warn")
    if profile:
        profile_name = str(profile.get("name") or profile.get("policy_id") or profile_id)
        profile_id = profile.get("id") or profile_id
        profile_badge = _account_badge(
            "missing profile" if profile.get("missing") else profile_id,
            "bad" if profile.get("missing") else "",
        )
    credentials = [
        c for c in app.get("credentials", [])
        if isinstance(c, Mapping)
    ]
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
            'this App can mint runtime keys.</p>'
        )
    )
    return (
        '<article class="card">'
        f"<h3>{name}</h3>"
        f"<p>{kind} App · Profile: {_esc(profile_name)}</p>"
        '<div class="meta">'
        + profile_badge
        + _account_badge(f"{app.get('active_credentials') or 0} active keys", "good")
        + "</div>"
        + mint_html
        + '<div class="list">'
        + credential_html
        + "</div>"
        + "</article>"
    )


def _account_section(
    account: Mapping[str, Any] | None,
    *,
    oidc_enabled: bool,
    login_url: str,
    logout_url: str,
) -> str:
    account = account or {"authenticated": False, "apps": []}
    if not account.get("authenticated"):
        action = (
            f'<a class="pill" href="{_esc(login_url)}">Sign in with OIDC</a>'
            if oidc_enabled
            else '<span class="pill">OIDC login not configured</span>'
        )
        return (
            '<section aria-labelledby="account-heading">'
            '<h2 id="account-heading">Account</h2>'
            '<div class="card">'
            "<h3>Not signed in</h3>"
            "<p>Sign in to manage Apps assigned to your account.</p>"
            f'<div class="meta">{action}</div>'
            "</div>"
            "</section>"
        )

    user = account.get("user") if isinstance(account.get("user"), Mapping) else {}
    user_label = _esc(user.get("email") or user.get("name") or "signed-in user")
    raw_role = _user_role(account)
    role_label = raw_role if raw_role in {"owner", "admin", "superadmin"} else "user"
    apps = [a for a in account.get("apps", []) if isinstance(a, Mapping)]
    can_admin = _can_admin(account)
    empty_html = (
        '<div class="card"><h3>No owned Apps</h3>'
        "<p>Create or assign Apps in Admin.</p>"
        '<div class="meta"><a class="pill" href="/admin">Open Admin</a></div>'
        "</div>"
        if can_admin
        else (
            '<div class="card"><h3>No Apps yet</h3>'
            "<p>Request access to create a pending Personal App.</p>"
            '<form class="actions" data-request-access>'
            '<button type="submit">Request access</button>'
            "</form></div>"
        )
    )
    apps_html = (
        "".join(_app_html(app) for app in apps)
        if apps else empty_html
    )
    return (
        '<section aria-labelledby="account-heading">'
        '<h2 id="account-heading">Account</h2>'
        '<div class="card">'
        f"<h3>{user_label}</h3>"
        "<p>Apps own runtime keys.</p>"
        '<div class="meta">'
        + _account_badge(f"role {_esc(role_label)}")
        + f'<a class="pill" href="{_esc(logout_url)}">Sign out</a>'
        + "</div>"
        "</div>"
        '<div id="account-result" class="message"></div>'
        f'<div class="grid">{apps_html}</div>'
        "</section>"
    )


def render_user_portal(
    *,
    title: str = "My AI Gateway",
    public_base_url: str = "https://ai.vivid.fish",
    account: Mapping[str, Any] | None = None,
    oidc_enabled: bool = False,
    login_url: str = "/login?return_to=/me",
    logout_url: str = "/me/logout",
    csrf_token: str = "",
) -> str:
    """Return the normal-user HTML portal.

    The page stores a runtime key only in the user's browser and uses the
    gateway-native APIs for effective-access checks. It is safe to serve
    without authentication because it exposes no private server data by itself.
    """
    base = public_base_url.rstrip("/") or "https://ai.vivid.fish"
    base_json = _json_for_script(base)
    csrf_json = _json_for_script(csrf_token)
    title_text = _esc(title)
    account_html = _account_section(
        account,
        oidc_enabled=oidc_enabled,
        login_url=login_url,
        logout_url=logout_url,
    )
    nav_html = _header_nav(account)
    nav_block = (
        '<nav aria-label="Gateway links">' + nav_html + "</nav>"
        if nav_html else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_text}</title>
  <style>{_USER_PORTAL_CSS}</style>
</head>
<body>
  <header>
    <h1>{title_text}</h1>
    {nav_block}
  </header>
  <main>
    {account_html}

    <section aria-labelledby="connect-heading">
      <h2 id="connect-heading">Runtime Key</h2>
      <div class="flow">
        <label class="label" for="runtime-key">App API key</label>
        <div class="row">
          <input id="runtime-key" type="password" autocomplete="off" spellcheck="false"
                 placeholder="opk_..." aria-describedby="connect-result">
          <button id="check-key" type="button">Check Access</button>
          <button id="forget-key" class="secondary" type="button">Forget</button>
        </div>
        <div id="connect-result" class="message">Paste an opk_ key to test access from this browser.</div>
      </div>
    </section>

    <section aria-labelledby="examples-heading">
      <h2 id="examples-heading">Settings</h2>
      <div class="flow">
        <div class="label" id="settings-label">OpenAI-compatible</div>
        <div id="openai-snippet" class="snippet"></div>
      </div>
    </section>

    <section aria-labelledby="access-heading">
      <h2 id="access-heading">Access</h2>
      <div class="two-col">
        <div class="access-group">
          <h3>Capabilities</h3>
          <div id="capabilities" class="list">
            <p class="empty">Check a key to load access.</p>
          </div>
        </div>
        <div class="access-group">
          <h3>Models</h3>
          <div id="models" class="list"></div>
        </div>
        <div class="access-group">
          <h3>Recent Jobs</h3>
          <div id="jobs" class="list"></div>
        </div>
      </div>
    </section>
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
