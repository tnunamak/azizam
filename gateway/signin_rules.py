"""Who may sign in with the identity provider, and what they get.

One evaluator. Provider sign-in, the What changes preview and the test sign-in
all call :func:`evaluate`, so the page can never show an answer that
enforcement would not give. The module has no I/O: callers pass the rules, what
the provider sent, and the facts about the person that rules do not decide.

Deny by default. A person gets in only when one of these holds:

* they are the owner,
* an invitation names their verified email,
* a rule lets them in (or makes them an administrator, which implies it), or
* the owner chose to let everyone else in (``fallback == "open"``).

Rules can do three things: let in, make administrator, give a plan. The first
matching rule that gives a plan wins; list order is the priority. A plan the
owner set by hand, or that an invitation carried, always beats any rule.
There is no "keep out" rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

FIELDS = ("group", "role", "email", "domain", "everyone")
FALLBACKS = ("closed", "open")
MAX_RULES = 100
MAX_VALUES = 200
_RULE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class RulesError(ValueError):
    """The rules document is malformed. ``code`` names the refusal."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Rule:
    id: str
    field: str
    values: tuple[str, ...]
    admit: bool
    admin: bool
    plan: str | None

    @property
    def lets_in(self) -> bool:
        return self.admit or self.admin

    @property
    def complete(self) -> bool:
        return (self.field == "everyone" or bool(self.values)) and bool(
            self.admit or self.admin or self.plan
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "field": self.field, "values": list(self.values),
            "admit": self.lets_in, "admin": self.admin, "plan": self.plan,
        }


@dataclass(frozen=True)
class Rules:
    fallback: str = "closed"
    rules: tuple[Rule, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"fallback": self.fallback, "rules": [rule.as_dict() for rule in self.rules]}

    def incomplete(self) -> list[Rule]:
        return [rule for rule in self.rules if not rule.complete]


@dataclass(frozen=True)
class Claims:
    """What the provider asserted about a person, already read from the token."""

    email: str = ""
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Standing:
    """Facts about the person that no rule decides."""

    is_owner: bool = False
    invited: bool = False
    # A plan the owner set by hand, or that an invitation carried.
    manual_plan: str | None = None


@dataclass(frozen=True)
class Outcome:
    admitted: bool
    admin: bool
    plan: str | None
    plan_from: str | None  # "manual" | "rule" | None
    rule_ids: tuple[str, ...]  # matching rules, in list order
    # Admitted only because of an invitation (no rule, no open fallback).
    invited_only: bool


def canonical_group(value: str) -> str:
    """A short group name means the group at the root, as the provider writes it."""
    return value if value.startswith("/") else "/" + value


def parse_rules(raw: Any, *, plan_exists: Callable[[str], bool] | None = None) -> Rules:
    """Validate a document from the owner or the store. Raises :class:`RulesError`.

    A rule that lets someone in because it makes them an administrator is
    normalised to ``admit = True``. Incomplete rules are kept (the page shows
    them); saving refuses them through :meth:`Rules.incomplete`.
    """
    if not isinstance(raw, Mapping):
        raise RulesError("invalid_rules", "Rules must be an object.")
    fallback = raw.get("fallback", "closed")
    if fallback not in FALLBACKS:
        raise RulesError("invalid_rules", "Everyone else must be kept out or let in.")
    items = raw.get("rules", [])
    if not isinstance(items, list) or len(items) > MAX_RULES:
        raise RulesError("invalid_rules", f"Use a list of at most {MAX_RULES} rules.")
    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise RulesError("invalid_rules", "Each rule must be an object.")
        field = item.get("field")
        if field not in FIELDS:
            raise RulesError("invalid_rules", "A rule applies to a group, role, email, email domain or everyone.")
        values_raw = item.get("values", [])
        if not isinstance(values_raw, list) or len(values_raw) > MAX_VALUES or any(
            not isinstance(value, str) for value in values_raw
        ):
            raise RulesError("invalid_rules", "A rule's values must be a list of text.")
        values: list[str] = []
        for value in values_raw:
            value = value.strip()
            if field in ("email", "domain"):
                value = value.lower()
            if value and value not in values:
                values.append(value)
        if field == "everyone":
            values = []
        for value in values:
            if len(value) > 320 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise RulesError("invalid_rules", "A rule value is too long or has control characters.")
        plan = item.get("plan") or None
        if plan is not None:
            if not isinstance(plan, str):
                raise RulesError("invalid_rules", "A plan must be chosen by name.")
            if plan_exists is not None and not plan_exists(plan):
                raise RulesError("plan_not_found", "A rule gives a plan that no longer exists.")
        admin = bool(item.get("admin"))
        admit = bool(item.get("admit")) or admin
        rule_id = item.get("id")
        if not isinstance(rule_id, str) or not _RULE_ID.match(rule_id) or rule_id in seen_ids:
            rule_id = f"rule_{index + 1}"
            while rule_id in seen_ids:
                rule_id += "x"
        seen_ids.add(rule_id)
        rules.append(Rule(rule_id, field, tuple(values), admit, admin, plan))
    return Rules(fallback=fallback, rules=tuple(rules))


def matches(rule: Rule, claims: Claims) -> bool:
    """True when the rule applies to what the provider sent."""
    if rule.field == "everyone":
        return True
    if rule.field == "email":
        return bool(claims.email) and claims.email.lower() in rule.values
    if rule.field == "domain":
        _, _, domain = claims.email.lower().partition("@")
        return bool(domain) and domain in rule.values
    if rule.field == "group":
        wanted = set(rule.values) | {canonical_group(value) for value in rule.values}
        return any(group in wanted for group in claims.groups)
    if rule.field == "role":
        return any(role in rule.values for role in claims.roles)
    return False


def evaluate(
    rules: Rules,
    claims: Claims,
    standing: Standing = Standing(),
    *,
    valid_plans: Iterable[str] | None = None,
) -> Outcome:
    """The one answer to "may they sign in, as what, with which plan"."""
    plans = None if valid_plans is None else set(valid_plans)
    matched = [rule for rule in rules.rules if rule.complete and matches(rule, claims)]
    by_rule = any(rule.lets_in for rule in matched)
    admitted = standing.is_owner or standing.invited or by_rule or rules.fallback == "open"
    admin = admitted and not standing.is_owner and any(rule.admin for rule in matched)
    plan: str | None = None
    plan_from: str | None = None
    if standing.manual_plan:
        plan, plan_from = standing.manual_plan, "manual"
    elif admitted:
        for rule in matched:
            if rule.plan and (plans is None or rule.plan in plans):
                plan, plan_from = rule.plan, "rule"
                break
    return Outcome(
        admitted=admitted,
        admin=admin,
        plan=plan,
        plan_from=plan_from,
        rule_ids=tuple(rule.id for rule in matched),
        invited_only=admitted and standing.invited and not standing.is_owner
        and not by_rule and rules.fallback != "open",
    )
