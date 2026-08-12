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

import time
from typing import Any, Mapping

from gateway.gateway_shell import (
    SHELL_CSS,
    can_admin,
    esc,
    json_for_script,
    render_bottom_tabs,
    render_shell_header,
)

__all__ = ["render_user_portal"]


_PAGE_CSS = """
  /* Page-specific styles for the user portal. The --vf-* palette and the vf-*
     component classes come from the vendored brand package via SHELL_CSS; this
     block only adds portal layout (hero, cards, key rows, activity feed) on top
     of those tokens. */
  body {
    background:
      radial-gradient(circle at 18% -10%, rgba(123,196,200,0.14), transparent 30rem),
      radial-gradient(circle at 100% 8%, rgba(212,154,166,0.10), transparent 24rem),
      var(--vf-bg);
    background-repeat: no-repeat;
    background-size: 100% 38rem, 100% 34rem, auto;
  }
  .shell-main {
    max-width: 960px;
    gap: var(--space-5);
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  .lead { color: var(--muted); margin: 0; line-height: 1.55; }
  .eyebrow {
    color: var(--vf-text-subtle);
    font-size: 0.74rem;
    font-weight: 750;
    letter-spacing: 0.16em;
    margin: 0;
    text-transform: uppercase;
  }
  /* Entry point: a calm, confident title (kicker + name), like the leading dev
     tools' keys surface. Not a chummy time-of-day greeting. */
  .portal-topbar {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--space-3);
    padding-bottom: var(--space-1);
  }
  .portal-topbar-id { display: grid; gap: 0.2rem; min-width: 0; }
  .portal-greeting {
    margin: 0;
    font-family: var(--font-serif);
    font-weight: 600;
    font-size: 1.7rem;
    line-height: 1.05;
    letter-spacing: -0.01em;
    color: var(--text);
  }
  .card, .app-create, .base-card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
    align-content: start;
    min-width: 0;
  }
  .card h3, .app-create h3 { margin: 0; font-size: 1rem; }
  .card p, .app-create p { margin: 0; color: var(--muted); line-height: 1.5; }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
  }
  .hero {
    display: grid;
    gap: var(--space-4);
    background:
      linear-gradient(180deg, rgba(123,196,200,0.07), rgba(123,196,200,0.015)),
      var(--panel);
    border: 1px solid var(--vf-border-strong);
    border-radius: calc(var(--radius) + 6px);
    padding: clamp(var(--space-4), 4vw, var(--space-6));
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 18px 60px rgba(0,0,0,0.18);
  }
  .hero h2 {
    margin: 0;
    max-width: 12ch;
    font-size: 4.1rem;
    line-height: 0.98;
    color: var(--text);
    text-transform: none;
    letter-spacing: 0;
  }
  .hero .lead { max-width: 44rem; font-size: 1.02rem; }
  .hero .actions a, .hero .actions button { font-size: 0.95rem; }
  /* Sign-in door: warm post-invite landing. */
  .hero-door { gap: var(--space-5); }
  .hero-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }
  .hero-top .eyebrow { margin: 0; }
  .hero-headline { max-width: 16ch; }
  .hero-actions { display: grid; gap: var(--space-3); max-width: 26rem; }
  .hero-actions .vf-button { width: 100%; }
  .hero-or {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    color: var(--soft);
    font-family: var(--font-mono);
    font-size: 0.72rem;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }
  .hero-or .rule { flex: 1; height: 1px; background: var(--line); }
  .base-card {
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    background: var(--vf-surface-1);
  }
  .base-card > div, .shell-main > section, .vf-section-head > div {
    min-width: 0;
  }
  .base-card h2 {
    margin: 0;
    color: var(--vf-text-subtle);
    font-size: 0.74rem;
    font-weight: 750;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }
  .base-card code {
    color: var(--text);
    font-size: 1rem;
    overflow-wrap: anywhere;
  }
  .base-card p { margin: 0; color: var(--muted); }
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
  button.icon-button {
    min-width: 6.25rem;
    padding: 0 0.85rem;
    color: var(--vf-accent);
    background: var(--panel-2);
    border-color: var(--line);
    white-space: nowrap;
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
  .toast {
    position: fixed;
    left: 50%;
    bottom: var(--space-4);
    z-index: 20;
    width: min(28rem, calc(100vw - 2rem));
    padding: var(--space-3);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: oklch(14.5% 0.016 250);
    color: var(--text);
    box-shadow: 0 18px 48px rgba(0,0,0,0.26);
    transform: translate(-50%, calc(100% + var(--space-6)));
    opacity: 0;
    pointer-events: none;
    transition: opacity 180ms ease, transform 180ms ease;
  }
  .toast.show {
    transform: translate(-50%, 0);
    opacity: 1;
  }
  .toast.good { color: var(--good); background: var(--good-bg); }
  .toast.warn { color: var(--warn); background: var(--warn-bg); }
  .toast.bad { color: var(--bad); background: var(--bad-bg); }
  .message .secret {
    display: grid;
    gap: var(--space-2);
  }
  .message .secret code {
    display: block;
    color: var(--text);
    overflow-wrap: anywhere;
  }
  .message .secret button {
    min-width: 6.25rem;
  }
  .snippet {
    min-width: 0;
    max-width: 100%;
    white-space: nowrap;
    overflow-x: auto;
    overflow-y: hidden;
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
  /* The apps + recent sections use the brand .vf-section / .vf-section-head /
     .vf-section-title pattern (kicker over an uppercase title, optional action);
     no gateway-local section-head styles are needed. */
  /* Empty-state hero CTA: a calm gradient card that leads to the first key. */
  .app-create {
    background:
      linear-gradient(180deg, rgba(123,196,200,0.06), transparent),
      var(--panel);
    border: 1px solid var(--vf-border-strong);
    border-radius: 14px;
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
  }
  .app-create.cta { padding: var(--space-5); }
  .cta-head { display: flex; align-items: center; gap: var(--space-3); }
  .cta-head h3 { margin: 0; font-size: 1.05rem; }
  .cta-head p { margin: 0.1rem 0 0; color: var(--muted); font-size: 0.9rem; }
  .cta-icon {
    flex: 0 0 auto;
    width: 2.25rem; height: 2.25rem;
    display: inline-flex; align-items: center; justify-content: center;
    border-radius: 10px;
    background: var(--panel-2);
    color: var(--accent);
  }
  .app-create-form { display: grid; gap: var(--space-2); margin: 0; }
  .create-help { margin: 0; color: var(--muted); line-height: 1.5; }
  /* "Create another App" stays a quiet disclosure once apps exist. */
  .app-create-disclosure {
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: var(--panel);
    padding: var(--space-3) var(--space-4);
  }
  .app-create-disclosure summary { cursor: pointer; color: var(--muted); }
  .app-create-disclosure[open] summary { color: var(--text); margin-bottom: var(--space-3); }
  .app-create-disclosure .app-create-form { margin-top: 0; }
  /* App card (design 03): a calm .vf-card whose summary is the app row —
     glyph · name · masked key/count · chevron. Tapping expands the keys and
     management controls in place. The card owns no padding; the summary and
     body set their own so the closed row reads as a single tappable line. */
  .app-card {
    padding: 0;
  }
  /* Keep the summary at its default (block) display: giving a <summary>
     display:flex silently breaks the native click-to-toggle in Chromium. The
     inner .vf-row does the horizontal layout instead. */
  .app-card > .app-card-summary {
    list-style: none;
    cursor: pointer;
    padding: var(--space-4);
  }
  .app-card > .app-card-summary::-webkit-details-marker { display: none; }
  .app-card > .app-card-summary .vf-row { width: 100%; justify-content: space-between; }
  .app-card-chevron {
    color: var(--soft);
    flex: 0 0 auto;
    display: inline-flex;
    transition: transform 0.18s ease;
  }
  .app-card[open] > .app-card-summary .app-card-chevron { transform: rotate(180deg); }
  @media (prefers-reduced-motion: reduce) { .app-card-chevron { transition: none; } }
  .app-card-body {
    padding: 0 var(--space-4) var(--space-4);
    display: grid;
    gap: var(--space-3);
    min-width: 0;
  }
  .app-detail-line { margin: 0; color: var(--muted); font-size: 0.86rem; line-height: 1.5; }
  /* Per-App initials avatar — a deterministic colored tile so each App is
     instantly distinguishable in the list (replaces a repeated generic glyph). */
  .app-avatar {
    width: 2.125rem;
    height: 2.125rem;
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 10px;
    font-family: var(--font-sans);
    font-size: 0.8rem;
    font-weight: 650;
    letter-spacing: 0.01em;
  }
  /* Trailing column of the collapsed app row: recency text + chevron. */
  .app-card-trailing {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    flex: 0 0 auto;
  }
  .app-card-usage {
    font-size: 0.8rem;
    color: var(--muted);
    white-space: nowrap;
  }
  /* .badge — used by the key-detail sheet's access-list cards (client JS). */
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
  .key-list { margin-top: var(--space-1); }
  /* A key is a calm, tappable row inside the expanded app card: label + masked
     value on the left, an honest recency on the right, then a chevron. The
     whole row opens the detail sheet. */
  .key-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    width: 100%;
    padding: 0.7rem 0.85rem;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: var(--panel-2);
    color: var(--text);
    text-align: left;
    cursor: pointer;
    min-height: 3rem;
  }
  .key-row:hover { border-color: var(--line-strong); background: var(--panel-3); }
  .key-row-main { display: grid; gap: 0.15rem; min-width: 0; flex: 1; }
  .key-row-name {
    font-weight: 600;
    font-size: 0.92rem;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .key-row-prefix {
    font-size: 0.78rem;
    color: var(--soft);
    font-family: var(--font-mono);
    letter-spacing: 0.02em;
  }
  .key-row-usage {
    font-size: 0.76rem;
    color: var(--muted);
    white-space: nowrap;
    flex: 0 0 auto;
  }
  .key-row-chevron { color: var(--soft); flex: 0 0 auto; display: inline-flex; }
  /* "+ New key" — quiet inline disclosure under the key list. */
  .key-create { margin-top: var(--space-1); }
  .key-create > summary {
    list-style: none;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--muted);
    font-size: 0.88rem;
    padding: 0.45rem 0.2rem;
    min-height: 2.75rem;
  }
  .key-create > summary::-webkit-details-marker { display: none; }
  .key-create[open] > summary { color: var(--text); }
  .key-create-plus {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.3rem;
    height: 1.3rem;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    color: var(--accent);
    font-size: 1rem;
    line-height: 1;
  }
  .key-create .key-form { margin-top: var(--space-2); }
  /* "Remove this App" — destructive, behind a quiet disclosure at the foot. */
  .app-remove { margin-top: var(--space-2); }
  .app-remove > summary {
    cursor: pointer;
    color: var(--soft);
    font-size: 0.82rem;
    padding: 0.35rem 0.2rem;
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
  }
  .app-remove[open] > summary { color: var(--muted); }
  .app-remove-warn {
    margin: var(--space-2) 0;
    color: var(--muted);
    font-size: 0.85rem;
    line-height: 1.5;
  }
  .key-note, .empty { color: var(--muted); font-size: 0.88rem; }
  .empty {
    border: 1px dashed var(--line);
    border-radius: var(--radius);
    margin: 0;
    padding: var(--space-4);
    text-align: center;
  }
  .access-list { display: grid; gap: var(--space-2); }
  /* Recent-activity feed — the design's divided-row feed (brand .vf-feed /
     .vf-feed-row). The rows are buttons (they open the receipt sheet), so this
     adds the button reset + hover on top of the brand row layout. */
  .recent-card .empty { margin: 0; padding: var(--space-5) var(--space-4); border: 0; }
  /* Light empty state: when the feed holds only the empty line, drop the card
     chrome so a "no activity yet" message doesn't carry more visual weight than
     real rows would (prior art: dev-tool keys dashboards keep empty states to a
     muted line, not a bordered box). */
  .recent-card:has(> .empty:only-child) {
    border: 0;
    background: transparent;
    padding: 0;
  }
  .activity-row {
    width: 100%;
    background: transparent;
    color: var(--text);
    text-align: left;
    cursor: pointer;
    border-left: 0;
    border-right: 0;
    border-radius: 0;
  }
  .activity-row:hover { background: var(--panel-2); }
  /* Receipt detail — labelled fact rows + a "why" block for failures. */
  .receipt-summary {
    margin: 0 0 var(--space-4);
    font-family: var(--font-serif);
    font-size: 1.02rem;
    line-height: 1.5;
    color: var(--muted);
  }
  .receipt-summary strong { color: var(--text); font-weight: 600; font-style: normal; }
  .receipt-facts { display: grid; gap: 0; margin-bottom: var(--space-4); }
  .receipt-fact {
    display: flex; justify-content: space-between; gap: var(--space-4);
    padding: 0.6rem 0; border-top: 1px solid var(--line);
  }
  .receipt-fact:first-child { border-top: 0; }
  .receipt-fact-key { color: var(--soft); font-size: 0.84rem; }
  .receipt-fact-val {
    color: var(--text); font-size: 0.84rem; font-family: var(--font-mono);
    text-align: right; overflow-wrap: anywhere;
  }
  .receipt-why {
    border: 1px solid color-mix(in srgb, var(--bad) 32%, var(--line));
    background: var(--bad-bg);
    border-radius: var(--radius); padding: var(--space-3);
    margin-bottom: var(--space-3);
  }
  .receipt-why h3 { margin: 0 0 var(--space-2); font-size: 0.92rem; color: var(--bad); }
  .receipt-why ul { margin: 0; padding-left: 1.1rem; color: var(--text); font-size: 0.86rem; line-height: 1.5; }
  /* Key-detail sheet (design 07): a calm grouped summary, not a wall. */
  .key-detail-sentence {
    margin: 0 0 var(--space-3);
    font-family: var(--font-serif);
    font-size: 1.05rem;
    line-height: 1.5;
    color: var(--muted);
  }
  .key-detail-sentence strong { color: var(--text); font-weight: 600; font-style: normal; }
  .key-detail-pills {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-bottom: var(--space-4);
  }
  .key-detail-all { margin-bottom: var(--space-4); }
  .key-detail-all > summary {
    cursor: pointer;
    color: var(--muted);
    font-size: 0.88rem;
    padding: 0.45rem 0;
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
  }
  .key-detail-all[open] > summary { color: var(--text); margin-bottom: var(--space-2); }
  .key-reveal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    margin-bottom: var(--space-4);
  }
  .key-reveal-head h2 { margin: 0; font-size: 1.15rem; }
  .confirm-description { margin: 0; color: var(--muted); line-height: 1.5; }
  .confirm-status { min-height: 1.5rem; margin: var(--space-3) 0 0; color: var(--muted); }
  .confirm-status.bad { color: var(--bad); }
  .confirm-actions {
    display: flex;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-4);
  }
  @media (max-width: 720px) {
    .confirm-actions > button { flex: 1 1 10rem; }
  }
  .secret { display: grid; gap: var(--space-3); }
  .reveal-warning {
    margin: 0;
    color: var(--good);
    line-height: 1.5;
    font-weight: 500;
  }
  .reveal-key {
    display: block;
    font-family: var(--font-mono);
    font-size: 0.95rem;
    color: var(--text);
    background: oklch(14.5% 0.016 250);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    padding: var(--space-3);
    overflow-wrap: anywhere;
  }
  .reveal-subhead {
    margin: var(--space-2) 0 0;
    color: var(--muted);
    font-size: 0.82rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .reveal-block {
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: oklch(14.5% 0.016 250);
    overflow: hidden;
  }
  .reveal-block-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--line);
  }
  .reveal-block-label {
    color: var(--muted);
    font-size: 0.78rem;
    font-weight: 600;
  }
  .reveal-copy {
    min-height: 1.9rem;
    padding: 0.2rem 0.7rem;
    border-radius: var(--radius-pill);
    border: 1px solid var(--line);
    background: var(--panel-2);
    color: var(--accent);
    font-size: 0.78rem;
    cursor: pointer;
  }
  .reveal-code {
    margin: 0;
    padding: var(--space-3);
    font-family: var(--font-mono);
    font-size: 0.82rem;
    line-height: 1.55;
    color: oklch(88% 0.05 168);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .reveal-status {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-1);
    color: var(--muted);
    font-size: 0.86rem;
    line-height: 1.45;
  }
  .health-card { padding: 0; }
  .health-card > .health-summary {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-4);
    cursor: pointer;
    list-style: none;
  }
  .health-card > .health-summary::-webkit-details-marker { display: none; }
  .health-card .vf-status-dot { width: 0.625rem; height: 0.625rem; flex: 0 0 auto; }
  .health-text { min-width: 0; flex: 1; }
  .health-line {
    font-weight: 600;
    color: var(--text);
    letter-spacing: -0.005em;
  }
  .health-detail {
    color: var(--muted);
    font-size: 0.86rem;
    margin-top: 0.1rem;
  }
  .health-chevron {
    color: var(--soft);
    flex: 0 0 auto;
    display: inline-flex;
    transition: transform 0.18s ease;
  }
  .health-card[open] > .health-summary .health-chevron { transform: rotate(180deg); }
  @media (prefers-reduced-motion: reduce) { .health-chevron { transition: none; } }
  .health-caps {
    padding: 0 var(--space-4) var(--space-4);
    display: grid;
    gap: var(--space-2);
  }
  .cap-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: 0.55rem 0;
    border-top: 1px solid var(--line);
    min-width: 0;
  }
  .cap-row-name {
    font-size: 0.9rem;
    color: var(--text);
    flex: 1;
    min-width: 0;
    overflow-wrap: break-word;
  }
  .cap-row-count {
    font-family: var(--font-mono);
    font-size: 0.78rem;
    color: var(--soft);
  }
  @media (max-width: 720px) {
    .hero h2 { font-size: 2.6rem; }
    .row, .key-form, .base-card { grid-template-columns: 1fr; }
    .actions button { width: 100%; }
    .base-card button { width: 100%; }
  }
"""


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


def _active_credentials(app: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Active credentials for an App (the ones that still grant access)."""
    return [
        c
        for c in app.get("credentials", [])
        if isinstance(c, Mapping) and c.get("active", True)
    ]


# The canonical secret-masking format used by Stripe / OpenAI / Resend: keep the
# scheme prefix readable, mask the middle with bullet dots, reveal the last four
# so a user can disambiguate which key a row is. Our stored ``prefix`` is the
# full random head of the key (e.g. ``opk_UVPUywNEQuZvgW``); we show
# ``opk_ •••• vgW`` style — scheme + dots + last-4-of-the-known-head. We never
# have or render the full secret. Mirrored in the client JS as ``maskKey``.
_MASK_DOTS = "••••"  # four U+2022 bullets


def _mask_key(prefix: str) -> str:
    """Return ``scheme_ •••• last4`` for a credential's stored prefix."""
    raw = str(prefix or "").strip()
    if not raw:
        return "key"
    scheme, sep, tail = raw.partition("_")
    if sep and tail:
        last4 = tail[-4:] if len(tail) >= 4 else tail
        return f"{scheme}_ {_MASK_DOTS} {last4}"
    last4 = raw[-4:] if len(raw) >= 4 else raw
    return f"{_MASK_DOTS} {last4}"


def _relative_time(ts: Any, *, now: int | None = None) -> str:
    """Human relative time for an epoch-seconds value ('2m ago', '3d ago')."""
    try:
        seconds_ago = (int(time.time()) if now is None else int(now)) - int(ts)
    except (TypeError, ValueError):
        return ""
    if seconds_ago < 0:
        seconds_ago = 0
    if seconds_ago < 45:
        return "just now"
    minutes = seconds_ago // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    return f"{days // 365}y ago"


def _key_usage_label(record: Mapping[str, Any]) -> str:
    """A short, honest recency label for a key row's trailing metadata.

    Prefers real ``last_used_at`` ('used 2m ago'); falls back to 'Never used'
    once a key exists but has no recorded use. (The auth path records use via
    ``admin_store.touch_credential_last_used``, so this is real signal.)
    """
    last_used = record.get("last_used_at")
    if last_used:
        rel = _relative_time(last_used)
        return f"used {rel}" if rel else "used recently"
    return "Never used"


def _sanitize_client_credential(credential: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "prefix",
        "display_name",
        "created_at",
        "expires_at",
        "last_used_at",
        "active",
    }
    return {key: credential.get(key) for key in allowed if key in credential}


def _credential_html(record: Mapping[str, Any]) -> str:
    if not record.get("active", True):
        return ""
    credential_id = esc(record.get("id"))
    label = esc(record.get("display_name") or "API key")
    masked = esc(_mask_key(str(record.get("prefix") or "")))
    # Each key is a calm, tappable row (design 03): label · masked value (Stripe/
    # OpenAI/Resend convention: scheme + dots + last 4) on the left, and an
    # honest recency on the right ("used 2m ago" / "Never used", from real
    # last_used_at). Tapping opens the detail sheet (View-access + Revoke live
    # there — no destructive buttons on the calm surface).
    usage = esc(_key_usage_label(record))
    return (
        '<button class="key-row" type="button" data-view-owned-key="'
        + credential_id
        + '" data-key-label="'
        + label
        + '" aria-label="Open ' + label + '">'
        '<span class="key-row-main">'
        f'<span class="key-row-name">{label}</span>'
        f'<span class="key-row-prefix">{masked}</span>'
        "</span>"
        f'<span class="key-row-usage">{usage}</span>'
        '<span class="key-row-chevron" aria-hidden="true">'
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>'
        "</span>"
        "</button>"
    )


def _app_html(app: Mapping[str, Any], *, show_permission_details: bool = False) -> str:
    app_id = esc(app.get("id"))
    name = esc(app.get("name") or app_id or "App")
    # Only admins (show_permission_details) see a permission line, naming the
    # actual Profile. Non-admins get no Profile/permission chrome on this calm
    # surface — it's operator vocabulary they can't act on.
    profile = app.get("profile") if isinstance(app.get("profile"), Mapping) else None
    permission_text = "your account permissions"
    raw_profile_id = str(app.get("profile_id") or "")
    if profile:
        raw_profile_id = str(profile.get("id") or raw_profile_id)
        if profile.get("missing"):
            permission_text = "permissions that are currently unavailable"
        elif show_permission_details:
            permission_text = str(
                profile.get("name")
                or profile.get("policy_id")
                or raw_profile_id
                or "account permissions"
            )
        elif not bool(profile.get("inherited")):
            permission_text = "custom account permissions"
    credentials = _active_credentials(app)
    credential_html = (
        "".join(_credential_html(c) for c in credentials)
        if credentials
        else '<p class="empty">No API keys for this App yet.</p>'
    )
    can_mint = bool(app.get("can_mint_keys"))
    # Creating a key is a quiet, opt-in disclosure — not a form sitting open on
    # the surface. The calm default is the list of existing keys; "+ New key"
    # reveals the name field on demand (progressive disclosure).
    mint_html = (
        '<details class="key-create" data-key-create="' + app_id + '">'
        '<summary class="key-create-summary">'
        '<span class="key-create-plus" aria-hidden="true">+</span> New key'
        "</summary>"
        '<form class="key-form" data-mint-owned-key="' + app_id + '">'
        '<label class="field"><span>What is this key for?</span>'
        '<input type="text" name="label" placeholder="MacBook, Open WebUI, deploy script" '
        'aria-label="API key label"></label>'
        '<button class="vf-button" type="submit">Create key</button>'
        "</form>"
        "</details>"
        if can_mint
        else (
            '<p class="empty">This App cannot create keys until its '
            "permissions are available.</p>"
        )
    )
    # "Remove App" is a destructive action — it lives behind a quiet disclosure
    # at the foot of the card, never as a red button on the calm surface.
    remove_html = (
        '<details class="app-remove">'
        "<summary>Remove this App</summary>"
        '<p class="app-remove-warn">Deleting this App revokes all of its keys '
        "immediately. Apps using them lose access.</p>"
        '<button class="vf-button vf-button-danger" type="button" data-delete-owned-app="'
        + app_id
        + '" data-app-name="'
        + name
        + '">Delete '
        + name
        + "</button>"
        "</details>"
    )
    # The card's calm face is the design's app row (design 03): a glyph tile, the
    # App name over a quiet key-count sub-line on the left, and the App's most
    # recent key usage on the right ("used 2m ago" / "Never used", from real
    # last_used_at) so the row is balanced and informative instead of a lone
    # chevron in dead space. Tapping expands the keys + management controls in
    # place; each key row carries its own masked value and recency.
    active_count = len(credentials)
    sub_text = (
        "No keys yet"
        if active_count == 0
        else f"{active_count} key" + ("" if active_count == 1 else "s")
    )
    # Most-recent use across this App's keys → the trailing metadata.
    last_used_values = [
        int(c["last_used_at"])
        for c in credentials
        if c.get("last_used_at")
    ]
    if last_used_values:
        usage_meta = "used " + (_relative_time(max(last_used_values)) or "recently")
    elif active_count:
        usage_meta = "Never used"
    else:
        usage_meta = ""
    usage_html = (
        f'<span class="app-card-usage">{esc(usage_meta)}</span>' if usage_meta else ""
    )
    # Admins who asked for permission detail get one quiet line naming the
    # Profile; non-admins never see permission/Profile chrome on this surface.
    detail_line = (
        f'<p class="app-detail-line">Keys use {esc(permission_text)}.</p>'
        if show_permission_details
        else ""
    )
    return (
        '<details class="vf-card app-card" data-app-card="' + app_id + '">'
        '<summary class="app-card-summary">'
        '<span class="vf-row">'
        '<span class="vf-row-main">'
        + _app_avatar(app.get("name") or app_id or "App")
        + '<span class="vf-row-id">'
        f'<span class="vf-row-name">{name}</span>'
        f'<span class="vf-row-sub">{esc(sub_text)}</span>'
        "</span>"
        "</span>"
        '<span class="app-card-trailing">'
        + usage_html
        + '<span class="app-card-chevron" aria-hidden="true">'
        + _chevron_svg()
        + "</span>"
        "</span>"
        "</span>"
        "</summary>"
        '<div class="app-card-body">'
        + detail_line
        + '<div class="key-list" data-key-list="' + app_id + '">'
        + credential_html
        + "</div>"
        + mint_html
        + remove_html
        + "</div>"
        + "</details>"
    )


def _brand_noun(brand: str, wordmark: tuple[str, str] | None) -> str:
    """A short brand noun for body copy.

    Uses the deployment wordmark (e.g. ``vivid.fish``) when set, else the
    neutral product label. Keeps Tim's deployment brand out of a generic
    install's copy — the brand is data, not baked into the renderer.
    """
    if wordmark:
        name, tld = wordmark
        return f"{name}.{tld}"
    return brand


def _signed_out_hero(
    *,
    oidc_enabled: bool,
    login_url: str,
    owner_exists: bool = True,
    brand: str = "AI Gateway",
    wordmark: tuple[str, str] | None = None,
) -> str:
    """Signed-out landing hero.

    The door always offers a working next step — never a dead button. When
    OIDC is configured it is the primary sign-in path. When it is not, the
    operator paths (create the local owner, local sign-in, or paste an
    ``oak_`` admin key) live on ``/setup``, so the hero links there rather
    than cloning that flow. ``owner_exists`` only changes the copy: a fresh
    gateway is set up, an existing one is signed in to.
    """
    brand_noun = _brand_noun(brand, wordmark)
    actions: list[str] = []
    if oidc_enabled:
        actions.append(
            f'<a class="vf-button vf-button-lg" href="{esc(login_url)}">'
            f"Continue with {esc(brand_noun)} ID</a>"
        )
        # OIDC is the happy path; setup stays available as a quiet fallback
        # for operators who need the local-owner or admin-key route.
        setup_label = "Set up this gateway" if not owner_exists else "Operator setup"
        actions.append(
            '<a class="vf-button vf-button-secondary vf-button-lg" '
            f'href="/setup">{setup_label}</a>'
        )
    elif not owner_exists:
        # Fresh gateway, no OIDC: the only real action is to claim it on /setup.
        actions.append(
            '<a class="vf-button vf-button-lg" href="/setup">Set up this gateway</a>'
        )
    else:
        # Owner exists but no OIDC: local owner sign-in lives on /setup.
        actions.append(
            '<a class="vf-button vf-button-lg" href="/setup">Sign in</a>'
        )

    # Helper copy only when it tells the user something the button doesn't. When
    # an owner already exists (OIDC off), the "Sign in" button is self-evident —
    # no operator jargon about identity providers or OIDC at the door. On a
    # truly fresh gateway, one plain line signals "you're the first one here".
    if not oidc_enabled and not owner_exists:
        helper_html = (
            '<p class="lead">This gateway is brand new — set it up to become its '
            "owner and get your first key.</p>"
        )
    else:
        helper_html = ""

    # When OIDC is the primary path, offer a quiet "or" divider before the
    # secondary action — the design's two-option door. When OIDC is off, the
    # single /setup action stands alone (no divider).
    actions_html = actions[0]
    if oidc_enabled and len(actions) > 1:
        actions_html = (
            actions[0]
            + '<div class="hero-or"><span class="rule"></span>'
            '<span>or</span><span class="rule"></span></div>'
            + actions[1]
        )

    eyebrow = esc(wordmark and f"{wordmark[0]}.{wordmark[1]}" or brand)
    return (
        '<section aria-labelledby="hero-heading">'
        '<div class="hero hero-door">'
        '<div class="hero-top">'
        f'<p class="eyebrow">{eyebrow}</p>'
        '<span class="vf-pill vf-pill-accent">invite only</span>'
        "</div>"
        '<h2 id="hero-heading" class="hero-headline">'
        "Sign in to use the gateway.</h2>"
        f'<p class="lead">Your {esc(brand_noun)} account controls the local '
        'models, media workflows, and API keys you can use.</p>'
        + helper_html
        + f'<div class="actions hero-actions">{actions_html}</div>'
        "</div>"
        "</section>"
    )


def _signed_in_account(account: Mapping[str, Any]) -> str:
    apps = [a for a in account.get("apps", []) if isinstance(a, Mapping)]
    is_admin = can_admin(account)
    # The form fields are shared between the empty-state hero CTA and the
    # "create another App" disclosure, so "Create one App for each tool or
    # script" copy lives once.
    create_form_fields = (
        '<form class="app-create-form" data-create-owned-app>'
        '<p class="create-help">Create one App for each tool or script that '
        "needs an API key.</p>"
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
        else '<div id="apps-list" class="apps-list"><p class="empty">Create your first App to get an <code>opk_</code> API key.</p></div>'
    )
    app_count = len(apps)
    is_empty = app_count == 0

    # Empty-state hero CTA (design: "Make your first key"). On a fresh account
    # the create form is wrapped in a calm gradient card that leads the eye to
    # the one action that matters. With apps present, the form is a quiet
    # secondary affordance under the section action.
    if is_empty:
        connect_block = (
            '<div class="app-create cta">'
            '<div class="cta-head">'
            '<span class="cta-icon" aria-hidden="true">'
            + _key_glyph()
            + "</span>"
            "<div>"
            '<h3>Make your first key</h3>'
            '<p>One App per tool or script you’ll connect.</p>'
            "</div>"
            "</div>"
            + create_form_fields
            + "</div>"
        )
        section_action = ""
    else:
        # The create form stays out of sight until "+ New" calls for it — the
        # calm default surface is just the app list. "+ New" in the section
        # header reveals this disclosure (design: the create affordance lives in
        # the header, not as a standing row above the apps).
        connect_block = (
            '<details class="app-create-disclosure" hidden>'
            "<summary>Create another App</summary>"
            + create_form_fields
            + "</details>"
        )
        section_action = (
            '<button class="vf-button vf-button-ghost vf-button-sm" type="button" '
            'data-focus-create-app aria-label="Create another App">+ New</button>'
        )

    # Entry point: the leading dev tools (Stripe / OpenAI / Resend) don't greet
    # on a keys surface — they open with a calm, confident title and the create
    # action, and reserve greetings for a pure overview. So Home opens with a
    # TopBar (kicker + "Your gateway"), not a chummy "Hi, {name}". No "Signed in
    # as …" line: the title plus the Sign-out control in the header already
    # carry identity; the Admin tab carries the admin path. (Prior art:
    # docs/solutions research on dev-tool key dashboards.)
    top_bar = (
        '<div class="portal-topbar">'
        '<div class="portal-topbar-id">'
        '<h1 class="portal-greeting">Your gateway</h1>'
        "</div>"
        "</div>"
    )
    # Design 03 order: a reachability card leads, then the "YOUR APPS" section
    # (kicker = key count + title + "+ New"), then the apps, then the activity
    # feed. The apps section header uses the brand .vf-section pattern (mono
    # kicker over an uppercase title) for one calm, consistent grouping voice.
    key_count = sum(len(_active_credentials(a)) for a in apps)
    apps_kicker = (
        f"{key_count} key" + ("" if key_count == 1 else "s")
    ) if key_count else "no keys yet"
    return (
        '<section aria-labelledby="apps-heading">'
        + top_bar
        + _health_card()
        + '<div class="vf-section">'
        '<div class="vf-section-head">'
        "<div>"
        f'<p class="vf-section-label">{esc(apps_kicker)}</p>'
        '<h2 id="apps-heading" class="vf-section-title">Your apps</h2>'
        "</div>"
        + section_action
        + "</div>"
        + '<div id="account-result" class="message"></div>'
        + '<div id="account-toast" class="toast" role="status" aria-live="polite"></div>'
        + _key_reveal_sheet()
        + _key_detail_sheet()
        + _receipt_detail_sheet()
        + connect_block
        + apps_html
        + "</div>"  # /.vf-section
        + _recent_activity_section()
        + _base_url_card()
        + "</section>"
    )


def _recent_activity_section() -> str:
    """The "Recent" feed — the user's own last few API calls.

    Populated lazily from the owner-scoped ``/me/api/activity`` (only the
    caller's apps' receipts, never anyone else's). Each row taps through to the
    addressable receipt detail. Honest empty state — no fabricated rows.
    """
    return (
        '<section class="vf-section recent-section" aria-labelledby="recent-heading">'
        '<div class="vf-section-head">'
        "<div>"
        '<p class="vf-section-label">last calls</p>'
        '<h2 id="recent-heading" class="vf-section-title">Recent activity</h2>'
        "</div>"
        "</div>"
        '<div class="vf-card vf-feed recent-card" id="recent-activity">'
        '<p class="empty" id="recent-empty">Loading recent activity…</p>'
        "</div>"
        "</section>"
    )


def _key_detail_sheet() -> str:
    """A sheet that answers "what can this key do?" in plain language.

    Opened by tapping a key row. Shows a derived plain-English capability
    summary grouped by modality (sentence + count pills), the full item list
    behind a "Show all" disclosure, and Revoke — the design's key-detail
    screen, as a sheet (mobile bottom / desktop dialog) rather than a separate
    route. Per-key activity is not tracked yet, so it states that honestly
    instead of inventing rows.
    """
    return (
        '<div id="key-detail-sheet" class="vf-sheet-backdrop" hidden>'
        '<div class="vf-sheet" role="dialog" aria-modal="true" tabindex="-1" '
        'aria-labelledby="key-detail-title">'
        '<span class="vf-sheet-grip" aria-hidden="true"></span>'
        '<div class="key-reveal-head">'
        '<div>'
        '<p class="vf-section-label" id="key-detail-mask"></p>'
        '<h2 id="key-detail-title">What this key can do</h2>'
        "</div>"
        '<button type="button" class="vf-button vf-button-ghost vf-button-sm" '
        'data-close-key-detail>Done</button>'
        "</div>"
        '<div id="key-detail-body"></div>'
        "</div>"
        "</div>"
    )


def _destructive_confirmation_sheet() -> str:
    """Shared confirmation sheet for irreversible account actions.

    The portal already uses the ``vf-sheet`` grammar for focused details and
    one-time key reveal. Destructive actions use the same contained sheet so
    they can state the impact, wait for the server result, and return focus to
    the invoking control when cancelled or when the request fails.
    """
    return (
        '<div id="destructive-confirm-sheet" class="vf-sheet-backdrop" hidden>'
        '<div class="vf-sheet" role="dialog" aria-modal="true" tabindex="-1" '
        'aria-labelledby="destructive-confirm-title" '
        'aria-describedby="destructive-confirm-description">'
        '<span class="vf-sheet-grip" aria-hidden="true"></span>'
        '<div class="key-reveal-head">'
        '<div>'
        '<p class="vf-section-label">confirm action</p>'
        '<h2 id="destructive-confirm-title"></h2>'
        "</div>"
        "</div>"
        '<p id="destructive-confirm-description" class="confirm-description"></p>'
        '<p id="destructive-confirm-status" class="confirm-status" '
        'role="status" aria-live="polite"></p>'
        '<div class="confirm-actions">'
        '<button type="button" class="vf-button vf-button-secondary" '
        'data-cancel-destructive-confirm>Cancel</button>'
        '<button type="button" class="vf-button vf-button-danger" '
        'data-confirm-destructive-action></button>'
        "</div>"
        "</div>"
        "</div>"
    )


def _receipt_detail_sheet() -> str:
    """The "why did this fail?" receipt detail, as an addressable sheet.

    Bound to ``/r/{request_id}``: tapping a Recent row opens it and pushes the
    URL; a shared/bookmarked ``/r/{id}`` opens it on cold start. Filled from the
    owner-scoped ``/me/api/receipts/{id}`` (the caller can only ever load their
    own; others 404). Sheet on mobile, centered dialog on desktop.
    """
    return (
        '<div id="receipt-sheet" class="vf-sheet-backdrop" hidden>'
        '<div class="vf-sheet" role="dialog" aria-modal="true" '
        'aria-labelledby="receipt-title">'
        '<span class="vf-sheet-grip" aria-hidden="true"></span>'
        '<div class="key-reveal-head">'
        '<div>'
        '<p class="vf-section-label" id="receipt-kicker">request</p>'
        '<h2 id="receipt-title">Request detail</h2>'
        "</div>"
        '<button type="button" class="vf-button vf-button-ghost vf-button-sm" '
        'data-close-receipt>Done</button>'
        "</div>"
        '<div id="receipt-body"></div>'
        "</div>"
        "</div>"
    )


def _key_reveal_sheet() -> str:
    """A bottom-sheet (mobile) / dialog (desktop) that reveals a minted key once.

    Both the auto-minted first key and any manually-minted key land here, so the
    reveal is a calm focused moment (design's "Save this key" step) rather than
    an inline block that shoves the page around. The body is filled by
    ``showMintedKey`` and cleared on close.
    """
    return (
        '<div id="key-reveal-sheet" class="vf-sheet-backdrop" hidden>'
        '<div class="vf-sheet" role="dialog" aria-modal="true" '
        'aria-labelledby="key-reveal-title">'
        '<span class="vf-sheet-grip" aria-hidden="true"></span>'
        '<div class="key-reveal-head">'
        '<div>'
        '<p class="vf-section-label">new key</p>'
        '<h2 id="key-reveal-title">Save this key</h2>'
        "</div>"
        '<button type="button" class="vf-button vf-button-ghost vf-button-sm" '
        'data-close-key-reveal>Done</button>'
        "</div>"
        '<div id="key-reveal-body"></div>'
        "</div>"
        "</div>"
    )


def _key_glyph() -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" '
        'width="18" height="18" aria-hidden="true">'
        '<circle cx="8" cy="12" r="3"/><path d="M11 12h10v3M16 12v3"/></svg>'
    )


# Distinct, calm hues for the per-App initials avatar — a deterministic accent
# so each App is instantly scannable in the list (a repeated generic glyph reads
# as a template, not real data). Tints stay muted to fit the dark surface.
_AVATAR_HUES = (
    ("#7BC4C8", "rgba(123,196,200,0.16)"),  # accent / sea-glass
    ("#D49AA6", "rgba(212,154,166,0.16)"),  # rose
    ("#9CC68B", "rgba(156,198,139,0.16)"),  # leaf
    ("#C9A66B", "rgba(201,166,107,0.16)"),  # sand
    ("#8FB4E0", "rgba(143,180,224,0.16)"),  # sky
    ("#B79AD4", "rgba(183,154,212,0.16)"),  # iris
)


def _app_initials(name: str) -> str:
    """Up to two initials from an App name (e.g. 'Claude Code' -> 'CC')."""
    words = [w for w in str(name or "").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _avatar_hue(name: str) -> tuple[str, str]:
    """A deterministic (fg, bg) hue pair for an App name."""
    key = str(name or "")
    total = sum(ord(c) for c in key) if key else 0
    return _AVATAR_HUES[total % len(_AVATAR_HUES)]


def _app_avatar(name: str) -> str:
    """A small colored initials tile standing in for a per-App icon."""
    fg, bg = _avatar_hue(name)
    initials = esc(_app_initials(name))
    return (
        f'<span class="app-avatar" aria-hidden="true" '
        f'style="color:{fg};background:{bg}">{initials}</span>'
    )


def _chevron_svg() -> str:
    return (
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>'
    )


def _health_card() -> str:
    """A calm reachability card at the top of signed-in home.

    Leads with a confident, quiet headline ("Gateway reachable") and a status
    dot — like the leading dev tools, it reassures rather than over-reports.
    The capability count ("N kinds of model") and the per-modality breakdown of
    *what works* live inside the expandable detail, not in the headline, so the
    calm surface isn't front-loaded with stats nobody asked for on every visit.
    Seeded reachable and refreshed by a live ``/health`` check so it never lies
    on first paint.
    """
    return (
        '<details class="vf-card health-card" id="gateway-health">'
        '<summary class="health-summary">'
        '<span class="vf-status-dot vf-status-dot-pulse" id="health-dot" '
        'aria-hidden="true"></span>'
        '<div class="health-text">'
        '<div class="health-line" id="health-line">Gateway reachable</div>'
        '<div class="health-detail" id="health-detail">Tap for details</div>'
        "</div>"
        '<span class="health-chevron" aria-hidden="true">'
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>'
        "</span>"
        "</summary>"
        '<div class="health-caps" id="health-caps">'
        '<p class="empty" id="health-caps-empty">Loading what works…</p>'
        "</div>"
        "</details>"
    )


def _base_url_card() -> str:
    return (
        '<section aria-labelledby="base-url-heading">'
        '<div class="base-card">'
        '<div>'
        '<h2 id="base-url-heading">Base URL</h2>'
        '<code id="base-url-display"></code>'
        '<p>Use this with OpenAI-compatible SDKs.</p>'
        '<div id="openai-snippet" class="snippet"></div>'
        "</div>"
        '<button class="icon-button" type="button" id="copy-base-url" aria-label="Copy base URL">Copy</button>'
        "</div>"
        "</section>"
    )


def render_user_portal(
    *,
    title: str = "AI Gateway",
    public_base_url: str = "https://ai.vivid.fish",
    account: Mapping[str, Any] | None = None,
    oidc_enabled: bool = False,
    owner_exists: bool = True,
    login_url: str = "/login?return_to=/",
    logout_url: str = "/me/logout",
    csrf_token: str = "",
    brand: str = "AI Gateway",
    wordmark: tuple[str, str] | None = None,
) -> str:
    """Return the normal-user HTML portal.

    ``owner_exists`` only affects the signed-out hero copy/action: a fresh
    gateway with no owner is pointed at setup to be claimed, while an
    already-claimed one is pointed at sign-in. It defaults to ``True`` so
    existing callers keep the "sign in" framing.
    """
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
        brand=brand,
        wordmark=wordmark,
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
            oidc_enabled=oidc_enabled,
            login_url=login_url,
            owner_exists=owner_exists,
            brand=brand,
            wordmark=wordmark,
        )

    # No paste-a-key "Check an API key" section: that was operator/debug tooling
    # on the end-user surface (exposed opk_/oak_ internals, browser-tab-only
    # semantics nobody asked about) and wasn't even rendered. Key verification
    # happens honestly in the reveal sheet's live probe; per-key inspection is
    # the key-detail sheet.
    body = primary_section

    # Bottom-tab nav only for signed-in users; the signed-out door stays a thin,
    # nav-free landing. On desktop the bar is hidden (header nav serves).
    tabbar_html = (
        render_bottom_tabs(active="home", admin=show_admin) if is_signed_in else ""
    )
    # This sheet is a direct body child so opening it can make every portal
    # surface inert without also inverting the confirmation itself.
    destructive_confirmation_sheet = (
        _destructive_confirmation_sheet() if is_signed_in else ""
    )
    body_class = "has-tabbar" if is_signed_in else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_text}</title>
  <style>{SHELL_CSS}{_PAGE_CSS}</style>
</head>
<body class="{body_class}">
  {header_html}
  <main class="shell-main">
    {body}
  </main>
  {tabbar_html}
  {destructive_confirmation_sheet}
  <script>
(() => {{
  const publicBase = {base_json};
  const csrfToken = {csrf_json};
  let accountSnapshot = {account_json};
  const $ = (id) => document.getElementById(id);
  const baseUrl = publicBase.replace(/\\/$/, "");
  const baseUrlDisplay = $("base-url-display");
  if (baseUrlDisplay) baseUrlDisplay.textContent = baseUrl + "/v1";

  const openaiSnippet = $("openai-snippet");
  if (openaiSnippet) {{
    // Just the base URL here; the real key + runnable call is shown once at
    // key creation. No dead "opk_..." placeholder.
    openaiSnippet.textContent = 'export OPENAI_BASE_URL="' + baseUrl + '/v1"';
  }}

  const copyBaseUrl = $("copy-base-url");
  if (copyBaseUrl) {{
    copyBaseUrl.addEventListener("click", () => copyText(baseUrl + "/v1", copyBaseUrl));
  }}

  // Live reachability for the home health card. /health is public, so this
  // works regardless of the runtime auth mode and never blocks the page. The
  // headline stays calm ("Gateway reachable"); only an actual failure rewrites
  // it. The detail line carries the freshness ("checked just now") and, once
  // capabilities load, the model-kinds count.
  const healthDot = $("health-dot");
  const healthDetail = $("health-detail");
  const healthLine = $("health-line");
  let capsDetailSuffix = "";
  function setHealthDetail(prefix) {{
    if (healthDetail) healthDetail.textContent = prefix + capsDetailSuffix;
  }}
  async function refreshHealth() {{
    if (!healthDot) return;
    try {{
      const res = await fetch("/health", {{ cache: "no-store" }});
      const ok = res.ok;
      healthDot.className =
        "vf-status-dot" + (ok ? " vf-status-dot-pulse" : " vf-status-dot-bad");
      if (healthLine) healthLine.textContent = ok ? "Gateway reachable" : "Gateway unreachable";
      setHealthDetail(ok ? "Checked just now" : "Gateway returned " + res.status);
    }} catch (_) {{
      healthDot.className = "vf-status-dot vf-status-dot-bad";
      if (healthLine) healthLine.textContent = "Gateway unreachable";
      setHealthDetail("No response");
    }}
  }}
  if (healthDot) {{
    refreshHealth();
    window.setInterval(refreshHealth, 30000);
  }}

  // "What works": fold the gateway's declared capabilities into the health
  // card. The count loads up front (cheap, one request) so the card's line can
  // say "N kinds of model"; the per-modality breakdown renders lazily the first
  // time the card is expanded. Honest: capability DECLARATIONS are listed —
  // they say what the gateway exposes, not that every backend is up right now
  // (the reachability dot covers "is it up?").
  const healthCard = $("gateway-health");
  const capsRoot = $("health-caps");
  let capsLoaded = false;
  async function loadCapabilities() {{
    if (capsLoaded) return;
    capsLoaded = true;
    try {{
      const res = await fetch("/gateway/v1/capabilities", {{ cache: "no-store" }});
      const data = res.ok ? await res.json() : {{}};
      const caps = Array.isArray(data.capabilities) ? data.capabilities : [];
      const groups = groupAccessByModality(caps, []);
      // The headline stays "Gateway reachable"; the model-kinds count rides
      // quietly on the detail line (" · N kinds of model"), and the full
      // breakdown lives in the expanded body below.
      if (groups.length) {{
        const kinds = groups.length;
        capsDetailSuffix = " · " + kinds + (kinds === 1 ? " kind" : " kinds") + " of model";
        if (healthDetail && /Checked just now/.test(healthDetail.textContent || "")) {{
          setHealthDetail("Checked just now");
        }}
      }}
      if (!capsRoot) return;
      capsRoot.textContent = "";
      if (!groups.length) {{
        const empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent =
          "No capabilities are declared yet. Connect a model backend in Admin.";
        capsRoot.append(empty);
        return;
      }}
      groups.forEach((g) => {{
        const row = document.createElement("div");
        row.className = "cap-row";
        const name = document.createElement("span");
        name.className = "cap-row-name";
        // Capitalize the modality label; the sentence fragment reads plainly.
        name.textContent = g.label + " — " + g.sentence;
        const count = document.createElement("span");
        count.className = "cap-row-count";
        count.textContent = "×" + g.count;
        row.append(name, count);
        capsRoot.append(row);
      }});
    }} catch (_) {{
      if (capsRoot) {{
        capsRoot.textContent = "";
        const err = document.createElement("p");
        err.className = "empty";
        err.textContent = "Couldn't load capabilities right now.";
        capsRoot.append(err);
      }}
    }}
  }}
  if (healthCard) {{
    // Load the count up front; render rows on first expand.
    loadCapabilities();
    healthCard.addEventListener("toggle", () => {{
      if (healthCard.open) loadCapabilities();
    }});
  }}

  let toastTimer = 0;
  function clearAccountResult() {{
    const accountResult = $("account-result");
    if (!accountResult) return;
    accountResult.className = "message";
    accountResult.textContent = "";
  }}

  function setAccountMessage(text, kind = "") {{
    const toast = $("account-toast");
    if (!toast) return;
    clearAccountResult();
    window.clearTimeout(toastTimer);
    toast.className = "toast" + (kind ? " " + kind : "") + " show";
    toast.textContent = text;
    toastTimer = window.setTimeout(() => {{
      toast.className = "toast" + (kind ? " " + kind : "");
    }}, kind === "bad" ? 6500 : 3000);
  }}

  async function copyText(text, button) {{
    const original = button ? (button.dataset.copyLabel || button.textContent || "Copy") : "";
    if (button) button.dataset.copyLabel = original;
    try {{
      await navigator.clipboard.writeText(text);
      if (button) button.textContent = "Copied";
    }} catch (_) {{
      if (button) button.textContent = "Failed";
    }} finally {{
      if (button) {{
        window.setTimeout(() => {{
          if (button.textContent === "Copied" || button.textContent === "Failed") {{
            button.textContent = button.dataset.copyLabel || original || "Copy";
          }}
        }}, 1300);
      }}
    }}
  }}

  function labelledCopyBlock(label, value, monospace) {{
    // A titled code block with its own Copy button. Used for the curl and the
    // env snippet so each is independently copy-pasteable.
    const block = document.createElement("div");
    block.className = "reveal-block";
    const head = document.createElement("div");
    head.className = "reveal-block-head";
    const title = document.createElement("span");
    title.className = "reveal-block-label";
    title.textContent = label;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "reveal-copy";
    copy.textContent = "Copy";
    copy.addEventListener("click", () => copyText(value, copy));
    head.append(title, copy);
    const pre = document.createElement("pre");
    pre.className = monospace ? "reveal-code" : "reveal-code";
    pre.textContent = value;
    block.append(head, pre);
    return block;
  }}

  function openKeyRevealSheet() {{
    const sheet = $("key-reveal-sheet");
    if (sheet) sheet.hidden = false;
  }}
  function closeKeyRevealSheet() {{
    const sheet = $("key-reveal-sheet");
    const body = $("key-reveal-body");
    if (sheet) sheet.hidden = true;
    if (body) body.textContent = "";
  }}
  document.addEventListener("click", (event) => {{
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    // Close on the Done button or on a backdrop (outside the sheet) click.
    if (target.closest("[data-close-key-reveal]")) {{ closeKeyRevealSheet(); return; }}
    const sheet = $("key-reveal-sheet");
    if (sheet && !sheet.hidden && target === sheet) closeKeyRevealSheet();
  }});

  function showMintedKey(key) {{
    const body = $("key-reveal-body");
    if (!body) return;
    body.textContent = "";

    const wrap = document.createElement("div");
    wrap.className = "secret";

    const text = document.createElement("p");
    text.className = "reveal-warning";
    text.textContent = "Here is your API key. Copy it now \\u2014 it is shown once and cannot be retrieved again. If you lose it, rotate to a new one.";

    const code = document.createElement("code");
    code.className = "reveal-key";
    code.textContent = key;

    const copyKey = document.createElement("button");
    copyKey.type = "button";
    copyKey.className = "vf-button";
    copyKey.textContent = "Copy key";
    copyKey.addEventListener("click", () => copyText(key, copyKey));

    // A runnable first call, key already filled in. Proves the key works in
    // one paste: copy, run, expect a 200. Bearer header, over the real base URL.
    const curl = "curl " + baseUrl + "/v1/models \\\\\\n  -H \\"Authorization: Bearer " + key + "\\"";
    const envSnippet =
      'export OPENAI_BASE_URL="' + baseUrl + '/v1"\\n' +
      'export OPENAI_API_KEY="' + key + '"';

    const tryHeading = document.createElement("p");
    tryHeading.className = "reveal-subhead";
    tryHeading.textContent = "Prove it works:";

    // Honest live status: run the same call ourselves so a non-200 is
    // explained, not a mystery the user discovers by pasting the curl.
    const status = document.createElement("div");
    status.className = "reveal-status";
    status.innerHTML = '<span class="vf-status-dot vf-status-dot-neutral" aria-hidden="true"></span><span class="reveal-status-text">Checking your key against the gateway\\u2026</span>';

    wrap.append(
      text,
      code,
      copyKey,
      tryHeading,
      labelledCopyBlock("Run this", curl, true),
      labelledCopyBlock("Or set env for OpenAI-compatible SDKs", envSnippet, true),
      status
    );
    body.append(wrap);
    openKeyRevealSheet();
    probeMintedKey(key, status);
  }}

  async function probeMintedKey(key, statusEl) {{
    const dot = statusEl.querySelector(".vf-status-dot");
    const label = statusEl.querySelector(".reveal-status-text");
    const set = (tone, msg) => {{
      dot.className = "vf-status-dot" + (tone === "ok" ? "" : " vf-status-dot-" + tone);
      label.textContent = msg;
    }};
    try {{
      const res = await fetch(baseUrl + "/v1/models", {{
        headers: {{ "Authorization": "Bearer " + key }},
        cache: "no-store"
      }});
      if (res.ok) {{
        set("ok", "Your key works \\u2014 the gateway returned your models (HTTP 200).");
      }} else if (res.status === 401 || res.status === 403) {{
        set("bad", "The gateway rejected this key (HTTP " + res.status + "). That is unexpected \\u2014 try rotating it.");
      }} else if (res.status === 503 || res.status === 502) {{
        set("warn", "Your key is valid, but no model backend is wired yet (HTTP " + res.status + "). Add one in Admin, then this call returns your models.");
      }} else {{
        set("warn", "Unexpected response (HTTP " + res.status + "). The key was created; check the gateway logs.");
      }}
    }} catch (_) {{
      set("warn", "Could not reach the gateway from this page to verify. The key was created.");
    }}
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

  let destructiveConfirmation = null;

  function focusAccountStatus() {{
    const status = $("account-toast");
    if (status) {{
      status.tabIndex = -1;
      status.focus({{ preventScroll: true }});
    }}
  }}

  function makeDestructiveBackgroundInert(sheet) {{
    return Array.from(document.body.children)
      .filter((element) => element !== sheet && element.tagName !== "SCRIPT")
      .map((element) => {{
        const previous = {{
          element,
          inert: Boolean(element.inert),
          ariaHidden: element.getAttribute("aria-hidden")
        }};
        element.inert = true;
        element.setAttribute("aria-hidden", "true");
        return previous;
      }});
  }}

  function restoreDestructiveBackground(background) {{
    (background || []).forEach((previous) => {{
      previous.element.inert = previous.inert;
      if (previous.ariaHidden === null) previous.element.removeAttribute("aria-hidden");
      else previous.element.setAttribute("aria-hidden", previous.ariaHidden);
    }});
  }}

  function destructiveFocusableElements(sheet) {{
    return Array.from(sheet.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
  }}

  function closeDestructiveConfirmation({{ restoreFocus = true }} = {{}}) {{
    const sheet = $("destructive-confirm-sheet");
    const state = destructiveConfirmation;
    if (sheet) sheet.hidden = true;
    restoreDestructiveBackground(state && state.background);
    destructiveConfirmation = null;
    if (restoreFocus && state && state.trigger && state.trigger.isConnected) state.trigger.focus();
  }}

  function openDestructiveConfirmation({{
    title, description, confirmLabel, trigger, mutation, onCommitted, refresh,
    successMessage, refreshFailureMessage
  }}) {{
    const sheet = $("destructive-confirm-sheet");
    const heading = $("destructive-confirm-title");
    const detail = $("destructive-confirm-description");
    const status = $("destructive-confirm-status");
    const cancel = sheet && sheet.querySelector("[data-cancel-destructive-confirm]");
    const confirm = sheet && sheet.querySelector("[data-confirm-destructive-action]");
    if (!sheet || !heading || !detail || !status ||
        !(cancel instanceof HTMLButtonElement) || !(confirm instanceof HTMLButtonElement)) return;
    destructiveConfirmation = {{
      trigger, mutation, onCommitted, refresh, successMessage, refreshFailureMessage,
      pending: false, committed: false, background: makeDestructiveBackgroundInert(sheet)
    }};
    heading.textContent = title;
    detail.textContent = description;
    status.className = "confirm-status";
    status.textContent = "";
    confirm.textContent = confirmLabel;
    cancel.disabled = false;
    confirm.disabled = false;
    sheet.hidden = false;
    // A destructive confirmation starts at the least-destructive action.
    cancel.focus();
  }}

  async function submitDestructiveConfirmation() {{
    const state = destructiveConfirmation;
    const sheet = $("destructive-confirm-sheet");
    const status = $("destructive-confirm-status");
    const confirm = sheet && sheet.querySelector("[data-confirm-destructive-action]");
    const cancel = sheet && sheet.querySelector("[data-cancel-destructive-confirm]");
    if (!state || state.pending || !(confirm instanceof HTMLButtonElement)) return;
    state.pending = true;
    confirm.disabled = true;
    if (cancel instanceof HTMLButtonElement) cancel.disabled = true;
    if (status) {{
      status.className = "confirm-status";
      status.textContent = "Working…";
    }}

    try {{
      await state.mutation();
    }} catch (error) {{
      // The DELETE did not commit. Close and return to its still-live invoker,
      // where a retry is truthful and deliberate.
      closeDestructiveConfirmation();
      setAccountMessage((error && error.message) || "Could not complete that action. Try again.", "bad");
      return;
    }}

    // From this point on the DELETE is committed. The confirmation cannot be
    // retried, even if refreshing the surrounding account snapshot fails.
    state.committed = true;
    if (typeof state.onCommitted === "function") state.onCommitted();
    closeDestructiveConfirmation({{ restoreFocus: false }});
    try {{
      if (typeof state.refresh === "function") await state.refresh();
      setAccountMessage(state.successMessage || "Action completed.", "good");
    }} catch (error) {{
      setAccountMessage(
        state.refreshFailureMessage || "Action completed, but the account view could not refresh. Reload this page.",
        "warn"
      );
    }}
    // The invoker may be gone after a committed delete. The durable status is
    // the logical surviving target for both refreshed and stale views.
    focusAccountStatus();
  }}

  document.addEventListener("click", (event) => {{
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const sheet = $("destructive-confirm-sheet");
    if (!sheet || sheet.hidden || (destructiveConfirmation && destructiveConfirmation.pending)) return;
    if (target.closest("[data-cancel-destructive-confirm]") || target === sheet) {{
      closeDestructiveConfirmation();
      return;
    }}
    if (target.closest("[data-confirm-destructive-action]")) submitDestructiveConfirmation();
  }});
  document.addEventListener("keydown", (event) => {{
    const sheet = $("destructive-confirm-sheet");
    if (event.key === "Tab" && sheet && !sheet.hidden) {{
      const focusable = destructiveFocusableElements(sheet);
      if (!focusable.length) {{
        event.preventDefault();
        sheet.focus();
        return;
      }}
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !sheet.contains(document.activeElement))) {{
        event.preventDefault();
        last.focus();
      }} else if (!event.shiftKey && (document.activeElement === last || !sheet.contains(document.activeElement))) {{
        event.preventDefault();
        first.focus();
      }}
      return;
    }}
    if (event.key === "Escape" && sheet && !sheet.hidden &&
        !(destructiveConfirmation && destructiveConfirmation.pending)) {{
      event.preventDefault();
      closeDestructiveConfirmation();
    }}
  }});

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
    article.append(h, p);
    if (badges.length) article.append(meta);
    return article;
  }}

  function text(value, fallback = "") {{
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }}

  // Mirrors of the server's _mask_key / _relative_time / usage helpers so a
  // client re-render (after create/mint/delete) matches the initial HTML.
  const MASK_DOTS = "\\u2022\\u2022\\u2022\\u2022";
  function maskKey(prefix) {{
    const raw = String(prefix || "").trim();
    if (!raw) return "key";
    const i = raw.indexOf("_");
    if (i > 0 && i < raw.length - 1) {{
      const scheme = raw.slice(0, i), tail = raw.slice(i + 1);
      const last4 = tail.length >= 4 ? tail.slice(-4) : tail;
      return scheme + "_ " + MASK_DOTS + " " + last4;
    }}
    const last4 = raw.length >= 4 ? raw.slice(-4) : raw;
    return MASK_DOTS + " " + last4;
  }}
  function relativeTime(ts) {{
    const n = parseInt(ts, 10);
    if (!n) return "";
    let s = Math.floor(Date.now() / 1000) - n;
    if (s < 0) s = 0;
    if (s < 45) return "just now";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    const d = Math.floor(h / 24);
    if (d < 7) return d + "d ago";
    const w = Math.floor(d / 7);
    if (w < 5) return w + "w ago";
    const mo = Math.floor(d / 30);
    if (mo < 12) return mo + "mo ago";
    return Math.floor(d / 365) + "y ago";
  }}
  function keyUsageLabel(credential) {{
    if (credential && credential.last_used_at) {{
      const rel = relativeTime(credential.last_used_at);
      return rel ? "used " + rel : "used recently";
    }}
    return "Never used";
  }}
  function appUsageLabel(credentials) {{
    const used = credentials
      .map((c) => parseInt(c.last_used_at, 10))
      .filter((n) => n);
    if (used.length) return "used " + (relativeTime(Math.max(...used)) || "recently");
    return credentials.length ? "Never used" : "";
  }}

  // Per-App initials avatar — mirrors the server's _app_initials / _avatar_hue.
  const AVATAR_HUES = [
    ["#7BC4C8", "rgba(123,196,200,0.16)"],
    ["#D49AA6", "rgba(212,154,166,0.16)"],
    ["#9CC68B", "rgba(156,198,139,0.16)"],
    ["#C9A66B", "rgba(201,166,107,0.16)"],
    ["#8FB4E0", "rgba(143,180,224,0.16)"],
    ["#B79AD4", "rgba(183,154,212,0.16)"]
  ];
  function appInitials(name) {{
    const words = String(name || "").split(/\\s+/).filter(Boolean);
    if (!words.length) return "?";
    if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return (words[0][0] + words[1][0]).toUpperCase();
  }}
  function avatarHue(name) {{
    const key = String(name || "");
    let total = 0;
    for (let i = 0; i < key.length; i++) total += key.charCodeAt(i);
    return AVATAR_HUES[total % AVATAR_HUES.length];
  }}

  function activeCredentials(app) {{
    return (Array.isArray(app.credentials) ? app.credentials : [])
      .filter((credential) => credential && credential.active !== false);
  }}

  // Inline SVGs, kept byte-identical to the server's chevron/glyph so a client
  // re-render after create/delete matches the initial HTML exactly.
  const CHEVRON_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>';
  // The app-card summary's down-chevron — byte-identical to the server's
  // _chevron_svg() so a client re-render matches the initial HTML.
  const APP_CHEVRON_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';

  function renderCredential(credential) {{
    // A calm, tappable row: label + masked value on the left, honest recency on
    // the right, then a chevron. Tapping opens the detail sheet (View-access +
    // Revoke live there). Mirrors the server's _credential_html.
    const label = text(credential.display_name, "API key");
    const row = document.createElement("button");
    row.type = "button";
    row.className = "key-row";
    row.dataset.viewOwnedKey = text(credential.id);
    row.dataset.keyLabel = label;
    row.setAttribute("aria-label", "Open " + label);

    const main = document.createElement("span");
    main.className = "key-row-main";
    const name = document.createElement("span");
    name.className = "key-row-name";
    name.textContent = label;
    const pfx = document.createElement("span");
    pfx.className = "key-row-prefix";
    pfx.textContent = maskKey(credential.prefix);
    main.append(name, pfx);

    const usage = document.createElement("span");
    usage.className = "key-row-usage";
    usage.textContent = keyUsageLabel(credential);

    const chevron = document.createElement("span");
    chevron.className = "key-row-chevron";
    chevron.setAttribute("aria-hidden", "true");
    chevron.innerHTML = CHEVRON_SVG;

    row.append(main, usage, chevron);
    return row;
  }}

  function renderApp(app) {{
    const appId = text(app.id);
    const credentials = activeCredentials(app);
    const appName = text(app.name, appId || "App");
    // A calm .vf-card whose summary is the design's app row; tapping expands the
    // keys + management controls in place. Mirrors the server's _app_html.
    const article = document.createElement("details");
    article.className = "vf-card app-card";
    article.dataset.appCard = appId;

    const summary = document.createElement("summary");
    summary.className = "app-card-summary";
    const row = document.createElement("span");
    row.className = "vf-row";
    const main = document.createElement("span");
    main.className = "vf-row-main";
    const avatar = document.createElement("span");
    avatar.className = "app-avatar";
    avatar.setAttribute("aria-hidden", "true");
    const hue = avatarHue(appName);
    avatar.style.color = hue[0];
    avatar.style.background = hue[1];
    avatar.textContent = appInitials(appName);
    const idBlock = document.createElement("span");
    idBlock.className = "vf-row-id";
    const name = document.createElement("span");
    name.className = "vf-row-name";
    name.textContent = appName;
    const sub = document.createElement("span");
    sub.className = "vf-row-sub";
    sub.textContent = credentials.length === 0
      ? "No keys yet"
      : credentials.length + (credentials.length === 1 ? " key" : " keys");
    idBlock.append(name, sub);
    main.append(avatar, idBlock);
    // Trailing: the App's most recent key usage + chevron (balances the row).
    const trailing = document.createElement("span");
    trailing.className = "app-card-trailing";
    const usageMeta = appUsageLabel(credentials);
    if (usageMeta) {{
      const u = document.createElement("span");
      u.className = "app-card-usage";
      u.textContent = usageMeta;
      trailing.append(u);
    }}
    const chevron = document.createElement("span");
    chevron.className = "app-card-chevron";
    chevron.setAttribute("aria-hidden", "true");
    chevron.innerHTML = APP_CHEVRON_SVG;
    trailing.append(chevron);
    row.append(main, trailing);
    summary.append(row);

    const body = document.createElement("div");
    body.className = "app-card-body";
    const title = {{ textContent: appName }};  // kept for delete-button labels below

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

    // "+ New key" — a quiet disclosure, not a form left open on the surface.
    let create = null;
    if (app.can_mint_keys !== false) {{
      create = document.createElement("details");
      create.className = "key-create";
      create.dataset.keyCreate = appId;
      const summary = document.createElement("summary");
      summary.className = "key-create-summary";
      const plus = document.createElement("span");
      plus.className = "key-create-plus";
      plus.setAttribute("aria-hidden", "true");
      plus.textContent = "+";
      summary.append(plus, " New key");
      const form = document.createElement("form");
      form.className = "key-form";
      form.dataset.mintOwnedKey = appId;
      const label = document.createElement("label");
      label.className = "field";
      const labelText = document.createElement("span");
      labelText.textContent = "What is this key for?";
      const input = document.createElement("input");
      input.type = "text";
      input.name = "label";
      input.placeholder = "MacBook, Open WebUI, deploy script";
      input.setAttribute("aria-label", "API key label");
      label.append(labelText, input);
      const createKey = document.createElement("button");
      createKey.type = "submit";
      createKey.className = "vf-button";
      createKey.textContent = "Create key";
      form.append(label, createKey);
      create.append(summary, form);
    }}

    // "Remove this App" — destructive, tucked behind a quiet disclosure.
    let remove = null;
    if (app.can_delete !== false) {{
      remove = document.createElement("details");
      remove.className = "app-remove";
      const summary = document.createElement("summary");
      summary.textContent = "Remove this App";
      const warn = document.createElement("p");
      warn.className = "app-remove-warn";
      warn.textContent = "Deleting this App revokes all of its keys immediately. Apps using them lose access.";
      const deleteApp = document.createElement("button");
      deleteApp.type = "button";
      deleteApp.className = "vf-button vf-button-danger";
      deleteApp.dataset.deleteOwnedApp = appId;
      deleteApp.dataset.appName = title.textContent;
      deleteApp.textContent = "Delete " + title.textContent;
      remove.append(summary, warn, deleteApp);
    }}

    body.append(list);
    if (create) body.append(create);
    if (remove) body.append(remove);
    article.append(summary, body);
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
      empty.textContent = "Create your first App to get an opk_ API key.";
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

  function humanizeId(value, fallback = "Gateway item") {{
    const raw = text(value, fallback);
    const prefixes = [
      ["image.generate.", "Image generation: "],
      ["image.edit.", "Image editing: "],
      ["audio.generate.", "Audio generation: "],
      ["speech.synthesis.", "Text to speech: "],
      ["speech.transcription.", "Speech to text: "],
      ["video.generate.", "Video: "],
      ["chat.", "Chat: "],
      ["embedding.", "Embeddings: "]
    ];
    for (const pair of prefixes) {{
      if (raw.startsWith(pair[0])) {{
        return pair[1] + raw.slice(pair[0].length).replace(/[._:-]+/g, " ").replace(/\\s+/g, " ").trim();
      }}
    }}
    return raw.replace(/[._:-]+/g, " ").replace(/\\s+/g, " ").trim()
      .replace(/\\b\\w/g, (char) => char.toUpperCase()) || fallback;
  }}

  function intentDescription(intent) {{
    const descriptions = {{
      "chat": "Chat with local language models.",
      "chat.completions": "Chat with OpenAI-compatible local language models.",
      "text.completions": "Generate or complete text.",
      "text.embeddings": "Create embeddings for search, retrieval, and memory.",
      "embeddings": "Create embeddings for search, retrieval, and memory.",
      "image.generate": "Generate images.",
      "image.edit": "Edit, inpaint, or restyle images.",
      "speech.synthesis": "Generate spoken audio from text.",
      "speech.transcription": "Transcribe speech to text.",
      "audio.generate": "Generate music, loops, or sound effects.",
      "video.generate": "Generate video jobs.",
      "models.list": "List available models."
    }};
    return descriptions[text(intent)] || "Allowed by this key.";
  }}

  function modalityDescription(item) {{
    const input = Array.isArray(item.input_modalities) ? item.input_modalities.filter(Boolean) : [];
    const output = Array.isArray(item.output_modalities) ? item.output_modalities.filter(Boolean) : [];
    const parts = [];
    if (input.length) parts.push("Accepts " + input.join(", ") + ".");
    if (output.length) parts.push("Returns " + output.join(", ") + ".");
    return parts.join(" ");
  }}

  function describeCapability(cap) {{
    const title = humanizeId(cap.label || cap.name || cap.workflow || cap.model || cap.intent);
    const declared = text(cap.description);
    const modalities = modalityDescription(cap);
    const caveats = Array.isArray(cap.caveats) && cap.caveats.length
      ? " Note: " + cap.caveats.join("; ")
      : "";
    return {{
      title,
      body: (declared || intentDescription(cap.intent)) + (modalities ? " " + modalities : "") + caveats
    }};
  }}

  function describeModel(model) {{
    const id = model.id || model.model || "Model";
    return {{
      title: humanizeId(id, "Model"),
      body: text(model.description) || intentDescription(model.intent || "chat.completions")
    }};
  }}

  // Map a capability/model to a coarse modality bucket so the key-detail sheet
  // can show a calm, grouped summary (design 07: "what this key can do") instead
  // of a wall of every backend model. Buckets collapse the duplicate entries
  // (same model listed by capability and by /v1/models) into one count.
  const MODALITY_GROUPS = [
    {{ key:"chat",      label:"Chat",        sentence:"chat with local language models" }},
    {{ key:"text",      label:"Text",        sentence:"complete and generate text" }},
    {{ key:"embeddings",label:"Embeddings",  sentence:"create embeddings for search and memory" }},
    {{ key:"images",    label:"Images",      sentence:"generate and edit images" }},
    {{ key:"audio",     label:"Audio",       sentence:"generate speech and audio" }},
    {{ key:"video",     label:"Video",       sentence:"generate video" }},
    {{ key:"other",     label:"Other",       sentence:"use other gateway capabilities" }},
  ];
  function modalityKey(item) {{
    const intent = text(item.intent || (item.object === "model" ? "chat.completions" : "")).toLowerCase();
    const out = (Array.isArray(item.output_modalities) ? item.output_modalities : [])
      .map((m) => text(m).toLowerCase());
    if (intent.startsWith("chat")) return "chat";
    if (intent.indexOf("embed") !== -1) return "embeddings";
    if (intent.startsWith("text")) return "text";
    if (intent.startsWith("image") || out.indexOf("image") !== -1) return "images";
    if (intent.startsWith("speech") || intent.startsWith("audio") || out.indexOf("audio") !== -1) return "audio";
    if (intent.startsWith("video") || out.indexOf("video") !== -1) return "video";
    if (out.indexOf("text") !== -1) return "text";
    return "other";
  }}
  // Returns the ordered modality groups present for this key (label, sentence, count).
  function groupAccessByModality(caps, models) {{
    const counts = {{}};
    [].concat(caps || [], models || []).forEach((item) => {{
      const k = modalityKey(item || {{}});
      counts[k] = (counts[k] || 0) + 1;
    }});
    return MODALITY_GROUPS
      .filter((g) => counts[g.key])
      .map((g) => ({{ label: g.label, sentence: g.sentence, count: counts[g.key] }}));
  }}

  function closeKeyDetailSheet() {{
    const sheet = $("key-detail-sheet"), body = $("key-detail-body");
    if (sheet) sheet.hidden = true;
    if (body) body.textContent = "";
  }}

  async function openKeyDetailSheet(credentialId, label) {{
    const sheet = $("key-detail-sheet"), body = $("key-detail-body");
    const mask = $("key-detail-mask");
    if (!sheet || !body) return;
    body.textContent = "";
    if (mask) mask.textContent = text(label);
    sheet.hidden = false;
    const loading = document.createElement("p");
    loading.className = "empty";
    loading.textContent = "Loading what this key can do…";
    body.append(loading);
    try {{
      const payload = await accountJson(
        "/me/api/credentials/" + encodeURIComponent(credentialId) + "/access"
      );
      body.textContent = "";
      // Plain-language summary of what the key unlocks.
      const caps = Array.isArray(payload.capabilities) ? payload.capabilities : [];
      const models = (payload.models && Array.isArray(payload.models.data))
        ? payload.models.data : (Array.isArray(payload.data) ? payload.data : []);
      const total = caps.length + models.length;
      // Calm, grouped summary (design 07) — a plain sentence naming what the
      // key unlocks by modality, then a row of pills with counts. The full
      // per-model list is available behind a "Show all" disclosure for power
      // users, not dumped as a wall by default.
      const groups = groupAccessByModality(caps, models);
      const sentence = document.createElement("p");
      sentence.className = "key-detail-sentence";
      if (total && groups.length) {{
        const phrases = groups.map((g) => g.sentence);
        let joined;
        if (phrases.length === 1) joined = phrases[0];
        else joined = phrases.slice(0, -1).join(", ") + ", and " + phrases[phrases.length - 1];
        sentence.append("This key can ");
        const strong = document.createElement("strong");
        strong.textContent = joined;
        sentence.append(strong, " on the gateway.");
      }} else {{
        sentence.textContent = "This key works, but no access details were returned.";
      }}
      body.append(sentence);
      if (groups.length) {{
        const pills = document.createElement("div");
        pills.className = "key-detail-pills";
        groups.forEach((g) => {{
          const pill = document.createElement("span");
          pill.className = "vf-pill vf-pill-accent";
          pill.textContent = g.count > 1 ? g.label + " ×" + g.count : g.label;
          pills.append(pill);
        }});
        body.append(pills);
      }}
      // Full list, opt-in. Reuses the existing item cards; de-duplicated view of
      // capabilities + models so power users can audit exactly what's allowed.
      if (total) {{
        const all = document.createElement("details");
        all.className = "key-detail-all";
        const summary = document.createElement("summary");
        summary.textContent = "Show all " + total + (total === 1 ? " item" : " items");
        all.append(summary);
        const items = document.createElement("div");
        items.className = "access-list";
        caps.forEach((cap) => {{
          const it = describeCapability(cap);
          items.appendChild(card(it.title, it.body));
        }});
        models.forEach((m) => {{
          const it = describeModel(m);
          items.appendChild(card(it.title, it.body));
        }});
        all.append(items);
        body.append(all);
      }}
      // Honest: per-key activity is not tracked yet — say so, don't invent it.
      const activityNote = document.createElement("p");
      activityNote.className = "empty";
      activityNote.textContent = "Per-key activity isn't recorded yet.";
      body.append(activityNote);
      // Revoke this key.
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "vf-button vf-button-danger vf-button-full";
      revoke.textContent = "Revoke this key";
      revoke.addEventListener("click", async () => {{
        openDestructiveConfirmation({{
          title: "Revoke this API key?",
          description: "Apps using it lose access immediately.",
          confirmLabel: "Revoke key",
          trigger: revoke,
          mutation: () => accountJson(
            "/me/api/credentials/" + encodeURIComponent(credentialId), {{ method: "DELETE" }}
          ),
          onCommitted: () => closeKeyDetailSheet(),
          refresh: () => refreshAccount(),
          successMessage: "Key revoked.",
          refreshFailureMessage: "Key revoked, but the account view could not refresh. Reload this page."
        }});
      }});
      body.append(revoke);
    }} catch (error) {{
      body.textContent = "";
      const err = document.createElement("p");
      err.className = "empty";
      err.textContent = error.message || "Could not load this key's access.";
      body.append(err);
    }}
  }}

  // ---- Recent activity + receipt detail ------------------------------------
  // The feed is owner-scoped server-side (/me/api/activity returns only the
  // caller's apps' receipts). Each row opens the addressable receipt detail.

  function statusTone(status) {{
    const n = parseInt(status, 10);
    if (!n || n >= 500) return "bad";
    if (n >= 400) return "warn";
    return "";
  }}
  function endpointLabel(r) {{
    // Prefer the intent (chat.completions) over the raw path; fall back to path.
    return text(r.intent) || text(r.path) || "request";
  }}
  function activityAppLabel(r) {{
    // Client audience hides api_key_name; lead with the resolved model or intent.
    return text(r.resolved_model) || text(r.requested_model) || endpointLabel(r);
  }}

  function renderActivityRow(r) {{
    // Design 03 feed row (brand .vf-feed-row): status dot · app/endpoint · a
    // right-aligned status-code-over-latency column. Non-2xx codes go amber/red.
    const row = document.createElement("button");
    row.type = "button";
    row.className = "vf-feed-row activity-row";
    row.dataset.openReceipt = text(r.request_id);
    const tone = statusTone(r.status);

    const dot = document.createElement("span");
    dot.className = "vf-status-dot" + (tone === "bad" ? " vf-status-dot-bad" : tone === "warn" ? " vf-status-dot-warn" : "");
    dot.setAttribute("aria-hidden", "true");

    const main = document.createElement("span");
    main.className = "vf-row-id";
    const app = document.createElement("span");
    app.className = "vf-row-name";
    app.textContent = activityAppLabel(r);
    const ep = document.createElement("span");
    ep.className = "vf-row-sub";
    ep.textContent = endpointLabel(r);
    main.append(app, ep);

    const stat = document.createElement("span");
    stat.className = "vf-row-stat" + (tone === "bad" ? " is-bad" : tone === "warn" ? " is-warn" : "");
    const st = document.createElement("span");
    st.className = "vf-row-stat-top";
    st.textContent = text(r.status, "—");
    stat.append(st);
    if (r.latency_ms != null) {{
      const lat = document.createElement("span");
      lat.className = "vf-row-stat-sub";
      lat.textContent = Math.round(Number(r.latency_ms)) + "ms";
      stat.append(lat);
    }}

    row.append(dot, main, stat);
    return row;
  }}

  let activityLoaded = false;
  async function loadActivity() {{
    if (activityLoaded) return;
    activityLoaded = true;
    const root = $("recent-activity");
    if (!root) return;
    try {{
      const data = await accountJson("/me/api/activity?limit=5");
      const receipts = Array.isArray(data.receipts) ? data.receipts : [];
      root.textContent = "";
      if (!receipts.length) {{
        const empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent = "No calls yet — your apps' recent requests will show here.";
        root.append(empty);
        return;
      }}
      receipts.forEach((r) => root.append(renderActivityRow(r)));
    }} catch (error) {{
      root.textContent = "";
      const err = document.createElement("p");
      err.className = "empty";
      err.textContent = "Couldn't load recent activity right now.";
      root.append(err);
    }}
  }}

  function receiptFact(key, value) {{
    const row = document.createElement("div");
    row.className = "receipt-fact";
    const k = document.createElement("span");
    k.className = "receipt-fact-key";
    k.textContent = key;
    const v = document.createElement("span");
    v.className = "receipt-fact-val";
    v.textContent = value;
    row.append(k, v);
    return row;
  }}

  function closeReceiptSheet(restoreUrl = true) {{
    const sheet = $("receipt-sheet"), body = $("receipt-body");
    if (sheet) sheet.hidden = true;
    if (body) body.textContent = "";
    // Pop /r/:id back to / without adding history noise.
    if (restoreUrl && location.pathname.indexOf("/r/") === 0) {{
      history.pushState({{}}, "", "/");
    }}
  }}

  async function openReceiptSheet(requestId, pushUrl = true) {{
    const sheet = $("receipt-sheet"), body = $("receipt-body");
    if (!sheet || !body) return;
    body.textContent = "";
    sheet.hidden = false;
    if (pushUrl) history.pushState({{}}, "", "/r/" + encodeURIComponent(requestId));
    const loading = document.createElement("p");
    loading.className = "empty";
    loading.textContent = "Loading request…";
    body.append(loading);
    try {{
      const r = await accountJson("/me/api/receipts/" + encodeURIComponent(requestId));
      body.textContent = "";
      // Plain-language summary line.
      const tone = statusTone(r.status);
      const summary = document.createElement("p");
      summary.className = "receipt-summary";
      const verb = tone === "" ? "succeeded" : (tone === "warn" ? "was rejected" : "failed");
      summary.append("This ");
      const strong = document.createElement("strong");
      strong.textContent = endpointLabel(r);
      summary.append(strong, " call " + verb + ".");
      body.append(summary);

      // "Why" block for non-success: rejected inputs, fallbacks, warnings.
      const rejected = r.rejected_inputs && typeof r.rejected_inputs === "object"
        ? Object.entries(r.rejected_inputs) : [];
      const fallbacks = Array.isArray(r.fallback_reasons) ? r.fallback_reasons : [];
      const warnings = Array.isArray(r.warnings) ? r.warnings : [];
      if (tone !== "" || rejected.length || fallbacks.length || warnings.length) {{
        const why = document.createElement("div");
        why.className = "receipt-why";
        const h = document.createElement("h3");
        h.textContent = "Why";
        why.append(h);
        const ul = document.createElement("ul");
        rejected.forEach(([name, reason]) => {{
          const li = document.createElement("li");
          li.textContent = name + ": " + reason;
          ul.append(li);
        }});
        fallbacks.forEach((f) => {{ const li = document.createElement("li"); li.textContent = f; ul.append(li); }});
        warnings.forEach((w) => {{ const li = document.createElement("li"); li.textContent = w; ul.append(li); }});
        if (!ul.childElementCount) {{
          const li = document.createElement("li");
          li.textContent = "The gateway returned status " + text(r.status, "?") + " with no further detail.";
          ul.append(li);
        }}
        why.append(ul);
        body.append(why);
      }}

      // Labelled facts.
      const facts = document.createElement("div");
      facts.className = "receipt-facts";
      if (r.status != null) facts.append(receiptFact("Status", text(r.status)));
      if (r.latency_ms != null) facts.append(receiptFact("Latency", Math.round(Number(r.latency_ms)) + " ms"));
      if (r.requested_model) facts.append(receiptFact("Requested model", text(r.requested_model)));
      if (r.resolved_model && r.resolved_model !== r.requested_model)
        facts.append(receiptFact("Resolved model", text(r.resolved_model)));
      if (r.intent) facts.append(receiptFact("Intent", text(r.intent)));
      if (r.path) facts.append(receiptFact("Path", text(r.path)));
      if (r.request_id) facts.append(receiptFact("Request ID", text(r.request_id)));
      if (facts.childElementCount) body.append(facts);
    }} catch (error) {{
      body.textContent = "";
      const err = document.createElement("p");
      err.className = "empty";
      err.textContent = (error && /404/.test(error.message))
        ? "That request wasn't found for your account."
        : (error.message || "Couldn't load this request.");
      body.append(err);
    }}
  }}

  // Wire activity-row taps, receipt-sheet close, and backdrop close.
  document.addEventListener("click", (event) => {{
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const row = target.closest("[data-open-receipt]");
    if (row) {{ openReceiptSheet(row.dataset.openReceipt); return; }}
    if (target.closest("[data-close-receipt]")) {{ closeReceiptSheet(); return; }}
    const rs = $("receipt-sheet");
    if (rs && !rs.hidden && target === rs) {{ closeReceiptSheet(); }}
  }});
  // Back button closes the receipt sheet (it's a routed view).
  window.addEventListener("popstate", () => {{
    const rs = $("receipt-sheet");
    if (location.pathname.indexOf("/r/") === 0) {{
      const id = decodeURIComponent(location.pathname.slice(3));
      if (id) openReceiptSheet(id, false);
    }} else if (rs && !rs.hidden) {{
      closeReceiptSheet(false);
    }}
  }});

  if ($("recent-activity")) loadActivity();
  // Cold-start deep link: /r/:id opens the receipt sheet on load.
  if (location.pathname.indexOf("/r/") === 0) {{
    const id = decodeURIComponent(location.pathname.slice(3));
    if (id) openReceiptSheet(id, false);
  }}

  renderApps((accountSnapshot && accountSnapshot.apps) || []);

  // Stripe-shaped first-run: the personal App exists from sign-in, but the raw
  // key only exists at the moment of creation. So on the first landing where
  // the personal App has no key yet, mint one and reveal it once with a
  // runnable curl. Idempotent: it only fires when active key count is zero, so
  // it never spams keys on later visits.
  function personalAppWithNoKey(snapshot) {{
    const apps = (snapshot && Array.isArray(snapshot.apps)) ? snapshot.apps : [];
    for (const app of apps) {{
      if (!app || app.disabled_at) continue;
      if (app.kind !== "personal") continue;
      if (app.can_mint_keys === false) continue;
      const active = (Array.isArray(app.credentials) ? app.credentials : [])
        .filter((c) => c && c.active !== false).length;
      if (active === 0) return app;
    }}
    return null;
  }}

  async function autoMintFirstKey() {{
    const app = personalAppWithNoKey(accountSnapshot);
    if (!app) return;
    try {{
      const data = await accountJson(
        "/me/api/apps/" + encodeURIComponent(app.id) + "/keys",
        {{ method: "POST", body: {{ label: "Default key" }} }}
      );
      const key = data && data.credential && data.credential.api_key;
      if (key) showMintedKey(key);
      await refreshAccount();
    }} catch (_) {{
      // Non-fatal: the operator can still mint a key by hand from the App card.
    }}
  }}
  autoMintFirstKey();

  // "+ New" in the section header opens the "create another App" disclosure and
  // focuses the name field.
  document.addEventListener("click", (event) => {{
    const trigger = event.target.closest("[data-focus-create-app]");
    if (!trigger) return;
    const disclosure = document.querySelector(".app-create-disclosure");
    if (disclosure) {{ disclosure.hidden = false; disclosure.open = true; }}
    const input = document.querySelector("[data-create-owned-app] [name=name]");
    if (input) input.focus();
  }});

  // App-card expand/collapse. The card is a native <details>, but we own the
  // toggle explicitly (preventDefault on the summary's default action) so the
  // behavior is deterministic regardless of how the summary's flex content is
  // laid out — and so a click on an inner control (a key row, "+ New key")
  // inside the OPEN body never accidentally collapses the card.
  document.addEventListener("click", (event) => {{
    const summary = event.target.closest(".app-card-summary");
    if (!summary) return;
    const card = summary.closest(".app-card");
    if (!card) return;
    event.preventDefault();
    card.open = !card.open;
  }});

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
      openKeyDetailSheet(credentialId, label);
      return;
    }}

    if (target.closest("[data-close-key-detail]")) {{ closeKeyDetailSheet(); return; }}
    const detailSheet = $("key-detail-sheet");
    if (detailSheet && !detailSheet.hidden && target === detailSheet) {{
      closeKeyDetailSheet(); return;
    }}

    // Revoking a key lives inside the key-detail sheet (openKeyDetailSheet),
    // reached by tapping a key row — there is no destructive button on the
    // calm surface, so no surface revoke handler is needed here.

    const deleteApp = target.closest("[data-delete-owned-app]");
    if (deleteApp) {{
      const appId = deleteApp.dataset.deleteOwnedApp;
      const appName = deleteApp.dataset.appName || "this App";
      openDestructiveConfirmation({{
        title: "Delete " + appName + "?",
        description: "Its API keys will stop working immediately.",
        confirmLabel: "Delete App",
        trigger: deleteApp,
        mutation: () => accountJson(
            "/me/api/apps/" + encodeURIComponent(appId),
            {{ method: "DELETE" }}
          ),
        refresh: () => refreshAccount(),
        successMessage: "App deleted.",
        refreshFailureMessage: "App deleted, but the account view could not refresh. Reload this page."
      }});
    }}
  }});
}})();
  </script>
</body>
</html>
"""
