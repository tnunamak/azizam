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
  .shell-main section,
  .shell-main section > div,
  .operator-hero,
  .operator-strip,
  .runtime-settings,
  .access-map,
  details.panel,
  form.flow {
    min-width: 0;
    max-width: 100%;
  }
  .operator-hero {
    display: grid;
    gap: var(--space-3);
    padding: clamp(1rem, 4vw, 1.5rem);
    background:
      linear-gradient(135deg, oklch(18% 0.04 205), oklch(14% 0.025 250) 52%, oklch(18% 0.035 170));
    border: 1px solid color-mix(in oklch, var(--line), oklch(72% 0.11 190) 35%);
    border-radius: var(--radius);
  }
  .operator-hero h1 { margin: 0; font-size: 2.6rem; line-height: 1; }
  .operator-hero p { margin: 0; max-width: 62rem; color: var(--muted); line-height: 1.55; }
  .operator-strip { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .operator-strip a, .operator-strip span {
    min-width: 0;
    min-height: 2.25rem;
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.35rem 0.7rem;
    background: oklch(16% 0.022 230);
    color: var(--text);
    white-space: normal;
  }
  .operator-strip a:hover { text-decoration: none; filter: brightness(1.06); }
  .section-intro { margin: -0.25rem 0 var(--space-3); color: var(--muted); line-height: 1.5; max-width: 58rem; }
  .subsection { display: grid; gap: var(--space-2); min-width: 0; }
  .subsection h3 { margin: 0; font-size: 0.95rem; color: var(--text); }
  .subsection .section-intro { margin: 0; }
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
  .access-map { display: grid; gap: var(--space-3); grid-template-columns: repeat(auto-fit, minmax(min(18rem, 100%), 1fr)); }
  .access-node { display: grid; gap: 0.35rem; background: var(--panel); border: 1px solid var(--line);
                 border-radius: var(--radius); padding: var(--space-3); }
  .access-node strong { color: var(--text); }
  .access-node span { color: var(--muted); line-height: 1.45; }
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
  @media (max-width: 760px) {
    .operator-hero h1 { font-size: 2.05rem; }
    table { display: block; overflow-x: auto; }
    form.flow .row { grid-template-columns: 1fr; }
    .choice-grid, .permission-grid { grid-template-columns: 1fr; }
    .facts div { grid-template-columns: 1fr; gap: 0.1rem; }
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
    access = (
        _profile_access_summary(profile)
        if profile_id != "default-local" or profile is not None
        else "inherited built-in default"
    )
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
        f'<td><div><code>{_esc(profile_label)}</code></div><div class="cell-sub">Effective Profile: {_esc(access)}</div>{profile_controls}</td>'
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
        "</tr>"
    )


def _config_source_row(record: Mapping[str, Any]) -> str:
    label = _esc(_safe_config_text(record.get("label") or record.get("name") or record.get("key")))
    domain = _esc(_safe_config_text(record.get("domain") or record.get("scope")))
    plane = _config_source_plane(record)
    plane_label = _esc(_config_plane_label(record, plane))
    badge_class = _source_badge_class(plane)
    source_value = _safe_config_text(record.get("source") or record.get("kind") or "unknown")
    source_ref = _safe_config_text(record.get("source_ref") or record.get("path") or record.get("key") or "")
    provisioned = _esc(source_value if not source_ref or source_ref == "—" else f"{source_value}: {source_ref}")
    authored_stored = _esc(_authored_stored_summary(record))
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
    lifecycle_meta = _esc(_lifecycle_meta_summary(record))
    recommendation = _esc(
        _safe_config_text(
            record.get("recommendation") or record.get("note") or record.get("description"),
            record=record,
        )
    )
    source_sub = provisioned
    if authored_stored:
        source_sub = f"{provisioned} | {authored_stored}" if provisioned and provisioned != "—" else authored_stored
    change_sub = recommendation
    if lifecycle_meta:
        change_sub = f"{lifecycle_meta} | {recommendation}" if recommendation and recommendation != "—" else lifecycle_meta
    return (
        "<tr>"
        f'<td><div class="cell-main">{label}</div><div class="cell-sub">{domain}</div></td>'
        f'<td><span class="badge {badge_class}">{plane_label}</span><div class="cell-sub">{source_sub}</div></td>'
        f'<td><div class="cell-main">{live_state}</div><div class="cell-sub">{apply_label}</div></td>'
        f'<td><div class="cell-main">{change_path}</div><div class="cell-sub">{change_sub}</div></td>'
        "</tr>"
    )


def _authored_stored_summary(record: Mapping[str, Any]) -> str:
    authored = _safe_config_text(record.get("authored_via") or "")
    stored = _safe_config_text(record.get("stored_in") or "")
    if not authored and not stored:
        return ""
    if authored and stored and authored != stored:
        return f"authored {authored}; stored {stored}"
    return f"authored {authored or stored}"


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


def _lifecycle_meta_summary(record: Mapping[str, Any]) -> str:
    parts: list[str] = []
    writable = record.get("writable")
    if isinstance(writable, Mapping) and (writable.get("dashboard") or writable.get("api")):
        targets = []
        if writable.get("dashboard"):
            targets.append("dashboard")
        if writable.get("api"):
            targets.append("API")
        parts.append("UI/API writable: " + "+".join(targets))
    elif isinstance(writable, Mapping):
        parts.append("UI/API writable: no")
    provisioning = str(record.get("provisioning") or "").strip().lower()
    provisioning_label = {
        "bundle_candidate": "in provisioning bundle",
        "bootstrap_only": "bootstrap-only",
        "secret_bootstrap": "bootstrap-only (secret)",
        "source_manifest": "source manifest",
        "split": "split provisioning",
    }.get(provisioning, "")
    if provisioning_label:
        parts.append(provisioning_label)
    return " | ".join(parts)


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
            ["Setting", "Source of truth", "Loaded value", "Change path"],
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
            ["Setting", "Source of truth", "Loaded value", "Change path"],
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


def _operator_intro() -> str:
    return (
        '<section class="operator-hero" aria-labelledby="operator-title">'
        '<div class="cell-sub">admin · ai.vivid.fish</div>'
        '<h1 id="operator-title">Gateway access workspace</h1>'
        "<p>Start with the thing making requests. Apps hold opk_ keys, Profiles "
        "describe what those keys can do, Identity mappings choose defaults, and "
        "Services show what capabilities exist right now.</p>"
        '<nav class="operator-strip" aria-label="Admin sections">'
        '<a href="#decision-path">Decision Path</a>'
        '<a href="#profiles">Profiles</a>'
        '<a href="#apps-keys">Apps &amp; Keys</a>'
        '<a href="#identity">Identity</a>'
        '<a href="#policy-test">Policy Test</a>'
        '<a href="#services">Services</a>'
        '<a href="#apis">APIs</a>'
        '<a href="#admin-keys">Admin Keys</a>'
        '<span><code>opk_</code>&nbsp;runtime App keys</span>'
        '<span><code>oak_</code>&nbsp;admin automation</span>'
        "</nav>"
        "</section>"
    )


def _section(title: str, body: str, *, section_id: str = "", intro: str = "") -> str:
    if not body.strip():
        return ""
    id_attr = f' id="{_esc(section_id)}"' if section_id else ""
    intro_html = f'<p class="section-intro">{_esc(intro)}</p>' if intro else ""
    return f"<section{id_attr}><h2>{_esc(title)}</h2>{intro_html}{body}</section>"


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
        '<p class="hint">Creates an <code>oak_</code> key for scripts that manage Apps, Profiles, keys, status, and audit data. '
        'It is not an OpenAI-compatible runtime key.</p>'
        '<div class="stack">'
        '<input type="text" name="name" placeholder="Automation name, e.g. key-rotation-script" aria-label="Admin automation name" required>'
        '<select name="role" aria-label="Admin automation role">'
        '<option value="admin">Admin</option>'
        + owner_option
        + "</select>"
        "</div>"
        '<div class="row">'
        '<span class="hint">The key is shown once. Do not paste it into chat apps, SDK tools, browser code, prompts, logs, or issue trackers.</span>'
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
    loaded_model = _esc(
        (model_visibility or {}).get("loaded_model") or "unknown"
    )
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
        + '<p class="form-note">Profiles are reusable runtime policies. Name the policy, choose what it can use, then assign it to Apps.</p>'
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
        + '<div class="form-section"><strong>Cost and release gates</strong>'
        + cost_grid
        + "</div>"
        + '<div class="form-section"><strong>Target policy</strong>'
        + '<p class="form-note">Choose whether clients see the allowed catalog or only currently available targets, and whether requests may activate cold targets. LLMs use loaded-model state; media workflows use installed workflow and service availability state.</p>'
        + target_visibility_grid
        + target_activation_grid
        + "</div>"
        + '<p class="form-note">Current loaded LLM: <code>'
        + loaded_model
        + "</code></p>"
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
        ["Service", "Role", "Capabilities", "Status", "Runtime", "Mode / resources"],
        (_service_row(service) for service in services),
    )
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
    access_map_html = (
        '<div class="access-map">'
        '<div class="access-node"><strong>Profile</strong><span>Plain-language policy: allowed APIs, blocked costs, model behavior, and guardrails.</span></div>'
        '<div class="access-node"><strong>App</strong><span>The thing making requests. Each App has an effective Profile and active opk_ keys.</span></div>'
        '<div class="access-node"><strong>Identity</strong><span>OIDC groups, roles, email, domain, gateway role, or all users can choose a default Profile for new runtime access.</span></div>'
        '<div class="access-node"><strong>Services</strong><span>Current capabilities available behind the gateway. They are visible here, not enabled here.</span></div>'
        "</div>"
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
        + services_table
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
        _start_card(apps)
        + result_html
        + flow_html
        + _subsection(
            "Current Apps",
            apps_table,
            intro="Shared and personal Apps with their assigned Profile and active opk_ keys.",
        )
        + _subsection(
            "Keys",
            credentials_table,
            intro="Revoke opk_ runtime keys here. oak_ admin automation keys stay separate.",
        )
    )
    profiles_html = profile_form_html + _subsection(
        "Current Profiles",
        profiles_table,
        intro="Reusable runtime policies that Apps can share.",
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
        "if(next&&main){main.replaceWith(next);initProfileForm()}};"
        "const json=async(url,body,method='POST')=>{const r=await fetch(url,{method,credentials:'same-origin',headers:{'content-type':'application/json','x-ai-gateway-admin-action':'1','x-ai-gateway-csrf':csrfToken},body:JSON.stringify(body)});"
        "const text=await r.text();let data;try{data=text?JSON.parse(text):{}}catch{data={error:text}};"
        "if(!r.ok){throw data}return data};"
        "document.addEventListener('submit',async e=>{"
        "const adminKeyForm=e.target.closest('[data-create-admin-key]'),profileForm=e.target.closest('[data-create-profile]'),simForm=e.target.closest('[data-simulate-policy]'),appForm=e.target.closest('[data-create-app]'),keyForm=e.target.closest('[data-mint-key]'),mappingForm=e.target.closest('[data-create-runtime-mapping]'),appProfileForm=e.target.closest('[data-update-app-profile]'),operatorForm=e.target.closest('[data-operator-control]'),oidcForm=e.target.closest('[data-oidc-config]'),extensionForm=e.target.closest('[data-extension-package]');"
        "if(!adminKeyForm&&!profileForm&&!simForm&&!appForm&&!keyForm&&!mappingForm&&!appProfileForm&&!operatorForm&&!oidcForm&&!extensionForm)return;e.preventDefault();"
        "if(adminKeyForm){const f=adminKeyForm,fd=new FormData(f);const body={name:fd.get('name'),role:fd.get('role')||'admin'};try{const data=await json(f.action,body);f.reset();await refreshMain();showSecret(data,'Admin automation key created. Copy it now; it will not be shown again.','Use this oak_ key only for /admin/api and /status. Never paste it into chat apps, SDK tools, browser code, prompts, logs, or issue trackers.')}catch(err){showError(err)}return}"
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
        "try{const data=await json(url,body);f.reset();await refreshMain();showSecret(data,'App API key created. Copy it now; it will not be shown again.','Use this opk_ key only in runtime Apps and SDK tools.','OpenAI-compatible setup: Base URL '+runtimeBaseUrl+' · API key {key}')}catch(err){showError(err)}}});"
        "document.addEventListener('click',async e=>{const revoke=e.target.closest('[data-revoke-credential]'),disable=e.target.closest('[data-disable-runtime-mapping]'),appDisable=e.target.closest('[data-disable-app]');"
        "if(!revoke&&!disable&&!appDisable)return;"
        "if(revoke){const id=revoke.dataset.revokeCredential;try{const data=await json('/admin/api/credentials/'+encodeURIComponent(id)+'/revoke',{});await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(appDisable){const id=appDisable.dataset.disableApp;try{const data=await json('/admin/api/apps/'+encodeURIComponent(id),{},'DELETE');await refreshMain();show(data)}catch(err){showError(err)}return}"
        "if(disable){const id=disable.dataset.disableRuntimeMapping;try{const r=await fetch('/admin/api/runtime-profile-mappings/'+encodeURIComponent(id),{method:'DELETE',credentials:'same-origin',headers:{'x-ai-gateway-admin-action':'1','x-ai-gateway-csrf':csrfToken}});const text=await r.text();const data=text?JSON.parse(text):{};if(!r.ok)throw data;await refreshMain();show(data)}catch(err){showError(err)}}});"
        "});"
        "</script>"
        "</head><body>"
        + header_html
        + '<main class="shell-main">'
        + _operator_intro()
        + _section(
            "Decision Path",
            access_map_html,
            section_id="decision-path",
            intro="The workspace follows the runtime decision path: Profile, App, key, Identity default, then service capability.",
        )
        + _section(
            "Profiles",
            profiles_html,
            section_id="profiles",
            intro="Profiles are reusable runtime policies. Assign them to Apps; users do not hand-edit policy.",
        )
        + _section(
            "Apps & Keys",
            apps_keys_html,
            section_id="apps-keys",
            intro="Create the App identity first, then mint one or more opk_ runtime keys under it.",
        )
        + _section(
            "Identity",
            identity_html,
            section_id="identity",
        )
        + _section(
            "Policy Test",
            policy_simulator_html,
            section_id="policy-test",
            intro="Check a Profile decision before changing runtime policy or while investigating a denial.",
        )
        + _section(
            "Services",
            services_html,
            section_id="services",
            intro="Configured backend services and capabilities behind the gateway. This is visibility first; runtime access still flows through Profiles.",
        )
        + _section(
            "APIs",
            api_surfaces_html,
            section_id="apis",
            intro="Compatible client-facing surfaces on the front of the gateway. Profiles decide which Apps can use each surface.",
        )
        + _section(
            "Admin Keys",
            admin_key_html,
            section_id="admin-keys",
            intro="oak_ keys are for scripts that call admin endpoints. Keep them separate from opk_ runtime App keys.",
        )
        + _section("Activity", activity_html, section_id="activity")
        + "</main></body></html>"
    )
