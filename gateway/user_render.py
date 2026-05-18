"""Server-side HTML rendering for the normal-user gateway portal.

``render_user_portal`` is intentionally pure. The live app serves the page at
``/me`` and the browser uses the user's runtime ``opk_`` key to call existing
gateway APIs. That keeps the first useful user surface small: no new session
model, no duplicated authorization, and no server-side handling of raw runtime
keys.
"""

from __future__ import annotations

import html
import json
from typing import Any

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
    border: 1px solid var(--line);
    border-radius: 999px;
    color: var(--muted);
    padding: 0.35rem 0.65rem;
    font-size: 0.84rem;
    background: var(--panel);
  }
  main {
    width: min(1240px, calc(100% - clamp(2rem, 6vw, 4rem)));
    margin: 0 auto;
    padding: var(--space-8) 0 var(--space-12);
    display: grid;
    gap: var(--space-8);
  }
  .hero {
    display: grid;
    grid-template-columns: minmax(0, 1.25fr) minmax(280px, 0.75fr);
    gap: var(--space-8);
    align-items: end;
  }
  .hero h2 {
    margin: 0;
    max-width: 16ch;
    font-size: clamp(2rem, 4vw, 4rem);
    line-height: 1;
    letter-spacing: 0;
  }
  .hero p {
    margin: var(--space-4) 0 0;
    max-width: 68ch;
    color: var(--muted);
    line-height: 1.55;
  }
  .status-panel {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
  }
  .status-line {
    display: flex;
    justify-content: space-between;
    gap: var(--space-3);
    align-items: baseline;
  }
  .status-line span:first-child, .label {
    color: var(--muted);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .status-line strong { font-size: 0.95rem; }
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
    min-height: 2.5rem;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: oklch(14.5% 0.016 245);
    color: var(--muted);
    padding: var(--space-3);
    line-height: 1.45;
  }
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
  .empty { color: var(--muted); font-style: italic; }
  .two-col {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: var(--space-4);
  }
  @media (max-width: 820px) {
    header { align-items: flex-start; flex-direction: column; }
    main { width: min(100% - 2rem, 1240px); padding-top: var(--space-6); }
    .hero { grid-template-columns: 1fr; align-items: start; }
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


def render_user_portal(
    *,
    title: str = "My AI Gateway",
    public_base_url: str = "https://ai.vivid.fish",
) -> str:
    """Return the normal-user HTML portal.

    The page stores a runtime key only in the user's browser and uses the
    gateway-native APIs for effective-access checks. It is safe to serve
    without authentication because it exposes no private server data by itself.
    """
    base = public_base_url.rstrip("/") or "https://ai.vivid.fish"
    base_json = _json_for_script(base)
    title_text = _esc(title)
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
    <nav aria-label="Gateway links">
      <a href="/.well-known/ai-gateway">Discovery</a>
      <a href="/llms.txt">llms.txt</a>
      <a href="/health">Health</a>
    </nav>
  </header>
  <main>
    <div class="hero">
      <div>
        <h2>Use your local AI stack with less guessing.</h2>
        <p>This page checks a runtime key against the gateway and shows the
        models, media workflows, job routes, and SDK settings that key can use.
        Permissions are controlled by the operator in the admin console.</p>
      </div>
      <aside class="status-panel" aria-label="Connection summary">
        <div class="status-line"><span>Base URL</span><strong id="base-url"></strong></div>
        <div class="status-line"><span>Key</span><strong id="key-state">Not checked</strong></div>
        <div class="status-line"><span>Capabilities</span><strong id="cap-count">Unknown</strong></div>
        <div class="status-line"><span>Jobs</span><strong id="job-count">Unknown</strong></div>
      </aside>
    </div>

    <section aria-labelledby="connect-heading">
      <h2 id="connect-heading">Connect</h2>
      <div class="flow">
        <label class="label" for="runtime-key">Runtime API key</label>
        <div class="row">
          <input id="runtime-key" type="password" autocomplete="off" spellcheck="false"
                 placeholder="opk_..." aria-describedby="connect-result">
          <button id="check-key" type="button">Check Access</button>
          <button id="forget-key" class="secondary" type="button">Forget</button>
        </div>
        <div id="connect-result" class="message">Paste a runtime key to test what this browser can use.</div>
      </div>
    </section>

    <section aria-labelledby="examples-heading">
      <h2 id="examples-heading">Use It</h2>
      <div class="grid">
        <article class="card">
          <h3>OpenAI-compatible</h3>
          <div id="openai-snippet" class="snippet"></div>
        </article>
        <article class="card">
          <h3>Anthropic Messages</h3>
          <div id="anthropic-snippet" class="snippet"></div>
        </article>
        <article class="card">
          <h3>Gateway-native jobs</h3>
          <div id="jobs-snippet" class="snippet"></div>
        </article>
      </div>
    </section>

    <section aria-labelledby="access-heading">
      <h2 id="access-heading">Effective Access</h2>
      <div id="capabilities" class="list">
        <p class="empty">Check a key to load available models and workflows.</p>
      </div>
    </section>

    <section aria-labelledby="inventory-heading">
      <h2 id="inventory-heading">Inventory</h2>
      <div class="two-col">
        <div id="models" class="list">
          <p class="empty">Model list has not been loaded yet.</p>
        </div>
        <div id="jobs" class="list">
          <p class="empty">Recent media jobs have not been loaded yet.</p>
        </div>
      </div>
    </section>

    <section aria-labelledby="failures-heading">
      <h2 id="failures-heading">Failure Meanings</h2>
      <div class="grid">
        <article class="card"><h3>401</h3><p>The key is missing, mistyped, expired, or revoked.</p></article>
        <article class="card"><h3>403</h3><p>The key is valid, but its assigned profile does not allow that request.</p></article>
        <article class="card"><h3>409</h3><p>The request conflicts with the current workflow or job state.</p></article>
        <article class="card"><h3>503</h3><p>A backend is down, disabled for the current mode, or unavailable.</p></article>
      </div>
    </section>
  </main>
  <script>
(() => {{
  const publicBase = {base_json};
  const storageKey = "vivid_fish_ai_gateway_runtime_key";
  const $ = (id) => document.getElementById(id);
  const keyInput = $("runtime-key");
  const result = $("connect-result");
  const baseUrl = publicBase.replace(/\\/$/, "");

  $("base-url").textContent = baseUrl + "/v1";
  $("openai-snippet").textContent = [
    'export OPENAI_BASE_URL="' + baseUrl + '/v1"',
    'export OPENAI_API_KEY="opk_..."',
    "",
    'curl "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY"'
  ].join("\\n");
  $("anthropic-snippet").textContent = [
    'export ANTHROPIC_BASE_URL="' + baseUrl + '/v1"',
    'export ANTHROPIC_API_KEY="opk_..."',
    "",
    "POST /v1/messages with Authorization: Bearer $ANTHROPIC_API_KEY"
  ].join("\\n");
  $("jobs-snippet").textContent = [
    'curl "' + baseUrl + '/gateway/v1/media/jobs" \\\\',
    '  -H "Authorization: Bearer opk_..." \\\\',
    '  -H "Content-Type: application/json" \\\\',
    "  -d '{{\\"intent\\":\\"image.generate\\",\\"model\\":\\"...\\"}}'"
  ].join("\\n");

  function setMessage(text, kind = "") {{
    result.className = "message" + (kind ? " " + kind : "");
    result.textContent = text;
  }}

  function runtimeKey() {{
    return keyInput.value.trim();
  }}

  function authHeaders() {{
    return {{ "Authorization": "Bearer " + runtimeKey() }};
  }}

  function updateKeyState() {{
    $("key-state").textContent = runtimeKey() ? "Stored in this browser" : "Not checked";
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
    const allowed = caps.filter((cap) => cap.available !== false);
    $("cap-count").textContent = allowed.length + " usable";
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
    $("job-count").textContent = jobs.length + " recent";
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
    localStorage.setItem(storageKey, runtimeKey());
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
      setMessage("Key works. This is the effective access visible to normal users and agents.", "good");
    }} catch (error) {{
      $("cap-count").textContent = "Unknown";
      $("job-count").textContent = "Unknown";
      setMessage(error.message || "Access check failed.", "bad");
    }}
  }}

  $("check-key").addEventListener("click", checkAccess);
  $("forget-key").addEventListener("click", () => {{
    localStorage.removeItem(storageKey);
    keyInput.value = "";
    updateKeyState();
    setMessage("Forgot the key in this browser.");
  }});
  keyInput.addEventListener("keydown", (event) => {{
    if (event.key === "Enter") checkAccess();
  }});

  keyInput.value = localStorage.getItem(storageKey) || "";
  updateKeyState();
}})();
  </script>
</body>
</html>
"""
