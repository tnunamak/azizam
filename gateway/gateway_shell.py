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
    --bg: oklch(16% 0.018 250);
    --panel: oklch(21% 0.02 250);
    --panel-2: oklch(25% 0.022 250);
    --line: oklch(34% 0.028 250);
    --text: oklch(93% 0.018 250);
    --muted: oklch(73% 0.034 250);
    --soft: oklch(62% 0.04 250);
    --accent: oklch(78% 0.15 168);
    --accent-ink: oklch(15% 0.028 168);
    --link: oklch(83% 0.10 220);
    --warn: oklch(83% 0.13 74);
    --warn-bg: oklch(27% 0.065 74);
    --bad: oklch(80% 0.13 28);
    --bad-bg: oklch(27% 0.062 28);
    --good: oklch(82% 0.13 150);
    --good-bg: oklch(26% 0.056 150);
    --focus: oklch(67% 0.16 235);
    --radius: 8px;
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
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  }
  button, input, select, textarea { font: inherit; color: inherit; }
  a { color: var(--link); text-decoration: none; }
  a:hover { text-decoration: underline; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em; }
  .shell-header {
    background: oklch(18% 0.02 250);
    border-bottom: 1px solid var(--line);
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
    gap: var(--space-2);
    color: var(--text);
    font-weight: 650;
    font-size: 0.98rem;
    letter-spacing: 0;
  }
  .shell-brand:hover { text-decoration: none; color: var(--text); }
  .shell-brand-dot {
    width: 0.6rem; height: 0.6rem;
    background: var(--accent);
    border-radius: 999px;
    box-shadow: 0 0 0 3px oklch(24% 0.05 168);
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
    min-height: 2.5rem;
    padding: 0.4rem 0.75rem;
    border-radius: 999px;
    border: 1px solid var(--line);
    background: var(--panel);
    color: var(--muted);
    font-size: 0.86rem;
    cursor: pointer;
  }
  .shell-nav a:hover, .shell-nav form button:hover {
    color: var(--text);
    text-decoration: none;
    border-color: oklch(45% 0.03 250);
  }
  .shell-nav a[aria-current="page"] {
    color: var(--text);
    background: var(--panel-2);
    border-color: oklch(48% 0.03 250);
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
        links.append(_nav_link("/", "Account", active=current == "account"))
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
        f"<span>{esc(brand)}</span>"
        "</a>"
        + nav_block
        + "</div>"
        "</header>"
    )
