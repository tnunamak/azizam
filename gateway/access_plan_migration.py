"""One-time move of admin.db from profiles to access plans (schema 6 -> 7).

Design v4 (docs/design/ACCESS_PLANS.md §4, §8). There is no compatibility
layer: after this runs, the profile tables, key-level plans, runtime
mappings, the built-in default and provider ``allowed_plans`` are gone.

What it does, in one transaction on the caller's connection:

1. Revokes keys that look like test artifacts and keys imported from
   ``proxy_api_keys.yml`` that were never used.
2. Computes what every app and person can do today (key plan, then app plan,
   then the person's plan, then the built-in default) and translates each
   old plan into a v4 plan. Plans with the same rules merge.
3. Gives every app an accountable person (service apps: the gateway owner)
   and exactly one assignment; gives every person a plan that covers their
   apps, so nobody loses access they use today.
4. Disables apps that look like test artifacts (reversible: Apps -> Turn on).
5. Rebuilds the tables without the old columns and deletes the old tables,
   settings and service accounts that only owned apps.

The returned report lists every revoked key, disabled app and plan, so the
operator can reverse any of them. A stored value that has no v4 form stops
the migration (``MigrationError``) and names the value.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from gateway.access_plans import (
    CAPABILITIES,
    PERSON,
    AccessPlan,
    CapabilityGrant,
    DirectEngineAccess,
    PlanCaps,
    plan_rules_json,
    plan_to_document,
    union_plans,
)
from gateway.capability_ids import INTENT_CAPABILITIES

BUILTIN_DEFAULT = "default-local"
OWNER_ROLE = "owner"
LOCAL_ADMIN_KEY_ISSUER = "local-admin-key"  # gateway.admin_runtime
# Words that mark an app or key someone made to try something out.
TEST_ARTIFACT = re.compile(
    r"(?i)(?<![a-z])(test|testing|temp|temporary|probe|proof|triage|delete-me|diagnosis|scratch|dummy)(?![a-z])"
)
IMPORT_SERVICE_ACCOUNT = "legacy-key-import"
IMPORTED_KEY_NAME = re.compile(r"^Imported legacy (?P<name>.+) key$")


class MigrationError(RuntimeError):
    """The data cannot be moved without an owner decision. Nothing changed."""


@dataclass
class MigrationReport:
    plans: list[dict[str, Any]] = field(default_factory=list)
    people: list[dict[str, Any]] = field(default_factory=list)
    apps: list[dict[str, Any]] = field(default_factory=list)
    revoked_keys: list[dict[str, Any]] = field(default_factory=list)
    disabled_apps: list[dict[str, Any]] = field(default_factory=list)
    renamed_keys: list[dict[str, Any]] = field(default_factory=list)
    deleted_profiles: list[dict[str, Any]] = field(default_factory=list)
    deleted_service_accounts: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items()}

    def lines(self) -> list[str]:
        out = ["Access plans migration (schema 7):"]
        for plan in self.plans:
            out.append(f"  plan {plan['name']!r} ({plan['id']}) from {', '.join(plan['from']) or 'new'}")
        for person in self.people:
            out.append(f"  person {person['name']!r}: {person['plan']}")
        for app in self.apps:
            out.append(f"  app {app['name']!r} ({app['id']}): {app['assignment']}"
                       + (f", accountable person {app['accountable']}" if app.get("accountable") else ""))
        for key in self.revoked_keys:
            out.append(f"  revoked key {key['name']!r} ({key['id']}) on {key['app']!r}: {key['reason']}")
        for app in self.disabled_apps:
            out.append(f"  disabled app {app['name']!r} ({app['id']}): {app['reason']}")
        for key in self.renamed_keys:
            out.append(f"  renamed key {key['id']}: {key['from']!r} -> {key['to']!r}")
        for profile in self.deleted_profiles:
            out.append(f"  deleted old plan {profile['name']!r} ({profile['id']}): {profile['outcome']}")
        for account in self.deleted_service_accounts:
            out.append(f"  deleted service account {account!r} (it only owned apps)")
        if self.dropped:
            out.append(f"  removed: {', '.join(self.dropped)}")
        return out


# ---------------------------------------------------------------------------
# Old plan -> v4 plan
# ---------------------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _capabilities_for_intents(intents: Iterable[str]) -> set[str]:
    """Old ``allow.intents`` matched an intent or any intent below it."""
    granted: set[str] = set()
    for allowed in intents:
        allowed = str(allowed)
        for intent, capability in INTENT_CAPABILITIES.items():
            if intent == allowed or intent.startswith(f"{allowed}."):
                granted.add(capability)
    return granted


def _provider_ids(value: Any) -> set[str]:
    from gateway.service_registry import canonical_provider_id

    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return set()
    return {canonical_provider_id(item) for item in value if canonical_provider_id(item)}


def _provider_allows(provider_admin: Mapping[str, Any], provider: str, plan_json_id: str) -> bool:
    """Old provider ``allowed_plans``: absent = any plan; else membership by JSON id."""
    policy = _mapping(_mapping(provider_admin.get("providers")).get(provider))
    if "allowed_plans" not in policy:
        return True
    raw = policy.get("allowed_plans")
    if isinstance(raw, str):
        allowed = {raw}
    elif isinstance(raw, (list, tuple)):
        allowed = {str(item) for item in raw}
    else:
        allowed = set()
    return plan_json_id in allowed


def translate_profile(
    doc: Mapping[str, Any] | None,
    *,
    json_id: str,
    label: str,
    provider_admin: Mapping[str, Any],
) -> AccessPlan:
    """Translate one stored profile (or the built-in default, ``doc=None``)."""
    doc = _mapping(doc)
    allow = _mapping(doc.get("allow"))
    for key in ("exposure", "trust_tiers", "resource_tiers", "tags"):
        if allow.get(key) is not None:
            raise MigrationError(f"{label}: allow.{key} has no access-plan form; remove it first")
    if doc.get("backends") is not None:
        raise MigrationError(f"{label}: backends has no access-plan form; remove it first")
    remote = _mapping(doc.get("remote"))
    caps = {}
    for old, new in (("max_usd_per_request", "usd_per_request"), ("budget_usd_per_day", "usd_per_day"),
                     ("budget_usd_per_month", "usd_per_month")):
        value = remote.get(old)
        if value is None:
            caps[new] = None
            continue
        try:
            caps[new] = Decimal(str(value))
        except InvalidOperation as exc:
            raise MigrationError(f"{label}: remote.{old} {value!r} is not a number of dollars") from exc
        if not caps[new].is_finite() or caps[new] <= 0:
            raise MigrationError(f"{label}: remote.{old} {value!r} is not a positive number of dollars")
    target = _mapping(doc.get("target_policy"))
    if str(_mapping(doc.get("model_access")).get("mode") or "") == "current_loaded" and not target:
        target = {"visibility": "available", "activation": "existing_only"}
    if str(target.get("visibility") or "catalog") != "catalog":
        raise MigrationError(f"{label}: target_policy.visibility {target.get('visibility')!r} has no access-plan form")

    intents = allow.get("intents")
    capabilities = set(CAPABILITIES) if intents is None else _capabilities_for_intents(intents)
    models, workflows = doc.get("models"), doc.get("workflows")
    if models is None and workflows is None:
        grant = CapabilityGrant(all_items=True)
    else:
        grant = CapabilityGrant(
            models=frozenset(str(item) for item in models or ()),
            workflows=frozenset(str(item) for item in workflows or ()),
        )
    native = _mapping(doc.get("native_passthrough"))
    if native.get("enabled"):
        engines = [str(item) for item in native.get("allowed_backends") or ()]
        direct = DirectEngineAccess(
            all_engines=not engines,
            engines=frozenset(engines),
            operations=frozenset(str(item) for item in native.get("allowed_operations") or ()) or frozenset({"*"}),
        )
    else:
        direct = DirectEngineAccess()
    paid = {
        provider for provider in _provider_ids(remote.get("providers"))
        if _provider_allows(provider_admin, provider, json_id)
    }
    receipt_detail = str(_mapping(doc.get("status_visibility")).get("level") or "basic")
    media_metadata = str(_mapping(doc.get("media_metadata")).get("default") or "none")
    return AccessPlan(
        id="",
        name="",
        can_use={capability: grant for capability in CAPABILITIES if capability in capabilities},
        direct_engine_access=direct,
        paid_providers=frozenset(paid),
        provider_fallback=bool(_mapping(doc.get("fallback")).get("cloud_allowed")) and bool(remote.get("allow_paid_fallback")),
        caps=PlanCaps(**caps),
        can_load_models=str(target.get("activation") or "may_activate") != "existing_only",
        receipt_detail=receipt_detail,
        media_metadata=media_metadata,
    )


# Design §8 names; the receipt detail each standard plan has there.
_STANDARD_RECEIPT_DETAIL = {
    "Everything": "full", "Local and paid": "full",
    "Local with direct engine access": "full", "Local only": "basic",
}


def _standard_name(plan: AccessPlan) -> str | None:
    """Design §8's name for a plan that grants every item, else None.

    A variant whose receipt detail differs from the standard plan's keeps
    today's detail and says so: "Local only (standard receipts)".
    """
    if plan.caps.any() or not all(plan.can_use.get(cap, CapabilityGrant()).all_items for cap in CAPABILITIES):
        return None
    direct, paid = plan.direct_engine_access.granted, bool(plan.paid_providers)
    if direct and paid:
        name = "Everything"
    elif paid:
        name = "Local and paid"
    elif direct:
        name = "Local with direct engine access"
    else:
        name = "Local only"
    if plan.receipt_detail != _STANDARD_RECEIPT_DETAIL[name]:
        name = f"{name} ({plan.receipt_detail} receipts)"
    return name


# ---------------------------------------------------------------------------
# Reading today's state
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _setting(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _active(key: Mapping[str, Any], now: int) -> bool:
    if key.get("revoked_at") is not None:
        return False
    expires = key.get("expires_at")
    return expires is None or int(expires) > now


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def migrate(conn: sqlite3.Connection, *, now: int | None = None) -> MigrationReport:
    """Move a schema-6 admin.db to access plans. Caller owns the transaction.

    The caller must disable foreign keys before its transaction (tables are
    rebuilt) and run ``PRAGMA foreign_key_check`` before committing.
    """
    ts = int(time.time()) if now is None else int(now)
    report = MigrationReport()
    if not _table_exists(conn, "profiles"):
        _rebuild_tables(conn)
        return report

    has_access = _table_exists(conn, "account_access")
    has_links = _table_exists(conn, "account_links")
    provider_admin = _mapping(_setting(conn, "provider_admin"))
    user_defaults = _mapping(_setting(conn, "user_access_defaults"))
    self_service = user_defaults.get("self_service_apps", True) is not False

    if _table_exists(conn, "runtime_profile_mappings"):
        active = _rows(conn, "SELECT source_kind, source_value FROM runtime_profile_mappings WHERE disabled_at IS NULL")
        if active:
            names = ", ".join(f"{row['source_kind']}={row['source_value']}" for row in active)
            raise MigrationError(
                f"active plan mappings ({names}) choose plans at sign-in; give those people a plan "
                "in People and disable the mappings first"
            )

    profiles = {row["id"]: row for row in _rows(conn, "SELECT id, name, profile_json FROM profiles")}
    translated: dict[str, AccessPlan] = {}
    for profile_id, row in profiles.items():
        try:
            doc = json.loads(row["profile_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationError(f"access plan {row['name']!r} is not valid JSON") from exc
        translated[profile_id] = translate_profile(
            doc, json_id=str(_mapping(doc).get("id") or profile_id), label=f"access plan {row['name']!r}",
            provider_admin=provider_admin,
        )
    translated[BUILTIN_DEFAULT] = translate_profile(
        None, json_id=BUILTIN_DEFAULT, label="the built-in default", provider_admin=provider_admin)

    users = {row["id"]: row for row in _rows(conn, "SELECT id, issuer, email, display_name FROM users")}
    people = {uid: user for uid, user in users.items() if user["issuer"] != LOCAL_ADMIN_KEY_ISSUER}
    person_plan_ids: dict[str, str | None] = {}
    if has_access:
        for row in _rows(conn, "SELECT user_id, profile_id FROM account_access"):
            person_plan_ids[row["user_id"]] = row["profile_id"]
    owners = [row["subject_id"] for row in _rows(
        conn,
        "SELECT subject_id FROM admin_role_assignments WHERE role=? AND subject_kind='user' "
        "AND revoked_at IS NULL ORDER BY granted_at, id", (OWNER_ROLE,),
    ) if row["subject_id"] in people]
    gateway_owner = owners[0] if owners else None

    apps = {row["id"]: row for row in _rows(conn, "SELECT * FROM apps ORDER BY created_at, id")}
    keys = _rows(conn, "SELECT id, subject_kind, subject_id, app_id, profile_id, display_name, prefix, "
                       "revoked_at, expires_at, last_used_at FROM credentials WHERE subject_kind='app' ORDER BY created_at, id")
    service_accounts = {row["id"]: row["name"] for row in _rows(conn, "SELECT id, name FROM service_accounts")}

    # 1. Revoke test keys and imported keys nobody ever used.
    for key in keys:
        if not _active(key, ts):
            continue
        app = apps.get(str(key["app_id"] or key["subject_id"]))
        app_name = app["name"] if app else "an unknown app"
        name = str(key["display_name"] or key["prefix"] or "")
        if TEST_ARTIFACT.search(name):
            reason = "its name marks it as a test key"
        elif IMPORTED_KEY_NAME.match(name) and key["last_used_at"] is None:
            reason = "imported from proxy_api_keys.yml and never used"
        else:
            continue
        conn.execute("UPDATE credentials SET revoked_at=? WHERE id=?", (ts, key["id"]))
        key["revoked_at"] = ts
        report.revoked_keys.append({"id": key["id"], "name": name, "app": app_name, "reason": reason})

    # 2. Today's effective plan of each app.
    def person_plan_today(user_id: str | None) -> str:
        if user_id and user_id in people and person_plan_ids.get(user_id):
            return str(person_plan_ids[user_id])
        return BUILTIN_DEFAULT

    def app_plan_today(app: Mapping[str, Any]) -> str:
        if app.get("default_profile_id"):
            return str(app["default_profile_id"])
        owner = app.get("owner_user_id")
        if self_service and owner and owner in people:
            return person_plan_today(owner)
        return BUILTIN_DEFAULT

    for plan_id in [*person_plan_ids.values(), *(a.get("default_profile_id") for a in apps.values()),
                    *(k.get("profile_id") for k in keys if _active(k, ts))]:
        if plan_id and plan_id not in translated:
            raise MigrationError(f"a person, app or key uses access plan {plan_id!r}, which does not exist")

    app_effective: dict[str, str] = {}
    for app_id, app in apps.items():
        active = [key for key in keys if str(key["app_id"] or key["subject_id"]) == app_id and _active(key, ts)]
        effective = {str(key["profile_id"]) if key["profile_id"] else app_plan_today(app) for key in active}
        if not effective:
            effective = {app_plan_today(app)}
        rules = {plan_rules_json(translated[plan]) for plan in effective}
        if len(rules) > 1:
            names = ", ".join(sorted(profiles[p]["name"] if p in profiles else "the built-in default" for p in effective))
            raise MigrationError(
                f"app {app['name']!r} has active keys on different access plans ({names}); "
                "revoke keys or move them to one plan first"
            )
        app_effective[app_id] = sorted(effective)[0]

    # 3. Accountable people.
    accountable: dict[str, str] = {}
    for app_id, app in apps.items():
        owner = app.get("owner_user_id")
        if owner and owner in people:
            accountable[app_id] = owner
        elif gateway_owner is not None:
            accountable[app_id] = gateway_owner
        else:
            raise MigrationError(
                f"app {app['name']!r} has no person who answers for it and the gateway has no owner"
            )

    # 4. Disable test apps.
    for app_id, app in apps.items():
        if app.get("disabled_at") is not None:
            continue
        has_active_key = any(str(k["app_id"] or k["subject_id"]) == app_id and _active(k, ts) for k in keys)
        if TEST_ARTIFACT.search(str(app["name"])):
            reason = "its name marks it as a test app"
        elif service_accounts.get(app.get("owner_service_account_id")) == IMPORT_SERVICE_ACCOUNT and not has_active_key:
            reason = "imported from proxy_api_keys.yml and has no key left"
        else:
            continue
        conn.execute("UPDATE apps SET disabled_at=? WHERE id=?", (ts, app_id))
        app["disabled_at"] = ts
        report.disabled_apps.append({"id": app_id, "name": app["name"], "reason": reason})

    # 5. Person plans cover what their enabled apps use today.
    person_rules: dict[str, AccessPlan] = {}
    for user_id in people:
        assigned = person_plan_ids.get(user_id)
        if assigned:
            person_rules[user_id] = translated[assigned]
            continue
        used = [translated[person_plan_today(user_id)]]
        used += [translated[app_effective[a]] for a, who in accountable.items()
                 if who == user_id and apps[a].get("disabled_at") is None]
        person_rules[user_id] = union_plans("", "", used)

    # 6. One stored plan per distinct rule set.
    stored: dict[str, dict[str, Any]] = {}

    def stored_plan(plan: AccessPlan, source: str | None) -> str:
        key = plan_rules_json(plan)
        if key not in stored:
            stored[key] = {"plan": plan, "id": _gen_id("plan"), "from": []}
        if source and source not in stored[key]["from"]:
            stored[key]["from"].append(source)
        return stored[key]["id"]

    def source_name(old_id: str) -> str:
        return profiles[old_id]["name"] if old_id in profiles else "the built-in default"

    person_assignment: dict[str, str] = {}
    for user_id, plan in person_rules.items():
        assigned = person_plan_ids.get(user_id)
        person_assignment[user_id] = stored_plan(plan, source_name(assigned) if assigned else None)
    app_assignment: dict[str, str] = {}
    for app_id, app in apps.items():
        plan = translated[app_effective[app_id]]
        person = accountable[app_id]
        if app["kind"] == "personal" and plan_rules_json(plan) == plan_rules_json(person_rules[person]):
            app_assignment[app_id] = PERSON
            stored_plan(plan, source_name(app_effective[app_id]))
        else:
            app_assignment[app_id] = stored_plan(plan, source_name(app_effective[app_id]))

    used_names: set[str] = set()
    for entry in stored.values():
        plan, sources = entry["plan"], entry["from"]
        olds = [s for s in sources if s != "the built-in default"]
        name = _standard_name(plan) or (re.sub(r"^Legacy ", "", olds[0]) if olds else "Custom")
        base, n = name, 2
        while name in used_names:
            name, n = f"{base} {n}", n + 1
        used_names.add(name)
        entry["name"] = name

    # 7. Rebuild tables in the v7 shape and write the new rows.
    _rebuild_tables(conn)
    for entry in stored.values():
        doc = plan_to_document(AccessPlan(**{**entry["plan"].__dict__, "id": entry["id"], "name": entry["name"]}))
        conn.execute(
            "INSERT INTO access_plans(id, name, plan_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (entry["id"], entry["name"], json.dumps(doc, sort_keys=True), ts, ts),
        )
        report.plans.append({"id": entry["id"], "name": entry["name"], "from": entry["from"]})
    plan_names = {entry["id"]: entry["name"] for entry in stored.values()}
    for app_id, app in apps.items():
        conn.execute(
            "UPDATE apps SET owner_user_id=?, access_plan=? WHERE id=?",
            (accountable[app_id], app_assignment[app_id], app_id),
        )
        assignment = app_assignment[app_id]
        report.apps.append({
            "id": app_id, "name": app["name"],
            "assignment": ("same as its person (" + plan_names[person_assignment[accountable[app_id]]] + ")")
            if assignment == PERSON else plan_names[assignment],
            "accountable": users[accountable[app_id]].get("display_name") or users[accountable[app_id]].get("email")
            if app.get("owner_user_id") != accountable[app_id] else None,
        })
    for user_id in people:
        if has_access:
            conn.execute(
                "INSERT INTO account_access(user_id, plan_id) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET plan_id=excluded.plan_id",
                (user_id, person_assignment[user_id]),
            )
        report.people.append({
            "id": user_id, "name": people[user_id].get("display_name") or people[user_id].get("email"),
            "plan": plan_names[person_assignment[user_id]],
        })
    if has_links:
        # Open invitations keep their plan; a plan nobody else holds loses it.
        conn.execute("CREATE TEMP TABLE plan_map(old TEXT PRIMARY KEY, new TEXT)")
        by_rules = {rules: entry["id"] for rules, entry in stored.items()}
        conn.executemany("INSERT INTO plan_map(old, new) VALUES (?, ?)", [
            (old, by_rules.get(plan_rules_json(plan))) for old, plan in translated.items()
        ])
        _swap(conn, "account_links", _ACCOUNT_LINKS_V7,
              "id, kind, token_hash, plan_id, user_id, email, created_by, created_at, expires_at, used_at, revoked_at",
              "id, kind, token_hash, (SELECT new FROM plan_map WHERE old = profile_id), user_id, email, "
              "created_by, created_at, expires_at, used_at, revoked_at")
        conn.execute("DROP TABLE plan_map")

    for key in keys:
        match = IMPORTED_KEY_NAME.match(str(key["display_name"] or ""))
        if match and _active(key, ts):
            new_name = f"{match['name']} key"
            conn.execute("UPDATE credentials SET display_name=? WHERE id=?", (new_name, key["id"]))
            report.renamed_keys.append({"id": key["id"], "from": key["display_name"], "to": new_name})

    held = {app_effective[a] for a in apps} | {p for p in person_plan_ids.values() if p}
    for profile_id, row in profiles.items():
        report.deleted_profiles.append({
            "id": profile_id, "name": row["name"],
            "outcome": "rules kept in " + plan_names[stored_plan(translated[profile_id], None)]
            if profile_id in held else "nobody used it",
        })

    # Service accounts that existed only to own apps.
    for account_id, name in service_accounts.items():
        owned = any(app.get("owner_service_account_id") == account_id for app in apps.values())
        has_keys = conn.execute(
            "SELECT 1 FROM credentials WHERE subject_kind='service_account' AND subject_id=?", (account_id,)
        ).fetchone()
        has_roles = conn.execute(
            "SELECT 1 FROM admin_role_assignments WHERE subject_kind='service_account' AND subject_id=?", (account_id,)
        ).fetchone()
        if owned and not has_keys and not has_roles:
            conn.execute("DELETE FROM service_accounts WHERE id=?", (account_id,))
            report.deleted_service_accounts.append(name)

    for setting in ("user_access_defaults", "lan_placeholder_keys"):
        if conn.execute("DELETE FROM operator_settings WHERE key=?", (setting,)).rowcount:
            report.dropped.append(f"setting {setting}")
        conn.execute("DELETE FROM operator_settings_generations WHERE setting_key=?", (setting,))
    if provider_admin and any("allowed_plans" in _mapping(p) for p in _mapping(provider_admin.get("providers")).values()):
        cleaned = json.loads(json.dumps(provider_admin))
        for policy in _mapping(cleaned.get("providers")).values():
            if isinstance(policy, dict):
                policy.pop("allowed_plans", None)
        conn.execute(
            "UPDATE operator_settings SET value_json=?, updated_at=? WHERE key='provider_admin'",
            (json.dumps(cleaned, ensure_ascii=False, sort_keys=True), ts),
        )
        report.dropped.append("provider allowed_plans")
    conn.execute("DROP TABLE IF EXISTS profiles")
    conn.execute("DROP TABLE IF EXISTS runtime_profile_mappings")
    report.dropped[:0] = ["table profiles", "table runtime_profile_mappings", "column credentials.profile_id",
                          "column apps.default_profile_id", "column apps.owner_service_account_id"]
    conn.execute(
        "INSERT INTO audit_events(id, created_at, actor_kind, actor_id, actor_label, action, target_kind, "
        "target_id, metadata_json) VALUES (?, ?, 'system', 'migration', 'System', 'access_plans.migrate', "
        "'admin_db', 'schema-7', ?)",
        (_gen_id("aud"), ts, json.dumps(report.as_dict(), sort_keys=True)),
    )
    return report


_APPS_V7 = """
CREATE TABLE apps_v7 (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  access_plan TEXT,
  created_at INTEGER NOT NULL,
  disabled_at INTEGER,
  FOREIGN KEY(owner_user_id) REFERENCES users(id)
)"""
_CREDENTIALS_V7 = """
CREATE TABLE credentials_v7 (
  id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  app_id TEXT,
  hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  display_name TEXT,
  scopes_json TEXT,
  created_at INTEGER NOT NULL,
  created_by TEXT,
  expires_at INTEGER,
  revoked_at INTEGER,
  last_used_at INTEGER,
  session_id TEXT,
  FOREIGN KEY(app_id) REFERENCES apps(id)
)"""
_ACCOUNT_ACCESS_V7 = """
CREATE TABLE account_access_v7 (
 user_id TEXT PRIMARY KEY REFERENCES users(id), plan_id TEXT REFERENCES access_plans(id),
 disabled_at INTEGER, removed_at INTEGER, require_registered_session INTEGER NOT NULL DEFAULT 0
)"""
_ACCOUNT_LINKS_V7 = """
CREATE TABLE account_links_v7 (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 plan_id TEXT REFERENCES access_plans(id), user_id TEXT REFERENCES users(id), email TEXT,
 created_by TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 used_at INTEGER, revoked_at INTEGER
)"""


def _swap(conn: sqlite3.Connection, table: str, ddl: str, columns: str, select: str) -> None:
    conn.execute(ddl)
    conn.execute(f"INSERT INTO {table}_v7({columns}) SELECT {select} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_v7 RENAME TO {table}")


def _rebuild_tables(conn: sqlite3.Connection) -> None:
    """Rebuild the tables whose old columns go away. Triggers and indexes are
    recreated by the caller's schema script."""
    if "default_profile_id" in _columns(conn, "apps") or "owner_service_account_id" in _columns(conn, "apps"):
        # Placeholder owner keeps NOT NULL during the copy; migrate() sets every row.
        _swap(conn, "apps", _APPS_V7, "id, kind, name, owner_user_id, access_plan, created_at, disabled_at",
              "id, kind, name, COALESCE(owner_user_id, ''), NULL, created_at, disabled_at")
    if "profile_id" in _columns(conn, "credentials"):
        _swap(conn, "credentials", _CREDENTIALS_V7,
              "id, subject_kind, subject_id, app_id, hash, prefix, display_name, scopes_json, created_at, "
              "created_by, expires_at, revoked_at, last_used_at, session_id",
              "id, subject_kind, subject_id, app_id, hash, prefix, display_name, scopes_json, created_at, "
              "created_by, expires_at, revoked_at, last_used_at, session_id")
    if _table_exists(conn, "account_access") and "profile_id" in _columns(conn, "account_access"):
        _swap(conn, "account_access", _ACCOUNT_ACCESS_V7,
              "user_id, plan_id, disabled_at, removed_at, require_registered_session",
              "user_id, NULL, disabled_at, removed_at, require_registered_session")


__all__ = ["MigrationError", "MigrationReport", "migrate", "translate_profile"]
