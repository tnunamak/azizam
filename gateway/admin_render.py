"""Server-side HTML rendering for the admin dashboard.

:func:`render_admin_dashboard` is a pure function: it takes a JSON-shaped
``snapshot`` (the kind a caller can assemble from :mod:`gateway.admin_api`
endpoints or directly from an :class:`~gateway.admin_api.AdminStore`) and
returns a complete HTML page. It performs no I/O, opens no templates, and
never imports persistence or auth modules.

The page is intentionally simple but real: it shows the apps, users, app
profiles, credentials (without raw key material), audit entries, and
discovery links, and it includes an HTML form for the "give my brother an
API key" flow — create an app, then mint a key against it.

Data-plane copy uses **App**, not "Client", per the project convention.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from gateway.gateway_shell import (
    SHELL_CSS,
    esc as _esc,
    json_for_script as _json_for_script,
    render_shell_header,
)

__all__ = ["render_admin_dashboard"]


_PAGE_CSS = """
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
  form.flow { display: grid; gap: 0.75rem; background: var(--panel);
              border: 1px solid var(--line); border-radius: var(--radius); padding: 0.9rem; }
  form.flow .row { display: grid; gap: var(--space-2);
                   grid-template-columns: minmax(0, 1fr) auto; }
  form.flow .stack { display: grid; gap: var(--space-2);
                     grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
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
  form.flow input:focus-visible, form.flow select:focus-visible, form.flow textarea:focus-visible,
  form.flow button:focus-visible, .panel summary:focus-visible {
    outline: 2px solid var(--focus); outline-offset: 2px;
  }
  button.inline { background: var(--bad-bg); color: var(--bad); border: 1px solid oklch(40% 0.08 25);
                  min-height: 2.5rem; padding: 0.3rem 0.65rem; border-radius: 6px; cursor: pointer; }
  button.inline:hover { filter: brightness(1.08); }
  .badge { font-size: 0.75rem; padding: 0.18rem 0.5rem;
           border-radius: 999px; background: var(--panel-2); color: var(--muted);
           border: 1px solid var(--line); display: inline-flex; align-items: center; }
  .badge.revoked, .badge.problem { background: var(--bad-bg); color: var(--bad); border-color: transparent; }
  .badge.active, .badge.ready { background: var(--good-bg); color: var(--good); border-color: transparent; }
  .badge.warn, .badge.attention { background: var(--warn-bg); color: var(--warn); border-color: transparent; }
  .problem-list { display: grid; gap: var(--space-2); margin: 0; padding: 0; list-style: none; }
  .problem-list li { background: var(--panel); border: 1px solid var(--line);
                     border-radius: var(--radius); padding: 0.65rem 0.75rem; }
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
                  border-radius: var(--radius); padding: 0.9rem; }
  details.panel summary { cursor: pointer; color: var(--text); font-weight: 650;
                          min-height: 2rem; display: inline-flex; align-items: center; }
  details.panel form.flow { margin-top: 0.75rem; background: transparent; border: 0; padding: 0; }
  .choice-grid { display: grid; gap: 0.65rem; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr)); }
  .choice-card { display: grid; gap: 0.3rem; padding: 0.75rem; border: 1px solid var(--line);
                 border-radius: 6px; background: oklch(14.5% 0.015 250); color: var(--muted); }
  .choice-card input { margin-right: 0.4rem; }
  .choice-card strong { color: var(--text); }
  .inline-options { display: flex; flex-wrap: wrap; gap: 0.55rem 1rem; }
  @media (max-width: 760px) {
    table { display: block; overflow-x: auto; }
    form.flow .row { grid-template-columns: 1fr; }
  }
"""


def _format_optional_list(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(_esc(v) for v in sorted(value))
    return _esc(value)


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
            "embedding": "embeddings",
            "embeddings": "embeddings",
            "image.generate": "image generation",
            "image.edit": "image editing",
            "audio.transcribe": "audio transcription",
            "audio.speech": "text to speech",
            "audio.generate": "music/audio generation",
            "video.generate": "video generation",
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
    return f"{cloud}; {native_text}"


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


def _app_row(
    record: Mapping[str, Any],
    *,
    profiles_by_id: Mapping[str, Mapping[str, Any]],
    active_keys_by_app: Mapping[str, int],
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
    if not profile_id:
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
    created = _esc(record.get("created_at") or "—")
    return (
        "<tr>"
        f'<td><div class="cell-main">{name}</div><div class="cell-sub"><code>{app_id}</code></div></td>'
        f"<td>{owner}</td>"
        f'<td><div><code>{_esc(profile_label)}</code></div><div class="cell-sub">{_esc(access)}</div></td>'
        f"<td>{_esc(active_keys)} active</td>"
        f'<td><span class="badge {status_class}">{status_text}</span></td>'
        f"<td>{created}</td>"
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
    when = _esc(record.get("at") or record.get("timestamp") or "—")
    actor = _esc(record.get("actor") or record.get("subject") or "—")
    action = _esc(record.get("action") or record.get("event") or "—")
    target = _esc(record.get("target") or record.get("resource") or "—")
    detail = _esc(record.get("detail") or record.get("note") or "")
    return (
        "<tr>"
        f"<td>{when}</td>"
        f"<td>{actor}</td>"
        f"<td>{action}</td>"
        f"<td>{target}</td>"
        f"<td>{detail}</td>"
        "</tr>"
    )


def _section(title: str, body: str) -> str:
    if not body.strip():
        return ""
    return f"<section><h2>{_esc(title)}</h2>{body}</section>"


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
        "<h3>No Apps yet</h3>"
        "<p>Create an API key for an app, bot, agent, or service. The gateway "
        "will create the matching App and access policy for you.</p>"
        '<a href="#create-api-key">Create API key</a>'
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
    user_options = '<option value="">No user owner (service-owned)</option>' + "".join(
        f'<option value="{_esc(user.get("id"))}">'
        f'{_esc(user.get("email") or user.get("name") or user.get("id"))}'
        "</option>"
        for user in users
    )
    profile_options = '<option value="">Default profile</option>' + "".join(
        f'<option value="{_esc(profile.get("id"))}">'
        f'{_esc(profile.get("label") or profile.get("name") or profile.get("id"))}'
        f' ({_esc(profile.get("id"))})'
        "</option>"
        for profile in profiles
    )
    create_form = (
        '<details class="panel"><summary>Advanced: create App manually</summary>'
        '<form class="flow" id="create-app" method="post" action="'
        + _esc(create_app_action)
        + '" data-create-app>'
        "<label>Create App manually</label>"
        '<div class="stack">'
        '<input type="text" name="name" placeholder="App name" aria-label="App name" required>'
        '<div class="choice-grid" aria-label="App ownership">'
        '<label class="choice-card"><span><input type="radio" name="kind" value="service" checked>'
        '<strong>Service-owned</strong></span><span>No user owner; use this for LibreChat, bots, and shared services.</span></label>'
        '<label class="choice-card"><span><input type="radio" name="kind" value="personal">'
        '<strong>Personal/user-owned</strong></span><span>Attach the App to a user for individual access.</span></label>'
        "</div>"
        f'<select name="owner_user_id" aria-label="User owner for personal App">{user_options}</select>'
        f'<select name="profile_id" aria-label="Existing Profile for App">{profile_options}</select>'
        "</div>"
        '<div class="row">'
        "<span></span>"
        '<button type="submit">Create app</button>'
        "</div>"
        "</form>"
        "</details>"
    )
    if not app_options:
        return create_form

    mint_form = (
        '<details class="panel"><summary>Create key for existing App</summary>'
        '<form class="flow" method="post" data-action-template="'
        + _esc(create_key_action_template)
        + '" data-mint-key>'
        "<label>Create runtime key</label>"
        '<div class="row">'
        f'<select name="app_id" aria-label="App for runtime key" required>{app_options}</select>'
        '<button type="submit">Create key</button>'
        "</div>"
        "</form>"
        "</details>"
    )
    return create_form + mint_form


def _service_access_form(
    *,
    create_app_action: str,
    create_profile_action: str,
    create_key_action_template: str,
) -> str:
    return (
        '<form class="flow" id="create-api-key" method="post" action="'
        + _esc(create_profile_action)
        + '" data-create-service-access data-app-action="'
        + _esc(create_app_action)
        + '" data-key-action-template="'
        + _esc(create_key_action_template)
        + '">'
        "<label>Create an API key</label>"
        '<div class="stack">'
        '<input type="text" name="service_name" placeholder="App or service name, e.g. LibreChat" aria-label="App or service name" required>'
        '<div class="inline-options" aria-label="Allowed capabilities">'
        '<label class="check"><input type="checkbox" name="service_capabilities" value="chat" checked> Chat</label>'
        '<label class="check"><input type="checkbox" name="service_capabilities" value="embeddings"> Embeddings</label>'
        '<label class="check"><input type="checkbox" name="service_capabilities" value="images"> Images</label>'
        '<label class="check"><input type="checkbox" name="service_capabilities" value="audio"> Audio</label>'
        '<label class="check"><input type="checkbox" name="service_capabilities" value="video"> Video</label>'
        "</div>"
        "</div>"
        '<div class="row">'
        "<span></span>"
        '<button type="submit">Create App and API key</button>'
        "</div>"
        "</form>"
    )


def _profile_form(
    profiles: list[Mapping[str, Any]],
    *,
    create_profile_action: str,
) -> str:
    existing = ", ".join(_profile_id(profile) for profile in profiles)
    existing_html = (
        f'<div class="hint">Existing Profiles: <code>{_esc(existing)}</code></div>'
        if existing
        else '<div class="hint">No stored Profiles yet.</div>'
    )
    return (
        existing_html
        + '<form class="flow" method="post" action="'
        + _esc(create_profile_action)
        + '" data-create-profile>'
        "<label>Create reusable access policy</label>"
        '<div class="stack">'
        '<input type="text" name="label" placeholder="Family chat" aria-label="Policy name" required>'
        '<select name="profile_preset" aria-label="Start from">'
        '<option value="family_chat">Family chat</option>'
        '<option value="coding_agents">Coding agents</option>'
        '<option value="image_tools">Image generation</option>'
        '<option value="automation">Automation</option>'
        '<option value="custom">Custom policy</option>'
        "</select>"
        '<div class="inline-options" aria-label="Allowed capabilities">'
        '<label class="check"><input type="checkbox" name="capabilities" value="chat" checked> Chat</label>'
        '<label class="check"><input type="checkbox" name="capabilities" value="embeddings"> Embeddings</label>'
        '<label class="check"><input type="checkbox" name="capabilities" value="images"> Images</label>'
        '<label class="check"><input type="checkbox" name="capabilities" value="audio"> Audio</label>'
        '<label class="check"><input type="checkbox" name="capabilities" value="video"> Video</label>'
        "</div>"
        '<select name="model_access" aria-label="Model and workflow access">'
        '<option value="recommended">Recommended safe defaults</option>'
        '<option value="all_local">All local and stable</option>'
        '<option value="opt_in">Only explicitly tagged workflows</option>'
        "</select>"
        "</div>"
        '<details class="panel"><summary>Advanced policy details</summary>'
        '<div class="stack">'
        '<input type="text" name="id" placeholder="Generated from policy name" aria-label="Policy id override">'
        '<select name="status_visibility" aria-label="Status detail">'
        '<option value="basic">Basic status</option>'
        '<option value="standard">Standard status</option>'
        '<option value="full">Full status</option>'
        '<option value="none">No status</option>'
        "</select>"
        '<div class="inline-options" aria-label="Allowed protocols">'
        '<label class="check"><input type="checkbox" name="protocols" value="openai" checked> OpenAI-compatible</label>'
        '<label class="check"><input type="checkbox" name="protocols" value="anthropic"> Anthropic-compatible</label>'
        "</div>"
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
        '<button type="submit">Create policy</button>'
        "</div>"
        "</form>"
    )


def _policy_simulator_form(
    profiles: list[Mapping[str, Any]],
    *,
    policy_simulate_action: str,
) -> str:
    profile_options = '<option value="">Local default</option>' + "".join(
        f'<option value="{_esc(_profile_id(profile))}">'
        f'{_esc(profile.get("label") or profile.get("name") or _profile_id(profile))}'
        "</option>"
        for profile in profiles
    )
    return (
        '<details class="panel"><summary>Simulate access</summary>'
        '<form class="flow" method="post" action="'
        + _esc(policy_simulate_action)
        + '" data-simulate-policy>'
        "<label>Request facts</label>"
        '<div class="stack">'
        f'<select name="profile" aria-label="Profile to simulate">{profile_options}</select>'
        '<input type="text" name="protocol" placeholder="protocol: openai.images" aria-label="Request protocol">'
        '<input type="text" name="intent" placeholder="intent: image.edit" aria-label="Request intent">'
        '<input type="text" name="workflow" placeholder="workflow id" aria-label="Workflow id">'
        '<input type="text" name="model" placeholder="model id" aria-label="Model id">'
        '<input type="text" name="backend" placeholder="backend: comfyui" aria-label="Backend id">'
        '<input type="text" name="capability_exposure" placeholder="exposure: default" aria-label="Capability exposure">'
        '<input type="text" name="capability_trust_tier" placeholder="trust: stable" aria-label="Capability trust tier">'
        '<input type="text" name="capability_resource_tier" placeholder="resource: batch" aria-label="Capability resource tier">'
        '<input type="text" name="capability_tags" placeholder="tags: image, controlnet" aria-label="Capability tags">'
        "</div>"
        '<div class="stack">'
        '<label class="check"><input type="checkbox" name="requires_cloud_fallback"> Requires cloud fallback</label>'
        '<label class="check"><input type="checkbox" name="native_passthrough"> Native passthrough request</label>'
        "</div>"
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
    base_url: str = "https://ai.vivid.fish/v1",
    create_app_action: str = "/admin/api/apps",
    create_profile_action: str = "/admin/api/profiles",
    create_key_action_template: str = "/admin/api/apps/{app_id}/keys",
    policy_simulate_action: str = "/admin/api/policy/simulate",
    logout_url: str = "/me/logout",
) -> str:
    """Render a server-side HTML admin dashboard from a snapshot dict.

    ``snapshot`` keys (all optional):

    * ``actor``: mapping with ``subject`` / ``email`` for the current admin
    * ``apps``, ``users``, ``profiles``, ``credentials``, ``audit``: lists
      of mapping records
    """

    apps = list(snapshot.get("apps") or [])
    users = list(snapshot.get("users") or [])
    profiles = list(snapshot.get("profiles") or [])
    credentials = list(snapshot.get("credentials") or [])
    audit = list(snapshot.get("audit") or [])
    actor = snapshot.get("actor") or {}
    actor_label = actor.get("email") or actor.get("subject") or "anonymous"
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
        ["App", "Owner", "Effective access", "Keys", "Status", "Created"],
        (
            _app_row(
                a,
                profiles_by_id=profiles_by_id,
                active_keys_by_app=active_keys_by_app,
            )
            for a in apps
        ),
    )
    users_table = _table(
        ["ID", "Name", "Email", "Roles"],
        (_user_row(u) for u in users),
    )
    profiles_table = _table(
        ["Access policy", "Allowed use", "Specific routes", "Guardrails", "Status"],
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

    flow_html = _flow_form(
        apps,
        users,
        profiles,
        create_app_action=create_app_action,
        create_key_action_template=create_key_action_template,
    )
    service_access_html = _service_access_form(
        create_app_action=create_app_action,
        create_profile_action=create_profile_action,
        create_key_action_template=create_key_action_template,
    )
    profile_form_html = _profile_form(
        profiles,
        create_profile_action=create_profile_action,
    )
    policy_simulator_html = (
        _policy_simulator_form(
            profiles,
            policy_simulate_action=policy_simulate_action,
        )
        if profiles
        else ""
    )

    result_html = '<pre class="result" id="admin-flow-result" aria-live="polite"></pre>'
    access_html = service_access_html + flow_html + result_html
    profiles_html = profile_form_html + profiles_table

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
        "const show=v=>{const out=document.getElementById('admin-flow-result');"
        "if(out)out.textContent=typeof v==='string'?v:JSON.stringify(v,null,2)};"
        "const list=v=>String(v||'').split(/[\\s,]+/).map(x=>x.trim()).filter(Boolean);"
        "const values=(fd,k)=>fd.getAll(k).flatMap(list);"
        "const slug=s=>String(s||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'')||'access';"
        "const title=s=>String(s||'').split(/[._-]+/).filter(Boolean).map(w=>w.charAt(0).toUpperCase()+w.slice(1)).join(' ')||'Access';"
        "const profilePresets={family_chat:{protocols:['openai'],tags:['family','chat'],capabilities:['chat'],exposure:'default',trust:'stable',resources:'interactive'},coding_agents:{protocols:['openai','anthropic'],tags:['agents','coding'],capabilities:['chat','embeddings'],exposure:'default',trust:'stable',resources:'interactive'},image_tools:{protocols:['openai'],tags:['image'],capabilities:['images'],exposure:'opt_in',trust:'stable',resources:'interactive'},automation:{protocols:['openai'],tags:['automation'],capabilities:['chat','embeddings'],exposure:'default',trust:'stable',resources:'batch'},custom:{protocols:['openai'],tags:[],capabilities:['chat'],exposure:'default',trust:'stable',resources:'interactive'}};"
        "const capabilityIntents={chat:['chat'],embeddings:['embeddings'],images:['image.generate','image.edit'],audio:['audio.transcribe','audio.speech','audio.generate'],video:['video.generate']};"
        "const refreshMain=async()=>{const r=await fetch('/admin',{credentials:'same-origin',cache:'no-store'});"
        "if(!r.ok)return;const html=await r.text();const doc=new DOMParser().parseFromString(html,'text/html');"
        "const next=doc.querySelector('main.shell-main'),main=document.querySelector('main.shell-main');"
        "if(next&&main)main.replaceWith(next)};"
        "const json=async(url,body)=>{const r=await fetch(url,{method:'POST',credentials:'same-origin',headers:{'content-type':'application/json','x-ai-gateway-admin-action':'1','x-ai-gateway-csrf':csrfToken},body:JSON.stringify(body)});"
        "const text=await r.text();let data;try{data=text?JSON.parse(text):{}}catch{data={error:text}};"
        "if(!r.ok){throw data}return data};"
        "document.addEventListener('submit',async e=>{"
        "const serviceForm=e.target.closest('[data-create-service-access]'),profileForm=e.target.closest('[data-create-profile]'),simForm=e.target.closest('[data-simulate-policy]'),appForm=e.target.closest('[data-create-app]'),keyForm=e.target.closest('[data-mint-key]');"
        "if(!serviceForm&&!profileForm&&!simForm&&!appForm&&!keyForm)return;e.preventDefault();"
        "if(serviceForm){const f=serviceForm,fd=new FormData(f);const serviceName=String(fd.get('service_name')||'').trim();const serviceSlug=slug(serviceName);const caps=values(fd,'service_capabilities');const selected=caps.length?caps:['chat'];const intents=[...new Set(selected.flatMap(c=>capabilityIntents[c]||[c]))];const profile={id:serviceSlug+'-access',protocols:['openai'],allow:{intents,exposure:['default'],trust_tiers:['stable'],resource_tiers:['interactive'],tags:[...new Set([serviceSlug,...selected])]},status_visibility:{level:'basic'},fallback:{cloud_allowed:false},native_passthrough:{enabled:false}};"
        "const profileLabel=title(serviceName)+' access';const profileBody={name:profileLabel,label:profileLabel,profile};"
        "const keyBody={label:title(serviceName)+' API key'};"
        "try{const createdProfile=await json(f.action,profileBody);const storedProfileId=(createdProfile.profile&&createdProfile.profile.id)||createdProfile.id||profile.id;"
        "const appBody={name:serviceName,kind:'service',owner_service_account_name:serviceName,profile_id:storedProfileId};"
        "const createdApp=await json(f.dataset.appAction,appBody);const appId=createdApp.id||createdApp.app_id||(createdApp.app&&createdApp.app.id)||appBody.name;let createdKey=null;"
        "const url=f.dataset.keyActionTemplate.replace('{app_id}',encodeURIComponent(appId));createdKey=await json(url,keyBody);"
        "const data={profile:createdProfile,app:createdApp,key:createdKey};f.reset();await refreshMain();show(data)}catch(err){show(err)}return}"
        "if(profileForm){const f=profileForm,fd=new FormData(f);const preset=profilePresets[fd.get('profile_preset')]||profilePresets.custom;const label=String(fd.get('label')||'').trim();const selectedCaps=values(fd,'capabilities');const caps=selectedCaps.length?selectedCaps:preset.capabilities;const profile={id:String(fd.get('id')||'').trim()||slug(label),protocols:preset.protocols,allow:{intents:[...new Set(caps.flatMap(c=>capabilityIntents[c]||[c]))],exposure:[fd.get('model_access')==='opt_in'?'opt_in':preset.exposure],trust_tiers:[preset.trust],resource_tiers:[preset.resources],tags:preset.tags},status_visibility:{level:fd.get('status_visibility')||'basic'},fallback:{cloud_allowed:fd.get('cloud_allowed')==='on'},native_passthrough:{enabled:fd.get('native_passthrough')==='on'}};"
        "const protocols=values(fd,'protocols');if(protocols.length) profile.protocols=protocols;"
        "const tags=values(fd,'tags');if(tags.length) profile.allow.tags=[...new Set([...(profile.allow.tags||[]),...tags])];if(fd.get('model_access')==='all_local')profile.allow.tags=[...new Set([...(profile.allow.tags||[]),'local','stable'])];"
        "const extra=String(fd.get('advanced_json')||'').trim();"
        "try{if(extra){Object.assign(profile,JSON.parse(extra))}"
        "const body={name:label||profile.id,label:label||profile.id,profile};"
        "const data=await json(f.action,body);f.reset();await refreshMain();show(data)}catch(err){show(err)}return}"
        "if(simForm){const f=simForm,fd=new FormData(f),request={};"
        "['protocol','intent','workflow','model','backend','capability_exposure','capability_trust_tier','capability_resource_tier'].forEach(k=>{const v=String(fd.get(k)||'').trim();if(v)request[k]=v});"
        "const tags=list(fd.get('capability_tags'));if(tags.length)request.capability_tags=tags;"
        "if(fd.get('requires_cloud_fallback')==='on')request.requires_cloud_fallback=true;"
        "if(fd.get('native_passthrough')==='on')request.native_passthrough=true;"
        "const profile=fd.get('profile')||{id:'default-local'};"
        "try{show(await json(f.action,{profile,request}))}catch(err){show(err)}return}"
        "if(appForm){const f=appForm,fd=new FormData(f);const body={name:fd.get('name'),kind:fd.get('kind')||'service'};"
        "if(fd.get('owner_user_id')) body.owner_user_id=fd.get('owner_user_id');"
        "if(fd.get('profile_id')) body.profile_id=fd.get('profile_id');"
        "try{const data=await json(f.action,body);f.reset();await refreshMain();show(data)}catch(err){show(err)}return}"
        "if(keyForm){const f=keyForm,fd=new FormData(f),appId=fd.get('app_id');"
        "const body={};"
        "const url=f.dataset.actionTemplate.replace('{app_id}',encodeURIComponent(appId));"
        "try{const data=await json(url,body);f.reset();await refreshMain();show(data)}catch(err){show(err)}}});"
        "document.addEventListener('click',async e=>{const btn=e.target.closest('[data-revoke-credential]');"
        "if(!btn)return;const id=btn.dataset.revokeCredential;"
        "try{const data=await json('/admin/api/credentials/'+encodeURIComponent(id)+'/revoke',{});await refreshMain();show(data)}catch(err){show(err)}});"
        "});"
        "</script>"
        "</head><body>"
        + header_html
        + '<main class="shell-main">'
        + _section("Start", _start_card(apps))
        + _section("API keys", access_html)
        + _section("Access policies", profiles_html)
        + _section("Test access", policy_simulator_html)
        + _section("Apps", apps_table)
        + _section("Users", users_table)
        + _section("Keys", credentials_table)
        + _section("Audit", audit_table)
        + "</main></body></html>"
    )
