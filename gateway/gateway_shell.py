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
    "render_bottom_tabs",
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
  /* The base color lives on html so a page taller than the viewport never
     falls through to a bare default below the body. */
  html { background: var(--bg); }
  html, body { min-height: 100%; }
  body {
    margin: 0;
    /* The accent glows are fixed to the viewport so they cover the whole page
       on scroll and the background never cuts off at the fold. */
    background:
      radial-gradient(circle at 12% -18%, rgba(123, 196, 200, 0.12), transparent 34rem),
      radial-gradient(circle at 90% 0%, rgba(212, 154, 166, 0.08), transparent 28rem),
      var(--bg);
    background-attachment: fixed, fixed, fixed;
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
    .shell-nav { margin-left: auto; }
    /* When a bottom tab bar provides section nav, the header's section links
       (Home/Admin/Status) are redundant on mobile — hide them and the identity
       pill, leaving only the logout control. (No tab bar, e.g. admin: header
       nav stays.) */
    body.has-tabbar .shell-nav-section,
    body.has-tabbar .shell-nav .who { display: none; }
  }

  /* ============================================================
     Design primitives — ported from the Vivid Fish design handoff
     (docs/design/vivid-fish-handoff/gateway/atoms.jsx). Shared by the
     user portal and admin so screens read as one calm, branded app.
     ============================================================ */

  /* Status dot — a small reachability/health indicator. */
  .vf-dot {
    display: inline-block;
    width: 0.5rem; height: 0.5rem;
    border-radius: var(--radius-pill);
    background: var(--soft);
    flex: 0 0 auto;
  }
  .vf-dot.ok   { background: var(--good); }
  .vf-dot.warn { background: var(--warn); }
  .vf-dot.bad  { background: var(--bad); }
  .vf-dot.info { background: var(--info); }
  .vf-dot.pulse { animation: vf-pulse 2.2s ease-in-out infinite; }
  @keyframes vf-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  @media (prefers-reduced-motion: reduce) {
    .vf-dot.pulse { animation: none; }
  }

  /* Eyebrow / kicker — mono, uppercase, tracked. */
  .vf-kicker {
    font-family: var(--font-mono);
    font-size: 0.65rem;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--soft);
    margin: 0;
  }

  /* Serif sentence — the "human sentence" voice for plain-language access. */
  .vf-sentence {
    margin: 0;
    font-family: var(--font-serif);
    font-style: italic;
    font-size: 0.98rem;
    line-height: 1.5;
    color: var(--text);
  }
  .vf-sentence.plain { font-style: normal; }
  .vf-sentence strong { color: var(--accent); font-weight: 600; font-style: normal; }

  /* Pill — mono, uppercase, tinted tone backgrounds. */
  .vf-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    height: 1.375rem;
    padding: 0 0.6rem;
    border-radius: var(--radius-pill);
    background: var(--panel-3);
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: 0.66rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .vf-pill.accent  { background: color-mix(in srgb, var(--accent) 14%, transparent); color: var(--accent); }
  .vf-pill.ok      { background: var(--good-bg); color: var(--good); }
  .vf-pill.warn    { background: var(--warn-bg); color: var(--warn); }
  .vf-pill.bad     { background: var(--bad-bg); color: var(--bad); }
  .vf-pill.info    { background: color-mix(in srgb, var(--info) 14%, transparent); color: var(--info); }

  /* Card — calm raised surface with the design's 14px radius. */
  .vf-card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: var(--space-4);
  }
  .vf-card.raised { background: var(--panel-2); }
  a.vf-card, button.vf-card, .vf-card[role="button"] { cursor: pointer; text-align: left; width: 100%; }
  a.vf-card:hover { text-decoration: none; border-color: var(--line-strong); }

  /* Button — pill-shaped, four variants matching the design's Btn. */
  .vf-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    height: 2.75rem;
    padding: 0 1.125rem;
    border-radius: var(--radius-pill);
    border: 1px solid transparent;
    font-family: var(--font-sans);
    font-size: 0.875rem;
    font-weight: 600;
    letter-spacing: -0.005em;
    cursor: pointer;
  }
  .vf-btn.full { width: 100%; }
  .vf-btn.lg { height: 3.25rem; font-size: 0.97rem; padding: 0 1.375rem; }
  .vf-btn.sm { height: 2rem; font-size: 0.78rem; padding: 0 0.875rem; font-weight: 500; }
  .vf-btn.primary {
    background: var(--accent);
    color: var(--accent-ink);
    box-shadow: 0 1px 0 rgba(255,255,255,0.12) inset, 0 4px 14px rgba(0,0,0,0.18);
  }
  .vf-btn.secondary { background: var(--panel-2); color: var(--text); border-color: var(--line-strong); font-weight: 500; }
  .vf-btn.ghost { background: transparent; color: var(--muted); font-weight: 500; }
  .vf-btn.danger { background: transparent; color: var(--bad); border-color: var(--bad); }
  .vf-btn:hover { filter: brightness(1.06); text-decoration: none; }
  .vf-btn:disabled { opacity: 0.4; cursor: not-allowed; filter: none; }

  /* Section — kicker + uppercase title with an optional trailing action. */
  .vf-section { display: grid; gap: 0.625rem; }
  .vf-section-head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); }
  .vf-section-head .vf-section-title {
    margin: 0;
    font-family: var(--font-sans);
    font-weight: 600;
    font-size: 0.8rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
  }

  /* Stacked rows inside a single card (padding: 0 card + divided rows). */
  .vf-rows { display: grid; }
  .vf-rows > .vf-row { display: flex; align-items: center; gap: 0.625rem; padding: 0.75rem 0.875rem; }
  .vf-rows > .vf-row + .vf-row { border-top: 1px solid var(--line); }

  /* ============================================================
     Mobile foundation — bottom-tab nav (design's native-app shell).
     Mobile-first: a fixed bottom bar with generous touch targets is the
     canonical navigation; on desktop it's hidden and the sticky header nav
     takes over. Content gets bottom padding on mobile so nothing hides
     behind the bar (incl. iOS safe-area inset).
     ============================================================ */
  .tabbar {
    position: fixed;
    left: 0; right: 0; bottom: 0;
    z-index: 20;
    display: grid;
    grid-template-columns: repeat(var(--tabbar-count, 5), 1fr);
    background: color-mix(in srgb, var(--bg) 94%, transparent);
    backdrop-filter: blur(14px);
    border-top: 1px solid var(--line);
    padding-bottom: env(safe-area-inset-bottom, 0px);
  }
  .tabbar-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.2rem;
    min-height: 3.5rem;
    padding: 0.5rem 0.25rem;
    color: var(--soft);
    font-size: 0.66rem;
    font-weight: 500;
    letter-spacing: 0.01em;
    -webkit-tap-highlight-color: transparent;
  }
  .tabbar-item:hover { color: var(--muted); text-decoration: none; }
  .tabbar-item.is-active { color: var(--accent); }
  .tabbar-icon { display: inline-flex; }
  .tabbar-label { line-height: 1; }
  /* Reserve space so the fixed bar never overlaps content on mobile. */
  body.has-tabbar {
    padding-bottom: calc(3.75rem + env(safe-area-inset-bottom, 0px));
  }
  /* Desktop: the header nav is the navigation; hide the bottom bar. */
  @media (min-width: 721px) {
    .tabbar { display: none; }
    body.has-tabbar { padding-bottom: 0; }
  }

  /* Touch-target floor on mobile: every interactive control >= 44px. */
  @media (max-width: 720px) {
    .vf-btn, .vf-btn.sm { min-height: 2.75rem; }
    button, .pill, summary { min-height: 2.75rem; }
    input, select, textarea { min-height: 2.75rem; }
  }

  /* Sheet / drawer — the design uses sheets, not modals, on mobile. A
     bottom-anchored panel that slides up; centered dialog on desktop. */
  .vf-sheet-backdrop {
    position: fixed; inset: 0; z-index: 30;
    background: rgba(4, 10, 16, 0.55);
    display: flex; align-items: flex-end; justify-content: center;
  }
  .vf-sheet {
    width: 100%;
    max-height: 88vh;
    overflow: auto;
    background: var(--panel);
    border-top-left-radius: 18px;
    border-top-right-radius: 18px;
    border: 1px solid var(--line);
    border-bottom: 0;
    padding: var(--space-5) var(--gutter)
      calc(var(--space-5) + env(safe-area-inset-bottom, 0px));
  }
  .vf-sheet-grip {
    width: 2.25rem; height: 0.25rem; border-radius: 999px;
    background: var(--line-strong); margin: 0 auto var(--space-3);
  }
  @media (min-width: 721px) {
    .vf-sheet-backdrop { align-items: center; }
    .vf-sheet {
      max-width: 32rem;
      border-radius: 16px;
      border-bottom: 1px solid var(--line);
    }
    .vf-sheet-grip { display: none; }
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
    return f'<a class="shell-nav-section" href="{esc(href)}"{current}>{esc(label)}</a>'


def render_shell_header(
    *,
    title: str = "AI Gateway",
    brand: str = "AI Gateway",
    wordmark: tuple[str, str] | None = None,
    current: str = "",
    show_account: bool = True,
    show_admin: bool = False,
    show_status: bool = False,
    actor_label: str | None = None,
    logout_url: str | None = None,
    extra_pills: Iterable[tuple[str, str]] = (),
) -> str:
    """Return the shared header HTML.

    ``brand`` is the product label shown in the header (defaults to the neutral
    "AI Gateway"). ``wordmark`` is an optional deployment wordmark as a
    ``(name, tld)`` pair, e.g. ``("vivid", "fish")``; when set it renders the
    stylized ``name.tld`` mark before the product label. It is ``None`` by
    default so a generic install shows only the neutral product mark — the
    deployment brand is data, not baked into product code.

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

    wordmark_html = ""
    if wordmark:
        mark_name, mark_tld = wordmark
        wordmark_html = (
            '<span class="shell-brand-word" aria-hidden="true">'
            f"<em>{esc(mark_name)}</em>"
            '<span class="dot">.</span>'
            f'<span class="fish">{esc(mark_tld)}</span>'
            "</span>"
        )

    return (
        '<header class="shell-header" role="banner">'
        '<div class="shell-header-inner">'
        f'<a class="shell-brand" href="/" aria-label="{esc(brand)} home">'
        '<span class="shell-brand-dot" aria-hidden="true"></span>'
        + wordmark_html
        + f'<span class="shell-brand-product">{esc(brand)}</span>'
        "</a>"
        + nav_block
        + "</div>"
        "</header>"
    )


# Bottom-tab icons (24-grid, single stroke), matching the design's icon set.
_TAB_ICONS: dict[str, str] = {
    "home": '<path d="M3 11l9-7 9 7v9a2 2 0 0 1-2 2h-4v-7h-6v7H5a2 2 0 0 1-2-2z"/>',
    "key": '<circle cx="8" cy="12" r="3"/><path d="M11 12h10v3M16 12v3"/>',
    "capability": (
        '<circle cx="12" cy="12" r="2"/>'
        '<path d="M12 4v3M12 17v3M4 12h3M17 12h3M6 6l2 2M16 16l2 2M6 18l2-2M16 8l2-2"/>'
    ),
    "signal": '<path d="M4 19h2v-5H4zM10 19h2v-9h-2zM16 19h2V5h-2z"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
}


def _tab_icon(name: str) -> str:
    body = _TAB_ICONS.get(name, "")
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" '
        'width="22" height="22" aria-hidden="true">' + body + "</svg>"
    )


def render_bottom_tabs(
    *,
    active: str = "home",
    admin: bool = False,
    tabs: Iterable[tuple[str, str, str, str]] | None = None,
) -> str:
    """Mobile bottom-tab navigation (hidden on desktop, where the header serves).

    Each tab is ``(id, label, href, icon_name)``. The default set lists ONLY
    destinations that actually exist — a tab that scrolls nowhere is nav that
    lies, and costs the user trust every time they tap it. The design's
    Keys/What-works/Activity tabs are added back, one at a time, in the commit
    that ships each target section (Phase 2). On desktop this bar is
    ``display:none`` and the header nav serves.
    """
    if tabs is not None:
        items = list(tabs)
    else:
        items = [("home", "Home", "/", "home")]
        if admin:
            items.append(("me", "Admin", "/admin", "shield"))
    links = "".join(
        f'<a class="tabbar-item{" is-active" if tid == active else ""}" '
        f'href="{esc(href)}"'
        f'{" aria-current=\"page\"" if tid == active else ""}>'
        f'<span class="tabbar-icon">{_tab_icon(icon)}</span>'
        f'<span class="tabbar-label">{esc(label)}</span>'
        "</a>"
        for tid, label, href, icon in items
    )
    return (
        '<nav class="tabbar" aria-label="Primary" '
        f'style="--tabbar-count:{len(items)}">'
        + links
        + "</nav>"
    )
