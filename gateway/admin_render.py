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

import html
import json
from typing import Any, Iterable, Mapping

__all__ = ["render_admin_dashboard"]


_DASHBOARD_CSS = """
  :root {
    color-scheme: dark;
    --bg: oklch(17% 0.018 255);
    --surface: oklch(21% 0.019 255);
    --surface-2: oklch(25% 0.02 255);
    --line: oklch(34% 0.026 255);
    --muted: oklch(72% 0.035 255);
    --text: oklch(92% 0.018 255);
    --good-bg: oklch(27% 0.055 150);
    --good: oklch(82% 0.13 150);
    --warn-bg: oklch(30% 0.07 65);
    --warn: oklch(84% 0.14 75);
    --bad-bg: oklch(27% 0.065 25);
    --bad: oklch(82% 0.13 25);
    --focus: oklch(67% 0.16 235);
  }
  * { box-sizing: border-box; }
  body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 0;
         background: var(--bg); color: var(--text); }
  header { padding: 1rem clamp(1rem, 3vw, 1.75rem); border-bottom: 1px solid var(--line);
           display: flex; align-items: center; justify-content: space-between; gap: 1rem;
           background: oklch(18.5% 0.02 255); position: sticky; top: 0; z-index: 2; }
  header h1 { font-size: 1.05rem; margin: 0; letter-spacing: 0; }
  header .actor { font-size: 0.85rem; color: var(--muted); }
  header nav { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
  header a { color: oklch(83% 0.09 235); text-decoration: none; font-size: 0.86rem; }
  main { padding: 1.25rem clamp(1rem, 3vw, 2rem) 2rem; display: grid; gap: 1.25rem;
         grid-template-columns: minmax(0, 1fr); max-width: 1120px;
         margin: 0 auto; }
  section { display: grid; gap: 0.75rem; }
  section h2 { font-size: 0.88rem; margin: 0;
               letter-spacing: 0.08em; text-transform: uppercase;
               color: var(--muted); }
  .summary { display: flex; flex-wrap: wrap; gap: 0.5rem; color: var(--muted); }
  .summary strong { color: var(--text); }
  .summary span { background: var(--surface); border: 1px solid var(--line);
                  border-radius: 999px; padding: 0.45rem 0.7rem; }
  .runtime-settings { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center;
                      padding: 0.75rem; background: var(--surface); border: 1px solid var(--line);
                      border-radius: 8px; }
  .runtime-settings .label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase;
                             letter-spacing: 0.08em; }
  .runtime-settings code, td code { font-family: ui-monospace, monospace; font-size: 0.86em;
                                    background: oklch(15.5% 0.016 255); padding: 0.12rem 0.35rem;
                                    border: 1px solid var(--line); border-radius: 4px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem;
          background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
          overflow: hidden; }
  th, td { text-align: left; padding: 0.5rem 0.75rem;
           border-bottom: 1px solid var(--line); vertical-align: top; }
  th { font-weight: 500; color: var(--muted); }
  tr:last-child td { border-bottom: none; }
  .cell-main { font-weight: 650; color: var(--text); }
  .cell-sub { color: var(--muted); font-size: 0.82rem; margin-top: 0.2rem; }
  form.flow { display: grid; gap: 0.75rem; background: var(--surface);
              border: 1px solid var(--line); border-radius: 8px; padding: 0.9rem; }
  form.flow .row { display: grid; gap: 0.5rem;
                   grid-template-columns: minmax(0, 1fr) auto; }
  form.flow .stack { display: grid; gap: 0.5rem;
                     grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
  form.flow label { font-size: 0.85rem; color: var(--muted); }
  form.flow input, form.flow select, form.flow textarea { background: oklch(15.5% 0.016 255);
                   border: 1px solid var(--line); color: var(--text);
                   min-height: 2.75rem; padding: 0.5rem 0.65rem; border-radius: 6px; }
  form.flow textarea { min-height: 5rem; resize: vertical; font-family: ui-monospace, monospace; }
  form.flow .check { display: flex; align-items: center; gap: 0.45rem; color: var(--muted); }
  form.flow .check input { width: auto; }
  form.flow button { background: var(--focus); color: oklch(12% 0.02 255); border: 0;
                     min-height: 2.75rem; padding: 0.55rem 1rem; border-radius: 6px;
                     cursor: pointer; font-weight: 600; }
  form.flow button:hover { filter: brightness(1.08); }
  button.inline { background: var(--bad-bg); color: var(--bad); border: 1px solid oklch(40% 0.08 25);
                  min-height: 2.75rem; padding: 0.3rem 0.55rem; border-radius: 6px; cursor: pointer; }
  button.inline:hover { filter: brightness(1.08); }
  .hint { margin: 0; color: var(--muted); font-size: 0.9rem; line-height: 1.45; }
  .badge { font-size: 0.75rem; padding: 0.15rem 0.45rem;
           border-radius: 4px; background: var(--surface-2); color: var(--muted); }
  .badge.revoked, .badge.problem { background: var(--bad-bg); color: var(--bad); }
  .badge.active, .badge.ready { background: var(--good-bg); color: var(--good); }
  .badge.warn, .badge.attention { background: var(--warn-bg); color: var(--warn); }
  .problem-list { display: grid; gap: 0.5rem; margin: 0; padding: 0; list-style: none; }
  .problem-list li { background: var(--surface); border: 1px solid var(--line);
                     border-radius: 8px; padding: 0.65rem 0.75rem; }
  .result { white-space: pre-wrap; background: oklch(15.5% 0.016 255); border: 1px solid var(--line);
            border-radius: 8px; padding: 0.75rem;
            color: oklch(90% 0.05 235); }
  .result:empty { display: none; }
  details.panel { background: var(--surface); border: 1px solid var(--line);
                  border-radius: 8px; padding: 0.9rem; }
  details.panel summary { cursor: pointer; color: var(--text); font-weight: 650; }
  details.panel form.flow { margin-top: 0.75rem; background: transparent; border: 0; padding: 0; }
  @media (max-width: 760px) {
    header { align-items: flex-start; flex-direction: column; }
    table { display: block; overflow-x: auto; }
    form.flow .row { grid-template-columns: 1fr; }
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


def _summary_bar(items: Iterable[tuple[str, Any]]) -> str:
    return (
        '<div class="summary">'
        + "".join(
            f"<span><strong>{_esc(value)}</strong> {_esc(label)}</span>"
            for label, value in items
        )
        + "</div>"
    )


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
        parts: list[str] = []
        if exposure:
            parts.append("exposure " + ", ".join(exposure))
        if trust:
            parts.append("trust " + ", ".join(trust))
        if resources:
            parts.append("resources " + ", ".join(resources))
        if intents:
            parts.append("intents " + ", ".join(intents))
        if tags:
            parts.append("tags " + ", ".join(tags))
        return "; ".join(parts)

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
        return ""
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
        "operator must attach a Profile"
        if not profile_id
        else _profile_access_summary(profile)
    )
    profile_label = profile_id or "profile required"
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
    protocols = _format_optional_list(doc.get("protocols"))
    overrides = []
    for name in ("workflows", "models", "backends"):
        values = _nested_list(doc.get(name))
        if values:
            overrides.append(f"{name}: " + ", ".join(values))
    override_text = "; ".join(overrides) if overrides else "—"
    visibility = _esc(_visibility_summary(record))
    return (
        "<tr>"
        f'<td><div class="cell-main">{label}</div><div class="cell-sub"><code>{profile_id}</code></div></td>'
        f"<td>{_esc(_profile_access_summary(record))}</td>"
        f"<td>{protocols}</td>"
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


def _connection_panel(*, base_url: str) -> str:
    return (
        '<div class="runtime-settings">'
        '<span class="label">Runtime base URL</span>'
        f'<code>{_esc(base_url)}</code>'
        '<span class="label">Key header</span>'
        '<code>Authorization: Bearer opk_...</code>'
        "</div>"
    )


def _access_issues(
    apps: list[Mapping[str, Any]],
    profiles_by_id: Mapping[str, Mapping[str, Any]],
    active_keys_by_app: Mapping[str, int],
) -> str:
    items: list[str] = []
    if not apps:
        items.append("Create an App before minting a runtime key.")
    for app in apps:
        app_id = str(app.get("id") or "")
        app_name = str(app.get("name") or app_id or "unnamed app")
        profile_id = _app_profile_id(app)
        if not profile_id:
            items.append(f"{app_name} has no Profile yet.")
        elif profile_id != "default-local" and profile_id not in profiles_by_id:
            items.append(f"{app_name} points at missing Profile {profile_id}.")
        if active_keys_by_app.get(app_id, 0) < 1:
            items.append(f"{app_name} has no active runtime key.")
    if not items:
        return ""

    return (
        '<ul class="problem-list">'
        + "".join(f"<li>{_esc(item)}</li>" for item in items)
        + "</ul>"
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
    if not app_options:
        app_options = '<option value="" disabled selected>Create an App first</option>'
    user_options = '<option value="">No user owner</option>' + "".join(
        f'<option value="{_esc(user.get("id"))}">'
        f'{_esc(user.get("email") or user.get("name") or user.get("id"))}'
        "</option>"
        for user in users
    )
    profile_options = '<option value="">Default profile</option>' + "".join(
        f'<option value="{_esc(profile.get("id"))}">'
        f'{_esc(profile.get("label") or profile.get("name") or profile.get("id"))}'
        "</option>"
        for profile in profiles
    )
    return (
        '<form class="flow" method="post" action="' + _esc(create_app_action) + '" data-create-app>'
        "<label>Create App</label>"
        '<div class="stack">'
        '<input type="text" name="name" placeholder="App name" aria-label="App name" required>'
        '<select name="kind" aria-label="App kind"><option value="service">Service app</option><option value="personal">Personal app</option></select>'
        f'<select name="owner_user_id" aria-label="User owner">{user_options}</select>'
        f'<select name="profile_id" aria-label="App Profile">{profile_options}</select>'
        "</div>"
        '<div class="row">'
        "<span></span>"
        '<button type="submit">Create app</button>'
        "</div>"
        "</form>"
        '<form class="flow" method="post" data-action-template="'
        + _esc(create_key_action_template)
        + '" data-mint-key>'
        "<label>Mint runtime key</label>"
        '<div class="row">'
        f'<select name="app_id" aria-label="App for runtime key" required>{app_options}</select>'
        '<input type="text" name="label" placeholder="Key label" aria-label="Runtime key label">'
        '<button type="submit">Mint key</button>'
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
        "<label>Create Profile</label>"
        '<div class="stack">'
        '<input type="text" name="id" placeholder="profile id" aria-label="Profile id" required>'
        '<input type="text" name="label" placeholder="Label" aria-label="Profile label">'
        '<select name="status_visibility" aria-label="Status visibility">'
        '<option value="basic">Basic status</option>'
        '<option value="standard">Standard status</option>'
        '<option value="full">Full status</option>'
        '<option value="none">No status</option>'
        "</select>"
        '<input type="text" name="protocols" placeholder="protocols: openai, anthropic" aria-label="Allowed protocols">'
        '<input type="text" name="intents" placeholder="intents: chat, image.generate" aria-label="Allowed intents">'
        '<input type="text" name="exposure" placeholder="exposure: default, opt_in" aria-label="Allowed exposure tiers">'
        '<input type="text" name="trust_tiers" placeholder="trust: stable, experimental" aria-label="Allowed trust tiers">'
        '<input type="text" name="resource_tiers" placeholder="resources: interactive, batch" aria-label="Allowed resource tiers">'
        '<input type="text" name="tags" placeholder="tags: local, image, family" aria-label="Allowed tags">'
        "</div>"
        '<div class="stack">'
        '<label class="check"><input type="checkbox" name="cloud_allowed"> Allow cloud fallback</label>'
        '<label class="check"><input type="checkbox" name="native_passthrough"> Allow native passthrough</label>'
        "</div>"
        '<textarea name="advanced_json" spellcheck="false" aria-label="Advanced profile JSON" placeholder="{&quot;models&quot;:[&quot;local-chat&quot;]}"></textarea>'
        '<div class="row">'
        "<span></span>"
        '<button type="submit">Create profile</button>'
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
    title: str = "Vivid Fish AI Gateway Admin",
    base_url: str = "https://ai.vivid.fish/v1",
    create_app_action: str = "/admin/api/apps",
    create_profile_action: str = "/admin/api/profiles",
    create_key_action_template: str = "/admin/api/apps/{app_id}/keys",
    policy_simulate_action: str = "/admin/api/policy/simulate",
) -> str:
    """Render a server-side HTML admin dashboard from a snapshot dict.

    ``snapshot`` keys (all optional):

    * ``actor``: mapping with ``subject`` / ``email`` for the current admin
    * ``counts``: pre-computed counts dict (otherwise derived from lists)
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

    counts = snapshot.get("counts") or {
        "users": len(users),
        "apps": len(apps),
        "profiles": len(profiles),
        "credentials": len(credentials),
        "active_credentials": sum(
            1 for c in credentials if not _is_revoked(c)
        ),
    }

    summary_html = _summary_bar(
        (
            ("Apps", counts.get("apps", 0)),
            ("Users", counts.get("users", 0)),
            ("Profiles", counts.get("profiles", 0)),
            ("active keys", counts.get("active_credentials", 0)),
        )
    )

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
        ["Profile", "Selectors", "Protocols", "Overrides", "Guardrails", "Visibility"],
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
    profile_form_html = _profile_form(
        profiles,
        create_profile_action=create_profile_action,
    )
    policy_simulator_html = _policy_simulator_form(
        profiles,
        policy_simulate_action=policy_simulate_action,
    )

    connection_html = _connection_panel(base_url=base_url)
    issues_html = _access_issues(apps, profiles_by_id, active_keys_by_app)
    result_html = '<pre class="result" id="admin-flow-result" aria-live="polite"></pre>'
    access_html = connection_html + flow_html + result_html
    profiles_html = profile_form_html + profiles_table

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        f"<style>{_DASHBOARD_CSS}</style>"
        "<script>"
        "document.addEventListener('DOMContentLoaded',()=>{"
        "const out=document.getElementById('admin-flow-result');"
        f"const csrfToken={csrf_json};"
        "const show=v=>{out.textContent=typeof v==='string'?v:JSON.stringify(v,null,2)};"
        "const list=v=>String(v||'').split(/[\\s,]+/).map(x=>x.trim()).filter(Boolean);"
        "const json=async(url,body)=>{const r=await fetch(url,{method:'POST',credentials:'same-origin',headers:{'content-type':'application/json','x-ai-gateway-admin-action':'1','x-ai-gateway-csrf':csrfToken},body:JSON.stringify(body)});"
        "const text=await r.text();let data;try{data=text?JSON.parse(text):{}}catch{data={error:text}};"
        "if(!r.ok){throw data}return data};"
        "document.querySelector('[data-create-profile]')?.addEventListener('submit',async e=>{e.preventDefault();"
        "const f=e.currentTarget,fd=new FormData(f);const profile={id:String(fd.get('id')||'').trim(),status_visibility:{level:fd.get('status_visibility')||'basic'},fallback:{cloud_allowed:fd.get('cloud_allowed')==='on'},native_passthrough:{enabled:fd.get('native_passthrough')==='on'}};"
        "const protocols=list(fd.get('protocols'));if(protocols.length) profile.protocols=protocols;"
        "const allow={};['intents','exposure','trust_tiers','resource_tiers','tags'].forEach(k=>{const v=list(fd.get(k));if(v.length) allow[k]=v});if(Object.keys(allow).length) profile.allow=allow;"
        "const extra=String(fd.get('advanced_json')||'').trim();"
        "try{if(extra){Object.assign(profile,JSON.parse(extra))}"
        "const body={name:fd.get('label')||profile.id,label:fd.get('label')||profile.id,profile};"
        "const data=await json(f.action,body);show(data);setTimeout(()=>location.reload(),800)}catch(err){show(err)}});"
        "document.querySelector('[data-simulate-policy]')?.addEventListener('submit',async e=>{e.preventDefault();"
        "const f=e.currentTarget,fd=new FormData(f),request={};"
        "['protocol','intent','workflow','model','backend','capability_exposure','capability_trust_tier','capability_resource_tier'].forEach(k=>{const v=String(fd.get(k)||'').trim();if(v)request[k]=v});"
        "const tags=list(fd.get('capability_tags'));if(tags.length)request.capability_tags=tags;"
        "if(fd.get('requires_cloud_fallback')==='on')request.requires_cloud_fallback=true;"
        "if(fd.get('native_passthrough')==='on')request.native_passthrough=true;"
        "const profile=fd.get('profile')||{id:'default-local'};"
        "try{show(await json(f.action,{profile,request}))}catch(err){show(err)}});"
        "document.querySelector('[data-create-app]')?.addEventListener('submit',async e=>{e.preventDefault();"
        "const f=e.currentTarget,fd=new FormData(f);const body={name:fd.get('name'),kind:fd.get('kind')||'service'};"
        "if(fd.get('owner_user_id')) body.owner_user_id=fd.get('owner_user_id');"
        "if(fd.get('profile_id')) body.profile_id=fd.get('profile_id');"
        "try{const data=await json(f.action,body);show(data);setTimeout(()=>location.reload(),800)}catch(err){show(err)}});"
        "document.querySelector('[data-mint-key]')?.addEventListener('submit',async e=>{e.preventDefault();"
        "const f=e.currentTarget,fd=new FormData(f),appId=fd.get('app_id');"
        "const body={}; if(fd.get('label')) body.label=fd.get('label');"
        "const url=f.dataset.actionTemplate.replace('{app_id}',encodeURIComponent(appId));"
        "try{show(await json(url,body))}catch(err){show(err)}});"
        "document.querySelectorAll('[data-revoke-credential]').forEach(btn=>btn.addEventListener('click',async e=>{"
        "const id=e.currentTarget.dataset.revokeCredential;"
        "try{show(await json('/admin/api/credentials/'+encodeURIComponent(id)+'/revoke',{}));setTimeout(()=>location.reload(),800)}catch(err){show(err)}}));"
        "});"
        "</script>"
        "</head><body>"
        "<header>"
        f"<h1>{_esc(title)}</h1><nav>"
        '<a href="/">Account</a><a href="/status">Status</a>'
        f'<span class="actor">Signed in as <strong>{_esc(actor_label)}</strong></span>'
        "</nav>"
        "</header>"
        "<main>"
        + _section("Overview", summary_html)
        + _section("Needs attention", issues_html)
        + _section("Access", access_html)
        + _section("Profiles", profiles_html)
        + _section("Policy test", policy_simulator_html)
        + _section("Apps", apps_table)
        + _section("Users", users_table)
        + _section("Keys", credentials_table)
        + _section("Audit", audit_table)
        + "</main></body></html>"
    )
