"""Server-side HTML rendering for the admin dashboard.

:func:`render_admin_dashboard` is a pure function: it takes a JSON-shaped
``snapshot`` (the kind a caller can assemble from :mod:`gateway.admin_api`
endpoints or directly from an :class:`~gateway.admin_api.AdminStore`) and
returns a complete HTML page. It performs no I/O, opens no templates, and
never imports persistence or auth modules.

The page is intentionally simple but real: it shows the apps, users, app
profiles, credentials (without raw key material), and audit entries. It also
includes the operator flows for Apps, App API keys, Profile templates, runtime
Profile mappings, admin automation keys, and policy simulation.

Data-plane copy uses **App**, not "Client", per the project convention.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

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
  .inline-form { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; margin-top: 0.45rem; }
  .inline-form select {
    flex: 1 1 12rem;
    min-width: 0;
    min-height: 2.25rem;
    background: oklch(15.5% 0.016 250);
    border: 1px solid var(--line);
    color: var(--text);
    padding: 0.35rem 0.5rem;
    border-radius: 6px;
  }
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
  .choice-card { display: grid; gap: 0.3rem; padding: 0.75rem; border: 1px solid var(--line);
                 border-radius: 6px; background: oklch(14.5% 0.015 250); color: var(--muted); }
  .choice-card input { margin-right: 0.4rem; }
  .choice-card strong { color: var(--text); }
  .inline-options { display: flex; flex-wrap: wrap; gap: 0.55rem 1rem; }
  .template-strip { display: grid; gap: 0.65rem; grid-template-columns: repeat(auto-fit, minmax(min(15rem, 100%), 1fr)); }
  .template-card {
    display: grid;
    grid-template-columns: 1.2rem minmax(0, 1fr);
    gap: 0.65rem;
    align-items: start;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.8rem;
    background: oklch(14.5% 0.015 250);
    cursor: pointer;
    min-width: 0;
  }
  .template-card:has(input:checked) {
    background: color-mix(in oklch, var(--focus), transparent 90%);
    border-color: color-mix(in oklch, var(--focus), var(--line) 35%);
  }
  .template-card input { width: 1rem; min-height: 1rem; margin: 0.15rem 0 0; accent-color: var(--focus); }
  .template-card strong { display: block; color: var(--text); }
  .template-card span { display: block; margin-top: 0.25rem; color: var(--muted); font-size: 0.85rem; line-height: 1.4; }
  @media (max-width: 760px) {
    .operator-hero h1 { font-size: 2.05rem; }
    table { display: block; overflow-x: auto; }
    form.flow .row { grid-template-columns: 1fr; }
    .choice-grid, .template-strip { grid-template-columns: 1fr; }
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
        intent_labels = {
            "chat": "chat",
            "chat.completions": "chat",
            "text.completions": "text completions",
            "text.embeddings": "embeddings",
            "embedding": "embeddings",
            "embeddings": "embeddings",
            "image.generate": "image generation",
            "image.edit": "image editing",
            "speech.synthesis": "text to speech",
            "speech.transcription": "speech transcription",
            "audio.transcribe": "audio transcription",
            "audio.speech": "text to speech",
            "audio.generate": "music/audio generation",
            "video.generate": "video generation",
            "models.list": "model list",
        }
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
    model_access = _profile_model_access_summary(record)
    return f"{cloud}; {native_text}; {model_access}"


def _profile_model_access_summary(record: Mapping[str, Any] | None) -> str:
    doc = _profile_doc(record)
    raw = doc.get("model_access")
    if isinstance(raw, Mapping) and raw.get("mode") == "current_loaded":
        return "current loaded LLM only"
    return "configured model catalog"


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
        f"<td>{role}</td>"
        f"<td>{capabilities}</td>"
        f"<td>{status_html}</td>"
        f"<td>{runtime}</td>"
        f"<td>{resources}</td>"
        "</tr>"
    )


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
            "<h3>Model visibility</h3>",
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
) -> str:
    loaded_model = _esc(
        (model_visibility or {}).get("loaded_model") or "unknown"
    )
    return (
        '<form class="flow" method="post" action="'
        + _esc(create_profile_action)
        + '" data-create-profile>'
        "<label>Create Profile</label>"
        '<p class="form-note">Profiles are reusable runtime policies. They decide which APIs, model behavior, and workflow costs an App can use.</p>'
        '<div class="cell-sub">Start from a template</div>'
        '<div class="template-strip" aria-label="Profile templates">'
        '<label class="template-card"><input type="radio" name="profile_preset" value="chat_only" checked>'
        '<span><strong>Family chat</strong><span>For a person, chatbot, or simple agent that only needs stable local language models.</span></span></label>'
        '<label class="template-card"><input type="radio" name="profile_preset" value="chat_embeddings">'
        '<span><strong>RAG / documents</strong><span>Chat plus embeddings for document search, code indexes, and memory features.</span></span></label>'
        '<label class="template-card"><input type="radio" name="profile_preset" value="chat_images">'
        '<span><strong>Creative chat</strong><span>Chat and approved image generation/edit workflows. No video, music, admin, or cloud fallback.</span></span></label>'
        '<label class="template-card"><input type="radio" name="profile_preset" value="image_audio">'
        '<span><strong>Media creator</strong><span>Approved image, edit, speech, STT, music, and sound workflows. No video.</span></span></label>'
        '<label class="template-card"><input type="radio" name="profile_preset" value="video_media">'
        '<span><strong>Video producer</strong><span>For expensive video jobs and media workflows where queueing is expected.</span></span></label>'
        '<label class="template-card"><input type="radio" name="profile_preset" value="full_runtime">'
        '<span><strong>Trusted power user</strong><span>Every runtime API, including provider-native passthrough. Still no admin control-plane access.</span></span></label>'
        '<label class="template-card"><input type="radio" name="profile_preset" value="custom">'
        '<span><strong>Custom</strong><span>Start from a minimal Profile, then adjust the advanced fields below.</span></span></label>'
        "</div>"
        '<div class="stack">'
        '<input type="text" name="label" placeholder="Profile name, e.g. Family chat" aria-label="Profile name" required>'
        "</div>"
        '<div class="form-section"><strong>Model access</strong>'
        '<div class="choice-grid" aria-label="Model access">'
        '<label class="choice-card"><span><input type="radio" name="model_access" value="catalog" checked>'
        '<strong>Configured catalog</strong></span><span>Apps can request models and roles this Profile allows. Good for normal clients.</span></label>'
        '<label class="choice-card"><span><input type="radio" name="model_access" value="current_loaded">'
        '<strong>Current loaded LLM only</strong></span><span>Only the loaded LLM appears in model lists. Requests for other models are denied instead of swapping models.</span></label>'
        "</div></div>"
        '<p class="form-note">Current loaded LLM: <code>'
        + loaded_model
        + "</code></p>"
        '<div class="policy-preview" id="profile-policy-preview" aria-live="polite">'
        "<strong>Profile preview</strong>"
        '<span data-profile-preview-allowed>Allowed: chat</span>'
        '<span data-profile-preview-blocked>Blocked: embeddings, images, audio, video, admin, cloud fallback</span>'
        '<span data-profile-preview-models>Models: configured catalog</span>'
        "</div>"
        '<details class="panel"><summary>Advanced policy details</summary>'
        '<div class="form-section"><strong>Allowed APIs</strong>'
        '<div class="inline-options" aria-label="Allowed protocols">'
        '<label class="check"><input type="checkbox" name="protocols" value="openai" checked> OpenAI-compatible</label>'
        '<label class="check"><input type="checkbox" name="protocols" value="anthropic"> Anthropic Messages</label>'
        '<label class="check"><input type="checkbox" name="protocols" value="ollama"> Ollama-compatible</label>'
        '<label class="check"><input type="checkbox" name="protocols" value="gateway"> Gateway jobs/discovery</label>'
        '<label class="check"><input type="checkbox" name="protocols" value="provider-native"> Provider-native passthrough</label>'
        "</div></div>"
        '<div class="stack">'
        '<input type="text" name="id" placeholder="Generated from policy name" aria-label="Policy id override">'
        '<select name="status_visibility" aria-label="Status detail">'
        '<option value="basic">Basic status</option>'
        '<option value="standard">Standard status</option>'
        '<option value="full">Full status</option>'
        '<option value="none">No status</option>'
        "</select>"
        '<input type="text" name="models" placeholder="optional model ids, comma separated" aria-label="Allowed model ids">'
        '<input type="text" name="workflows" placeholder="optional workflow ids, comma separated" aria-label="Allowed workflow ids">'
        '<input type="text" name="tags" placeholder="optional tags: family, agents" aria-label="Policy tags">'
        "</div>"
        '<div class="stack">'
        '<label class="check"><input type="checkbox" name="cloud_allowed"> Allow cloud fallback</label>'
        '<label class="check"><input type="checkbox" name="native_passthrough"> Allow native passthrough</label>'
        "</div>"
        '<textarea name="advanced_json" spellcheck="false" aria-label="Raw policy JSON" placeholder="{&quot;models&quot;:[&quot;local-chat&quot;]}"></textarea>'
        "</details>"
        '<div class="row">'
        "<span></span>"
        '<button type="submit">Create Profile</button>'
        "</div>"
        "</form>"
    )


def _policy_simulator_form(
    profiles: list[Mapping[str, Any]],
    *,
    policy_simulate_action: str,
) -> str:
    profile_options = _profile_options(profiles, include_default=True, default_label="Local default")
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
        '<option value="openai">OpenAI-compatible</option>'
        '<option value="anthropic">Anthropic Messages</option>'
        '<option value="ollama">Ollama-compatible</option>'
        '<option value="gateway">Gateway jobs/discovery</option>'
        '<option value="provider-native">Provider-native passthrough</option>'
        "</select>"
        '<select name="intent" aria-label="Request intent">'
        '<option value="chat">Chat</option>'
        '<option value="text.embeddings">Embeddings</option>'
        '<option value="image.generate">Image generation</option>'
        '<option value="image.edit">Image editing</option>'
        '<option value="speech.synthesis">Text to speech</option>'
        '<option value="speech.transcription">Speech transcription</option>'
        '<option value="audio.generate">Music/audio generation</option>'
        '<option value="video.generate">Video generation</option>'
        '<option value="models.list">List models</option>'
        "</select>"
        '<select name="capability_exposure" aria-label="Capability exposure">'
        '<option value="default">Default capability</option>'
        '<option value="opt_in">Opt-in capability</option>'
        '<option value="private">Private capability</option>'
        "</select>"
        '<select name="capability_trust_tier" aria-label="Capability trust tier">'
        '<option value="stable">Stable</option>'
        '<option value="experimental">Experimental</option>'
        '<option value="private">Private</option>'
        "</select>"
        '<select name="capability_resource_tier" aria-label="Capability resource tier">'
        '<option value="interactive">Interactive</option>'
        '<option value="batch">Batch</option>'
        '<option value="expensive">Expensive</option>'
        "</select>"
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
    identity_mappings = list(snapshot.get("identity_mappings") or [])
    model_visibility = snapshot.get("model_visibility")
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
    )
    policy_simulator_html = _policy_simulator_form(
        profiles,
        policy_simulate_action=policy_simulate_action,
    )
    runtime_mapping_html = _runtime_mapping_form(
        profiles,
        create_mapping_action=create_mapping_action,
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
    services_html = _model_visibility_panel(
        model_visibility if isinstance(model_visibility, Mapping) else None
    ) + services_table
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
        "const values=(fd,k)=>fd.getAll(k).flatMap(list);"
        "const slug=s=>String(s||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'')||'profile';"
        "const policyTemplates={chat_only:{label:'Chat only',protocols:['openai','anthropic','ollama'],intents:['chat','text.completions'],exposure:['default'],trust:['stable'],resources:['interactive'],status:'basic',native_passthrough:false},chat_embeddings:{label:'Chat + embeddings',protocols:['openai','anthropic','ollama'],intents:['chat','text.completions','text.embeddings'],exposure:['default'],trust:['stable'],resources:['interactive'],status:'basic',native_passthrough:false},chat_images:{label:'Chat + images',protocols:['openai','anthropic','ollama','gateway'],intents:['chat','text.completions','image.generate','image.edit'],exposure:['default','opt_in'],trust:['stable'],resources:['interactive','batch'],status:'standard',native_passthrough:false},image_audio:{label:'Images + audio',protocols:['openai','gateway'],intents:['image.generate','image.edit','speech.synthesis','speech.transcription','audio.generate'],exposure:['default','opt_in'],trust:['stable'],resources:['interactive','batch'],status:'standard',native_passthrough:false},video_media:{label:'Video producer',protocols:['openai','gateway'],intents:['image.generate','image.edit','speech.synthesis','speech.transcription','audio.generate','video.generate'],exposure:['default','opt_in'],trust:['stable'],resources:['interactive','batch','expensive'],status:'standard',native_passthrough:false},full_runtime:{label:'Full runtime access',protocols:['openai','anthropic','ollama','gateway','provider-native'],intents:['chat','text.completions','text.embeddings','image.generate','image.edit','speech.synthesis','speech.transcription','audio.generate','video.generate','models.list'],exposure:['default','opt_in'],trust:['stable','experimental','private'],resources:['interactive','batch','expensive'],status:'standard',native_passthrough:true},custom:{label:'Custom policy',protocols:['openai'],intents:['chat'],exposure:['default'],trust:['stable'],resources:['interactive'],status:'basic',native_passthrough:false}};"
        "const policyPreview={chat_only:{allowed:'chat',blocked:'embeddings, images, audio, video, admin, cloud fallback'},chat_embeddings:{allowed:'chat, embeddings',blocked:'images, audio, video, admin, cloud fallback'},chat_images:{allowed:'chat, image generation, image editing',blocked:'video, music/audio generation, admin, cloud fallback'},image_audio:{allowed:'image generation, image editing, speech, transcription, music/audio generation',blocked:'video, admin, cloud fallback'},video_media:{allowed:'image, audio, and video generation workflows',blocked:'admin, cloud fallback'},full_runtime:{allowed:'chat, embeddings, images, audio, video, model list, provider-native passthrough',blocked:'admin control plane'},custom:{allowed:'custom Profile details',blocked:'anything not explicitly allowed'}};"
        "const buildProfile=(templateId,profileId)=>{const t=policyTemplates[templateId]||policyTemplates.chat_only;const allow={intents:t.intents,exposure:t.exposure,trust_tiers:t.trust,resource_tiers:t.resources};if(t.tags)allow.tags=t.tags;return{id:profileId,protocols:t.protocols,allow,status_visibility:{level:t.status||'basic'},fallback:{cloud_allowed:false},native_passthrough:{enabled:!!t.native_passthrough},model_access:{mode:'catalog'}}};"
        "const setChecks=(form,name,items)=>{const allowed=new Set(items||[]);form.querySelectorAll(`[name=\"${name}\"]`).forEach(el=>{el.checked=allowed.has(el.value)})};"
        "const syncProfileTemplate=form=>{if(!form)return;const fd=new FormData(form);const preset=fd.get('profile_preset')||'chat_only';const t=policyTemplates[preset]||policyTemplates.chat_only;setChecks(form,'protocols',t.protocols);const native=form.querySelector('[name=\"native_passthrough\"]');if(native)native.checked=!!t.native_passthrough;const status=form.querySelector('[name=\"status_visibility\"]');if(status)status.value=t.status||'basic'};"
        "const updateProfilePreview=form=>{form=form||document.querySelector('[data-create-profile]');if(!form)return;const fd=new FormData(form);const preset=fd.get('profile_preset')||'chat_only';const copy=policyPreview[preset]||policyPreview.chat_only;const modelMode=fd.get('model_access')==='current_loaded'?'current loaded LLM only':'configured catalog';const a=form.querySelector('[data-profile-preview-allowed]'),b=form.querySelector('[data-profile-preview-blocked]'),m=form.querySelector('[data-profile-preview-models]');if(a)a.textContent='Allowed: '+copy.allowed;if(b)b.textContent='Blocked: '+copy.blocked;if(m)m.textContent='Models: '+modelMode};"
        "const initProfileForm=()=>{const form=document.querySelector('[data-create-profile]');syncProfileTemplate(form);updateProfilePreview(form)};"
        "document.addEventListener('change',e=>{const form=e.target.closest&&e.target.closest('[data-create-profile]');if(!form)return;if(e.target.name==='profile_preset')syncProfileTemplate(form);updateProfilePreview(form)});initProfileForm();"
        "const refreshMain=async()=>{const r=await fetch('/admin',{credentials:'same-origin',cache:'no-store'});"
        "if(!r.ok)return;const html=await r.text();const doc=new DOMParser().parseFromString(html,'text/html');"
        "const next=doc.querySelector('main.shell-main'),main=document.querySelector('main.shell-main');"
        "if(next&&main){main.replaceWith(next);initProfileForm()}};"
        "const json=async(url,body,method='POST')=>{const r=await fetch(url,{method,credentials:'same-origin',headers:{'content-type':'application/json','x-ai-gateway-admin-action':'1','x-ai-gateway-csrf':csrfToken},body:JSON.stringify(body)});"
        "const text=await r.text();let data;try{data=text?JSON.parse(text):{}}catch{data={error:text}};"
        "if(!r.ok){throw data}return data};"
        "document.addEventListener('submit',async e=>{"
        "const adminKeyForm=e.target.closest('[data-create-admin-key]'),profileForm=e.target.closest('[data-create-profile]'),simForm=e.target.closest('[data-simulate-policy]'),appForm=e.target.closest('[data-create-app]'),keyForm=e.target.closest('[data-mint-key]'),mappingForm=e.target.closest('[data-create-runtime-mapping]'),appProfileForm=e.target.closest('[data-update-app-profile]');"
        "if(!adminKeyForm&&!profileForm&&!simForm&&!appForm&&!keyForm&&!mappingForm&&!appProfileForm)return;e.preventDefault();"
        "if(adminKeyForm){const f=adminKeyForm,fd=new FormData(f);const body={name:fd.get('name'),role:fd.get('role')||'admin'};try{const data=await json(f.action,body);f.reset();await refreshMain();showSecret(data,'Admin automation key created. Copy it now; it will not be shown again.','Use this oak_ key only for /admin/api and /status. Never paste it into chat apps, SDK tools, browser code, prompts, logs, or issue trackers.')}catch(err){showError(err)}return}"
        "if(profileForm){const f=profileForm,fd=new FormData(f);const label=String(fd.get('label')||'').trim();const templateId=fd.get('profile_preset')||'chat_only';const profile=buildProfile(templateId,String(fd.get('id')||'').trim()||slug(label));profile.status_visibility={level:fd.get('status_visibility')||profile.status_visibility.level};profile.fallback={cloud_allowed:fd.get('cloud_allowed')==='on'};profile.native_passthrough={enabled:profile.native_passthrough.enabled||fd.get('native_passthrough')==='on'};profile.model_access={mode:fd.get('model_access')||'catalog'};"
        "const protocols=values(fd,'protocols');if(protocols.length) profile.protocols=protocols;"
        "const models=values(fd,'models');if(models.length) profile.models=models;"
        "const workflows=values(fd,'workflows');if(workflows.length) profile.workflows=workflows;"
        "const tags=values(fd,'tags');if(tags.length) profile.allow.tags=[...new Set([...(profile.allow.tags||[]),...tags])];"
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
