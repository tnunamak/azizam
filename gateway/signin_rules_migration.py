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
* removes the four lists from ``oidc_provider_config``, so there is one place.
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
                {"notice": NOTICE_REMOVED_OWNER_RULE if dropped else None}, now,
            )
    if oidc is not None and any(key in oidc for key in LEGACY_LISTS):
        _write_setting(conn, OIDC_KEY, {k: v for k, v in oidc.items() if k not in LEGACY_LISTS}, now)

    grants = conn.execute(
        "SELECT * FROM admin_role_assignments WHERE granted_by = ? AND subject_kind='user' "
        "AND role='admin' AND provenance='manual' AND revoked_at IS NULL AND scope_app_id IS NULL",
        (LEGACY_GRANT_SOURCE,),
    ).fetchall()
    for grant in grants:
        conn.execute(
            "UPDATE admin_role_assignments SET provenance='oidc', last_confirmed_at=? WHERE id=?",
            (now, grant["id"]),
        )
        relabelled = conn.execute("SELECT * FROM admin_role_assignments WHERE id=?", (grant["id"],)).fetchone()
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
