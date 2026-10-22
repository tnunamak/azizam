"""Store-backed parts of sign-in rules: facts about people, the preview, enforcement.

``gateway.signin_rules`` is the one evaluator. This module feeds it facts read
from admin.db and applies what it answers:

* :func:`decide` is the answer at provider sign-in.
* :func:`preview` is the What changes answer for a proposed state, computed
  with the same evaluator over the claims each person last signed in with.
* :func:`apply_changes` makes a saved proposal true: it removes what rules no
  longer support (administrator grants, rule-given plans, sessions, admin keys)
  and gives plans that rules now give. Administrator grants a rule newly gives
  arrive at the person's next sign-in, because a grant counts only once a
  sign-in confirmed it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from gateway import accounts, admin_store, remembered_people, session_guard, signin_rules
from gateway.session_guard import BrowserSession
from gateway.admin_runtime import LOCAL_ADMIN_KEY_ISSUER
from gateway.signin_rules import Claims, Outcome, Rules, Standing

RULES_KEY = "signin_rules"
STATUS_KEY = "signin_status"
SYSTEM_ACTOR = "signin-rules"


def norm_issuer(address: str) -> str:
    """The provider address as compared: no spaces, no trailing slash, scheme and host in lower case.

    The path keeps its case. Two addresses that differ only in these ways are the same provider.
    This is for comparing. What is stored is the issuer the provider names, which a sign-in
    checks character for character.
    """
    text = (address or "").strip().rstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.netloc:
        return text
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, parts.fragment))


class SignInError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


def plan_names(path: Path) -> dict[str, str]:
    return {str(plan["id"]): str(plan["name"]) for plan in admin_store.list_access_plans(path=path)}


def load_rules(path: Path) -> Rules:
    """The saved rules. A damaged document keeps everyone out (deny by default)."""
    record = admin_store.get_operator_setting(RULES_KEY, path=path)
    if not record:
        return Rules()
    try:
        return signin_rules.parse_rules(record["value"])
    except signin_rules.RulesError:
        return Rules()


def load_status(path: Path) -> dict[str, Any]:
    record = admin_store.get_operator_setting(STATUS_KEY, path=path)
    value = record["value"] if record else {}
    return dict(value) if isinstance(value, Mapping) else {}


def write_status(path: Path, status: Mapping[str, Any], *, updated_by: str = SYSTEM_ACTOR) -> None:
    admin_store.put_operator_setting(STATUS_KEY, dict(status), updated_by=updated_by, path=path)


def write_status_in(conn: sqlite3.Connection, status: Mapping[str, Any], *, updated_by: str = SYSTEM_ACTOR) -> None:
    """``write_status`` inside a transaction the caller owns and commits."""
    admin_store.put_operator_setting_in(conn, STATUS_KEY, dict(status), updated_by=updated_by)


def revisions(path: Path) -> str:
    """One revision for everything the page saves together."""
    parts = [
        admin_store.operator_setting_revision(admin_store.get_operator_setting(key, path=path))
        for key in (RULES_KEY, "oidc_provider_config", "sign_in", remembered_people.SETTING_KEY)
    ]
    return "-".join(parts)


def revisions_in(conn: sqlite3.Connection) -> str:
    """:func:`revisions` as one open transaction sees it."""
    return "-".join(admin_store.operator_setting_revision_in(conn, key)
                    for key in (RULES_KEY, "oidc_provider_config", "sign_in", remembered_people.SETTING_KEY))


# ---------------------------------------------------------------------------
# facts about people
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonFacts:
    id: str
    name: str
    email: str
    issuer: str
    # Providers this person signs in through: their own issuer when they were
    # made by a provider, plus every issuer the owner linked to them.
    provider_issuers: frozenset[str]
    is_owner: bool
    disabled: bool
    has_password: bool
    claims: Claims
    plan_id: str | None
    plan_is_rule: bool
    invited: bool
    # Holds an administrator grant a rule gave.
    admin_grant: bool
    admin_keys: tuple[str, ...] = field(default_factory=tuple)

    @property
    def standing(self) -> Standing:
        manual = self.plan_id if self.plan_id and not self.plan_is_rule else None
        return Standing(is_owner=self.is_owner, invited=self.invited, manual_plan=manual)


def _connection_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def people_facts(path: Path, *, now: int | None = None) -> list[PersonFacts]:
    """Every person who can appear in a preview, in the order they were created."""
    ts = int(time.time()) if now is None else now
    with admin_store._conn(path) as conn:
        users = _connection_rows(conn, "SELECT * FROM users ORDER BY created_at ASC, id ASC")
        access = {r["user_id"]: r for r in _connection_rows(conn, "SELECT * FROM account_access")}
        grants: dict[str, list[sqlite3.Row]] = {}
        for row in _connection_rows(
            conn,
            "SELECT * FROM admin_role_assignments WHERE subject_kind='user' AND revoked_at IS NULL "
            "AND scope_app_id IS NULL",
        ):
            grants.setdefault(row["subject_id"], []).append(row)
        passwords = {r[0] for r in _connection_rows(conn, "SELECT user_id FROM local_password_credentials")}
        groups: dict[str, list[str]] = {}
        roles: dict[str, list[str]] = {}
        for row in _connection_rows(conn, "SELECT user_id, claim_kind, claim_value FROM user_identity_claims"):
            (groups if row["claim_kind"] == "oidc_group" else roles).setdefault(row["user_id"], []).append(
                row["claim_value"])
        linked: dict[str, set[str]] = {}
        for row in _connection_rows(conn, "SELECT user_id, issuer FROM oidc_identity_links"):
            linked.setdefault(row["user_id"], set()).add(row["issuer"])
        verified = {r[0] for r in _connection_rows(
            conn, "SELECT user_id FROM account_oidc_verification WHERE email_verified=1")}
        accepted = {r[0] for r in _connection_rows(
            conn, "SELECT user_id FROM account_links WHERE kind='invite' AND used_at IS NOT NULL "
                  "AND user_id IS NOT NULL")}
        pending = {str(r[0]).lower() for r in _connection_rows(
            conn, "SELECT email FROM account_links WHERE kind='invite' AND used_at IS NULL "
                  "AND revoked_at IS NULL AND expires_at > ? AND email IS NOT NULL", (ts,))}
        rule_plans = {r["user_id"]: r["plan_id"] for r in _connection_rows(
            conn, "SELECT user_id, plan_id FROM signin_rule_plans")}
        keys: dict[str, list[str]] = {}
        for row in _connection_rows(
            conn,
            "SELECT id, created_by FROM credentials WHERE subject_kind='service_account' "
            "AND revoked_at IS NULL AND created_by LIKE 'user:%' AND (expires_at IS NULL OR expires_at > ?)",
            (ts,),
        ):
            keys.setdefault(str(row["created_by"])[5:], []).append(row["id"])
    out: list[PersonFacts] = []
    for user in users:
        if str(user["issuer"]) == LOCAL_ADMIN_KEY_ISSUER:
            continue
        state = access.get(user["id"])
        if state is not None and state["removed_at"] is not None:
            continue
        uid = user["id"]
        email = str(user["email"] or "").strip().lower()
        held = grants.get(uid, [])
        plan_id = state["plan_id"] if state is not None else None
        provider_issuers = set(linked.get(uid, set()))
        if user["issuer"] != "local":
            provider_issuers.add(str(user["issuer"]))
        provider_issuers = {norm_issuer(issuer) for issuer in provider_issuers}
        out.append(PersonFacts(
            id=uid,
            name=str(user["display_name"] or user["email"] or uid),
            email=email,
            issuer=str(user["issuer"]),
            provider_issuers=frozenset(provider_issuers),
            is_owner=any(g["role"] == "owner" for g in held),
            disabled=state is not None and state["disabled_at"] is not None,
            has_password=uid in passwords,
            claims=Claims(email=email if uid in verified else "", groups=tuple(sorted(groups.get(uid, []))),
                          roles=tuple(sorted(roles.get(uid, [])))),
            plan_id=plan_id,
            plan_is_rule=bool(plan_id and rule_plans.get(uid) == plan_id),
            invited=uid in accepted or (bool(email) and email in pending and uid in verified),
            admin_grant=any(g["role"] == "admin" and g["provenance"] == "oidc" for g in held),
            admin_keys=tuple(keys.get(uid, [])),
        ))
    return out


# ---------------------------------------------------------------------------
# provider sign-in
# ---------------------------------------------------------------------------


def claim_view(claims: Mapping[str, Any], *, client_id: str, groups_claim: str) -> Claims:
    """What the rules see of an ID token."""
    from gateway.admin_oidc import access_names_from_oidc_claims

    names = access_names_from_oidc_claims(claims, client_id=client_id, groups_claim=groups_claim)
    # Only an email the provider vouches for can match an email or domain rule, or an invitation:
    # an absent claim often means the provider lets people edit their own address.
    verified = claims.get("email_verified") is True
    return Claims(
        email=str(claims.get("email") or "").strip().lower() if verified else "",
        groups=tuple(names["oidc_group"]),
        roles=tuple(names["oidc_role"]),
    )


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    user: dict[str, Any] | None
    claims: Claims
    standing: Standing


def pending_invite_for(path: Path, email: str, *, now: int | None = None) -> bool:
    """An open invitation names this email."""
    ts = int(time.time()) if now is None else now
    if not email:
        return False
    with admin_store._conn(path) as conn:
        return conn.execute(
            "SELECT 1 FROM account_links WHERE kind='invite' AND used_at IS NULL AND revoked_at IS NULL "
            "AND expires_at > ? AND LOWER(email) = ? LIMIT 1", (ts, email.lower()),
        ).fetchone() is not None


def standing_for_identity(
    path: Path,
    user: Mapping[str, Any] | None,
    claims: Claims,
    *,
    email_verified: bool,
    bootstrap_owner: bool = False,
) -> Standing:
    """Owner, invitation and plan facts for someone who is signing in (or testing)."""
    is_owner = bootstrap_owner
    plan_id: str | None = None
    plan_is_rule = False
    invited = email_verified and pending_invite_for(path, claims.email)
    if user is not None:
        uid = str(user["id"])
        is_owner = is_owner or "owner" in admin_store.effective_admin_roles("user", uid, path=path)
        with admin_store._conn(path) as conn:
            state = conn.execute("SELECT plan_id FROM account_access WHERE user_id=?", (uid,)).fetchone()
            plan_id = state["plan_id"] if state else None
            if plan_id:
                plan_is_rule = conn.execute(
                    "SELECT 1 FROM signin_rule_plans WHERE user_id=? AND plan_id=?", (uid, plan_id)
                ).fetchone() is not None
            invited = invited or conn.execute(
                "SELECT 1 FROM account_links WHERE kind='invite' AND used_at IS NOT NULL AND user_id=? LIMIT 1",
                (uid,),
            ).fetchone() is not None
    return Standing(is_owner=is_owner, invited=invited,
                    manual_plan=plan_id if plan_id and not plan_is_rule else None)


def decide(
    path: Path,
    claims: Mapping[str, Any],
    *,
    issuer: str,
    client_id: str,
    groups_claim: str,
    bootstrap_owner: bool = False,
    rules: Rules | None = None,
) -> Decision:
    """May this verified ID token's person sign in, and as what."""
    view = claim_view(claims, client_id=client_id, groups_claim=groups_claim)
    user = admin_store.find_oidc_user(issuer, str(claims.get("sub") or ""), path=path)
    standing = standing_for_identity(
        path, user, view, email_verified=claims.get("email_verified") is True,
        bootstrap_owner=bootstrap_owner,
    )
    outcome = signin_rules.evaluate(
        rules if rules is not None else load_rules(path), view, standing,
        valid_plans=plan_names(path),
    )
    return Decision(outcome=outcome, user=user, claims=view, standing=standing)


def sync_rule_plan(path: Path, user_id: str, outcome: Outcome, *, now: int | None = None) -> str | None:
    """Make the person's plan match the rules, if rules (and not the owner) own it.

    A plan set by hand or carried by an invitation is never touched. A plan a
    rule gave is replaced when another rule now gives one, and removed when no
    rule gives one. Returns the plan id the person now has.
    """
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        plan = sync_rule_plan_in(conn, user_id, outcome, now=now)
        conn.commit()
        return plan


def sync_rule_plan_in(conn: sqlite3.Connection, user_id: str, outcome: Outcome, *, now: int | None = None) -> str | None:
    """:func:`sync_rule_plan` inside a transaction the caller owns and commits."""
    ts = int(time.time()) if now is None else now
    row = conn.execute(
        "SELECT plan_id FROM account_access WHERE user_id=? AND removed_at IS NULL", (user_id,)
    ).fetchone()
    if row is None:
        return None
    current = row["plan_id"]
    marker = conn.execute("SELECT plan_id FROM signin_rule_plans WHERE user_id=?", (user_id,)).fetchone()
    rule_given = current is not None and marker is not None and marker["plan_id"] == current
    if current is not None and not rule_given:
        if marker is not None:  # a stale marker: the plan was changed by hand
            conn.execute("DELETE FROM signin_rule_plans WHERE user_id=?", (user_id,))
        return current
    wanted = outcome.plan if outcome.plan_from == "rule" else None
    if wanted != current:
        conn.execute("UPDATE account_access SET plan_id=? WHERE user_id=?", (wanted, user_id))
        admin_store._record_audit_in_transaction(
            conn, "signin.plan.set" if wanted else "signin.plan.clear",
            actor_kind="system", actor_id=SYSTEM_ACTOR, actor_label="Sign-in rules",
            target_kind="user", target_id=user_id,
            metadata={"plan_id": wanted, "was": current}, now=ts,
        )
    if wanted:
        conn.execute(
            "INSERT INTO signin_rule_plans(user_id, plan_id, set_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET plan_id=excluded.plan_id, set_at=excluded.set_at",
            (user_id, wanted, ts),
        )
    else:
        conn.execute("DELETE FROM signin_rule_plans WHERE user_id=?", (user_id,))
    return wanted


# ---------------------------------------------------------------------------
# worlds, preview, enforcement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    """One side of a before/after comparison."""

    rules: Rules
    password: bool
    provider: bool
    issuer: str  # normalised with norm_issuer; "" when no provider is set up


def person_outcome(world: World, person: PersonFacts, plans: Mapping[str, str]) -> Outcome:
    return signin_rules.evaluate(world.rules, person.claims, person.standing, valid_plans=plans)


def can_sign_in(world: World, person: PersonFacts, outcome: Outcome) -> bool:
    if person.disabled:
        return False
    if person.is_owner:
        return True
    if person.has_password and world.password:
        return True
    return bool(world.provider and world.issuer and world.issuer in person.provider_issuers and outcome.admitted)


def holds_admin(world: World, person: PersonFacts, outcome: Outcome) -> bool:
    return bool(world.provider and world.issuer in person.provider_issuers and outcome.admitted and outcome.admin
                and not person.disabled)


def _signs_in_through_provider(person: PersonFacts) -> bool:
    return bool(person.provider_issuers) and not (person.has_password and person.issuer == "local")


def issuer_changes(before: World, after: World) -> bool:
    """The provider stays on but is now a different address: its people become new people."""
    return bool(before.provider and before.issuer and after.provider and after.issuer
                and before.issuer != after.issuer)


def preview(path: Path, before: World, after: World, plans: Mapping[str, str]) -> dict[str, Any]:
    """The people a change touches, by the same evaluator that enforces sign-in."""
    issuer_moved = issuer_changes(before, after)
    changed: list[dict[str, Any]] = []
    unaffected: list[str] = []
    signed_out = 0
    people = people_facts(path)
    for person in people:
        ob = person_outcome(before, person, plans)
        oa = person_outcome(after, person, plans)
        if issuer_moved and norm_issuer(person.issuer) == before.issuer:
            can_b = can_sign_in(before, person, ob)
            can_a = person.is_owner or bool(person.has_password and after.password)
            admin_a = False
            plan_a = person.plan_id  # the old accounts keep their plans
        else:
            can_b = can_sign_in(before, person, ob)
            can_a = can_sign_in(after, person, oa)
            admin_a = holds_admin(after, person, oa)
            plan_a = _plan_after(person, oa, can_a)
        # Before: is an administrator now, or will be at the next sign-in.
        admin_b = (person.admin_grant or holds_admin(before, person, ob)) and not person.is_owner
        plan_b = _plan_after(person, ob, can_b)
        line = {
            "id": person.id,
            "name": person.name,
            "sign_in": {"before": can_b, "after": can_a,
                        "via": "provider" if _signs_in_through_provider(person) else "password"},
            "admin": {"before": admin_b, "after": admin_a},
            "plan": {"before": _plan_ref(plan_b, plans), "after": _plan_ref(plan_a, plans)},
            "admin_keys": len(person.admin_keys),
        }
        touched = (can_b != can_a) or (admin_b != admin_a) or (plan_b != plan_a)
        if issuer_moved and norm_issuer(person.issuer) == before.issuer:
            touched = False  # the issuer line covers them
            if can_b and not person.is_owner:
                signed_out += 1
        if touched:
            changed.append(line)
        else:
            unaffected.append(person.name)
    return {
        "people": changed,
        "unaffected": unaffected,
        "issuer_change": (
            {"old_host": _host(before.issuer), "new_host": _host(after.issuer),
             "people": sum(1 for p in people if norm_issuer(p.issuer) == before.issuer),
             # Of those, the ones who lose their sessions and whose keys pause until they sign in again.
             "signed_out": signed_out}
            if issuer_moved else None
        ),
    }


def _plan_after(person: PersonFacts, outcome: Outcome, can_after: bool) -> str | None:
    if person.plan_id and not person.plan_is_rule:
        return person.plan_id  # set by hand or by an invitation: rules never touch it
    if not person.provider_issuers:
        return person.plan_id  # a person who only uses a password is not a rules person
    if not can_after or not outcome.admitted:
        return None
    return outcome.plan if outcome.plan_from == "rule" else None


def _plan_ref(plan_id: str | None, plans: Mapping[str, str]) -> dict[str, str] | None:
    if not plan_id or plan_id not in plans:
        return None
    return {"id": plan_id, "name": plans[plan_id]}


def _host(address: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(address).netloc or address
    except ValueError:
        return address


def adopt_admin_keys_in(conn: sqlite3.Connection, key_ids: Iterable[str], owner_id: str) -> int:
    """Keep these keys working by making the owner the person who stands behind them.

    Runs inside a transaction the caller owns and commits.
    """
    ids = list(key_ids)
    for key_id in ids:
        row = conn.execute("SELECT subject_id FROM credentials WHERE id=?", (key_id,)).fetchone()
        if row is None:
            continue
        conn.execute("UPDATE credentials SET created_by=? WHERE id=?", (f"user:{owner_id}", key_id))
        conn.execute(
            "UPDATE admin_role_assignments SET granted_by=? WHERE subject_kind='service_account' "
            "AND subject_id=? AND revoked_at IS NULL", (owner_id, row["subject_id"]),
        )
    return len(ids)


def apply_changes(
    tx: admin_store.Transaction,
    path: Path,
    before: World,
    after: World,
    plans: Mapping[str, str],
    *,
    keep_admin_keys: Iterable[str],
    owner_id: str,
    end_provider_access: Callable[[admin_store.Transaction, str], None],
    actor_label: str | None = None,
    actor_id: str | None = None,
    actor_kind: str = "user",
) -> list[dict[str, Any]]:
    """Make a saved change true for the people it touches. Returns who lost administrator.

    Every write goes through ``tx``: the caller commits them with the saved
    settings, or none of them happen. A step that cannot be rolled back
    registers itself with ``tx.after_commit``.
    """
    conn = tx.conn
    keep = set(keep_admin_keys)
    issuer_moved = issuer_changes(before, after)
    removed: list[dict[str, Any]] = []
    for person in people_facts(path):
        ob = person_outcome(before, person, plans)
        oa = person_outcome(after, person, plans)
        moved = issuer_moved and norm_issuer(person.issuer) == before.issuer
        can_b = can_sign_in(before, person, ob)
        can_a = (person.is_owner or bool(person.has_password and after.password)) if moved \
            else can_sign_in(after, person, oa)
        admin_a = False if moved else holds_admin(after, person, oa)
        # A grant the rules did not support before this change is not this
        # change's to remove: the person's next sign-in reconciles it.
        supported = holds_admin(before, person, ob) or before.rules != after.rules \
            or before.provider != after.provider or before.issuer != after.issuer
        lost_admin = person.admin_grant and not admin_a and supported
        # Kept out by the rules while the provider stays on: the same cut-off
        # a refused sign-in gets. Turning the provider off ends administrators
        # only; members' sessions and keys are not the provider switch's to end.
        kept_out = after.provider and not moved and can_b and not can_a and not person.is_owner
        if lost_admin and not person.is_owner:
            admin_store.reconcile_oidc_admin_roles_in(conn, person.id, set())
            accounts.revoke_person_sessions_in(conn, person.id)
            tx.after_commit(accounts.invalidate_asset_families)
            revoked = kept = 0
            if person.id in keep:
                kept = adopt_admin_keys_in(conn, person.admin_keys, owner_id)
            else:
                for key_id in person.admin_keys:
                    if admin_store.revoke_credential_in(conn, key_id):
                        revoked += 1
            removed.append({"id": person.id, "name": person.name, "keys_revoked": revoked, "keys_kept": kept})
            admin_store._record_audit_in_transaction(
                conn, "signin.admin_removed", actor_kind=actor_kind, actor_id=actor_id, actor_label=actor_label,
                target_kind="user", target_id=person.id,
                metadata={"keys_revoked": revoked, "keys_kept": kept}, now=int(time.time()),
            )
        elif lost_admin:
            # The owner is not an administrator by rule; a grant left from before goes quietly.
            admin_store.reconcile_oidc_admin_roles_in(conn, person.id, set())
        # A link to the old issuer no longer proves who anyone is: its members, like people the rules now
        # keep out, lose their sessions, MCP connections and (until they sign in again) their personal keys.
        # The owner signs in with a password and keeps working.
        if kept_out or (moved and can_b and not person.is_owner):
            end_provider_access(tx, person.id)
        if not moved and person.provider_issuers:
            sync_rule_plan_in(conn, person.id, oa if can_a else _no_plan(oa))
    return removed


def _no_plan(outcome: Outcome) -> Outcome:
    """The outcome for someone the rules no longer let in: a rule-given plan goes."""
    return Outcome(admitted=False, admin=False, plan=None, plan_from=None,
                   rule_ids=outcome.rule_ids, invited_only=False)


# ---------------------------------------------------------------------------
# what the rule pickers offer
# ---------------------------------------------------------------------------


def seen_values(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Values seen in past sign-ins, with how many people carried each."""
    out: dict[str, list[dict[str, Any]]] = {"group": [], "role": [], "email": [], "domain": []}
    people = [p for p in people_facts(path) if p.provider_issuers]
    for key, attr in (("group", "groups"), ("role", "roles")):
        counts: dict[str, int] = {}
        for person in people:
            for value in getattr(person.claims, attr):
                counts[value] = counts.get(value, 0) + 1
        kind = "oidc_group" if key == "group" else "oidc_role"
        with admin_store._conn(path) as conn:
            tested = [r[0] for r in conn.execute(
                "SELECT value FROM signin_seen_values WHERE kind=? ORDER BY value", (kind,))]
        for value in tested:
            counts.setdefault(value, 0)
        out[key] = [
            {"value": value, "count": count, **({"note": "seen in a test"} if count == 0 else {})}
            for value, count in sorted(counts.items())
        ]
    domains: dict[str, int] = {}
    for person in people:
        if person.email:
            out["email"].append({"value": person.email, "count": 1, "note": person.name})
            domain = person.email.partition("@")[2]
            if domain:
                domains[domain] = domains.get(domain, 0) + 1
    with admin_store._conn(path) as conn:
        tested_emails = [r[0] for r in conn.execute(
            "SELECT value FROM signin_seen_values WHERE kind='email' ORDER BY value")]
    known = {entry["value"] for entry in out["email"]}
    for value in tested_emails:
        if value not in known:
            out["email"].append({"value": value, "count": 0, "note": "seen in a test"})
            domain = value.partition("@")[2]
            if domain:
                domains.setdefault(domain, 0)
    out["email"].sort(key=lambda entry: entry["value"])
    out["domain"] = [
        {"value": value, "count": count, **({"note": "seen in a test"} if count == 0 else {})}
        for value, count in sorted(domains.items())
    ]
    return out


def remember_test_values(path: Path, claims: Claims, *, now: int | None = None) -> None:
    """Keep what a test sign-in saw, so the pickers can offer it."""
    ts = int(time.time()) if now is None else now
    rows = [("oidc_group", g) for g in claims.groups] + [("oidc_role", r) for r in claims.roles]
    if claims.email:
        rows.append(("email", claims.email))
    if not rows:
        return
    with admin_store._conn(path) as conn:
        conn.executemany(
            "INSERT INTO signin_seen_values(kind, value, seen_at) VALUES (?,?,?) "
            "ON CONFLICT(kind, value) DO UPDATE SET seen_at=excluded.seen_at", [(k, v, ts) for k, v in rows],
        )
        conn.commit()


def rule_matches(path: Path, rules: Rules, *, with_names: bool) -> dict[str, dict[str, Any]]:
    """Who has signed in and matches each rule."""
    people = [p for p in people_facts(path) if p.provider_issuers]
    out: dict[str, dict[str, Any]] = {}
    for rule in rules.rules:
        matched = [p for p in people if rule.complete and signin_rules.matches(rule, p.claims)]
        out[rule.id] = {"count": len(matched), "names": [p.name for p in matched] if with_names else []}
    return out


def audit_snapshot(rules: Rules) -> dict[str, Any]:
    return json.loads(json.dumps(rules.as_dict()))


# ---------------------------------------------------------------------------
# test sign-in verdict and owner linking
# ---------------------------------------------------------------------------


def _outcome_json(outcome: Outcome, plans: Mapping[str, str]) -> dict[str, Any]:
    return {
        "admitted": outcome.admitted, "admin": outcome.admin, "plan": _plan_ref(outcome.plan, plans),
        "rule_ids": list(outcome.rule_ids), "invited": outcome.invited_only,
    }


def test_verdict(path: Path, run: Any, after: World, saved: World, plans: Mapping[str, str]) -> dict[str, Any]:
    """What the rules would do with the person who just signed in at the test.

    The owner's own identity is told apart from everyone else's: rules never
    decide the owner. An identity nobody has linked yet, with the owner's
    verified email, can be linked to the owner.
    """
    issuer_raw = run.issuer or run.cfg.issuer
    issuer = norm_issuer(issuer_raw)
    user = admin_store.find_oidc_user(issuer_raw, run.subject, path=path)
    people = {p.id: p for p in people_facts(path)}
    tester = people.get(run.user_id)
    bound = people.get(str(user["id"])) if user else None
    if bound is not None and bound.is_owner:
        return {"kind": "owner_linked"}
    if (bound is None and tester is not None and tester.is_owner and run.claims.email
            and run.claims.email == tester.email and run.email_verified):
        stale = bool(tester.provider_issuers) and issuer not in tester.provider_issuers
        return {"kind": "owner_unlinked", "email": run.claims.email, "old_link_stale": stale}
    standing = standing_for_identity(path, user, run.claims, email_verified=run.email_verified)
    outcome = signin_rules.evaluate(after.rules, run.claims, standing, valid_plans=plans)
    if bound is not None:
        return {"kind": "person", "person": {"id": bound.id, "name": bound.name},
                "outcome": _outcome_json(outcome, plans)}
    return {
        "kind": "new", "email": run.claims.email, "host": _host(issuer_raw),
        "issuer_changed": bool(saved.provider and saved.issuer and issuer != saved.issuer),
        "outcome": _outcome_json(outcome, plans),
    }


def link_identity(path: Path, issuer: str, subject: str, user_id: str, *, linked_by: str | None,
                  session: BrowserSession | None = None, now: int | None = None) -> None:
    """Make this provider identity sign in as this person.

    A link is a way to sign in, so it is a credential: ``session`` is the browser sign-in that asked, checked in
    this transaction (decision D41)."""
    ts = int(time.time()) if now is None else now
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            session_guard.require_live(conn, session)
        except BaseException:
            conn.rollback()
            raise
        existing = conn.execute(
            "SELECT id FROM users WHERE issuer=? AND subject=?", (issuer, subject)).fetchone()
        taken = conn.execute(
            "SELECT user_id FROM oidc_identity_links WHERE issuer=? AND subject=?", (issuer, subject)).fetchone()
        if (existing is not None and existing["id"] != user_id) or (taken is not None and taken["user_id"] != user_id):
            conn.rollback()
            raise SignInError("identity_taken", "That sign-in already belongs to someone else.", 409)
        conn.execute(
            "INSERT INTO oidc_identity_links(issuer, subject, user_id, linked_at, linked_by) VALUES (?,?,?,?,?) "
            "ON CONFLICT(issuer, subject) DO UPDATE SET user_id=excluded.user_id, linked_at=excluded.linked_at",
            (issuer, subject, user_id, ts, linked_by),
        )
        admin_store._record_audit_in_transaction(
            conn, "signin.owner_linked", actor_kind="user", actor_id=linked_by, actor_label=None,
            target_kind="user", target_id=user_id, metadata={"issuer": issuer}, now=ts,
        )
        conn.commit()
