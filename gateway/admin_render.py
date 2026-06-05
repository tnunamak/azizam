"""Server-side HTML rendering for the admin dashboard.

:func:`render_admin_dashboard` is a pure function: it takes a JSON-shaped
``snapshot`` (the kind a caller can assemble from :mod:`gateway.admin_api`
endpoints or directly from an :class:`~gateway.admin_api.AdminStore`) and
returns a complete HTML page. It performs no I/O, opens no templates, and
never imports persistence or auth modules.

The page is intentionally simple but real: it shows the apps, users, app
profiles, credentials (without raw key material), and audit entries. It also
includes the operator flows for Apps, App API keys, Profile creation, runtime
Profile mappings, admin automation keys, and policy simulation.

Data-plane copy uses **App**, not "Client", per the project convention.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from gateway import service_registry
from gateway.admin_catalog import (
    coerce_policy_catalog,
    profile_policy_catalog,
    profile_policy_label_map,
)
from gateway.gateway_shell import (
    SHELL_CSS,
    esc as _esc,
    json_for_script as _json_for_script,
    render_shell_header,
)

__all__ = ["render_admin_dashboard"]


_PAGE_CSS = """
  .admin-content section,
  .admin-content section > div,
  .runtime-settings,
  details.panel,
  form.flow {
    min-width: 0;
    max-width: 100%;
  }
  /* Admin shell: a real left sidebar + a content area that shows one section
     at a time. Wider than the default shell since admin tables need room. */
  .admin-shell {
    width: min(1280px, 100%);
    margin: 0 auto;
    padding: var(--space-5) var(--gutter) var(--space-12);
    display: grid;
    grid-template-columns: 232px minmax(0, 1fr);
    gap: var(--space-6);
    align-items: start;
  }
  .admin-sidebar {
    position: sticky;
    top: calc(3.5rem + var(--space-4));
    display: grid;
    gap: var(--space-3);
    min-width: 0;
  }
  .admin-sidebar-head { display: grid; gap: 0.2rem; }
  .admin-sidebar-head h1 { margin: 0; font-size: 1.15rem; line-height: 1.2; color: var(--text); }
  .admin-nav { display: grid; gap: 0.15rem; }
  .admin-nav-link {
    display: block;
    padding: 0.5rem 0.7rem;
    border-radius: var(--radius-sm);
    color: var(--muted);
    border: 1px solid transparent;
    font-size: 0.92rem;
  }
  .admin-nav-link:hover { color: var(--text); text-decoration: none; background: var(--panel); }
  .admin-nav-link.is-active {
    color: var(--text);
    background: var(--panel-2);
    border-color: var(--line-strong);
    font-weight: 600;
  }
  .admin-legend { color: var(--muted); font-size: 0.82rem; }
  .admin-legend summary { cursor: pointer; }
  .admin-content { display: grid; gap: var(--space-6); min-width: 0; }
  /* Only the active section is shown; the sidebar switches between them. */
  .admin-section { display: none; }
  .admin-section.is-active { display: grid; gap: var(--space-3); }
  /* Explanation on demand, not a preamble. */
  .section-help { color: var(--muted); font-size: 0.85rem; margin: -0.15rem 0 var(--space-2); }
  /* "Things that want a look" — the operator's landing list. */
  .attention-panel {
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: var(--panel);
    overflow: hidden;
  }
  .attention-panel.clear {
    display: flex; align-items: center; gap: var(--space-3);
    padding: var(--space-4);
  }
  .attention-panel.clear strong { color: var(--text); }
  .attention-panel.clear p { margin: 0.1rem 0 0; color: var(--muted); font-size: 0.86rem; }
  .attention-heading {
    margin: 0; padding: var(--space-3) var(--space-4);
    font-size: 0.78rem; font-weight: 600; letter-spacing: 0.04em;
    text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--line);
  }
  .attention-row {
    display: flex; align-items: flex-start; gap: var(--space-3);
    padding: 0.75rem var(--space-4);
    color: var(--text); text-decoration: none;
    border-bottom: 1px solid var(--line);
  }
  .attention-row:last-child { border-bottom: 0; }
  .attention-row:hover { background: var(--panel-2); }
  .attention-row .vf-status-dot { margin-top: 0.35rem; flex: 0 0 auto; }
  .attention-text { display: grid; gap: 0.1rem; min-width: 0; }
  .attention-text strong { color: var(--text); font-weight: 600; font-size: 0.92rem; }
  .attention-detail { color: var(--muted); font-size: 0.83rem; }
  .section-help summary { cursor: pointer; color: var(--soft); }
  .section-help p { margin: var(--space-1) 0 0; line-height: 1.5; max-width: 58rem; }
  .subsection { display: grid; gap: var(--space-2); min-width: 0; }
  .subsection h3 { margin: 0; font-size: 0.95rem; color: var(--text); }
  @media (max-width: 860px) {
    /* min-width:0 on the grid chain lets the horizontal nav scroller actually
       CLIP+scroll instead of leaking its content width into the page (the
       classic CSS-grid overflow gotcha). */
    .admin-shell { grid-template-columns: 1fr; }
    .admin-shell > * { min-width: 0; }
    .admin-sidebar { position: static; min-width: 0; max-width: 100%; }
    .admin-nav {
      grid-auto-flow: column;
      grid-auto-columns: max-content;
      overflow-x: auto;
      gap: var(--space-1);
      min-width: 0;
      max-width: 100%;
    }
    .admin-nav-link { white-space: nowrap; }
  }
  .summary { display: flex; flex-wrap: wrap; gap: var(--space-2); color: var(--muted); }
  .summary strong { color: var(--text); }
  .summary span { background: var(--panel); border: 1px solid var(--line);
                  border-radius: 999px; padding: 0.45rem 0.75rem; }
  .runtime-settings { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center;
                      padding: 0.75rem; background: var(--panel); border: 1px solid var(--line);
                      border-radius: var(--radius); }
  .runtime-settings .label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase;
                             letter-spacing: 0.08em; }
  .runtime-settings code, td code { font-family: ui-monospace, monospace; font-size: 0.86em;
                                    background: oklch(15.5% 0.016 250); padding: 0.12rem 0.35rem;
                                    border: 1px solid var(--line); border-radius: 4px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem;
          background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
          overflow: hidden; }
  th, td { text-align: left; padding: 0.55rem 0.75rem;
           border-bottom: 1px solid var(--line); vertical-align: top; }
  th { font-weight: 500; color: var(--muted); }
  tr:last-child td { border-bottom: none; }
  .cell-main { font-weight: 650; color: var(--text); }
  .cell-sub { color: var(--muted); font-size: 0.82rem; margin-top: 0.2rem; }
  .source-plane-list { display: grid; gap: var(--space-3); }
  .source-plane { display: grid; gap: var(--space-2); }
  .source-plane h4 { margin: 0; color: var(--text); font-size: 0.9rem; }
  .source-plane p { margin: 0; color: var(--muted); line-height: 1.45; max-width: 70ch; }
  .inline-form { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; margin-top: 0.45rem; }
  .inline-form select,
  .inline-form input {
    flex: 1 1 12rem;
    min-width: 0;
    min-height: 2.25rem;
    background: oklch(15.5% 0.016 250);
    border: 1px solid var(--line);
    color: var(--text);
    padding: 0.35rem 0.5rem;
    border-radius: 6px;
  }
  .inline-form input[type="number"] { flex-basis: 8rem; }
  .inline-form button {
    min-height: 2.25rem;
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 0.35rem 0.65rem;
    cursor: pointer;
  }
  .inline-form button:hover { filter: brightness(1.08); }
  form.flow { display: grid; gap: 0.75rem; background: var(--panel);
              border: 1px solid var(--line); border-radius: var(--radius); padding: 0.9rem; min-width: 0; }
  form.flow .row { display: grid; gap: var(--space-2);
                   grid-template-columns: minmax(0, 1fr) auto; }
  form.flow .stack { display: grid; gap: var(--space-2);
                     grid-template-columns: repeat(auto-fit, minmax(min(180px, 100%), 1fr)); }
  form.flow label { font-size: 0.85rem; color: var(--muted); }
  form.flow input, form.flow select, form.flow textarea {
    background: oklch(15.5% 0.016 250);
    border: 1px solid var(--line); color: var(--text);
    min-height: 2.75rem; padding: 0.5rem 0.65rem; border-radius: 6px;
  }
  form.flow textarea { min-height: 5rem; resize: vertical; font-family: ui-monospace, monospace; }
  form.flow .check { display: flex; align-items: center; gap: 0.45rem; color: var(--muted); }
  form.flow .check input { width: auto; min-height: 0; }
  form.flow button { background: var(--focus); color: oklch(12% 0.02 250); border: 0;
                     min-height: 2.75rem; padding: 0.55rem 1rem; border-radius: 6px;
                     cursor: pointer; font-weight: 600; }
  form.flow button:hover { filter: brightness(1.08); }
  form.flow .form-note { margin: 0; color: var(--muted); line-height: 1.5; }
  form.flow input:focus-visible, form.flow select:focus-visible, form.flow textarea:focus-visible,
  form.flow button:focus-visible, .panel summary:focus-visible {
    outline: 2px solid var(--focus); outline-offset: 2px;
  }
  button.inline { background: var(--bad-bg); color: var(--bad); border: 1px solid oklch(40% 0.08 25);
                  min-height: 2.5rem; padding: 0.3rem 0.65rem; border-radius: 6px; cursor: pointer; }
  button.inline:hover { filter: brightness(1.08); }
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
  .secret {
    display: grid;
    gap: var(--space-2);
  }
  .secret code {
    display: block;
    color: var(--text);
    overflow-wrap: anywhere;
  }
  .secret .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .secret button {
    background: var(--focus);
    color: oklch(12% 0.02 250);
    border: 0;
    min-height: 2.5rem;
    min-width: 6.25rem;
    padding: 0.45rem 0.8rem;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 600;
  }
  .badge { font-size: 0.75rem; padding: 0.18rem 0.5rem;
           border-radius: 999px; background: var(--panel-2); color: var(--muted);
           border: 1px solid var(--line); display: inline-flex; align-items: center; }
  .badge.revoked, .badge.problem { background: var(--bad-bg); color: var(--bad); border-color: transparent; }
  .badge.active, .badge.ready { background: var(--good-bg); color: var(--good); border-color: transparent; }
  .badge.warn, .badge.attention { background: var(--warn-bg); color: var(--warn); border-color: transparent; }
  .problem-list { display: grid; gap: var(--space-2); margin: 0; padding: 0; list-style: none; }
  .problem-list li { background: var(--panel); border: 1px solid var(--line);
                     border-radius: var(--radius); padding: 0.65rem 0.75rem; }
  .info-grid {
    display: grid;
    gap: var(--space-3);
    grid-template-columns: repeat(auto-fit, minmax(min(20rem, 100%), 1fr));
  }
  .info-panel {
    display: grid;
    gap: var(--space-2);
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-4);
    min-width: 0;
  }
  .info-panel h3 { margin: 0; font-size: 1rem; }
  .info-panel p { margin: 0; color: var(--muted); line-height: 1.5; }
  .facts {
    display: grid;
    gap: 0.35rem;
    margin: 0;
    color: var(--muted);
  }
  .facts div {
    display: grid;
    gap: var(--space-2);
    grid-template-columns: 8.5rem minmax(0, 1fr);
    align-items: start;
  }
  .facts dt { color: var(--soft); }
  .facts dd { margin: 0; min-width: 0; overflow-wrap: anywhere; color: var(--text); }
  .operator-control-list { display: grid; gap: var(--space-2); }
  .operator-control {
    display: grid;
    gap: 0.45rem;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 0.75rem;
  }
  .operator-control form { margin-top: 0.15rem; }
  .form-section {
    display: grid;
    gap: var(--space-2);
  }
  .form-section > strong { color: var(--text); font-size: 0.9rem; }
  .policy-preview {
    display: grid;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: oklch(14.5% 0.015 250);
    padding: 0.75rem;
    color: var(--muted);
    line-height: 1.45;
  }
  .policy-preview strong { color: var(--text); }
  .start-card {
    display: grid;
    gap: var(--space-2);
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: var(--space-4);
  }
  .start-card h3 { margin: 0; font-size: 1rem; }
  .start-card p { margin: 0; color: var(--muted); line-height: 1.5; }
  .start-card a {
    justify-self: start;
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.4rem 0.8rem;
    background: var(--panel-2);
    color: var(--text);
  }
  .start-card a:hover { text-decoration: none; filter: brightness(1.06); }
  .result { white-space: pre-wrap; background: oklch(15.5% 0.016 250); border: 1px solid var(--line);
            border-radius: var(--radius); padding: 0.75rem;
            color: oklch(90% 0.05 235); font-family: ui-monospace, monospace; font-size: 0.86rem; }
  .result:empty { display: none; }
  details.panel { background: var(--panel); border: 1px solid var(--line);
                  border-radius: var(--radius); padding: 0.9rem; min-width: 0; }
  details.panel summary { cursor: pointer; color: var(--text); font-weight: 650;
                          min-height: 2rem; display: inline-flex; align-items: center; }
  details.panel form.flow { margin-top: 0.75rem; background: transparent; border: 0; padding: 0; }
  .choice-grid { display: grid; gap: 0.65rem; grid-template-columns: repeat(auto-fit, minmax(min(13rem, 100%), 1fr)); }
  .permission-grid { display: grid; gap: 0.55rem; grid-template-columns: repeat(auto-fit, minmax(min(14rem, 100%), 1fr)); }
  .choice-card { display: grid; gap: 0.3rem; padding: 0.75rem; border: 1px solid var(--line);
                 border-radius: 6px; background: oklch(14.5% 0.015 250); color: var(--muted); }
  .choice-card input { margin-right: 0.4rem; }
  .choice-card strong { color: var(--text); }
  .permission-grid .check {
    align-items: flex-start;
    background: oklch(14.5% 0.015 250);
    border: 1px solid var(--line);
    border-radius: 6px;
    min-height: 3rem;
    padding: 0.65rem 0.7rem;
  }
  .permission-grid .check span { display: grid; gap: 0.15rem; }
  .permission-grid .check strong { color: var(--text); font-size: 0.88rem; }
  .permission-grid .check em { color: var(--muted); font-size: 0.78rem; font-style: normal; line-height: 1.35; }
  .inline-options { display: flex; flex-wrap: wrap; gap: 0.55rem 1rem; }
  /* Backend (service) CRUD. */
  .service-crud { display: grid; gap: var(--space-3); margin-bottom: var(--space-3); }
  .service-crud-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
  .service-crud-intro { margin: 0; color: var(--muted); font-size: 0.86rem; line-height: 1.5; max-width: 48rem; }
  .notice {
    border: 1px solid color-mix(in srgb, var(--warn) 40%, var(--line));
    background: var(--warn-bg);
    color: var(--text);
    border-radius: var(--radius);
    padding: var(--space-3);
    font-size: 0.88rem;
  }
  .service-form {
    display: grid; gap: var(--space-3);
    border: 1px solid var(--line); border-radius: var(--radius);
    background: var(--panel); padding: var(--space-4); min-width: 0;
  }
  .service-form h4 { margin: 0; font-size: 1rem; }
  .field-label { display: grid; gap: 0.3rem; font-size: 0.85rem; color: var(--muted); min-width: 0; }
  .field-label input, .field-label select { min-width: 0; }
  .field-hint { color: var(--soft); font-size: 0.78rem; font-weight: 400; }
  .field-group { border: 1px solid var(--line); border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); display: flex; flex-wrap: wrap; gap: 0.4rem 1rem; margin: 0; }
  .field-group legend { color: var(--muted); font-size: 0.8rem; padding: 0 0.3rem; }
  .service-form-advanced > summary { cursor: pointer; color: var(--muted); font-size: 0.85rem; padding: 0.3rem 0; }
  .row-actions { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; }
  .service-test-result { font-size: 0.84rem; min-height: 1.1rem; }
  .service-test-result.good { color: var(--good); }
  .service-test-result.bad { color: var(--bad); }
  @media (max-width: 760px) {
    /* Tables were the #1 mobile-overflow culprit (a 7-column table is ~1200px
       wide — horizontal scroll OR it pushes the page sideways). On mobile we
       drop the table grid entirely and stack each ROW as a card: the header
       row is hidden and every cell becomes a full-width line. No sideways
       scroll, no clipped columns. The rows already carry self-describing
       context (.cell-main / .cell-sub / <code> ids), so losing the column
       headers costs little. */
    .admin-content table { display: block; width: 100%; max-width: 100%; border: 0; }
    .admin-content thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
    .admin-content tbody { display: block; width: 100%; }
    .admin-content tbody tr {
      display: block;
      width: 100%;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel);
      padding: var(--space-2) var(--space-3);
      margin-bottom: var(--space-3);
    }
    .admin-content tbody td {
      display: block;
      width: 100%;
      max-width: 100%;
      border: 0;
      padding: 0.3rem 0;
      overflow-wrap: anywhere;
      white-space: normal;
    }
    .admin-content tbody td:not(:last-child) { border-bottom: 1px solid var(--line); }
    /* Long ids / tokens and form controls inside cards must never force width. */
    .admin-content tbody td code,
    .admin-content tbody td .cell-sub { overflow-wrap: anywhere; word-break: break-word; max-width: 100%; }
    .admin-content tbody td input,
    .admin-content tbody td select,
    .admin-content tbody td button,
    .admin-content tbody td form { max-width: 100%; box-sizing: border-box; }
    .admin-content tbody td form { display: flex; flex-wrap: wrap; gap: 0.4rem; }
    .admin-content section,
    .admin-content section > div { min-width: 0; max-width: 100%; }
    /* Belt-and-braces: clip the content column so no stray wide descendant
       (a hidden section's table, a long token) can scroll the whole PAGE
       sideways. The card layout means nothing legitimately needs to. */
    .admin-content { overflow-x: hidden; }
    .admin-shell, main.admin-shell { overflow-x: hidden; max-width: 100%; }
    form.flow .row { grid-template-columns: 1fr; }
    .choice-grid, .permission-grid { grid-template-columns: 1fr; }
    .facts div { grid-template-columns: 1fr; gap: 0.1rem; }
    /* Touch-target floor for the section nav on mobile. */
    .admin-nav-link { min-height: 2.75rem; display: flex; align-items: center; }
    /* The attention panel reads fine as-is on mobile (it's already a stack). */
  }
"""


def _format_optional_list(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(_esc(v) for v in sorted(value))
    return _esc(value)


def _format_key_value_summary(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, Mapping):
        parts = []
        for key, item in value.items():
            if item in (None, "", [], {}, ()):
                continue
            label = str(key).replace("_", " ")
            parts.append(f"{label}: {item}")
        return "; ".join(parts)
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(str(item) for item in sorted(value) if item)
    return str(value)


def _as_iterable_items(value: Any) -> list[Any]:
    if value in (None, "", [], {}, ()):
        return []
    if isinstance(value, Mapping):
        items: list[Any] = []
        for key, item in value.items():
            if item in (None, "", [], {}, ()):
                continue
            if isinstance(item, (list, tuple, set, frozenset)):
                for nested in item:
                    if isinstance(nested, Mapping):
                        items.append({"kind": key, **nested})
                    elif nested not in (None, "", [], {}, ()):
                        items.append({"kind": key, "value": nested})
            elif isinstance(item, Mapping):
                items.append({"kind": key, **item})
            else:
                items.append({"kind": key, "value": item})
        return items
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for item in value if item not in (None, "", [], {}, ())]
    return [value]


def _format_code_object(item: Any) -> str:
    if isinstance(item, Mapping):
        kind = item.get("kind") or item.get("type") or item.get("object_kind") or item.get("role")
        identifier = (
            item.get("id")
            or item.get("name")
            or item.get("module")
            or item.get("entrypoint")
            or item.get("path")
            or item.get("value")
        )
        if kind and identifier:
            return f"{kind}: {identifier}"
        if identifier:
            return str(identifier)
        return _format_key_value_summary(item)
    return str(item)


def _extension_code_objects(record: Mapping[str, Any]) -> list[str]:
    candidates: list[Any] = []
    for key in ("code_objects", "staged_code_objects", "staged_code", "code"):
        candidates.extend(_as_iterable_items(record.get(key)))
    for parent_key in ("review", "validation", "staged"):
        parent = record.get(parent_key)
        if not isinstance(parent, Mapping):
            continue
        for key in ("code_objects", "staged_code_objects", "staged_code", "code"):
            candidates.extend(_as_iterable_items(parent.get(key)))

    formatted: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        label = _format_code_object(item).strip()
        if not label or label in seen:
            continue
        seen.add(label)
        formatted.append(label)
    return formatted


_SECRET_MARKERS = ("secret", "token", "key", "password", "credential")


def _is_secret_config_source(record: Mapping[str, Any]) -> bool:
    for key in ("label", "domain", "name", "key", "path", "value"):
        value = record.get(key)
        if value and any(marker in str(value).lower() for marker in _SECRET_MARKERS):
            return True
    return bool(record.get("secret") or record.get("sensitive"))


def _safe_config_text(value: Any, *, record: Mapping[str, Any] | None = None) -> str:
    if value in (None, "", [], {}, ()):
        return "—"
    if record is not None and _is_secret_config_source(record):
        return "redacted"
    text = _format_key_value_summary(value)
    if len(text) > 96:
        text = text[:93].rstrip() + "..."
    if "/" in text and len(text) > 48:
        text = "..." + text[-45:]
    return text


def _source_badge_class(source: str) -> str:
    normalized = source.lower()
    if normalized in {"admin", "live", "ui", "ui-owned", "dashboard"}:
        return "ready"
    if normalized in {"env", "yaml", "file", "manifest", "bootstrap", "provisioned"}:
        return "attention"
    if normalized in {"mixed", "split"}:
        return "warn"
    return ""


def _profile_doc(record: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not record:
        return {}
    profile = record.get("profile")
    if isinstance(profile, Mapping):
        return profile
    return record


def _profile_id(record: Mapping[str, Any] | None) -> str:
    if not record:
        return "default-local"
    return str(record.get("id") or _profile_doc(record).get("id") or "default-local")


def _nested_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(str(item) for item in value)
    return []


def _catalog_options(
    catalog: Mapping[str, list[Mapping[str, Any]]], section: str
) -> list[Mapping[str, Any]]:
    options = catalog.get(section)
    return list(options) if isinstance(options, list) else []


def _visible_catalog_options(
    catalog: Mapping[str, list[Mapping[str, Any]]],
    section: str,
    *,
    flag: str,
) -> list[Mapping[str, Any]]:
    return [
        option
        for option in _catalog_options(catalog, section)
        if option.get(flag, True) is not False
    ]


def _catalog_option_label(option: Mapping[str, Any], *, summary: bool = False) -> str:
    if summary:
        return str(
            option.get("summary_label")
            or option.get("label")
            or option.get("id")
            or ""
        )
    return str(
        option.get("label")
        or option.get("summary_label")
        or option.get("id")
        or ""
    )


def _catalog_default_summary(
    catalog: Mapping[str, list[Mapping[str, Any]]],
    section: str,
    *,
    separator: str = ", ",
    empty: str = "none selected",
) -> str:
    labels = [
        _catalog_option_label(option, summary=True)
        for option in _catalog_options(catalog, section)
        if option.get("default")
    ]
    labels = [label for label in labels if label]
    return separator.join(labels) if labels else empty


def _catalog_select_options(
    catalog: Mapping[str, list[Mapping[str, Any]]],
    section: str,
    *,
    selected: str | None = None,
) -> str:
    options = []
    for option in _visible_catalog_options(catalog, section, flag="simulator"):
        value = str(option.get("id") or "")
        if not value:
            continue
        is_selected = selected == value or (
            selected is None and bool(option.get("default"))
        )
        options.append(
            '<option value="'
            + _esc(value)
            + '"'
            + (" selected" if is_selected else "")
            + ">"
            + _esc(_catalog_option_label(option))
            + "</option>"
        )
    return "".join(options)


def _option_grid(
    *,
    name: str,
    options: Iterable[Mapping[str, Any]],
    aria_label: str,
) -> str:
    items = []
    for option in options:
        value = str(option.get("id") or "")
        if not value:
            continue
        checked = " checked" if option.get("default") else ""
        label = _esc(_catalog_option_label(option))
        description = _esc(option.get("description") or "")
        description_html = f"<em>{description}</em>" if description else ""
        items.append(
            '<label class="check"><input type="checkbox" name="'
            + _esc(name)
            + '" value="'
            + _esc(value)
            + '"'
            + checked
            + f"> <span><strong>{label}</strong>{description_html}</span></label>"
        )
    if not items:
        return ""
    return f'<div class="permission-grid" aria-label="{_esc(aria_label)}">{"".join(items)}</div>'


def _radio_grid(
    *,
    name: str,
    options: Iterable[Mapping[str, Any]],
    aria_label: str,
) -> str:
    items = []
    for option in options:
        value = str(option.get("id") or "")
        if not value:
            continue
        checked = " checked" if option.get("default") else ""
        label = _esc(_catalog_option_label(option))
        description = _esc(option.get("description") or "")
        items.append(
            '<label class="choice-card"><span><input type="radio" name="'
            + _esc(name)
            + '" value="'
            + _esc(value)
            + '"'
            + checked
            + f"><strong>{label}</strong></span><span>{description}</span></label>"
        )
    if not items:
        return ""
    return f'<div class="choice-grid" aria-label="{_esc(aria_label)}">{"".join(items)}</div>'


def _inventory_items(inventory: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    raw = inventory.get(name)
    if not isinstance(raw, list):
        return []
    items: list[Mapping[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping) and item.get("id"):
            items.append(item)
        elif item:
            items.append({"id": str(item)})
    return items


def _datalist(datalist_id: str, items: Iterable[Mapping[str, Any]]) -> str:
    options = []
    for item in items:
        value = str(item.get("id") or "")
        if not value:
            continue
        label = item.get("label") or item.get("description") or ""
        label_attr = f' label="{_esc(label)}"' if label else ""
        options.append(f'<option value="{_esc(value)}"{label_attr}></option>')
    if not options:
        return ""
    return f'<datalist id="{_esc(datalist_id)}">{"".join(options)}</datalist>'


def _text_input_with_datalist(
    *,
    name: str,
    placeholder: str,
    aria_label: str,
    datalist_id: str,
    items: Iterable[Mapping[str, Any]],
) -> str:
    items_list = list(items)
    list_attr = f' list="{_esc(datalist_id)}"' if items_list else ""
    return (
        '<input type="text" name="'
        + _esc(name)
        + '" placeholder="'
        + _esc(placeholder)
        + '" aria-label="'
        + _esc(aria_label)
        + '"'
        + list_attr
        + ">"
        + _datalist(datalist_id, items_list)
    )


def _profile_access_summary(record: Mapping[str, Any] | None) -> str:
    doc = _profile_doc(record)
    allow = _nested_mapping(doc.get("allow"))
    intents = _nested_list(allow.get("intents"))
    trust = _nested_list(allow.get("trust_tiers"))
    resources = _nested_list(allow.get("resource_tiers"))
    exposure = _nested_list(allow.get("exposure"))
    tags = _nested_list(allow.get("tags"))

    if intents or trust or resources or exposure or tags:
        labels: list[str] = []
        intent_labels = profile_policy_label_map("intents")
        intent_labels.update({
            "chat.completions": "chat",
            "embedding": "embeddings",
            "embeddings": "embeddings",
            "audio.transcribe": "audio transcription",
            "audio.speech": "text to speech",
        })
        for intent in intents:
            labels.append(intent_labels.get(intent, intent))
        if not labels and tags:
            labels.extend(tags)
        if not labels:
            labels.append("selected local capabilities")
        summary = ", ".join(dict.fromkeys(labels))
        guardrails: list[str] = []
        if exposure:
            guardrails.append("default access" if "default" in exposure else "opt-in access")
        if trust:
            guardrails.append(
                "stable" if trust == ["stable"] else "stable and experimental"
            )
        if resources:
            guardrails.append(
                "interactive" if resources == ["interactive"] else ", ".join(resources)
            )
        return summary + (f" ({'; '.join(guardrails)})" if guardrails else "")

    protocols = _nested_list(doc.get("protocols"))
    workflows = _nested_list(doc.get("workflows"))
    models = _nested_list(doc.get("models"))
    if protocols or workflows or models:
        parts = []
        if protocols:
            parts.append("protocols " + ", ".join(protocols))
        if workflows:
            parts.append("workflows " + ", ".join(workflows))
        if models:
            parts.append("models " + ", ".join(models))
        return "; ".join(parts)

    return "Local defaults; cloud fallback and native passthrough stay off"


def _profile_guardrail_summary(record: Mapping[str, Any] | None) -> str:
    doc = _profile_doc(record)
    fallback = _nested_mapping(doc.get("fallback"))
    native = _nested_mapping(doc.get("native_passthrough"))
    cloud = "cloud allowed" if fallback.get("cloud_allowed") else "local only"
    native_text = "native allowed" if native.get("enabled") else "native off"
    target_policy = _profile_target_policy_summary(record)
    return f"{cloud}; {native_text}; {target_policy}"


def _profile_target_policy_summary(record: Mapping[str, Any] | None) -> str:
    doc = _profile_doc(record)
    raw = doc.get("target_policy")
    if isinstance(raw, Mapping):
        visibility = str(raw.get("visibility") or "catalog")
        activation = str(raw.get("activation") or "may_activate")
        return f"targets {visibility}; activation {activation.replace('_', ' ')}"
    legacy = doc.get("model_access")
    if isinstance(legacy, Mapping) and legacy.get("mode") == "current_loaded":
        return "targets available; activation existing only"
    return "targets catalog; activation may activate"


def _visibility_summary(record: Mapping[str, Any] | None) -> str:
    doc = _profile_doc(record)
    raw = doc.get("status_visibility")
    if isinstance(raw, Mapping):
        return str(raw.get("level") or "basic")
    if isinstance(raw, str):
        return raw
    return "basic"


def _is_revoked(record: Mapping[str, Any]) -> bool:
    return bool(record.get("revoked_at") or record.get("revoked"))


def _credential_row(record: Mapping[str, Any]) -> str:
    cred_id = _esc(record.get("id") or record.get("credential_id"))
    raw_cred_id = str(record.get("id") or record.get("credential_id") or "")
    app_id = _esc(record.get("app_id"))
    label = _esc(record.get("label") or record.get("display_name") or record.get("name") or "—")
    created = _esc(record.get("created_at") or "—")
    revoked = _is_revoked(record)
    badge_class = "revoked" if revoked else "active"
    badge_text = "revoked" if revoked else "active"
    action = (
        "—"
        if revoked or not raw_cred_id
        else (
            '<button class="inline" type="button" data-revoke-credential="'
            + _esc(raw_cred_id)
            + '" aria-label="Revoke credential '
            + _esc(raw_cred_id)
            + '">Revoke</button>'
        )
    )
    return (
        "<tr>"
        f"<td><code>{cred_id}</code></td>"
        f"<td><code>{app_id}</code></td>"
        f"<td>{label}</td>"
        f"<td>{created}</td>"
        f'<td><span class="badge {badge_class}">{badge_text}</span></td>'
        f"<td>{action}</td>"
        "</tr>"
    )


def _app_profile_id(record: Mapping[str, Any]) -> str:
    profile_id = (
        record.get("default_profile_id")
        or record.get("profile_id")
        or record.get("profile")
    )
    if profile_id:
        return str(profile_id)
    if record.get("kind") == "personal" or record.get("owner_user_id"):
        return "default-local"
    return "default-local"


def _profile_options(
    profiles: list[Mapping[str, Any]],
    *,
    selected: str = "",
    include_default: bool = True,
    default_label: str = "Default local",
) -> str:
    options = (
        f'<option value="default-local"{(" selected" if selected in ("", "default-local") else "")}>'
        f"{_esc(default_label)} (default-local)</option>"
        if include_default
        else ""
    )
    for profile in profiles:
        profile_id = _profile_id(profile)
        if include_default and profile_id == "default-local":
            continue
        label = profile.get("label") or profile.get("name") or profile_id
        selected_attr = " selected" if profile_id == selected else ""
        options += (
            f'<option value="{_esc(profile_id)}"{selected_attr}>'
            f"{_esc(label)} ({_esc(profile_id)})</option>"
        )
    return options


def _app_row(
    record: Mapping[str, Any],
    *,
    profiles: list[Mapping[str, Any]],
    profiles_by_id: Mapping[str, Mapping[str, Any]],
    active_keys_by_app: Mapping[str, int],
    update_app_action_template: str,
) -> str:
    raw_app_id = str(record.get("id") or "")
    app_id = _esc(raw_app_id)
    name = _esc(record.get("name") or "—")
    owner = _esc(
        record.get("owner")
        or record.get("owner_user_id")
        or record.get("owner_service_account_id")
        or record.get("user_id")
        or "—"
    )
    profile_id = _app_profile_id(record)
    profile = profiles_by_id.get(profile_id)
    active_keys = active_keys_by_app.get(raw_app_id, 0)
    status_class = "ready"
    status_text = "ready"
    disabled = bool(record.get("disabled_at"))
    if disabled:
        status_class = "problem"
        status_text = "disabled"
    elif not profile_id:
        status_class = "problem"
        status_text = "needs profile"
    elif profile_id != "default-local" and profile is None:
        status_class = "problem"
        status_text = "profile missing"
    elif active_keys < 1:
        status_class = "attention"
        status_text = "needs key"
    # The full resolved-policy summary isn't echoed per app row (it lives once
    # in the Profiles table) — the Apps list shows the Profile name + status.
    profile_label = profile_id or "default-local"
    profile_controls = (
        '<form class="inline-form" method="post" action="'
        + _esc(update_app_action_template.replace("{app_id}", raw_app_id))
        + '" data-update-app-profile>'
        + f'<select name="profile_id" aria-label="Change Profile for {_esc(raw_app_id)}">'
        + _profile_options(profiles, selected=profile_label, include_default=True)
        + "</select>"
        + '<button type="submit">Change Profile</button>'
        + "</form>"
    )
    action = (
        "—"
        if disabled or not raw_app_id
        else (
            '<button class="inline" type="button" data-disable-app="'
            + _esc(raw_app_id)
            + '" aria-label="Disable App '
            + _esc(raw_app_id)
            + '">Disable</button>'
        )
    )
    created = _esc(record.get("created_at") or "—")
    return (
        "<tr>"
        f'<td><div class="cell-main">{name}</div><div class="cell-sub"><code>{app_id}</code></div></td>'
        f"<td>{owner}</td>"
        f'<td><div><code>{_esc(profile_label)}</code></div>{profile_controls}</td>'
        f"<td>{_esc(active_keys)} active</td>"
        f'<td><span class="badge {status_class}">{status_text}</span></td>'
        f"<td>{created}</td>"
        f"<td>{action}</td>"
        "</tr>"
    )


def _user_row(record: Mapping[str, Any]) -> str:
    user_id = _esc(record.get("id"))
    email = _esc(record.get("email") or "—")
    name = _esc(record.get("name") or "—")
    roles = _format_optional_list(record.get("roles"))
    return (
        "<tr>"
        f"<td><code>{user_id}</code></td>"
        f"<td>{name}</td>"
        f"<td>{email}</td>"
        f"<td>{roles}</td>"
        "</tr>"
    )


def _profile_row(record: Mapping[str, Any]) -> str:
    doc = _profile_doc(record)
    profile_id = _esc(_profile_id(record))
    label = _esc(record.get("label") or record.get("name") or "—")
    overrides = []
    for name in ("workflows", "models", "backends"):
        values = _nested_list(doc.get(name))
        if values:
            label_name = {
                "workflows": "workflows",
                "models": "models",
                "backends": "backends",
            }[name]
            overrides.append(f"{label_name}: " + ", ".join(values))
    override_text = "; ".join(overrides) if overrides else "All matching local routes"
    visibility = _esc(_visibility_summary(record))
    return (
        "<tr>"
        f'<td><div class="cell-main">{label}</div><div class="cell-sub"><code>{profile_id}</code></div></td>'
        f"<td>{_esc(_profile_access_summary(record))}</td>"
        f"<td>{_esc(override_text)}</td>"
        f"<td>{_esc(_profile_guardrail_summary(record))}</td>"
        f"<td>{visibility}</td>"
        "</tr>"
    )


def _audit_row(record: Mapping[str, Any]) -> str:
    when = _esc(record.get("at") or record.get("timestamp") or record.get("created_at") or "—")
    actor = _esc(
        record.get("actor")
        or record.get("subject")
        or (
            f'{record.get("actor_kind")}:{record.get("actor_id")}'
            if record.get("actor_id")
            else ""
        )
        or "—"
    )
    action = _esc(record.get("action") or record.get("event") or "—")
    target = _esc(
        record.get("target")
        or record.get("resource")
        or (
            f'{record.get("target_kind")}:{record.get("target_id")}'
            if record.get("target_id")
            else ""
        )
        or "—"
    )
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        detail_value = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    else:
        detail_value = metadata
    detail = _esc(record.get("detail") or record.get("note") or detail_value or "")
    return (
        "<tr>"
        f"<td>{when}</td>"
        f"<td>{actor}</td>"
        f"<td>{action}</td>"
        f"<td>{target}</td>"
        f"<td>{detail}</td>"
        "</tr>"
    )


def _api_surface_row(record: Mapping[str, Any]) -> str:
    name = _esc(record.get("name") or "—")
    route_base = _esc(record.get("route_base") or record.get("base") or "—")
    status_value = str(record.get("status") or "unknown")
    status_class = {
        "live": "ready",
        "partial": "warn",
        "planned": "attention",
    }.get(status_value, "attention")
    profile_gates = _format_optional_list(record.get("profile_gates"))
    clients = _format_optional_list(record.get("clients"))
    description = _esc(record.get("description") or "")
    return (
        "<tr>"
        f'<td><div class="cell-main">{name}</div><div class="cell-sub">{description}</div></td>'
        f"<td><code>{route_base}</code></td>"
        f'<td><span class="badge {status_class}">{_esc(status_value)}</span></td>'
        f"<td>{profile_gates}</td>"
        f"<td>{clients}</td>"
        "</tr>"
    )


def _service_row(record: Mapping[str, Any]) -> str:
    name = _esc(record.get("name") or record.get("key") or "—")
    key = _esc(record.get("key") or "—")
    role = _esc(record.get("role") or "—")
    source = _esc(record.get("source") or "ui")
    editable = "editable" if record.get("editable", True) else "read-only"
    priority = record.get("priority")
    capabilities = _format_optional_list(record.get("capabilities"))
    status_value = str(record.get("status") or record.get("live_status") or "unknown")
    status_class = {
        "up": "ready",
        "healthy": "ready",
        "ok": "ready",
        "standby": "attention",
        "degraded": "warn",
        "down": "problem",
        "pending": "warn",
        "unknown": "attention",
    }.get(status_value, "attention")
    runtime = _esc(record.get("runtime") or record.get("current") or "—")
    note = _esc(record.get("note") or record.get("last_error") or "")
    status_html = f'<span class="badge {status_class}">{_esc(status_value)}</span>'
    if note:
        status_html += f'<div class="cell-sub">{note}</div>'
    resource_parts = []
    resource_mode = _format_key_value_summary(record.get("resource_mode"))
    resource_summary = _format_key_value_summary(record.get("resource_summary"))
    queue = _format_key_value_summary(record.get("queue"))
    resource_limits = _format_key_value_summary(record.get("resource_limits"))
    if resource_mode:
        resource_parts.append(f'<div class="cell-main">{_esc(resource_mode)}</div>')
    if resource_summary:
        resource_parts.append(f'<div class="cell-sub">{_esc(resource_summary)}</div>')
    if queue:
        resource_parts.append(f'<div class="cell-sub">queue: {_esc(queue)}</div>')
    if resource_limits:
        resource_parts.append(f'<div class="cell-sub">limits: {_esc(resource_limits)}</div>')
    resources = "".join(resource_parts) or "—"
    return (
        "<tr>"
        f'<td><div class="cell-main">{name}</div><div class="cell-sub"><code>{key}</code></div></td>'
        f"<td>{role}<div class=\"cell-sub\">{source}; {editable}"
        + (f"; priority {_esc(priority)}" if priority is not None else "")
        + "</div></td>"
        f"<td>{capabilities}</td>"
        f"<td>{status_html}</td>"
        f"<td>{runtime}</td>"
        f"<td>{resources}</td>"
        f"<td>{_service_actions(record)}</td>"
        "</tr>"
    )


def _service_actions(record: Mapping[str, Any]) -> str:
    """Per-row controls for a backend: enable/disable, edit, delete.

    All rows can be enabled/disabled. Only operator-added (``source == "ui"``)
    rows can be edited or hard-deleted; provisioned/builtin rows are managed by
    their source of truth and so are disable-only, with a lock badge.
    """
    service_id = _esc(record.get("id") or record.get("key") or "")
    name = _esc(record.get("name") or record.get("key") or service_id)
    source = str(record.get("source") or "ui")
    is_ui = source == "ui"
    enabled = record.get("enabled", True)
    toggle_label = "Disable" if enabled else "Enable"
    parts = [
        f'<button type="button" class="vf-button vf-button-ghost vf-button-sm" '
        f'data-service-toggle="{service_id}" data-enabled="{"1" if enabled else "0"}">'
        f"{toggle_label}</button>"
    ]
    if is_ui:
        parts.append(
            f'<button type="button" class="vf-button vf-button-ghost vf-button-sm" '
            f'data-service-edit="{service_id}">Edit</button>'
        )
        parts.append(
            f'<button type="button" class="vf-button vf-button-ghost vf-button-sm" '
            f'data-service-delete="{service_id}" data-service-name="{name}">'
            "Delete</button>"
        )
    else:
        parts.append(
            f'<span class="badge" title="Managed by configuration — edit the '
            f'config file to change or remove">{_esc(source)}</span>'
        )
    return '<div class="row-actions">' + "".join(parts) + "</div>"


def _service_form(record: Mapping[str, Any] | None = None) -> str:
    """The add/edit-a-backend form (server-rendered; works without JS).

    When ``record`` is given, the form is pre-filled for editing (the id is
    fixed). Secrets are referenced by env-var name only — the key itself is
    never rendered or accepted here.
    """
    rec = record or {}
    raw_config = rec.get("config")
    config: Mapping[str, Any] = raw_config if isinstance(raw_config, Mapping) else {}
    editing = bool(rec)
    service_id = _esc(rec.get("id") or "")
    name = _esc(rec.get("name") or config.get("name") or "")
    host = _esc(config.get("host") or "")
    port = config.get("port")
    base_path = _esc(config.get("base_path") or ("/v1" if not editing else ""))
    base_url = ""
    if host:
        authority = f"{host}:{port}" if port else str(host)
        base_url = _esc(f"http://{authority}{base_path or ''}")
    api_key_env = _esc(config.get("api_key_env") or "")
    note = _esc(config.get("note") or "")
    role = str(rec.get("role") or config.get("role") or "required")
    priority = rec.get("priority")
    enabled = rec.get("enabled", True)
    managed = False
    lifecycle = rec.get("lifecycle")
    if isinstance(lifecycle, Mapping):
        managed = bool(lifecycle.get("managed"))
    caps = set(_normalize_caps(rec.get("capabilities")))

    preset_options = "".join(
        f'<option value="{_esc(p["id"])}">{_esc(p["label"])}</option>'
        for p in service_registry.PROVIDER_PRESETS
    )
    cap_choices = ("chat", "completions", "vision", "embeddings", "image", "audio", "speech")
    cap_boxes = "".join(
        '<label class="check"><input type="checkbox" name="capabilities" '
        f'value="{_esc(c)}"{" checked" if c in caps else ""}> {_esc(c)}</label>'
        for c in cap_choices
    )
    role_radios = "".join(
        '<label class="check"><input type="radio" name="role" '
        f'value="{_esc(r)}"{" checked" if role == r else ""}> {_esc(r)}</label>'
        for r in ("required", "standby")
    )
    form_kind = "edit" if editing else "add"
    heading = f"Edit {name}" if editing else "Add a service"
    submit_label = "Save changes" if editing else "Add service"
    id_field = (
        f'<input type="hidden" name="id" value="{service_id}">' if editing else ""
    )
    preset_row = (
        ""
        if editing
        else (
            '<label class="field-label">Provider'
            f'<select name="preset" data-service-preset>{preset_options}</select>'
            "</label>"
        )
    )
    return (
        f'<form class="service-form" data-service-form="{form_kind}"'
        + (f' data-service-id="{service_id}"' if editing else "")
        + ' hidden>'
        + id_field
        + f"<h4>{_esc(heading)}</h4>"
        + '<label class="field-label">Name'
        f'<input type="text" name="name" value="{name}" '
        'placeholder="e.g. Local vLLM" required></label>'
        + preset_row
        + '<label class="field-label">Base URL'
        f'<input type="text" name="base_url" value="{base_url}" '
        'placeholder="http://127.0.0.1:8000/v1" data-service-base-url></label>'
        + '<label class="field-label">API key env var <span class="field-hint">'
        "(name of the environment variable holding the key — never the key "
        'itself)</span>'
        f'<input type="text" name="api_key_env" value="{api_key_env}" '
        'placeholder="MY_BACKEND_API_KEY" autocomplete="off"></label>'
        + '<fieldset class="field-group"><legend>Capabilities</legend>'
        + cap_boxes
        + "</fieldset>"
        + '<label class="check"><input type="checkbox" name="enabled" '
        + ("checked" if enabled else "")
        + "> Enabled</label>"
        + '<details class="service-form-advanced"><summary>Advanced</summary>'
        + '<fieldset class="field-group"><legend>Role</legend>'
        + role_radios
        + "</fieldset>"
        + '<label class="field-label">Priority'
        f'<input type="number" name="priority" value="{_esc(priority) if priority is not None else ""}" '
        'min="0" placeholder="100"></label>'
        + '<label class="check"><input type="checkbox" name="managed" '
        + ("checked" if managed else "")
        + "> Managed (gateway starts/stops this service)</label>"
        + '<label class="field-label">Note'
        f'<input type="text" name="note" value="{note}" '
        'placeholder="Optional operator note"></label>'
        + "</details>"
        + '<div id="service-test-result" class="service-test-result"></div>'
        + '<div class="row-actions">'
        + '<button type="button" class="vf-button vf-button-secondary vf-button-sm" '
        'data-service-test>Test connection</button>'
        + f'<button type="submit" class="vf-button vf-button-sm">{_esc(submit_label)}</button>'
        + '<button type="button" class="vf-button vf-button-ghost vf-button-sm" '
        'data-service-form-cancel>Cancel</button>'
        + "</div>"
        + "</form>"
    )


def _normalize_caps(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(c).strip() for c in value if str(c).strip()]
    return []


def _service_crud_panel(services: list[Mapping[str, Any]]) -> str:
    """The operator-managed backends panel: add form, restart banner, list.

    Backends are durable in admin.db; a change applies on the next gateway
    start (no in-process hot reload), so an honest banner says so after edits.
    """
    add_button = (
        '<button type="button" class="vf-button vf-button-sm" '
        'data-service-add>+ Add service</button>'
    )
    return (
        '<div class="service-crud" data-service-crud>'
        '<div class="service-crud-head">'
        '<p class="service-crud-intro">Services are the upstream/local systems '
        "this gateway calls (a model server, ComfyUI, TTS/STT, an "
        "OpenAI-compatible endpoint). They are stored in the gateway database; "
        "add, edit, enable, or remove one here, and changes take effect when the "
        "gateway restarts.</p>"
        + add_button
        + "</div>"
        '<div id="service-restart-banner" class="notice" hidden>'
        "Saved. Restart the gateway to apply service changes.</div>"
        '<div id="service-form-host">'
        + _service_form()
        + "</div>"
        '<div id="service-crud-message" class="message"></div>'
        "</div>"
    )


# Client helpers for the backend CRUD UI, spliced into the admin page script
# (which provides json(), refreshMain(), show(), showError()). The single
# server-rendered form (#service-form-host .service-form) is reused for both add
# and edit; edit fetches the current record from the registry to pre-fill.
_SERVICE_PRESETS_JSON = json.dumps(
    {
        p["id"]: {
            "base_path": p["base_path"],
            "default_port": p["default_port"],
            "capabilities": p["capabilities"],
            "native_passthrough": p["native_passthrough"],
        }
        for p in service_registry.PROVIDER_PRESETS
    }
)

_SERVICE_CRUD_JS = (
    "const SERVICE_PRESETS=" + _SERVICE_PRESETS_JSON + ";"
    "function serviceForm(){return document.querySelector('#service-form-host .service-form')}"
    "function showServiceForm(rec){const f=serviceForm();if(!f)return;f.reset&&f.reset();"
    "const tr=document.getElementById('service-test-result');if(tr)tr.textContent='';"
    "if(rec){fillServiceForm(f,rec)}f.hidden=false;const n=f.querySelector('[name=name]');if(n)n.focus()}"
    "function hideServiceForm(){const f=serviceForm();if(f)f.hidden=true}"
    "function showRestartBanner(){const b=document.getElementById('service-restart-banner');if(b)b.hidden=false}"
    "function fillServiceForm(f,rec){const c=(rec&&rec.config)||{};"
    "const set=(k,v)=>{const el=f.querySelector('[name='+k+']');if(el)el.value=v==null?'':v};"
    "set('name',rec.name||c.name||'');"
    "let url='';if(c.host){url='http://'+c.host+(c.port?(':'+c.port):'')+(c.base_path||'')}set('base_url',url);"
    "set('api_key_env',c.api_key_env||'');set('note',c.note||'');set('priority',rec.priority!=null?rec.priority:'');"
    "const caps=(rec.capabilities||[]);f.querySelectorAll('[name=capabilities]').forEach(cb=>{cb.checked=caps.indexOf(cb.value)!==-1});"
    "f.querySelectorAll('[name=role]').forEach(rb=>{rb.checked=rb.value===(rec.role||'required')});"
    "const en=f.querySelector('[name=enabled]');if(en)en.checked=rec.enabled!==false;"
    "const mg=f.querySelector('[name=managed]');if(mg)mg.checked=!!(rec.lifecycle&&rec.lifecycle.managed)}"
    "function applyServicePreset(id){const p=SERVICE_PRESETS[id];const f=serviceForm();if(!p||!f)return;"
    "const u=f.querySelector('[name=base_url]');if(u&&!u.value){u.value='http://127.0.0.1'+(p.default_port?(':'+p.default_port):'')+(p.base_path||'')}"
    "if(p.capabilities){f.querySelectorAll('[name=capabilities]').forEach(cb=>{cb.checked=p.capabilities.indexOf(cb.value)!==-1})}}"
    "function serviceFormBody(fd){const body={};body.name=String(fd.get('name')||'').trim();"
    "const url=String(fd.get('base_url')||'').trim();if(url)body.base_url=url;"
    "const preset=fd.get('preset');if(preset)body.preset=preset;"
    "const env=String(fd.get('api_key_env')||'').trim();if(env)body.api_key_env=env;"
    "const note=String(fd.get('note')||'').trim();if(note)body.note=note;"
    "const pr=String(fd.get('priority')||'').trim();if(pr)body.priority=Number(pr);"
    "body.capabilities=fd.getAll('capabilities');body.role=fd.get('role')||'required';"
    "body.enabled=fd.get('enabled')!=null;body.managed=fd.get('managed')!=null;return body}"
    "async function editService(id){try{const r=await fetch('/admin/api/operator/service-registry',{credentials:'same-origin',cache:'no-store',headers:{'x-ai-gateway-admin-action':'1','x-ai-gateway-csrf':csrfToken}});const data=await r.json();"
    "const reg=data.service_registry||data;const rec=(reg.services||{})[id];if(!rec){showError({error:{message:'service not found'}});return}"
    "showServiceForm(null);const f=serviceForm();if(!f)return;f.dataset.serviceForm='edit';f.dataset.serviceId=id;"
    "fillServiceForm(f,rec)}catch(err){showError(err)}}"
    "async function testServiceConnection(){const f=serviceForm();if(!f)return;const fd=new FormData(f);const body=serviceFormBody(fd);"
    "const out=document.getElementById('service-test-result');if(out){out.textContent='Testing\\u2026';out.className='service-test-result'}"
    "try{const data=await json('/admin/api/operator/services/test',body);const p=data.probe||{};"
    "if(out){out.className='service-test-result '+(p.ok?'good':'bad');out.textContent=p.ok?('Reachable (HTTP '+p.status+')'):('Unreachable: '+(p.error||'no response'))}}"
    "catch(err){if(out){out.className='service-test-result bad';out.textContent='Test failed: '+((err&&err.error&&err.error.message)||err)}}}"
)


def _config_source_row(record: Mapping[str, Any]) -> str:
    label = _esc(_safe_config_text(record.get("label") or record.get("name") or record.get("key")))
    domain = _esc(_safe_config_text(record.get("domain") or record.get("scope")))
    live_state = _esc(
        _safe_config_text(
            record.get("current_value_summary")
            or record.get("current")
            or record.get("value")
            or record.get("status")
            or "loaded"
        )
    )
    change_path = _esc(_config_change_path(record))
    apply_label = _esc(_apply_mode_label(record))
    recommendation = _esc(
        _safe_config_text(
            record.get("recommendation") or record.get("note") or record.get("description"),
            record=record,
        )
    )
    # Three columns the operator can act on: the setting, what loaded, and how to
    # change it (apply-mode + path). The provenance taxonomy (source-of-truth
    # badge, authored/stored, "in provisioning bundle") is forensic detail an
    # operator rarely acts on — dropped from the default view.
    return (
        "<tr>"
        f'<td><div class="cell-main">{label}</div><div class="cell-sub">{domain}</div></td>'
        f'<td><div class="cell-main">{live_state}</div><div class="cell-sub">{apply_label}</div></td>'
        f'<td><div class="cell-main">{change_path}</div><div class="cell-sub">{recommendation}</div></td>'
        "</tr>"
    )


def _apply_mode_label(record: Mapping[str, Any]) -> str:
    explicit = str(record.get("apply_mode") or "").strip().lower()
    label = {
        "live": "Applies live",
        "restart": "Restart required",
        "reload_or_restart": "Reload or restart",
        "read_only": "Read-only here",
        "split": "Live + restart (split)",
    }.get(explicit, "")
    if label:
        return label
    # Fallback to legacy editability rendering when apply_mode is absent.
    if bool(record.get("restart_required")):
        return "Restart required"
    return _editability_label(record)


def _config_source_plane(record: Mapping[str, Any]) -> str:
    plane = str(record.get("plane") or "").strip().lower()
    if plane:
        return plane
    owner = str(record.get("ui_owner") or record.get("ownership") or "").strip().lower()
    source = str(record.get("source") or record.get("kind") or "").strip().lower()
    if owner in {"ui", "admin", "dashboard"} or source in {"ui", "admin", "admin.db"}:
        return "dashboard"
    if owner in {"file", "env"} or source in {"file", "env", "yaml", "proxy_config.yml"}:
        return "bootstrap"
    if owner == "manifest" or source == "manifest":
        return "provisioned"
    return "split"


def _config_plane_label(record: Mapping[str, Any], plane: str) -> str:
    raw = record.get("plane_label")
    if raw:
        return str(raw)
    return {
        "dashboard": "Managed here",
        "bootstrap": "Bootstrap config",
        "provisioned": "Provisioned artifacts",
        "manifest": "Provisioned artifacts",
        "split": "Split ownership",
        "mixed": "Split ownership",
    }.get(plane, "Other")


def _editability_label(record: Mapping[str, Any]) -> str:
    value = str(
        record.get("editability")
        or record.get("ownership")
        or record.get("mode")
        or ("read-only" if record.get("readonly") else "")
    ).strip().lower()
    return {
        "runtime": "Editable live",
        "restart": "Restart required",
        "restart required": "Restart required",
        "read_only": "Read-only here",
        "read-only": "Read-only here",
        "advanced": "Advanced external config",
        "ui-owned": "Editable live",
    }.get(value, _safe_config_text(value))


def _config_change_path(record: Mapping[str, Any]) -> str:
    explicit = record.get("change_path")
    if explicit:
        return _safe_config_text(explicit)
    plane = _config_source_plane(record)
    source = _safe_config_text(record.get("source") or record.get("kind") or "source")
    source_ref = _safe_config_text(
        record.get("source_ref")
        or record.get("key")
        or (record.get("label") if source == "env" else "")
    )
    editability = str(record.get("editability") or "").strip().lower()
    if plane == "dashboard":
        return "Manage in this dashboard."
    if plane == "bootstrap":
        if source == "env" and source_ref != "—":
            return f"Set {source_ref} in the service environment and restart."
        if source_ref != "—":
            return f"Edit {source} field {source_ref} and restart."
        return "Edit the provisioned file/env value and restart."
    if plane in {"provisioned", "manifest"}:
        return "Edit the manifest/catalog source; reload or restart if needed."
    if editability == "runtime":
        return "Use dashboard controls for runtime overrides."
    return "Use dashboard controls for runtime overrides; edit bootstrap fields outside the dashboard."


def _config_sources_panel(records: Iterable[Mapping[str, Any]]) -> str:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        grouped.setdefault(_config_source_plane(record), []).append(record)
    if not grouped:
        return ""
    order = ("dashboard", "bootstrap", "provisioned", "manifest", "split", "mixed")
    intros = {
        "dashboard": "Runtime policy and operator choices stored by the gateway and editable here.",
        "bootstrap": "Values needed before the gateway starts. Change them in files/env, then restart or reload.",
        "provisioned": "Versioned workflow, model, and capability definitions. The dashboard shows what loaded.",
        "manifest": "Versioned workflow, model, and capability definitions. The dashboard shows what loaded.",
        "split": "Controls with both runtime knobs and provisioned bootstrap fields.",
        "mixed": "Controls with both runtime knobs and provisioned bootstrap fields.",
    }
    sections: list[str] = []
    for plane in order:
        rows = grouped.pop(plane, [])
        if not rows:
            continue
        title = _config_plane_label(rows[0], plane)
        table = _table(
            ["Setting", "Loaded value", "How to change it"],
            (_config_source_row(row) for row in rows),
        )
        sections.append(
            '<div class="source-plane">'
            f"<h4>{_esc(title)}</h4>"
            f"<p>{_esc(intros.get(plane, 'Configuration sources for this gateway.'))}</p>"
            + table
            + "</div>"
        )
    for plane, rows in sorted(grouped.items()):
        table = _table(
            ["Setting", "Loaded value", "How to change it"],
            (_config_source_row(row) for row in rows),
        )
        sections.append(
            '<div class="source-plane">'
            f"<h4>{_esc(_config_plane_label(rows[0], plane))}</h4>"
            + table
            + "</div>"
        )
    return '<div class="source-plane-list">' + "".join(sections) + "</div>"


def _operator_control_title(record: Mapping[str, Any], fallback: str) -> str:
    return str(record.get("label") or record.get("name") or record.get("key") or fallback)


def _operator_control_summary(record: Mapping[str, Any]) -> str:
    parts = []
    for label, keys in (
        ("Current", ("current", "value", "mode", "enabled")),
        ("Default", ("default", "default_value")),
        ("Scope", ("scope", "domain", "service")),
        ("Owner", ("owner", "source")),
        ("Status", ("status",)),
    ):
        value = next((record.get(key) for key in keys if record.get(key) not in (None, "", [], {}, ())), "")
        formatted = _format_key_value_summary(value)
        if formatted:
            parts.append(f"{label}: {_esc(formatted)}")
    return "; ".join(parts) or _esc(record.get("summary") or record.get("note") or "Configured")


def _operator_control_form(record: Mapping[str, Any]) -> str:
    action = record.get("action_url") or record.get("action")
    form_kind = str(record.get("form") or "")
    if action and form_kind == "service_availability":
        service_id = str(record.get("service_id") or record.get("key") or "").strip()
        if not service_id:
            return ""
        current = str(record.get("current") or record.get("state") or "available")
        reason = _esc(record.get("reason") or "")
        retry_after = record.get("retry_after_seconds")
        retry_value = "" if retry_after in (None, "") else _esc(retry_after)
        state_options = []
        for value, label in (
            ("available", "Available"),
            ("degraded", "Degraded"),
            ("draining", "Draining"),
            ("maintenance", "Maintenance"),
            ("disabled", "Disabled"),
        ):
            selected = " selected" if value == current else ""
            state_options.append(
                f'<option value="{value}"{selected}>{label}</option>'
            )
        method = _esc(record.get("method") or "PUT")
        prefix = f"overrides.{service_id}."
        return (
            '<form class="inline-form" method="post" action="'
            + _esc(action)
            + '" data-operator-control data-method="'
            + method
            + '">'
            f'<select name="{_esc(prefix)}state" aria-label="Service mode">'
            + "".join(state_options)
            + "</select>"
            f'<input name="{_esc(prefix)}reason" value="{reason}" placeholder="Reason" aria-label="Reason">'
            f'<input name="{_esc(prefix)}retry_after_seconds" value="{retry_value}" type="number" min="0" placeholder="Retry seconds" aria-label="Retry after seconds">'
            '<button type="submit">Update</button>'
            "</form>"
        )
    if action and form_kind == "media_job_admission":
        current = record.get("current") if isinstance(record.get("current"), Mapping) else record
        disabled_intents = _esc(", ".join(str(v) for v in current.get("disabled_intents", []) or []))
        disabled_resources = _esc(", ".join(str(v) for v in current.get("disabled_resource_classes", []) or []))
        reserved_tokens = _esc(", ".join(str(v) for v in current.get("reserved_tokens", []) or []))
        max_queue_depth = current.get("max_queue_depth")
        retry_after = current.get("retry_after_seconds")
        max_queue_value = "" if max_queue_depth in (None, "") else _esc(max_queue_depth)
        retry_value = "" if retry_after in (None, "") else _esc(retry_after)
        method = _esc(record.get("method") or "PUT")
        return (
            '<form class="inline-form" method="post" action="'
            + _esc(action)
            + '" data-operator-control data-method="'
            + method
            + '" data-list-fields="disabled_intents disabled_resource_classes reserved_tokens">'
            f'<input name="disabled_intents" value="{disabled_intents}" placeholder="Disabled intents" aria-label="Disabled intents">'
            f'<input name="disabled_resource_classes" value="{disabled_resources}" placeholder="Disabled resources" aria-label="Disabled resource classes">'
            f'<input name="reserved_tokens" value="{reserved_tokens}" placeholder="Reserved tokens" aria-label="Reserved tokens">'
            f'<input name="max_queue_depth" value="{max_queue_value}" type="number" min="0" placeholder="Max queue" aria-label="Max queue depth">'
            f'<input name="retry_after_seconds" value="{retry_value}" type="number" min="0" placeholder="Retry seconds" aria-label="Retry after seconds">'
            '<button type="submit">Update</button>'
            "</form>"
        )
    supported_values = (
        record.get("supported_values")
        or record.get("allowed_values")
        or record.get("options")
    )
    if not action or not isinstance(supported_values, (list, tuple)) or not supported_values:
        return ""
    name = str(record.get("field") or "value")
    current = str(record.get("current") or record.get("value") or record.get("mode") or "")
    options = []
    for option in supported_values:
        option_value = str(option.get("value") if isinstance(option, Mapping) else option)
        option_label = str(option.get("label") if isinstance(option, Mapping) else option)
        selected = " selected" if option_value == current else ""
        options.append(
            f'<option value="{_esc(option_value)}"{selected}>{_esc(option_label)}</option>'
        )
    method = _esc(record.get("method") or "PATCH")
    return (
        '<form class="inline-form" method="post" action="'
        + _esc(action)
        + '" data-operator-control data-method="'
        + method
        + '">'
        f'<select name="{_esc(name)}" aria-label="Operator control value">'
        + "".join(options)
        + "</select>"
        '<button type="submit">Update</button>'
        "</form>"
    )


def _operator_controls_panel(groups: Mapping[str, Any]) -> str:
    controls: list[str] = []
    for group_label, records in groups.items():
        if isinstance(records, Mapping):
            iterable = [
                {"key": key, **value} if isinstance(value, Mapping) else {"key": key, "value": value}
                for key, value in records.items()
            ]
        elif isinstance(records, (list, tuple)):
            iterable = [record for record in records if isinstance(record, Mapping)]
        else:
            iterable = []
        for index, record in enumerate(iterable, start=1):
            title = _esc(_operator_control_title(record, f"{group_label} {index}"))
            summary = _operator_control_summary(record)
            controls.append(
                '<div class="operator-control">'
                f'<div><div class="cell-main">{title}</div>'
                f'<div class="cell-sub">{_esc(group_label)} override</div></div>'
                f"<p>{summary}</p>"
                + _operator_control_form(record)
                + "</div>"
            )
    if not controls:
        return ""
    return '<div class="operator-control-list">' + "".join(controls) + "</div>"


def _workflow_inventory_row(record: Mapping[str, Any]) -> str:
    workflow_id = _esc(record.get("id") or "—")
    label = _esc(record.get("label") or record.get("display_name") or workflow_id)
    description = _esc(record.get("description") or "")
    intent = _esc(record.get("intent") or "—")
    backends = _format_optional_list(record.get("backends") or record.get("supported_backends"))
    tier = _esc(record.get("resource_tier") or "—")
    trust = _esc(record.get("trust_tier") or "—")
    return (
        "<tr>"
        f'<td><div class="cell-main">{label}</div><div class="cell-sub"><code>{workflow_id}</code></div>'
        f'<div class="cell-sub">{description}</div></td>'
        f"<td>{intent}</td>"
        f"<td>{backends}</td>"
        f"<td>{tier}</td>"
        f"<td>{trust}</td>"
        "</tr>"
    )


def _extension_package_row(record: Mapping[str, Any]) -> str:
    package_id = _esc(record.get("id") or "—")
    name = _esc(record.get("name") or package_id)
    version = _esc(record.get("version") or "—")
    enabled = bool(record.get("enabled"))
    status_class = "active" if enabled else "revoked"
    status = "enabled" if enabled else "disabled"
    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
    source_label = _esc(source.get("path") or source.get("url") or source.get("directory") or "—")
    counts = _format_key_value_summary(record.get("object_counts") or {})
    services = _format_optional_list(record.get("services"))
    frontends = _format_optional_list(record.get("protocol_frontends"))
    workflows = _format_optional_list(record.get("workflows"))
    capabilities = _format_optional_list(record.get("capabilities"))
    warnings = _format_optional_list(record.get("warnings"))
    code_objects = _format_optional_list(_extension_code_objects(record))
    code_notice = (
        "code staged for admin review; enable records data, not arbitrary GitHub execution"
        if code_objects != "—"
        else ""
    )
    details = "; ".join(
        part
        for part in (
            f"objects: {counts}" if counts else "",
            f"services: {services}" if services != "—" else "",
            f"frontends: {frontends}" if frontends != "—" else "",
            f"workflows: {workflows}" if workflows != "—" else "",
            f"capabilities: {capabilities}" if capabilities != "—" else "",
            f"staged code: {code_objects}" if code_objects != "—" else "",
            code_notice,
            f"warnings: {warnings}" if warnings != "—" else "",
        )
        if part
    )
    return (
        "<tr>"
        f'<td><div class="cell-main">{name}</div><div class="cell-sub"><code>{package_id}</code></div></td>'
        f"<td>{version}</td>"
        f'<td><span class="badge {status_class}">{status}</span></td>'
        f"<td><code>{source_label}</code></td>"
        f"<td>{_esc(details or '—')}</td>"
        "</tr>"
    )


def _extension_package_panel(index: Mapping[str, Any] | None) -> str:
    packages = index.get("packages") if isinstance(index, Mapping) else {}
    if not isinstance(packages, Mapping):
        packages = {}
    package_rows = _table(
        ["Package", "Version", "Status", "Source", "Objects"],
        (
            _extension_package_row({"id": package_id, **package})
            for package_id, package in sorted(packages.items())
            if isinstance(package, Mapping)
        ),
    )
    form = (
        '<form class="flow" method="post" action="/admin/api/extensions/validate" '
        'data-extension-package data-enable-action="/admin/api/extensions/enable">'
        "<label>Validate or enable extension package</label>"
        '<p class="form-note">Local directories and Git/GitHub URLs are staged for review. '
        "Data-only packages can be live-enabled into registry records; packages with code-bearing adapters or translators show staged code and require an explicit trust review before any loader uses that code.</p>"
        '<div class="row">'
        '<select name="source_type" aria-label="Extension package source type">'
        '<option value="local_directory">Local directory</option>'
        '<option value="git">Git / GitHub URL</option>'
        "</select>"
        '<input type="text" name="path" placeholder="/path/to/extension-package" aria-label="Extension package directory">'
        '<input type="text" name="url" placeholder="https://github.com/user/package.git" aria-label="Git package URL">'
        '<input type="text" name="ref" placeholder="tag, branch, or commit" aria-label="Git ref">'
        '<input type="text" name="subdir" placeholder="optional subdir" aria-label="Git package subdir">'
        '<div class="inline-form">'
        '<button type="submit" value="validate">Validate</button>'
        '<button type="submit" value="enable" data-enable-extension-package>Enable</button>'
        "</div>"
        "</div>"
        "</form>"
    )
    return package_rows + form


def _identity_mapping_row(record: Mapping[str, Any]) -> str:
    mapping_id = str(record.get("id") or "")
    source = _esc(record.get("source") or "—")
    profile = _esc(record.get("profile") or record.get("profile_id") or "default-local")
    scope = _esc(record.get("scope") or "runtime default")
    note = _esc(record.get("note") or "")
    action = (
        "—"
        if not mapping_id
        else (
            '<button class="inline" type="button" data-disable-runtime-mapping="'
            + _esc(mapping_id)
            + '">Remove</button>'
        )
    )
    return (
        "<tr>"
        f"<td>{source}</td>"
        f"<td>{scope}</td>"
        f"<td><code>{profile}</code></td>"
        f"<td>{note}</td>"
        f"<td>{action}</td>"
        "</tr>"
    )


def _model_visibility_panel(model_visibility: Mapping[str, Any] | None) -> str:
    if not model_visibility:
        return ""
    facts = []
    for label, keys in (
        ("Mode", ("mode",)),
        ("Loaded LLM", ("loaded_model", "current_model")),
        ("Models API", ("list_behavior",)),
        ("Routing", ("route_behavior",)),
        ("Constraints", ("constraints", "resource_mode")),
    ):
        value = next((model_visibility.get(key) for key in keys if model_visibility.get(key)), "")
        formatted = _format_key_value_summary(value)
        if formatted:
            facts.append(f"<div><dt>{label}</dt><dd>{_esc(formatted)}</dd></div>")
    if not facts:
        return ""
    return "".join(
        [
            '<div class="info-panel">',
            "<h3>LLM model visibility</h3>",
            '<dl class="facts">',
            *facts,
            "</dl>",
            "</div>",
        ]
    )


# Ordered admin sections: (section_id, sidebar label). The sidebar shows one
# section at a time (research-backed: a left sidebar of grouped destinations
# scans faster and scales better than one long anchor-linked page).
ADMIN_SECTIONS: tuple[tuple[str, str], ...] = (
    ("apps-keys", "Apps & Keys"),
    ("profiles", "Profiles"),
    ("identity", "Identity"),
    ("services", "Services"),
    ("apis", "APIs"),
    ("admin-keys", "Admin Keys"),
    ("activity", "Activity"),
)


def _admin_sidebar(brand_noun: str = "AI Gateway") -> str:
    """A real left sidebar of admin sections, one visible at a time.

    Replaces the old single-page scroll + anchor-link strip. The first section
    is active on load; a small script switches sections and persists the
    choice. The `opk_`/`oak_` legend moves to an on-demand hint, not chrome.
    """
    links = "".join(
        f'<a class="admin-nav-link" data-admin-nav="{_esc(sid)}" '
        f'href="#{_esc(sid)}">{_esc(label)}</a>'
        for sid, label in ADMIN_SECTIONS
    )
    return (
        '<aside class="admin-sidebar" aria-label="Admin sections">'
        '<div class="admin-sidebar-head">'
        # One landmark heading, no decorative eyebrow or vibe title — the global
        # header already shows the brand and that you're in admin.
        '<h1 id="operator-title">Admin</h1>'
        "</div>"
        f'<nav class="admin-nav">{links}</nav>'
        # No "Key types" legend: operators act via buttons and the key is shown
        # once at creation — the internal opk_/oak_ prefixes were chrome that
        # only taught two tokens nobody types.
        "</aside>"
    )


# Sidebar nav: show one section at a time, persist the choice, honor #hash
# deep-links. Kept tiny and dependency-free; the admin page already ships inline
# script for its flows.
_ADMIN_NAV_SCRIPT = """
<script>
// Exposed as window.__adminNavInit so the admin's refreshMain (which replaces
// <main> after each action) can re-wire the sidebar without a full reload.
window.__adminNavInit = function () {
  const KEY = "vivid_fish_admin_section";
  const links = Array.from(document.querySelectorAll("[data-admin-nav]"));
  const sections = Array.from(document.querySelectorAll("[data-admin-section]"));
  if (!links.length || !sections.length) return;
  const ids = sections.map((s) => s.getAttribute("data-admin-section"));

  function show(id) {
    if (!ids.includes(id)) id = ids[0];
    sections.forEach((s) => {
      s.classList.toggle("is-active", s.getAttribute("data-admin-section") === id);
    });
    links.forEach((a) => {
      const on = a.getAttribute("data-admin-nav") === id;
      a.classList.toggle("is-active", on);
      if (on) a.setAttribute("aria-current", "true");
      else a.removeAttribute("aria-current");
    });
    try { localStorage.setItem(KEY, id); } catch (_) {}
  }

  links.forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      show(a.getAttribute("data-admin-nav"));
    });
  });

  // Attention-list rows jump to the section that fixes the issue.
  Array.from(document.querySelectorAll("[data-admin-jump]")).forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const target = a.getAttribute("data-admin-jump");
      if (ids.includes(target)) show(target);
    });
  });

  const hash = (location.hash || "").replace(/^#/, "");
  let stored = null;
  try { stored = localStorage.getItem(KEY); } catch (_) {}
  show(ids.includes(hash) ? hash : (stored || ids[0]));
};
window.__adminNavInit();
</script>
"""


def _section(title: str, body: str, *, section_id: str = "", intro: str = "") -> str:
    if not body.strip():
        return ""
    id_attr = f' id="{_esc(section_id)}"' if section_id else ""
    # Explanation is on demand, not a mandatory preamble: the section leads with
    # its data/actions, and "What's this?" reveals the intro for anyone who
    # wants it (progressive disclosure).
    intro_html = (
        '<details class="section-help"><summary>What’s this?</summary>'
        f"<p>{_esc(intro)}</p></details>"
        if intro
        else ""
    )
    return (
        f'<section class="admin-section"{id_attr} data-admin-section="'
        f'{_esc(section_id)}">'
        f"<h2>{_esc(title)}</h2>{intro_html}{body}</section>"
    )


def _subsection(title: str, body: str, *, intro: str = "") -> str:
    if not body.strip():
        return ""
    intro_html = f'<p class="section-intro">{_esc(intro)}</p>' if intro else ""
    return f'<div class="subsection"><h3>{_esc(title)}</h3>{intro_html}{body}</div>'


def _table(headers: Iterable[str], rows: Iterable[str]) -> str:
    headers_list = list(headers)
    header_html = "".join(f"<th>{_esc(h)}</th>" for h in headers_list)
    rows_list = list(rows)
    if not rows_list:
        return ""
    body_html = "".join(rows_list)
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"


def _attention_panel(items: list[Mapping[str, Any]]) -> str:
    """The operator's landing list: "things that want a look".

    Replaces a stat-grid (which costs attention on every visit to say "all is
    well"). When everything's healthy this is a single calm "all clear" line;
    otherwise it's a short, ranked list of what's actually broken or incomplete,
    each linking to the section that fixes it.
    """
    if not items:
        return (
            '<div class="attention-panel clear">'
            '<span class="vf-status-dot" aria-hidden="true"></span>'
            "<div><strong>Everything looks healthy.</strong>"
            "<p>No Apps or services need attention right now.</p></div>"
            "</div>"
        )
    rows = []
    for item in items:
        severity = str(item.get("severity") or "attention")
        dot = (
            "vf-status-dot-bad" if severity == "problem" else "vf-status-dot-warn"
        )
        section = str(item.get("section") or "apps-keys")
        title = _esc(item.get("title") or "Needs a look")
        detail = _esc(item.get("detail") or "")
        rows.append(
            f'<a class="attention-row" href="#{_esc(section)}" '
            f'data-admin-jump="{_esc(section)}">'
            f'<span class="vf-status-dot {dot}" aria-hidden="true"></span>'
            f"<span class=\"attention-text\"><strong>{title}</strong>"
            f"<span class=\"attention-detail\">{detail}</span></span>"
            "</a>"
        )
    count = len(items)
    heading = f"{count} thing{'s' if count != 1 else ''} want{'s' if count == 1 else ''} a look"
    return (
        '<div class="attention-panel">'
        f'<p class="attention-heading">{heading}</p>'
        + "".join(rows)
        + "</div>"
    )


def _start_card(apps: list[Mapping[str, Any]]) -> str:
    if apps:
        return ""
    return (
        '<div class="start-card">'
        "<h3>No App keys yet</h3>"
        "<p>Create an App for the tool or person first, then mint one or more "
        "keys under that App for rotation and cleanup.</p>"
        '<a href="#create-app">Create App</a>'
        "</div>"
    )


def _flow_form(
    apps: list[Mapping[str, Any]],
    users: list[Mapping[str, Any]],
    profiles: list[Mapping[str, Any]],
    *,
    create_app_action: str,
    create_key_action_template: str,
) -> str:
    app_options = "".join(
        f'<option value="{_esc(app.get("id"))}">'
        f'{_esc(app.get("name") or app.get("id"))}'
        "</option>"
        for app in apps
    )
    user_options = '<option value="">No user owner (shared App)</option>' + "".join(
        f'<option value="{_esc(user.get("id"))}">'
        f'{_esc(user.get("email") or user.get("name") or user.get("id"))}'
        "</option>"
        for user in users
    )
    profile_options = _profile_options(profiles, include_default=True)
    create_form = (
        '<form class="flow" id="create-app" method="post" action="'
        + _esc(create_app_action)
        + '" data-create-app>'
        "<label>Create App</label>"
        '<p class="form-note">An App is the thing using the gateway: LibreChat, Open WebUI, Continue, a bot, a script, or a person-owned client.</p>'
        '<div class="stack">'
        '<input type="text" name="name" placeholder="App name, e.g. LibreChat" aria-label="App name" required>'
        '<div class="choice-grid" aria-label="App ownership">'
        '<label class="choice-card"><span><input type="radio" name="kind" value="service" checked>'
        '<strong>Shared App</strong></span><span>No user owner; use this for LibreChat, bots, and household tools.</span></label>'
        '<label class="choice-card"><span><input type="radio" name="kind" value="personal">'
        '<strong>Personal/user-owned</strong></span><span>Attach the App to a user for individual access.</span></label>'
        "</div>"
        f'<select name="owner_user_id" aria-label="User owner for personal App">{user_options}</select>'
        f'<select name="profile_id" aria-label="Existing Profile for App">{profile_options}</select>'
        "</div>"
        '<div class="row">'
        "<span></span>"
        '<button type="submit">Create App</button>'
        "</div>"
        "</form>"
    )
    if not app_options:
        return create_form

    mint_form = (
        '<form class="flow" method="post" data-action-template="'
        + _esc(create_key_action_template)
        + '" data-mint-key>'
        "<label>Create key for existing App</label>"
        '<p class="form-note">Keys are credentials under an App. Revoke one key without deleting the App or changing its Profile.</p>'
        '<div class="stack">'
        f'<select name="app_id" aria-label="App for App API key" required>{app_options}</select>'
        '<input type="text" name="label" placeholder="Key label (optional)" aria-label="Key label">'
        "</div>"
        '<div class="row">'
        "<span></span>"
        '<button type="submit">Create key</button>'
        "</div>"
        "</form>"
    )
    return create_form + mint_form


def _admin_key_form(*, create_admin_key_action: str, can_mint_owner: bool) -> str:
    owner_option = (
        '<option value="owner">Owner</option>'
        if can_mint_owner
        else '<option value="owner" disabled>Owner (owner role required)</option>'
    )
    return (
        '<form class="flow" method="post" action="'
        + _esc(create_admin_key_action)
        + '" data-create-admin-key>'
        "<label>Create admin automation key</label>"
        '<p class="hint">A key for scripts that manage Apps, Profiles, keys, status, and audit data — '
        "not for runtime model calls.</p>"
        '<div class="stack">'
        '<input type="text" name="name" placeholder="Automation name, e.g. key-rotation-script" aria-label="Admin automation name" required>'
        '<select name="role" aria-label="Admin automation role">'
        '<option value="admin">Admin</option>'
        + owner_option
        + "</select>"
        "</div>"
        '<div class="row">'
        '<span class="hint">Shown once at creation. Keep it out of chat apps, prompts, logs, and shared code.</span>'
        '<button type="submit">Create admin key</button>'
        "</div>"
        "</form>"
    )


def _runtime_mapping_form(
    profiles: list[Mapping[str, Any]],
    *,
    create_mapping_action: str,
) -> str:
    profile_options = _profile_options(profiles, include_default=True, default_label="Built-in default")
    return (
        '<form class="flow" method="post" action="'
        + _esc(create_mapping_action)
        + '" data-create-runtime-mapping>'
        "<label>Add runtime Profile mapping</label>"
        '<p class="form-note">Map signed-in users, OIDC groups, OIDC roles, email domains, or exact emails to a default runtime Profile. This does not grant admin access.</p>'
        '<p class="form-note">When more than one mapping matches, the first match wins: exact email, OIDC group, OIDC role, email domain, gateway role, all signed-in users.</p>'
        '<div class="stack">'
        '<select name="source_kind" aria-label="Identity source type">'
        '<option value="oidc_group">OIDC group</option>'
        '<option value="oidc_role">OIDC role</option>'
        '<option value="email">Exact email</option>'
        '<option value="domain">Email domain</option>'
        '<option value="gateway_role">Gateway admin role</option>'
        '<option value="all_users">All signed-in users</option>'
        "</select>"
        '<input type="text" name="source_value" placeholder="trusted-users, person@example.com, or example.com" aria-label="Identity source value">'
        f'<select name="profile_id" aria-label="Default runtime Profile">{profile_options}</select>'
        "</div>"
        '<div class="row">'
        '<span class="hint">For all signed-in users, the source value is ignored.</span>'
        '<button type="submit">Add mapping</button>'
        "</div>"
        "</form>"
    )


def _profile_form(
    profiles: list[Mapping[str, Any]],
    *,
    create_profile_action: str,
    model_visibility: Mapping[str, Any] | None = None,
    policy_catalog: Mapping[str, list[Mapping[str, Any]]] | None = None,
    profile_inventory: Mapping[str, Any] | None = None,
) -> str:
    catalog = policy_catalog or profile_policy_catalog()
    inventory = profile_inventory or {}
    protocol_grid = _option_grid(
        name="protocols",
        options=_visible_catalog_options(catalog, "protocols", flag="profile_form"),
        aria_label="Allowed APIs",
    )
    intent_grid = _option_grid(
        name="intents",
        options=_catalog_options(catalog, "intents"),
        aria_label="Allowed work",
    )
    cost_grid = _option_grid(
        name="resource_tiers",
        options=_catalog_options(catalog, "resource_tiers"),
        aria_label="Allowed resource tiers",
    ) + _option_grid(
        name="exposure",
        options=_catalog_options(catalog, "exposure"),
        aria_label="Allowed exposure classes",
    ) + _option_grid(
        name="trust_tiers",
        options=_catalog_options(catalog, "trust_tiers"),
        aria_label="Allowed trust tiers",
    )
    target_visibility_grid = _radio_grid(
        name="target_visibility",
        options=_catalog_options(catalog, "target_visibility"),
        aria_label="Target visibility",
    )
    target_activation_grid = _radio_grid(
        name="target_activation",
        options=_catalog_options(catalog, "target_activation"),
        aria_label="Target activation",
    )
    status_options = "".join(
        '<option value="' + _esc(option.get("id") or "") + '"'
        + (" selected" if option.get("default") else "")
        + ">"
        + _esc(_catalog_option_label(option))
        + "</option>"
        for option in _catalog_options(catalog, "status_visibility")
        if option.get("id")
    )
    if not status_options:
        status_options = '<option value="basic">Basic status</option>'
    model_items = _inventory_items(inventory, "models")
    workflow_items = _inventory_items(inventory, "workflows")
    backend_items = _inventory_items(inventory, "backends")
    preview_apis = _catalog_default_summary(catalog, "protocols")
    preview_work = _catalog_default_summary(catalog, "intents")
    preview_cost = (
        _catalog_default_summary(catalog, "resource_tiers")
        + "; "
        + _catalog_default_summary(catalog, "exposure")
        + "; "
        + _catalog_default_summary(catalog, "trust_tiers")
    )
    preview_targets = (
        _catalog_default_summary(catalog, "target_visibility")
        + "; "
        + _catalog_default_summary(catalog, "target_activation")
    )
    return (
        '<form class="flow" method="post" action="'
        + _esc(create_profile_action)
        + '" data-create-profile>'
        + "<label>Create Profile</label>"
        + '<p class="form-note">Name the policy, choose what it can use, then assign it to Apps.</p>'
        + '<div class="stack">'
        + '<input type="text" name="label" placeholder="Profile name, e.g. Daisy full runtime" aria-label="Profile name" required>'
        + "</div>"
        + '<div class="form-section"><strong>APIs</strong>'
        + '<p class="form-note">Choose the client-facing APIs this Profile can call.</p>'
        + protocol_grid
        + "</div>"
        + '<div class="form-section"><strong>Allowed work</strong>'
        + '<p class="form-note">Select the capabilities this Profile can use. Admin access is never granted here.</p>'
        + intent_grid
        + "</div>"
        # Cost/release gates and target policy have sensible defaults — most
        # profiles accept them. Tuck them behind a disclosure so the common
        # case is name + APIs + Allowed work, not four mandatory-looking groups.
        # (The "Current loaded LLM" echo is dropped — it's status, not an input.)
        + '<details class="panel"><summary>Cost &amp; release gates and target policy</summary>'
        + '<div class="form-section"><strong>Cost and release gates</strong>'
        + cost_grid
        + "</div>"
        + '<div class="form-section"><strong>Target policy</strong>'
        + '<p class="form-note">Choose whether clients see the allowed catalog or only currently available targets, and whether requests may activate cold targets.</p>'
        + target_visibility_grid
        + target_activation_grid
        + "</div>"
        + "</details>"
        + '<div class="policy-preview" id="profile-policy-preview" aria-live="polite">'
        + "<strong>Profile preview</strong>"
        + '<span data-profile-preview-apis>APIs: '
        + _esc(preview_apis)
        + "</span>"
        + '<span data-profile-preview-work>Work: '
        + _esc(preview_work)
        + "</span>"
        + '<span data-profile-preview-cost>Cost gates: '
        + _esc(preview_cost)
        + "</span>"
        + '<span data-profile-preview-models>Targets: '
        + _esc(preview_targets)
        + "</span>"
        + '<span data-profile-preview-guardrails>Guardrails: local only; provider-native passthrough off; admin blocked</span>'
        + "</div>"
        + '<div class="form-section"><strong>Guardrails</strong>'
        + '<div class="stack">'
        + '<label class="check"><input type="checkbox" name="cloud_allowed"> Allow cloud fallback</label>'
        + '<label class="check"><input type="checkbox" name="native_passthrough"> Allow provider-native passthrough</label>'
        + "</div></div>"
        + '<details class="panel"><summary>Advanced constraints</summary>'
        + '<p class="form-note">Optional limits for specific model ids, workflow ids, backend ids, tags, and raw policy fields. Suggestions come from current gateway inventory; typed ids are still allowed.</p>'
        + '<div class="stack">'
        + '<input type="text" name="id" placeholder="Generated from Profile name" aria-label="Profile id override">'
        + _text_input_with_datalist(
            name="models",
            placeholder="optional LLM model ids, comma separated",
            aria_label="Allowed LLM model ids",
            datalist_id="profile-model-options",
            items=model_items,
        )
        + _text_input_with_datalist(
            name="workflows",
            placeholder="optional workflow ids, comma separated",
            aria_label="Allowed workflow ids",
            datalist_id="profile-workflow-options",
            items=workflow_items,
        )
        + _text_input_with_datalist(
            name="backends",
            placeholder="optional backend ids, comma separated",
            aria_label="Allowed backend ids",
            datalist_id="profile-backend-options",
            items=backend_items,
        )
        + '<input type="text" name="tags" placeholder="optional tags: family, agents" aria-label="Policy tags">'
        + '<select name="status_visibility" aria-label="Status detail">'
        + status_options
        + "</select>"
        + "</div>"
        + '<textarea name="advanced_json" spellcheck="false" aria-label="Raw policy JSON" placeholder="{&quot;models&quot;:[&quot;local-chat&quot;]}"></textarea>'
        + "</details>"
        + '<div class="row">'
        + "<span></span>"
        + '<button type="submit">Create Profile</button>'
        + "</div>"
        + "</form>"
    )


def _oidc_value(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(str(item) for item in value if str(item).strip())
    return str(value)


def _oidc_config_form(
    config: Mapping[str, Any] | None,
    *,
    oidc_config_action: str,
) -> str:
    record = config if isinstance(config, Mapping) else {}
    enabled = bool(record.get("enabled"))
    checked = " checked" if enabled else ""
    secret_set = bool(record.get("client_secret_set")) or bool(
        record.get("client_secret") == "<redacted>"
    )
    secret_note = (
        '<p class="form-note">Client secret saved. Leave the field blank to keep it.</p>'
        if secret_set
        else '<p class="form-note">Paste the client secret once. It is stored redacted and is not shown again.</p>'
    )
    source_note = (
        '<p class="form-note">Loaded from bootstrap config. Saving here makes admin.db the live source for OIDC provider settings.</p>'
        if record.get("source") == "bootstrap"
        else ""
    )
    return (
        '<form class="flow" method="post" action="'
        + _esc(oidc_config_action)
        + '" data-oidc-config>'
        "<label>OIDC browser login</label>"
        '<p class="form-note">Controls who can sign in to this dashboard. Runtime access still comes from Profiles and identity mappings.</p>'
        + source_note
        + '<label class="check"><input type="checkbox" name="enabled"'
        + checked
        + "> Enable OIDC login</label>"
        '<div class="stack">'
        + '<input type="url" name="issuer" placeholder="Issuer URL, e.g. https://idp.example.com/realms/home" aria-label="OIDC issuer" value="'
        + _esc(_oidc_value(record, "issuer"))
        + '">'
        + '<input type="text" name="client_id" placeholder="Client ID" aria-label="OIDC client ID" value="'
        + _esc(_oidc_value(record, "client_id"))
        + '">'
        + '<input type="password" name="client_secret" placeholder="'
        + ("Leave blank to keep saved secret" if secret_set else "Client secret")
        + '" aria-label="OIDC client secret" autocomplete="off">'
        + '<input type="url" name="redirect_uri" placeholder="https://ai.vivid.fish/admin/oidc/callback" aria-label="OIDC redirect URI" value="'
        + _esc(_oidc_value(record, "redirect_uri"))
        + '">'
        + "</div>"
        + secret_note
        + '<div class="stack">'
        + '<input type="text" name="scopes" placeholder="openid email profile" aria-label="OIDC scopes" value="'
        + _esc(_oidc_value(record, "scopes"))
        + '">'
        + '<input type="text" name="allowed_email_domains" placeholder="Allowed email domains, comma separated" aria-label="Allowed email domains" value="'
        + _esc(_oidc_value(record, "allowed_email_domains"))
        + '">'
        + '<input type="text" name="admin_groups" placeholder="Admin groups, comma separated" aria-label="OIDC admin groups" value="'
        + _esc(_oidc_value(record, "admin_groups"))
        + '">'
        + '<input type="text" name="admin_roles" placeholder="Admin roles, comma separated" aria-label="OIDC admin roles" value="'
        + _esc(_oidc_value(record, "admin_roles"))
        + '">'
        + "</div>"
        + '<details class="panel"><summary>Advanced OIDC endpoints</summary>'
        + '<p class="form-note">Usually discovered from the issuer. Fill these only when your IdP needs explicit endpoints.</p>'
        + '<div class="stack">'
        + '<input type="url" name="authorization_endpoint" placeholder="Authorization endpoint" aria-label="OIDC authorization endpoint" value="'
        + _esc(_oidc_value(record, "authorization_endpoint"))
        + '">'
        + '<input type="url" name="token_endpoint" placeholder="Token endpoint" aria-label="OIDC token endpoint" value="'
        + _esc(_oidc_value(record, "token_endpoint"))
        + '">'
        + '<input type="url" name="jwks_uri" placeholder="JWKS URI" aria-label="OIDC JWKS URI" value="'
        + _esc(_oidc_value(record, "jwks_uri"))
        + '">'
        + '<input type="number" min="0" name="clock_skew_seconds" placeholder="Clock skew seconds" aria-label="OIDC clock skew seconds" value="'
        + _esc(_oidc_value(record, "clock_skew_seconds"))
        + '">'
        + "</div></details>"
        + '<div class="row">'
        + '<span class="hint">'
        + ("OIDC login is enabled." if enabled else "OIDC login is off until enabled here.")
        + "</span>"
        + '<button type="submit">Save OIDC</button>'
        + "</div>"
        + "</form>"
    )


def _policy_simulator_form(
    profiles: list[Mapping[str, Any]],
    *,
    policy_simulate_action: str,
    policy_catalog: Mapping[str, list[Mapping[str, Any]]] | None = None,
) -> str:
    profile_options = _profile_options(profiles, include_default=True, default_label="Local default")
    catalog = policy_catalog or profile_policy_catalog()
    protocol_options = _catalog_select_options(catalog, "protocols", selected="openai")
    if not protocol_options:
        protocol_options = '<option value="">No protocols available</option>'
    intent_options = _catalog_select_options(catalog, "intents", selected="chat")
    if not intent_options:
        intent_options = '<option value="">No intents available</option>'
    exposure_options = _catalog_select_options(catalog, "exposure", selected="default")
    if not exposure_options:
        exposure_options = '<option value="">No exposure classes available</option>'
    trust_options = _catalog_select_options(catalog, "trust_tiers", selected="stable")
    if not trust_options:
        trust_options = '<option value="">No trust tiers available</option>'
    resource_options = _catalog_select_options(catalog, "resource_tiers", selected="interactive")
    if not resource_options:
        resource_options = '<option value="">No resource tiers available</option>'
    return (
        '<details class="panel"><summary>Simulate access</summary>'
        '<form class="flow" method="post" action="'
        + _esc(policy_simulate_action)
        + '" data-simulate-policy>'
        "<label>Debug/Test access</label>"
        '<p class="form-note">Ask whether a request would be allowed without sending it to a model. Live model-list and current-loaded LLM checks still happen on the runtime route.</p>'
        '<div class="stack">'
        f'<select name="profile" aria-label="Profile to simulate">{profile_options}</select>'
        '<select name="protocol" aria-label="Request protocol">'
        + protocol_options
        + "</select>"
        '<select name="intent" aria-label="Request intent">'
        + intent_options
        + "</select>"
        '<select name="capability_exposure" aria-label="Capability exposure">'
        + exposure_options
        + "</select>"
        '<select name="capability_trust_tier" aria-label="Capability trust tier">'
        + trust_options
        + "</select>"
        '<select name="capability_resource_tier" aria-label="Capability resource tier">'
        + resource_options
        + "</select>"
        "</div>"
        '<details class="panel"><summary>Advanced request fields</summary>'
        '<div class="stack">'
        '<input type="text" name="workflow" placeholder="workflow id" aria-label="Workflow id">'
        '<input type="text" name="model" placeholder="model id" aria-label="Model id">'
        '<input type="text" name="backend" placeholder="backend: comfyui" aria-label="Backend id">'
        '<input type="text" name="capability_tags" placeholder="tags: image, controlnet" aria-label="Capability tags">'
        "</div>"
        '<div class="stack">'
        '<label class="check"><input type="checkbox" name="requires_cloud_fallback"> Requires cloud fallback</label>'
        '<label class="check"><input type="checkbox" name="native_passthrough"> Native passthrough request</label>'
        "</div>"
        "</details>"
        '<div class="row">'
        "<span></span>"
        '<button type="submit">Simulate</button>'
        "</div>"
        "</form>"
        "</details>"
    )


def render_admin_dashboard(
    snapshot: Mapping[str, Any],
    *,
    title: str = "AI Gateway Admin",
    brand: str = "AI Gateway",
    wordmark: tuple[str, str] | None = None,
    base_url: str = "https://ai.vivid.fish/openai/v1",
    create_app_action: str = "/admin/api/apps",
    create_profile_action: str = "/admin/api/profiles",
    create_mapping_action: str = "/admin/api/runtime-profile-mappings",
    create_key_action_template: str = "/admin/api/apps/{app_id}/keys",
    update_app_action_template: str = "/admin/api/apps/{app_id}",
    create_admin_key_action: str = "/admin/api/admin-keys",
    policy_simulate_action: str = "/admin/api/policy/simulate",
    oidc_config_action: str = "/admin/api/oidc-config",
    logout_url: str = "/me/logout",
) -> str:
    """Render a server-side HTML admin dashboard from a snapshot dict.

    ``snapshot`` keys (all optional):

    * ``actor``: mapping with ``subject`` / ``email`` for the current admin
    * ``apps``, ``users``, ``profiles``, ``credentials``, ``audit``: lists
      of mapping records
    """

    apps = [
        app
        for app in list(snapshot.get("apps") or [])
        if not (isinstance(app, Mapping) and app.get("disabled_at"))
    ]
    attention = [a for a in list(snapshot.get("attention") or []) if isinstance(a, Mapping)]
    users = list(snapshot.get("users") or [])
    profiles = list(snapshot.get("profiles") or [])
    credentials = [
        credential
        for credential in list(snapshot.get("credentials") or [])
        if not credential.get("app_id") or any(
            str(app.get("id") or "") == str(credential.get("app_id") or "")
            for app in apps
        )
    ]
    audit = list(snapshot.get("audit") or [])
    api_surfaces = list(snapshot.get("api_surfaces") or [])
    services = list(snapshot.get("services") or [])
    workflow_inventory = list(snapshot.get("workflow_inventory") or [])
    identity_mappings = list(snapshot.get("identity_mappings") or [])
    oidc_config = snapshot.get("oidc_config")
    if not isinstance(oidc_config, Mapping):
        oidc_config = {}
    config_sources = [
        source
        for source in list(snapshot.get("config_sources") or [])
        if isinstance(source, Mapping)
    ]
    operator_override_groups = {
        "Operator": snapshot.get("operator_overrides"),
        "Resource": snapshot.get("resource_overrides"),
        "Service": snapshot.get("service_overrides"),
    }
    model_visibility = snapshot.get("model_visibility")
    extension_packages = snapshot.get("extension_packages")
    if not isinstance(extension_packages, Mapping):
        overrides = snapshot.get("operator_overrides")
        extension_packages = (
            overrides.get("extension_package_index")
            if isinstance(overrides, Mapping)
            else {}
        )
    if not isinstance(extension_packages, Mapping):
        extension_packages = {}
    policy_catalog = coerce_policy_catalog(snapshot.get("policy_catalog"))
    profile_inventory = snapshot.get("profile_inventory")
    if not isinstance(profile_inventory, Mapping):
        profile_inventory = {}
    actor = snapshot.get("actor") or {}
    actor_label = actor.get("email") or actor.get("subject") or "anonymous"
    raw_actor_roles = actor.get("roles")
    actor_roles = (
        {str(role) for role in raw_actor_roles}
        if isinstance(raw_actor_roles, (list, tuple, set, frozenset))
        else set()
    )
    csrf_json = _json_for_script(str(actor.get("csrf_token") or ""))
    profiles_by_id = {_profile_id(p): p for p in profiles}
    active_keys_by_app: dict[str, int] = {}
    for credential in credentials:
        if _is_revoked(credential):
            continue
        app_id = str(credential.get("app_id") or "")
        if app_id:
            active_keys_by_app[app_id] = active_keys_by_app.get(app_id, 0) + 1

    apps_table = _table(
        ["App", "Owner", "Effective access", "Keys", "Status", "Created", "Action"],
        (
            _app_row(
                a,
                profiles=profiles,
                profiles_by_id=profiles_by_id,
                active_keys_by_app=active_keys_by_app,
                update_app_action_template=update_app_action_template,
            )
            for a in apps
        ),
    )
    users_table = _table(
        ["ID", "Name", "Email", "Roles"],
        (_user_row(u) for u in users),
    )
    profiles_table = _table(
        ["Profile", "Allowed use", "Specific routes", "Guardrails", "Status"],
        (_profile_row(p) for p in profiles),
    )
    credentials_table = _table(
        ["ID", "App", "Label", "Created", "Status", "Action"],
        (_credential_row(c) for c in credentials),
    )
    audit_table = _table(
        ["When", "Actor", "Action", "Target", "Detail"],
        (_audit_row(e) for e in audit),
    )
    api_surfaces_table = _table(
        ["API", "Route", "Status", "Profile gates", "Common clients"],
        (_api_surface_row(surface) for surface in api_surfaces),
    )
    services_table = _table(
        ["Service", "Role", "Capabilities", "Status", "Runtime", "Mode / resources", "Actions"],
        (_service_row(service) for service in services),
    )
    service_crud_panel = _service_crud_panel(services)
    config_sources_panel = _config_sources_panel(config_sources)
    workflow_inventory_table = _table(
        ["Workflow", "Intent", "Backend", "Tier", "Trust"],
        (_workflow_inventory_row(workflow) for workflow in workflow_inventory),
    )
    extension_package_panel = (
        _extension_package_panel(extension_packages)
        if (
            services
            or workflow_inventory
            or config_sources
            or model_visibility
            or extension_packages
        )
        else ""
    )
    identity_mappings_table = _table(
        ["Identity source", "Scope", "Default Profile", "Note", "Action"],
        (_identity_mapping_row(mapping) for mapping in identity_mappings),
    )

    flow_html = _flow_form(
        apps,
        users,
        profiles,
        create_app_action=create_app_action,
        create_key_action_template=create_key_action_template,
    )
    admin_key_html = _admin_key_form(
        create_admin_key_action=create_admin_key_action,
        can_mint_owner="owner" in actor_roles,
    )
    profile_form_html = _profile_form(
        profiles,
        create_profile_action=create_profile_action,
        model_visibility=model_visibility if isinstance(model_visibility, Mapping) else None,
        policy_catalog=policy_catalog,
        profile_inventory=profile_inventory,
    )
    policy_simulator_html = _policy_simulator_form(
        profiles,
        policy_simulate_action=policy_simulate_action,
        policy_catalog=policy_catalog,
    )
    runtime_mapping_html = _runtime_mapping_form(
        profiles,
        create_mapping_action=create_mapping_action,
    )
    oidc_config_html = _oidc_config_form(
        oidc_config,
        oidc_config_action=oidc_config_action,
    )

    result_html = (
        '<div class="message" id="admin-flow-message" aria-live="polite"></div>'
        '<details class="panel" id="admin-flow-debug" hidden>'
        '<summary>Show raw response</summary>'
        '<pre class="result" id="admin-flow-result"></pre>'
        "</details>"
    )
    api_surfaces_html = api_surfaces_table
    services_html = (
        _model_visibility_panel(
            model_visibility if isinstance(model_visibility, Mapping) else None
        )
        + _subsection(
            "Runtime Control",
            _operator_controls_panel(operator_override_groups),
            intro="Pause services, describe maintenance, and cap expensive media queues without editing files or restarting the gateway.",
        )
        + _subsection(
            "Configured services",
            service_crud_panel + services_table,
            intro="The model/inference systems this gateway calls. Services you add here can be edited or removed; ones provisioned from config or built in can be enabled or disabled here and changed in the config file. The table shows every service's source, status, and capabilities.",
        )
        + _subsection(
            "Sources of Truth",
            config_sources_panel,
            intro="Shows which settings are dashboard-managed, which are provisioned from files/env/manifests, and what requires restart or reload.",
        )
        + _subsection(
            "Workflows",
            workflow_inventory_table,
            intro="Gateway workflow manifests currently available to Profiles and media routes.",
        )
        + _subsection(
            "Extension Packages",
            extension_package_panel,
            intro="Validate local or Git/GitHub packages, enable data-owned registry records, and review staged code objects when present.",
        )
    )
    apps_keys_html = (
        _attention_panel(attention)
        + _start_card(apps)
        + result_html
        + flow_html
        + _subsection(
            "Current Apps",
            apps_table,
            intro="Shared and personal Apps with their assigned Profile and active keys.",
        )
        + _subsection(
            "Keys",
            credentials_table,
            intro="Revoke App keys here. Admin automation keys are managed separately.",
        )
    )
    profiles_html = (
        profile_form_html
        + _subsection(
            "Current Profiles",
            profiles_table,
            intro="Reusable runtime policies that Apps can share.",
        )
        # The policy simulator lives here (where a denial investigation starts),
        # not as its own permanent sidebar slot for a rarely-used debug tool.
        + _subsection(
            "Test a decision",
            policy_simulator_html,
            intro="Check whether a Profile would allow a request — for debugging a denial.",
        )
    )
    identity_html = (
        _subsection(
            "OIDC login",
            oidc_config_html,
            intro="Configure browser sign-in for users and admins. Profile mappings below decide runtime access.",
        )
        + _subsection(
            "Runtime Profile mappings",
            runtime_mapping_html + identity_mappings_table,
            intro="Default Profiles for self-service Apps. These mappings do not grant admin access.",
        )
        + _subsection(
            "People",
            users_table,
            intro="OIDC users who can sign in or own personal Apps.",
        )
    )
    activity_html = _subsection(
        "Audit",
        audit_table,
        intro="Recent admin and gateway control-plane events.",
    )

    header_html = render_shell_header(
        title=title,
        brand=brand,
        wordmark=wordmark,
        current="admin",
        show_account=True,
        show_admin=True,
        show_status=True,
        actor_label=str(actor_label),
        logout_url=logout_url,
    )

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        f"<style>{SHELL_CSS}{_PAGE_CSS}</style>"
        "<script>"
        "document.addEventListener('DOMContentLoaded',()=>{"
        f"const csrfToken={csrf_json};"
        f"const runtimeBaseUrl={_json_for_script(base_url)};"
        f"const policyCatalog={_json_for_script(policy_catalog)};"
        "const raw=v=>{const out=document.getElementById('admin-flow-result'),wrap=document.getElementById('admin-flow-debug');"
        "if(out)out.textContent=typeof v==='string'?v:JSON.stringify(v,null,2);if(wrap)wrap.hidden=false};"
        "const message=(text,kind='')=>{const el=document.getElementById('admin-flow-message');if(!el)return;"
        "el.className='message'+(kind?' '+kind:'');el.textContent=text};"
        "const copyText=async(text,button)=>{const original=button?(button.dataset.copyLabel||button.textContent||'Copy'):'';if(button)button.dataset.copyLabel=original;try{await navigator.clipboard.writeText(text);if(button)button.textContent='Copied'}catch(_){if(button)button.textContent='Failed'}finally{if(button)setTimeout(()=>{if(button.textContent==='Copied'||button.textContent==='Failed')button.textContent=button.dataset.copyLabel||original||'Copy'},1300)}};"
        "const showError=err=>{raw(err);message((err&&err.error&&(err.error.message||err.error.code))||err.message||'Request failed','bad')};"
        "const show=v=>{raw(v);message('Done.','good')};"
        "const showSecret=(v,lead,warning,setup)=>{raw(v);const key=(v&&v.credential&&v.credential.api_key)||(v&&v.key&&v.key.credential&&v.key.credential.api_key);const el=document.getElementById('admin-flow-message');"
        "if(!el){return}el.className='message good';el.textContent='';if(!key){el.textContent='Created, but no one-time key was returned.';return}"
        "const wrap=document.createElement('div');wrap.className='secret';const p=document.createElement('p');p.textContent=lead;"
        "const code=document.createElement('code');code.textContent=key;const w=document.createElement('p');w.textContent=warning;const actions=document.createElement('div');actions.className='actions';"
        "const copy=document.createElement('button');copy.type='button';copy.textContent='Copy key';copy.addEventListener('click',()=>copyText(key,copy));"
        "actions.append(copy);if(setup){const setupText=String(setup).replaceAll('{key}',key);const setupP=document.createElement('p');setupP.textContent=setupText;const copySetup=document.createElement('button');copySetup.type='button';copySetup.textContent='Copy setup';copySetup.addEventListener('click',()=>copyText(setupText,copySetup));actions.append(copySetup);wrap.append(p,code,setupP,w,actions)}else{wrap.append(p,code,w,actions)}el.append(wrap)};"
        "const list=v=>String(v||'').split(/[\\s,]+/).map(x=>x.trim()).filter(Boolean);"
        "const assign=(obj,path,value)=>{const parts=String(path).split('.').filter(Boolean);if(!parts.length)return;let cur=obj;parts.slice(0,-1).forEach(p=>{cur[p]=cur[p]||{};cur=cur[p]});cur[parts[parts.length-1]]=value};"
        "const values=(fd,k)=>fd.getAll(k).flatMap(list);"
        "const slug=s=>String(s||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'')||'profile';"
        "const labelsFor=section=>Object.fromEntries((policyCatalog[section]||[]).map(o=>[o.id,o.summary_label||o.label||o.id]));"
        "const labelFor=(section,value)=>labelsFor(section)[value]||value;"
        "const protocolLabels=labelsFor('protocols');"
        "const intentLabels=labelsFor('intents');"
        "const resourceLabels=labelsFor('resource_tiers');"
        "const exposureLabels=labelsFor('exposure');"
        "const trustLabels=labelsFor('trust_tiers');"
        "const labelList=(items,labels,empty)=>items.length?items.map(x=>labels[x]||x).join(', '):empty;"
        "const checkedValues=(form,name)=>Array.from(form.querySelectorAll(`[name=\"${name}\"]:checked`)).map(el=>el.value);"
        "const profileProtocols=(fd,form)=>{const protocols=checkedValues(form,'protocols');if(fd.get('native_passthrough')==='on'&&!protocols.includes('provider-native'))protocols.push('provider-native');return protocols};"
        "const buildProfileFromForm=(fd,form,profileId)=>{const allow={intents:checkedValues(form,'intents'),exposure:checkedValues(form,'exposure'),trust_tiers:checkedValues(form,'trust_tiers'),resource_tiers:checkedValues(form,'resource_tiers')};const tags=values(fd,'tags');if(tags.length)allow.tags=tags;const profile={id:profileId,protocols:profileProtocols(fd,form),allow,status_visibility:{level:fd.get('status_visibility')||'basic'},fallback:{cloud_allowed:fd.get('cloud_allowed')==='on'},native_passthrough:{enabled:fd.get('native_passthrough')==='on'},target_policy:{visibility:fd.get('target_visibility')||'catalog',activation:fd.get('target_activation')||'may_activate'}};const models=values(fd,'models');if(models.length)profile.models=models;const workflows=values(fd,'workflows');if(workflows.length)profile.workflows=workflows;const backends=values(fd,'backends');if(backends.length)profile.backends=backends;return profile};"
        "const updateProfilePreview=form=>{form=form||document.querySelector('[data-create-profile]');if(!form)return;const fd=new FormData(form);const apis=profileProtocols(fd,form),work=checkedValues(form,'intents'),resources=checkedValues(form,'resource_tiers'),exposure=checkedValues(form,'exposure'),trust=checkedValues(form,'trust_tiers');const targetVisibility=labelFor('target_visibility',fd.get('target_visibility')||'catalog');const targetActivation=labelFor('target_activation',fd.get('target_activation')||'may_activate');const guard=(fd.get('cloud_allowed')==='on'?'cloud fallback allowed':'local only')+'; '+(fd.get('native_passthrough')==='on'?'provider-native passthrough on':'provider-native passthrough off')+'; admin blocked';const a=form.querySelector('[data-profile-preview-apis]'),w=form.querySelector('[data-profile-preview-work]'),c=form.querySelector('[data-profile-preview-cost]'),m=form.querySelector('[data-profile-preview-models]'),g=form.querySelector('[data-profile-preview-guardrails]');if(a)a.textContent='APIs: '+labelList(apis,protocolLabels,'none selected');if(w)w.textContent='Work: '+labelList(work,intentLabels,'none selected');if(c)c.textContent='Cost gates: '+labelList(resources,resourceLabels,'none selected')+'; '+labelList(exposure,exposureLabels,'none selected')+'; '+labelList(trust,trustLabels,'none selected');if(m)m.textContent='Targets: '+targetVisibility+'; '+targetActivation;if(g)g.textContent='Guardrails: '+guard};"
        "const initProfileForm=()=>updateProfilePreview(document.querySelector('[data-create-profile]'));"
        "document.addEventListener('change',e=>{const form=e.target.closest&&e.target.closest('[data-create-profile]');if(form)updateProfilePreview(form)});initProfileForm();"
        "const refreshMain=async()=>{const r=await fetch('/admin',{credentials:'same-origin',cache:'no-store'});"
        "if(!r.ok)return;const html=await r.text();const doc=new DOMParser().parseFromString(html,'text/html');"
        "const next=doc.querySelector('main.shell-main'),main=document.querySelector('main.shell-main');"
        "if(next&&main){main.replaceWith(next);initProfileForm();if(window.__adminNavInit)window.__adminNavInit()}};"
        "const json=async(url,body,method='POST')=>{const r=await fetch(url,{method,credentials:'same-origin',headers:{'content-type':'application/json','x-ai-gateway-admin-action':'1','x-ai-gateway-csrf':csrfToken},body:JSON.stringify(body)});"
        "const text=await r.text();let data;try{data=text?JSON.parse(text):{}}catch{data={error:text}};"
        "if(!r.ok){throw data}return data};"
        "document.addEventListener('submit',async e=>{"
        "const adminKeyForm=e.target.closest('[data-create-admin-key]'),profileForm=e.target.closest('[data-create-profile]'),simForm=e.target.closest('[data-simulate-policy]'),appForm=e.target.closest('[data-create-app]'),keyForm=e.target.closest('[data-mint-key]'),mappingForm=e.target.closest('[data-create-runtime-mapping]'),appProfileForm=e.target.closest('[data-update-app-profile]'),operatorForm=e.target.closest('[data-operator-control]'),oidcForm=e.target.closest('[data-oidc-config]'),extensionForm=e.target.closest('[data-extension-package]'),serviceForm=e.target.closest('[data-service-form]');"
        "if(!adminKeyForm&&!profileForm&&!simForm&&!appForm&&!keyForm&&!mappingForm&&!appProfileForm&&!operatorForm&&!oidcForm&&!extensionForm&&!serviceForm)return;e.preventDefault();"
        "if(adminKeyForm){const f=adminKeyForm,fd=new FormData(f);const body={name:fd.get('name'),role:fd.get('role')||'admin'};try{const data=await json(f.action,body);f.reset();await refreshMain();showSecret(data,'Admin automation key created. Copy it now; it will not be shown again.','Use this admin key only for /admin/api and /status. Never paste it into chat apps, SDK tools, browser code, prompts, logs, or issue trackers.')}catch(err){showError(err)}return}"
        "if(oidcForm){const f=oidcForm,fd=new FormData(f);const body={enabled:fd.get('enabled')==='on'};"
        "['issuer','client_id','redirect_uri','authorization_endpoint','token_endpoint','jwks_uri'].forEach(k=>{body[k]=String(fd.get(k)||'').trim()});"
        "const secret=String(fd.get('client_secret')||'').trim();if(secret)body.client_secret=secret;"
        "['scopes','allowed_email_domains','admin_groups','admin_roles'].forEach(k=>{body[k]=list(fd.get(k))});"
        "const skew=String(fd.get('clock_skew_seconds')||'').trim();if(skew)body.clock_skew_seconds=Number(skew);"
        "try{const data=await json(f.action,body,'PUT');await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(profileForm){const f=profileForm,fd=new FormData(f);const label=String(fd.get('label')||'').trim();const profile=buildProfileFromForm(fd,f,String(fd.get('id')||'').trim()||slug(label));"
        "const extra=String(fd.get('advanced_json')||'').trim();"
        "try{if(extra){Object.assign(profile,JSON.parse(extra))}"
        "const body={name:label||profile.id,label:label||profile.id,profile};"
        "const data=await json(f.action,body);f.reset();await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(simForm){const f=simForm,fd=new FormData(f),request={};"
        "['protocol','intent','workflow','model','backend','capability_exposure','capability_trust_tier','capability_resource_tier'].forEach(k=>{const v=String(fd.get(k)||'').trim();if(v)request[k]=v});"
        "const tags=list(fd.get('capability_tags'));if(tags.length)request.capability_tags=tags;"
        "if(fd.get('requires_cloud_fallback')==='on')request.requires_cloud_fallback=true;"
        "if(fd.get('native_passthrough')==='on')request.native_passthrough=true;"
        "const profile=fd.get('profile')||{id:'default-local'};"
        "try{show(await json(f.action,{profile,request}))}catch(err){showError(err)}return}"
        "if(mappingForm){const f=mappingForm,fd=new FormData(f);const body={source_kind:fd.get('source_kind'),source_value:fd.get('source_value'),profile_id:fd.get('profile_id')||'default-local'};"
        "try{const data=await json(f.action,body);f.reset();await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(appProfileForm){const f=appProfileForm,fd=new FormData(f);const body={profile_id:fd.get('profile_id')||'default-local'};"
        "try{const data=await json(f.action,body,'PATCH');await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(operatorForm){const f=operatorForm,fd=new FormData(f),body={},listFields=new Set(String(f.dataset.listFields||'').split(/\\s+/).filter(Boolean));"
        "fd.forEach((v,k)=>{if(listFields.has(k)){assign(body,k,list(v));return}if(v==='')return;assign(body,k,v)});"
        "try{const data=await json(f.action,body,f.dataset.method||'PATCH');await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(extensionForm){const f=extensionForm,fd=new FormData(f),type=String(fd.get('source_type')||'local_directory').trim();const source={type};"
        "['path','url','ref','subdir'].forEach(k=>{const v=String(fd.get(k)||'').trim();if(v)source[k]=v});"
        "const enable=e.submitter&&e.submitter.dataset.enableExtensionPackage!==undefined;const url=enable?f.dataset.enableAction:f.action;"
        "try{let payload={source};if(enable){const reviewData=await json(f.action,{source});payload.review=reviewData.review}"
        "const data=await json(url,payload);if(enable)await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(appForm){const f=appForm,fd=new FormData(f);const body={name:fd.get('name'),kind:fd.get('kind')||'service'};"
        "if(fd.get('owner_user_id')) body.owner_user_id=fd.get('owner_user_id');"
        "if(fd.get('profile_id')) body.profile_id=fd.get('profile_id');"
        "try{const data=await json(f.action,body);f.reset();await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(keyForm){const f=keyForm,fd=new FormData(f),appId=fd.get('app_id');"
        "const body={};const label=String(fd.get('label')||'').trim();if(label)body.label=label;"
        "const url=f.dataset.actionTemplate.replace('{app_id}',encodeURIComponent(appId));"
        "try{const data=await json(url,body);f.reset();await refreshMain();showSecret(data,'App API key created. Copy it now; it will not be shown again.','Use this key only in runtime Apps and SDK tools.','OpenAI-compatible setup: Base URL '+runtimeBaseUrl+' · API key {key}')}catch(err){showError(err)}return}"
        "if(serviceForm){const f=serviceForm,fd=new FormData(f);const body=serviceFormBody(fd);"
        "const editing=f.dataset.serviceForm==='edit';const sid=f.dataset.serviceId||'';"
        "const url=editing?'/admin/api/operator/services/'+encodeURIComponent(sid):'/admin/api/operator/services';"
        "try{await json(url,body,editing?'PATCH':'POST');await refreshMain();showRestartBanner()}catch(err){showError(err)}return}});"
        "document.addEventListener('click',async e=>{const revoke=e.target.closest('[data-revoke-credential]'),disable=e.target.closest('[data-disable-runtime-mapping]'),appDisable=e.target.closest('[data-disable-app]');"
        "const svcAdd=e.target.closest('[data-service-add]'),svcEdit=e.target.closest('[data-service-edit]'),svcDelete=e.target.closest('[data-service-delete]'),svcToggle=e.target.closest('[data-service-toggle]'),svcTest=e.target.closest('[data-service-test]'),svcCancel=e.target.closest('[data-service-form-cancel]');"
        "if(svcAdd){e.preventDefault();showServiceForm(null);return}"
        "if(svcCancel){e.preventDefault();hideServiceForm();return}"
        "if(svcTest){e.preventDefault();await testServiceConnection();return}"
        "if(svcEdit){e.preventDefault();await editService(svcEdit.dataset.serviceEdit);return}"
        "if(svcDelete){e.preventDefault();const id=svcDelete.dataset.serviceDelete,nm=svcDelete.dataset.serviceName||id;if(!confirm('Delete service '+nm+'? It is removed from the gateway and takes effect on restart.'))return;try{await json('/admin/api/operator/services/'+encodeURIComponent(id),{},'DELETE');await refreshMain();showRestartBanner()}catch(err){showError(err)}return}"
        "if(svcToggle){e.preventDefault();const id=svcToggle.dataset.serviceToggle,enabled=svcToggle.dataset.enabled==='1';try{await json('/admin/api/operator/services/'+encodeURIComponent(id)+'/enabled',{enabled:!enabled});await refreshMain();showRestartBanner()}catch(err){showError(err)}return}"
        "if(!revoke&&!disable&&!appDisable)return;"
        "if(revoke){const id=revoke.dataset.revokeCredential;try{const data=await json('/admin/api/credentials/'+encodeURIComponent(id)+'/revoke',{});await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(appDisable){const id=appDisable.dataset.disableApp;try{const data=await json('/admin/api/apps/'+encodeURIComponent(id),{},'DELETE');await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(disable){const id=disable.dataset.disableRuntimeMapping;try{const r=await fetch('/admin/api/runtime-profile-mappings/'+encodeURIComponent(id),{method:'DELETE',credentials:'same-origin',headers:{'x-ai-gateway-admin-action':'1','x-ai-gateway-csrf':csrfToken}});const text=await r.text();const data=text?JSON.parse(text):{};if(!r.ok)throw data;await refreshMain();show(data)}catch(err){showError(err)}}});"
        "document.addEventListener('change',e=>{const p=e.target.closest('[data-service-preset]');if(p)applyServicePreset(p.value)});"
        + _SERVICE_CRUD_JS
        + "});"
        "</script>"
        "</head><body>"
        + header_html
        + '<main class="shell-main admin-shell">'
        + _admin_sidebar(
            f"{wordmark[0]}.{wordmark[1]}" if wordmark else brand
        )
        + '<div class="admin-content">'
        + _section(
            "Apps & Keys",
            apps_keys_html,
            section_id="apps-keys",
            intro="Create the App identity first, then mint one or more keys under it.",
        )
        + _section(
            "Profiles",
            profiles_html,
            section_id="profiles",
            intro="Profiles are reusable runtime policies. Assign them to Apps; users do not hand-edit policy.",
        )
        + _section(
            "Identity",
            identity_html,
            section_id="identity",
            intro="Map OIDC groups, roles, email, domain, or gateway role to a default Profile for new runtime access.",
        )
        + _section(
            "Services",
            services_html,
            section_id="services",
            intro="The upstream and local systems this gateway can call or manage — model servers, ComfyUI, TTS/STT, and the LiteLLM substrate — plus the protocol lanes and capabilities they expose. Runtime access to them still flows through Profiles.",
        )
        + _section(
            "APIs",
            api_surfaces_html,
            section_id="apis",
            intro="Client-facing API surfaces. Profiles decide which Apps can use each one.",
        )
        + _section(
            "Admin Keys",
            admin_key_html,
            section_id="admin-keys",
            intro="Admin automation keys are for scripts that call admin endpoints. Keep them separate from App keys.",
        )
        + _section("Activity", activity_html, section_id="activity")
        + "</div>"
        + _ADMIN_NAV_SCRIPT
        + "</main></body></html>"
    )
