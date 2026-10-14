"""One-time data migration (admin schema version 9): sign-in lists become rules.

Before version 9 the identity provider's allow-lists lived in
``oidc_provider_config`` (``allowed_email_domains``, ``admin_emails``,
``admin_groups``, ``admin_roles``) and one function read them at sign-in.
Version 9 keeps who may sign in, and what they get, as ordered rules in the
``signin_rules`` setting (gateway.signin_rules). This migration:

* writes the rules that mean what the old lists meant. Domains and emails let
  people in; groups and roles make them administrators (which implies letting
  them in). An email that belongs to an active owner is dropped: the owner is
  always let in, so a rule that only names the owner does nothing. A one-time
  note tells the owner (``signin_status.notice``).
* keeps the old behaviour for "everyone else". The old code let everyone in
  while both the email list and the domain list were empty and the provider
  was on. That becomes ``fallback = "open"``. Anything else is ``"closed"``.
* removes the old lists from ``oidc_provider_config``, so there is one place.
  ``admin_emails`` stays while no owner has ever existed: it names who may
  become the first owner, and nothing else.
* marks the administrator grants the old group rule gave by hand-written
  provenance. Earlier code recorded them with ``granted_by =
  'oidc-trusted-group'`` and provenance ``manual``, so nothing ever revoked
  them. They become rule-derived (provenance ``oidc``) and get one session
  length to be confirmed by a sign-in, like every grant the provider gives.

Idempotent: it never overwrites a ``signin_rules`` setting that exists, and it
only touches grants that are still marked as above.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

SIGNIN_RULES_KEY = "signin_rules"
SIGNIN_STATUS_KEY = "signin_status"
OIDC_KEY = "oidc_provider_config"
LEGACY_LISTS = ("allowed_email_domains", "admin_emails", "admin_groups", "admin_roles")
LEGACY_GRANT_SOURCE = "oidc-trusted-group"
AUDIT_ACTION = "operator.signin_rules.migrate"
NOTICE_REMOVED_OWNER_RULE = "removed_owner_rule"
# An upgrade that kept the old open door: everyone with a provider account can still get in.
NOTICE_OPEN_DOOR = "open_door"


def _setting(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_setting(conn: sqlite3.Connection, key: str, value: dict[str, Any], now: int) -> None:
    conn.execute(
        "INSERT INTO operator_settings(key, value_json, updated_at, updated_by) VALUES (?,?,?,'migration') "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True), now),
    )
    conn.execute(
        "INSERT INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES (?,1) "
        "ON CONFLICT(setting_key) DO UPDATE SET operator_settings_generation=operator_settings_generation+1",
        (key,),
    )


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _owner_emails(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT LOWER(u.email) FROM admin_role_assignments r JOIN users u ON u.id = r.subject_id "
        "WHERE r.role='owner' AND r.subject_kind='user' AND r.revoked_at IS NULL "
        "AND r.scope_app_id IS NULL AND u.email IS NOT NULL"
    ).fetchall()
    return {row[0] for row in rows if row[0]}


def _rules_from_lists(oidc: dict[str, Any], owner_emails: set[str]) -> tuple[dict[str, Any], list[str]]:
    """The rules document for the old lists, and the owner emails it dropped."""
    domains = [d.lower() for d in _strings(oidc.get("allowed_email_domains"))]
    emails = [e.lower() for e in _strings(oidc.get("admin_emails"))]
    groups = _strings(oidc.get("admin_groups"))
    roles = _strings(oidc.get("admin_roles"))
    dropped = [e for e in emails if e in owner_emails]
    kept_emails = [e for e in emails if e not in owner_emails]
    rules: list[dict[str, Any]] = []

    def add(field: str, values: list[str], *, admin: bool) -> None:
        if values:
            rules.append({
                "id": f"rule_{len(rules) + 1}", "field": field, "values": values,
                "admit": True, "admin": admin, "plan": None,
            })

    add("group", groups, admin=True)
    add("role", roles, admin=True)
    add("domain", domains, admin=False)
    add("email", kept_emails, admin=False)
    # The old code let everyone in while both lists were empty and the provider was on.
    open_door = bool(oidc.get("enabled")) and not emails and not domains
    return {"fallback": "open" if open_door else "closed", "rules": rules}, dropped


def migrate(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run inside the caller's transaction. Returns what changed, for the audit entry and tests."""
    from gateway import admin_store

    now = int(time.time())
    report: dict[str, Any] = {"rules": None, "fallback": None, "dropped_owner_emails": 0, "relabelled_grants": []}

    oidc = _setting(conn, OIDC_KEY)
    if _setting(conn, SIGNIN_RULES_KEY) is None:
        doc, dropped = _rules_from_lists(oidc or {}, _owner_emails(conn))
        _write_setting(conn, SIGNIN_RULES_KEY, doc, now)
        report.update(rules=doc["rules"], fallback=doc["fallback"], dropped_owner_emails=len(dropped))
        if _setting(conn, SIGNIN_STATUS_KEY) is None:
            _write_setting(
                conn, SIGNIN_STATUS_KEY,
                {"notice": NOTICE_REMOVED_OWNER_RULE if dropped
                 else NOTICE_OPEN_DOOR if doc["fallback"] == "open" else None,
                 # The lists the database already answered for: the config file's copies never apply to them.
                 "database_lists": sorted(key for key in LEGACY_LISTS if oidc and key in oidc)}, now,
            )
    if oidc is not None and any(key in oidc for key in LEGACY_LISTS):
        owner_ever = conn.execute("SELECT 1 FROM admin_role_assignments WHERE role='owner' LIMIT 1").fetchone()
        _write_setting(conn, OIDC_KEY, {
            k: v for k, v in oidc.items() if k not in LEGACY_LISTS or (k == "admin_emails" and not owner_ever)
        }, now)

    # The caller's connection may return plain tuples; the grant helpers read columns by name.
    rows = conn.cursor()
    rows.row_factory = sqlite3.Row
    grants = rows.execute(
        "SELECT * FROM admin_role_assignments WHERE granted_by = ? AND subject_kind='user' "
        "AND role='admin' AND provenance='manual' AND revoked_at IS NULL AND scope_app_id IS NULL",
        (LEGACY_GRANT_SOURCE,),
    ).fetchall()
    for grant in grants:
        conn.execute(
            "UPDATE admin_role_assignments SET provenance='oidc', last_confirmed_at=? WHERE id=?",
            (now, grant["id"]),
        )
        relabelled = rows.execute("SELECT * FROM admin_role_assignments WHERE id=?", (grant["id"],)).fetchone()
        admin_store._audit_admin_role(conn, "admin_role.relabel", relabelled, now,
                                      actor_kind="system", actor_id="migration")
        report["relabelled_grants"].append(grant["id"])

    admin_store._record_audit_in_transaction(
        conn, AUDIT_ACTION,
        actor_kind="system", actor_id="migration", actor_label="Schema 9 migration",
        target_kind="operator_setting", target_id=SIGNIN_RULES_KEY,
        metadata={
            "rules": [
                {"field": rule["field"], "values": len(rule["values"]), "admin": rule["admin"]}
                for rule in (report["rules"] or [])
            ],
            "fallback": report["fallback"],
            "dropped_owner_emails": report["dropped_owner_emails"],
            "relabelled_grants": len(report["relabelled_grants"]),
        },
        now=now,
    )
    return report


def adopt_bootstrap_lists(path, bootstrap, env) -> bool:
    """First start only: turn lists a deployment kept in its config file into rules.

    Before schema 9 ``admin.oidc.allowed_email_domains``, ``admin_groups`` and
    ``admin_roles`` could live in the config file or in environment variables
    (``admin_groups_env``). The upgrade of admin.db cannot see them, so the first
    start after it reads them once, adds the rules they meant (skipping any that
    exist), and remembers it did (``signin_status.bootstrap_lists_adopted``).
    Later edits are the owner's, made on the Sign-in page. Returns True if it
    changed the rules.
    """
    from gateway import admin_store

    status_record = admin_store.get_operator_setting(SIGNIN_STATUS_KEY, path=path)
    status = dict(status_record["value"]) if status_record else {}
    if status.get("bootstrap_lists_adopted"):
        return False

    def listed(key: str) -> list[str]:
        value = bootstrap.get(key)
        if not value and bootstrap.get(f"{key}_env"):
            value = [part.strip() for chunk in str(env.get(str(bootstrap[f"{key}_env"]), "")).splitlines()
                     for part in chunk.split(",")]
        return _strings(value if isinstance(value, (list, str)) else [])

    # ``database_lists`` is written only by ``migrate``, so it marks a database that held sign-in data before
    # schema 9. A new database has none: the door stays closed there whatever the config file enables.
    upgraded = "database_lists" in status
    answered = set(status.get("database_lists", []))
    lists = {key: ([] if key in answered else listed(key))
             for key in ("allowed_email_domains", "admin_emails", "admin_groups", "admin_roles")}
    # Whether the door was open depends on both allow-lists; if the database answered either, it decided.
    door_decided = bool(answered & {"allowed_email_domains", "admin_emails"})
    changed = False
    if any(lists.values()) or (bootstrap.get("enabled") and not door_decided):
        with admin_store._conn(path) as conn:
            owners = _owner_emails(conn)
            record = conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (SIGNIN_RULES_KEY,)).fetchone()
        current = json.loads(record[0]) if record else {"fallback": "closed", "rules": []}
        proposed, _ = _rules_from_lists({**lists, "enabled": bool(bootstrap.get("enabled"))}, owners)
        have = {(r["field"], tuple(r["values"]), r["admin"]) for r in current.get("rules", [])}
        added = [dict(r, id=f"rule_{len(current.get('rules', [])) + i + 1}")
                 for i, r in enumerate(r for r in proposed["rules"]
                                       if (r["field"], tuple(r["values"]), r["admin"]) not in have)]
        fallback = current.get("fallback", "closed")
        opened = False
        if upgraded and not door_decided and not current.get("rules") and fallback == "closed" \
                and proposed["fallback"] == "open":
            fallback = "open"
            opened = True
        if added or fallback != current.get("fallback"):
            admin_store.put_operator_setting(
                SIGNIN_RULES_KEY, {"fallback": fallback, "rules": [*current.get("rules", []), *added]},
                updated_by="migration", path=path)
            changed = True
            if opened and not status.get("notice"):
                status["notice"] = NOTICE_OPEN_DOOR
    status["bootstrap_lists_adopted"] = True
    admin_store.put_operator_setting(SIGNIN_STATUS_KEY, status, updated_by="migration", path=path)
    return changed
