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
from typing import Any, Iterable, Mapping

__all__ = ["render_admin_dashboard"]


_DASHBOARD_CSS = """
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 0;
         background: #0f1115; color: #e6e8ef; }
  header { padding: 1.25rem 1.5rem; border-bottom: 1px solid #1f2330;
           display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 1.15rem; margin: 0; letter-spacing: 0.02em; }
  header .actor { font-size: 0.85rem; color: #9aa3b8; }
  main { padding: 1.5rem; display: grid; gap: 1.5rem;
         grid-template-columns: minmax(0, 1fr); max-width: 1100px;
         margin: 0 auto; }
  section { background: #161a24; border: 1px solid #1f2330;
            border-radius: 10px; padding: 1.25rem; }
  section h2 { font-size: 1rem; margin: 0 0 0.75rem 0;
               letter-spacing: 0.04em; text-transform: uppercase;
               color: #9aa3b8; }
  .counts { display: grid; grid-template-columns: repeat(auto-fit,
             minmax(140px, 1fr)); gap: 0.75rem; }
  .count { background: #0f1320; border: 1px solid #1f2330;
           padding: 0.75rem 1rem; border-radius: 8px; }
  .count .label { font-size: 0.75rem; color: #9aa3b8;
                  text-transform: uppercase; letter-spacing: 0.05em; }
  .count .value { font-size: 1.5rem; font-weight: 600;
                  margin-top: 0.25rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
  th, td { text-align: left; padding: 0.5rem 0.75rem;
           border-bottom: 1px solid #1f2330; vertical-align: top; }
  th { font-weight: 500; color: #9aa3b8; }
  tr:last-child td { border-bottom: none; }
  td code { font-family: ui-monospace, monospace; font-size: 0.85em;
            background: #0f1320; padding: 0.1rem 0.35rem;
            border-radius: 4px; }
  .empty { color: #6b7390; font-style: italic; }
  form.flow { display: grid; gap: 0.75rem; }
  form.flow .row { display: grid; gap: 0.5rem;
                   grid-template-columns: minmax(0, 1fr) auto; }
  form.flow label { font-size: 0.85rem; color: #9aa3b8; }
  form.flow input, form.flow select { background: #0f1320;
                   border: 1px solid #1f2330; color: #e6e8ef;
                   padding: 0.5rem 0.65rem; border-radius: 6px; }
  form.flow button { background: #3b82f6; color: white; border: 0;
                     padding: 0.55rem 1rem; border-radius: 6px;
                     cursor: pointer; font-weight: 600; }
  form.flow button:hover { background: #2563eb; }
  ul.links { list-style: none; padding: 0; margin: 0;
             display: flex; flex-wrap: wrap; gap: 0.5rem; }
  ul.links a { color: #93c5fd; text-decoration: none;
               background: #0f1320; border: 1px solid #1f2330;
               padding: 0.35rem 0.6rem; border-radius: 999px;
               font-size: 0.85rem; }
  .badge { font-size: 0.75rem; padding: 0.15rem 0.45rem;
           border-radius: 4px; background: #1f2330; color: #9aa3b8; }
  .badge.revoked { background: #3b1f24; color: #fca5a5; }
  .badge.active { background: #173a26; color: #86efac; }
"""


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _count_card(label: str, value: Any) -> str:
    return (
        '<div class="count">'
        f'<div class="label">{_esc(label)}</div>'
        f'<div class="value">{_esc(value)}</div>'
        "</div>"
    )


def _empty_row(columns: int, message: str) -> str:
    return (
        f'<tr><td colspan="{columns}" class="empty">{_esc(message)}</td></tr>'
    )


def _format_optional_list(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(_esc(v) for v in sorted(value))
    return _esc(value)


def _is_revoked(record: Mapping[str, Any]) -> bool:
    return bool(record.get("revoked_at") or record.get("revoked"))


def _credential_row(record: Mapping[str, Any]) -> str:
    cred_id = _esc(record.get("id") or record.get("credential_id"))
    app_id = _esc(record.get("app_id"))
    label = _esc(record.get("label") or record.get("name") or "—")
    created = _esc(record.get("created_at") or "—")
    revoked = _is_revoked(record)
    badge_class = "revoked" if revoked else "active"
    badge_text = "revoked" if revoked else "active"
    return (
        "<tr>"
        f"<td><code>{cred_id}</code></td>"
        f"<td><code>{app_id}</code></td>"
        f"<td>{label}</td>"
        f"<td>{created}</td>"
        f'<td><span class="badge {badge_class}">{badge_text}</span></td>'
        "</tr>"
    )


def _app_row(record: Mapping[str, Any]) -> str:
    app_id = _esc(record.get("id"))
    name = _esc(record.get("name") or "—")
    owner = _esc(record.get("owner") or record.get("user_id") or "—")
    profile = _esc(record.get("profile_id") or record.get("profile") or "—")
    created = _esc(record.get("created_at") or "—")
    return (
        "<tr>"
        f"<td><code>{app_id}</code></td>"
        f"<td>{name}</td>"
        f"<td>{owner}</td>"
        f"<td><code>{profile}</code></td>"
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
    profile_id = _esc(record.get("id"))
    label = _esc(record.get("label") or record.get("name") or "—")
    protocols = _format_optional_list(record.get("protocols"))
    workflows = _format_optional_list(record.get("workflows"))
    visibility = _esc(record.get("status_visibility") or "basic")
    return (
        "<tr>"
        f"<td><code>{profile_id}</code></td>"
        f"<td>{label}</td>"
        f"<td>{protocols}</td>"
        f"<td>{workflows}</td>"
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
    return f"<section><h2>{_esc(title)}</h2>{body}</section>"


def _table(headers: Iterable[str], rows: Iterable[str], *, empty: str) -> str:
    headers_list = list(headers)
    header_html = "".join(f"<th>{_esc(h)}</th>" for h in headers_list)
    rows_list = list(rows)
    if not rows_list:
        rows_list = [_empty_row(len(headers_list) or 1, empty)]
    body_html = "".join(rows_list)
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"


def _flow_form(
    apps: list[Mapping[str, Any]],
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
        app_options = '<option value="" disabled selected>No apps yet — create one above</option>'
    key_action = _esc(create_key_action_template.replace("{app_id}", ""))
    return (
        '<form class="flow" method="post" action="' + _esc(create_app_action) + '">'
        "<label>Create an app (e.g. <em>brother-laptop</em>):</label>"
        '<div class="row">'
        '<input type="text" name="name" placeholder="App name" required>'
        '<button type="submit">Create app</button>'
        "</div>"
        "</form>"
        '<form class="flow" method="post" data-action-template="'
        + _esc(create_key_action_template)
        + '" onsubmit="this.action=this.dataset.actionTemplate.replace('
        "'{app_id}', this.app_id.value); return true;\">"
        "<label>Mint an API key for an existing app:</label>"
        '<div class="row">'
        f'<select name="app_id" required>{app_options}</select>'
        '<button type="submit">Mint key</button>'
        "</div>"
        '<input type="hidden" name="placeholder_action" value="' + key_action + '">'
        "</form>"
    )


def _discovery_links(links: Iterable[Mapping[str, Any] | tuple[str, str]]) -> str:
    items: list[str] = []
    for link in links:
        if isinstance(link, Mapping):
            label = link.get("label") or link.get("name") or link.get("href")
            href = link.get("href") or link.get("url") or "#"
        else:
            label, href = link
        items.append(f'<li><a href="{_esc(href)}">{_esc(label)}</a></li>')
    if not items:
        return '<p class="empty">No discovery links configured.</p>'
    return f'<ul class="links">{"".join(items)}</ul>'


def render_admin_dashboard(
    snapshot: Mapping[str, Any],
    *,
    title: str = "Admin · openai-proxy",
    create_app_action: str = "/admin/api/apps",
    create_key_action_template: str = "/admin/api/apps/{app_id}/keys",
) -> str:
    """Render a server-side HTML admin dashboard from a snapshot dict.

    ``snapshot`` keys (all optional):

    * ``actor``: mapping with ``subject`` / ``email`` for the current admin
    * ``counts``: pre-computed counts dict (otherwise derived from lists)
    * ``apps``, ``users``, ``profiles``, ``credentials``, ``audit``: lists
      of mapping records
    * ``discovery_links``: iterable of ``{label, href}`` mappings or
      ``(label, href)`` tuples
    """

    apps = list(snapshot.get("apps") or [])
    users = list(snapshot.get("users") or [])
    profiles = list(snapshot.get("profiles") or [])
    credentials = list(snapshot.get("credentials") or [])
    audit = list(snapshot.get("audit") or [])
    discovery_links = list(snapshot.get("discovery_links") or [])
    actor = snapshot.get("actor") or {}
    actor_label = actor.get("email") or actor.get("subject") or "anonymous"

    counts = snapshot.get("counts") or {
        "users": len(users),
        "apps": len(apps),
        "profiles": len(profiles),
        "credentials": len(credentials),
        "active_credentials": sum(
            1 for c in credentials if not _is_revoked(c)
        ),
    }

    counts_html = "".join(
        _count_card(label, counts.get(key, 0))
        for label, key in (
            ("Apps", "apps"),
            ("Users", "users"),
            ("Profiles", "profiles"),
            ("Credentials", "credentials"),
            ("Active keys", "active_credentials"),
        )
    )

    apps_table = _table(
        ["ID", "Name", "Owner", "Profile", "Created"],
        (_app_row(a) for a in apps),
        empty="No apps yet — create one below.",
    )
    users_table = _table(
        ["ID", "Name", "Email", "Roles"],
        (_user_row(u) for u in users),
        empty="No users yet.",
    )
    profiles_table = _table(
        ["ID", "Label", "Protocols", "Workflows", "Visibility"],
        (_profile_row(p) for p in profiles),
        empty="No app profiles configured.",
    )
    credentials_table = _table(
        ["ID", "App", "Label", "Created", "Status"],
        (_credential_row(c) for c in credentials),
        empty="No credentials issued.",
    )
    audit_table = _table(
        ["When", "Actor", "Action", "Target", "Detail"],
        (_audit_row(e) for e in audit),
        empty="No audit entries yet.",
    )

    flow_html = _flow_form(
        apps,
        create_app_action=create_app_action,
        create_key_action_template=create_key_action_template,
    )

    discovery_html = _discovery_links(discovery_links)

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        f"<style>{_DASHBOARD_CSS}</style>"
        "</head><body>"
        "<header>"
        f"<h1>{_esc(title)}</h1>"
        f'<div class="actor">Signed in as <strong>{_esc(actor_label)}</strong></div>'
        "</header>"
        "<main>"
        + _section("Overview", f'<div class="counts">{counts_html}</div>')
        + _section("Give an app an API key", flow_html)
        + _section("Apps", apps_table)
        + _section("Users", users_table)
        + _section("App profiles", profiles_table)
        + _section("Credentials", credentials_table)
        + _section("Audit log", audit_table)
        + _section("Discovery", discovery_html)
        + "</main></body></html>"
    )
