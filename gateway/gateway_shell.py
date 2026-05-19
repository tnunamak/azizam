"""Shared chrome for the gateway's browser surfaces.

Both ``/`` (user portal) and ``/admin`` use the same shell so the site feels
like one app instead of two grafted-together pages. The shell is intentionally
small: a stylesheet, a header builder, and a couple of helpers. It owns the
top-of-page chrome (logo, nav, sign-in / sign-out affordance) and nothing
else.

All functions are pure: no I/O, no framework imports, no template engine.
Callers compose returned strings into the per-page body. This keeps the
existing "server-rendered pure renderer" contract intact.
"""

from __future__ import annotations

import html
import json
from typing import Any, Iterable, Mapping

__all__ = [
    "SHELL_CSS",
    "esc",
    "json_for_script",
    "render_shell_header",
    "can_admin",
    "user_role",
]


# Single design language for the whole site. Both portals import this.
SHELL_CSS = """
  :root {
    color-scheme: dark;
    --vf-bg: #0A1620;
    --vf-surface-1: #0E1E2C;
    --vf-surface-2: #142A3D;
    --vf-surface-3: #1C3850;
    --vf-border: rgba(95, 145, 210, 0.14);
    --vf-border-strong: rgba(125, 180, 230, 0.26);
    --vf-text: #E8E2D3;
    --vf-muted: #8FA8BD;
    --vf-subtle: #5C7589;
    --vf-accent: #7BC4C8;
    --vf-accent-ink: #07151D;
    --vf-ok: #6FB89A;
    --vf-warn: #D9B86A;
    --vf-bad: #D88370;
    --vf-info: #7FB4C7;
    --bg: var(--vf-bg);
    --panel: var(--vf-surface-1);
    --panel-2: var(--vf-surface-2);
    --panel-3: var(--vf-surface-3);
    --line: var(--vf-border);
    --line-strong: var(--vf-border-strong);
    --text: var(--vf-text);
    --muted: var(--vf-muted);
    --soft: var(--vf-subtle);
    --accent: var(--vf-accent);
    --accent-ink: var(--vf-accent-ink);
    --link: var(--vf-accent);
    --warn: var(--vf-warn);
    --warn-bg: color-mix(in srgb, var(--vf-warn) 12%, transparent);
    --bad: var(--vf-bad);
    --bad-bg: color-mix(in srgb, var(--vf-bad) 12%, transparent);
    --good: var(--vf-ok);
    --good-bg: color-mix(in srgb, var(--vf-ok) 12%, transparent);
    --focus: var(--vf-accent);
    --radius: 12px;
    --radius-sm: 8px;
    --radius-pill: 999px;
    --space-1: 0.25rem;
    --space-2: 0.5rem;
    --space-3: 0.75rem;
    --space-4: 1rem;
    --space-5: 1.25rem;
    --space-6: 1.5rem;
    --space-8: 2rem;
    --space-12: 3rem;
    --shell-max: 1080px;
    --gutter: clamp(1.25rem, 4vw, 2rem);
    --font-sans: 'Geist', -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    --font-serif: 'Newsreader', ui-serif, Georgia, serif;
    --font-mono: 'JetBrains Mono', 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background:
      radial-gradient(circle at 12% -18%, rgba(123, 196, 200, 0.12), transparent 34rem),
      radial-gradient(circle at 90% 0%, rgba(212, 154, 166, 0.08), transparent 28rem),
      var(--bg);
    color: var(--text);
    font: 15px/1.5 var(--font-sans);
    letter-spacing: 0;
  }
  button, input, select, textarea { font: inherit; color: inherit; }
  a { color: var(--link); text-decoration: none; }
  a:hover { text-decoration: underline; }
  code { font-family: var(--font-mono); font-size: 0.9em; }
  .shell-header {
    background: color-mix(in srgb, var(--bg) 92%, transparent);
    border-bottom: 1px solid var(--line);
    backdrop-filter: blur(14px);
    position: sticky;
    top: 0;
    z-index: 5;
  }
  .shell-header-inner {
    width: min(var(--shell-max), 100%);
    margin: 0 auto;
    padding: var(--space-3) var(--gutter);
    display: flex;
    align-items: center;
    gap: var(--space-4);
    min-height: 3.5rem;
    flex-wrap: wrap;
  }
  .shell-brand {
    display: inline-flex;
    align-items: center;
    gap: var(--space-3);
    color: var(--text);
    font-weight: 600;
    font-size: 1rem;
    letter-spacing: 0;
  }
  .shell-brand:hover { text-decoration: none; color: var(--text); }
  .shell-brand-dot {
    width: 0.7rem; height: 0.7rem;
    background: var(--accent);
    border-radius: var(--radius-pill);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 16%, transparent);
  }
  .shell-brand-word {
    display: inline-flex;
    align-items: baseline;
    gap: 0;
    white-space: nowrap;
  }
  .shell-brand-word em {
    font-family: var(--font-serif);
    font-style: italic;
    font-weight: 500;
    letter-spacing: 0;
  }
  .shell-brand-word .dot {
    color: var(--accent);
    font-family: var(--font-mono);
    padding: 0 0.08rem;
  }
  .shell-brand-word .fish {
    font-family: var(--font-mono);
    font-size: 0.94em;
    letter-spacing: 0.01em;
  }
  .shell-brand-product {
    color: var(--muted);
    font-size: 0.9rem;
    font-weight: 500;
  }
  .shell-brand-sub {
    color: var(--muted);
    font-weight: 400;
    font-size: 0.85rem;
  }
  .shell-nav {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
    margin-left: auto;
  }
  .shell-nav a, .shell-nav .pill, .shell-nav form button {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    min-height: 2.75rem;
    padding: 0.45rem 0.85rem;
    border-radius: var(--radius-pill);
    border: 1px solid var(--line);
    background: var(--panel);
    color: var(--muted);
    font-size: 0.86rem;
    cursor: pointer;
  }
  .shell-nav a:hover, .shell-nav form button:hover {
    color: var(--text);
    text-decoration: none;
    border-color: var(--line-strong);
  }
  .shell-nav a[aria-current="page"] {
    color: var(--text);
    background: var(--panel-2);
    border-color: var(--line-strong);
  }
  .shell-nav .who {
    color: var(--muted);
    background: transparent;
    border-color: transparent;
    padding-left: 0;
    padding-right: var(--space-1);
    cursor: default;
  }
  .shell-nav .who strong { color: var(--text); font-weight: 600; }
  .shell-nav form { margin: 0; }
  .shell-main {
    width: min(var(--shell-max), 100%);
    margin: 0 auto;
    padding: var(--space-6) var(--gutter) var(--space-12);
    display: grid;
    gap: var(--space-6);
  }
  .shell-main > section {
    display: grid;
    gap: var(--space-3);
  }
  .shell-main > section > h2 {
    margin: 0;
    font-size: 0.78rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: 600;
  }
  .shell-main > section > .hint {
    margin: 0;
    color: var(--muted);
    line-height: 1.5;
  }
  :is(a, button, input, select, textarea, summary):focus-visible {
    outline: 2px solid var(--focus);
    outline-offset: 3px;
  }
  @media (max-width: 720px) {
    .shell-header-inner { gap: var(--space-2); }
    .shell-nav { width: 100%; margin-left: 0; }
    .shell-nav .who { order: -1; width: 100%; padding-left: 0; }
  }
"""


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def json_for_script(value: Any) -> str:
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def user_role(account: Mapping[str, Any] | None) -> str:
    if not account or not account.get("authenticated"):
        return "none"
    user_value = account.get("user")
    user = user_value if isinstance(user_value, Mapping) else {}
    return str(user.get("role") or "none")


def can_admin(account: Mapping[str, Any] | None) -> bool:
    return user_role(account) in {"owner", "admin", "superadmin"}


def _nav_link(href: str, label: str, *, active: bool) -> str:
    current = ' aria-current="page"' if active else ""
    return f'<a href="{esc(href)}"{current}>{esc(label)}</a>'


def render_shell_header(
    *,
    title: str = "AI Gateway",
    brand: str = "AI Gateway",
    current: str = "",
    show_account: bool = True,
    show_admin: bool = False,
    show_status: bool = False,
    actor_label: str | None = None,
    logout_url: str | None = None,
    extra_pills: Iterable[tuple[str, str]] = (),
) -> str:
    """Return the shared header HTML.

    ``current`` is one of ``"account"``, ``"admin"``, ``"status"`` and marks the
    active nav link. ``actor_label`` adds a "Signed in as ..." pill and, when
    ``logout_url`` is set, a Sign out link. The brand always links to ``/``.
    """

    links: list[str] = []
    if show_account:
        links.append(_nav_link("/", "Home", active=current == "account"))
    if show_admin:
        links.append(_nav_link("/admin", "Admin", active=current == "admin"))
    if show_status:
        links.append(_nav_link("/status", "Status", active=current == "status"))

    for href, label in extra_pills:
        links.append(_nav_link(href, label, active=False))

    actor_html = ""
    if actor_label:
        actor_html = (
            '<span class="pill who" aria-label="Signed-in account">'
            f"<strong>{esc(actor_label)}</strong>"
            "</span>"
        )
    logout_html = ""
    if logout_url:
        logout_html = f'<a href="{esc(logout_url)}">Sign out</a>'

    nav_html = actor_html + "".join(links) + logout_html
    nav_block = (
        '<nav class="shell-nav" aria-label="Site sections">'
        + nav_html
        + "</nav>"
        if nav_html
        else ""
    )

    return (
        '<header class="shell-header" role="banner">'
        '<div class="shell-header-inner">'
        f'<a class="shell-brand" href="/" aria-label="{esc(brand)} home">'
        '<span class="shell-brand-dot" aria-hidden="true"></span>'
        '<span class="shell-brand-word" aria-hidden="true">'
        '<em>vivid</em><span class="dot">.</span><span class="fish">fish</span>'
        "</span>"
        f'<span class="shell-brand-product">{esc(brand)}</span>'
        "</a>"
        + nav_block
        + "</div>"
        "</header>"
    )
