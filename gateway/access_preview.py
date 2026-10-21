"""Everything one access plan can and cannot use (admin "Check access").

The preview does not decide anything itself. The caller evaluates every
capability the gateway offers with the same policy view discovery uses
(:func:`proxy._capability_for_access`: ``evaluate`` plus the category gate),
then builds the Models catalog twice: once over every capability ("offered")
and once over the permitted ones ("permitted"). This module only compares the
two, so an entry is shown as blocked exactly when the real catalog would not
list it. The rule that decided each capability is read off the decision
(``Capability.policy_rule``) and written as a plain sentence here; nothing in
this module works out an answer of its own.

Pure: no I/O, no engine names.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from gateway.capabilities import Capability
from gateway.capability_ids import CAPABILITY_LABELS
from gateway.policy import (
    CANNOT_TELL_RULES,
    RULE_ALLOWS,
    RULE_ALLOWS_ALL,
    RULE_CAPABILITY_NOT_GRANTED,
    RULE_CATEGORY_HIDDEN,
    RULE_CATEGORY_NO_GRANT,
    RULE_CATEGORY_NOT_ALLOWED,
    RULE_CATEGORY_UNAVAILABLE,
    RULE_DIRECT_ENGINE,
    RULE_FALLBACK,
    RULE_ITEM_NOT_LISTED,
    RULE_LISTS_ITEM,
    RULE_NO_PLAN,
    RULE_PROVIDER,
    RULE_PROVIDER_UNAVAILABLE,
    DecidingRule,
)

ALLOWED = "allowed"
BLOCKED = "blocked"
PARTIAL = "partial"
# The answer depends on something that could not be read. Enforcement refuses such
# an item too, so it is never counted as usable.
UNKNOWN = "unknown"

FULL = "full"
LIMITED = "limited"

# The order the Models page shows its groups in.
GROUP_ORDER = (
    "chat", "image", "video", "music", "sfx", "speech", "audio",
    "transcribe", "decide", "embed", "rerank",
)

_CATEGORY_PREFIX = "category rules:"
_CATEGORY_HIDDEN = "CATEGORY_HIDDEN"
_POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"
_GENERIC_REASON = "This access plan doesn't include it."
_HIDDEN_SENTENCE = "It is in a restricted category that this plan can't see."
# An administrator may not open Categories: the sentence names no category and does not say one is restricted.
_LIMITED_HIDDEN_SENTENCE = "Blocked by a category rule"
_UNAVAILABLE_SENTENCE = "The category rules couldn't be read just now, so access is refused until they can be."
_NO_GRANT_SENTENCE = "The category rules don't give this plan access to it."


def plain_reason(reason: str, *, full: bool = True) -> str:
    """A recorded decision reason as a sentence an owner can read.

    Used when a decision recorded text but no rule. Plan reasons are already
    sentences. Category-rule reasons carry a code and ids; say what happened
    without them. ``full`` is False for an administrator, who is not told that
    a restricted category exists.
    """
    text = str(reason).strip()
    if not text.startswith(_CATEGORY_PREFIX):
        return text
    if _CATEGORY_HIDDEN in text:
        return _HIDDEN_SENTENCE if full else _LIMITED_HIDDEN_SENTENCE
    if _POLICY_UNAVAILABLE in text:
        return _UNAVAILABLE_SENTENCE
    return _NO_GRANT_SENTENCE


class _Words:
    """What a rule is called in the owner's words: plan and category names, item names."""

    def __init__(
        self,
        plan_names: Mapping[str, str],
        category_names: Mapping[str, str],
        item_names: Mapping[str, str],
        detail: str,
    ) -> None:
        self.plan_names = plan_names
        self.category_names = category_names
        self.item_names = item_names
        self.full = detail == FULL

    def plan(self, rule: DecidingRule) -> str:
        """"The Kids plan", or "the app's Tablet plan" when the app's plan refused."""
        name = self.plan_names.get(rule.id or "")
        if rule.side == "app":
            return f"the app's {name} plan" if name else "the app's plan"
        return f"the {name} plan" if name else "this plan"

    def category(self, rule: DecidingRule) -> str | None:
        return self.category_names.get(rule.id or "") if self.full else None

    def item(self, name: str | None) -> str:
        return self.item_names.get(name or "", name or "this item")


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _kind_words(kind_label: str | None) -> str:
    return (kind_label or "matching").lower()


def _sentence(rule: DecidingRule, words: _Words, kind_label: str | None, item: str) -> str:
    """One sentence for a rule, with names and no internal ids."""
    code = rule.code
    capability = CAPABILITY_LABELS.get(rule.subject or "", rule.subject or "that").lower()
    if code == RULE_NO_PLAN:
        return ("This app has no access plan, so nothing is allowed." if rule.side == "app"
                else "No access plan is assigned yet, so nothing is allowed.")
    if code == RULE_CAPABILITY_NOT_GRANTED:
        return f"{_capital(words.plan(rule))} doesn't include {capability}."
    if code == RULE_ITEM_NOT_LISTED:
        return (f"{_capital(words.plan(rule))} lists the {_kind_words(kind_label)} items it allows, "
                f"and {item} isn't one of them.")
    if code == RULE_DIRECT_ENGINE:
        return f"{_capital(words.plan(rule))} doesn't allow calling {words.item(rule.subject) if rule.subject else 'engines'} directly."
    if code == RULE_PROVIDER:
        return f"{_capital(words.plan(rule))} doesn't allow the paid provider {rule.subject or 'it needs'}."
    if code == RULE_FALLBACK:
        return f"{_capital(words.plan(rule))} doesn't allow a paid provider when the engine is unavailable."
    if code == RULE_ALLOWS_ALL:
        return f"{_capital(words.plan(rule))} includes every {capability} item, now and as they are added."
    if code == RULE_LISTS_ITEM:
        return f"{_capital(words.plan(rule))} lists {words.item(rule.subject) if rule.subject else 'this item'}."
    if code == RULE_ALLOWS:
        return f"{_capital(words.plan(rule))} allows it."
    if code == RULE_CATEGORY_HIDDEN:
        name = words.category(rule)
        if not words.full:
            return _LIMITED_HIDDEN_SENTENCE
        return f"It is in the {name} category, which this plan can't see." if name else _HIDDEN_SENTENCE
    if code == RULE_CATEGORY_NO_GRANT:
        return _NO_GRANT_SENTENCE
    if code == RULE_CATEGORY_NOT_ALLOWED:
        name = words.category(rule)
        return f"The {name} category's rules don't allow it." if name else "The category rules don't allow it."
    if code == RULE_CATEGORY_UNAVAILABLE:
        return _UNAVAILABLE_SENTENCE
    if code == RULE_PROVIDER_UNAVAILABLE:
        return "The online provider's setup couldn't be read, so access can't be worked out."
    return _GENERIC_REASON


def _rule_view(rule: DecidingRule, words: _Words, kind_label: str | None, item: str) -> dict[str, Any]:
    """The wire form of a rule: code, kind, the id and name of what to edit, and the sentence.

    The owner gets a category's id and name. An administrator may read plans but
    not open Categories, so a category stays unnamed.
    """
    if rule.kind == "plan":
        rule_id, name = rule.id, words.plan_names.get(rule.id or "")
    elif rule.kind == "category" and words.full:
        rule_id, name = rule.id, words.category_names.get(rule.id or "")
    else:
        rule_id, name = None, None
    return {
        "code": rule.code,
        "kind": rule.kind,
        "id": rule_id,
        "name": name,
        "plain_reason": _sentence(rule, words, kind_label, item),
    }


def _rank(code: str | None, kind: str | None) -> int:
    """Which reason leads on a row: an explicit block, then a missing grant, then a cannot-tell."""
    if code in CANNOT_TELL_RULES:
        return 2
    return 0 if kind == "category" else 1


def _rows(catalog: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [*(catalog.get("aliases") or ()), *(catalog.get("models") or ())]


def _key(row: Mapping[str, Any]) -> tuple[bool, str]:
    return bool(row.get("alias")), str(row["id"])


def _kinds(row: Mapping[str, Any]) -> dict[str, tuple[str, set[str]]]:
    """Kind id -> (label, intents) for the capabilities a row offers."""
    kinds: dict[str, tuple[str, set[str]]] = {}
    for summary in row.get("capabilities") or ():
        kind = str(summary.get("capability") or summary.get("intent"))
        label, intents = kinds.setdefault(kind, (str(summary.get("label") or kind), set()))
        intents.add(str(summary.get("intent")))
    return kinds


def _names(row: Mapping[str, Any]) -> list[str]:
    """Every name a decision about this row may carry, the row's own first."""
    names = [str(row["id"]), *map(str, row.get("use_names") or ())]
    names.extend(str(target) for target in (row.get("resolves_to") or {}).values())
    if row.get("workflow"):
        names.append(str(row["workflow"]))
    return list(dict.fromkeys(names))


def _by_name(decided: Iterable[Capability]) -> dict[str, dict[str, Capability]]:
    """Name -> intent -> the first capability decided under that name."""
    found: dict[str, dict[str, Capability]] = {}
    for capability in decided:
        for name in (capability.model, capability.workflow, capability.resolved_workflow):
            if name:
                found.setdefault(str(name), {}).setdefault(capability.intent, capability)
    return found


def _decision_for(
    row: Mapping[str, Any], intents: Iterable[str], decided: Mapping[str, Mapping[str, Capability]],
) -> tuple[DecidingRule | None, str | None]:
    """The rule and recorded text of the leading decision for these intents of a row.

    The row's own name is tried before the names it reaches; among the intents,
    the strongest reason leads.
    """
    best: tuple[DecidingRule | None, str | None] | None = None
    for name in _names(row):
        by_intent = decided.get(name, {})
        for intent in sorted(intents):
            capability = by_intent.get(intent)
            if capability is None:
                continue
            rule = capability.policy_rule
            text = capability.policy_reasons[0] if capability.policy_reasons else None
            if rule is None and text is None:
                continue  # the gateway alias projection records no reason and is never served
            if best is None or _rank(rule and rule.code, rule and rule.kind) < _rank(best[0] and best[0].code, best[0] and best[0].kind):
                best = (rule, text)
    return best if best is not None else (None, None)


def _kind(
    kind: str, label: str, intents: set[str], allowed_intents: set[str], row: Mapping[str, Any],
    denied: Mapping[str, Mapping[str, Capability]], allowed: Mapping[str, Mapping[str, Capability]],
    words: _Words, item: str,
) -> dict[str, Any]:
    blocked_intents = sorted(intents - allowed_intents)
    if blocked_intents:
        rule, text = _decision_for(row, blocked_intents, denied)
        state = UNKNOWN if rule is not None and rule.code in CANNOT_TELL_RULES else BLOCKED
        view = _rule_view(rule, words, label, item) if rule is not None else None
        reason = view["plain_reason"] if view else (plain_reason(text, full=words.full) if text else _GENERIC_REASON)
        return {"kind": kind, "label": label, "allowed": False, "state": state, "reason": reason, "rule": view}
    rule, _text = _decision_for(row, intents, allowed)
    view = _rule_view(rule, words, label, item) if rule is not None else None
    return {"kind": kind, "label": label, "allowed": True, "state": ALLOWED, "reason": None, "rule": view}


def _lead_rank(kind: Mapping[str, Any]) -> int:
    rule = kind["rule"]
    return _rank(rule and rule["code"], rule and rule["kind"])


def _entry(
    row: Mapping[str, Any],
    permitted: Mapping[str, Any] | None,
    denied: Mapping[str, Mapping[str, Capability]],
    allowed: Mapping[str, Mapping[str, Capability]],
    words: _Words,
) -> dict[str, Any]:
    offered_kinds = _kinds(row)
    permitted_kinds = _kinds(permitted) if permitted is not None else {}
    name = str(row.get("display_name") or row["id"])
    kinds = [
        _kind(kind, label, intents, permitted_kinds.get(kind, (label, set()))[1], row, denied, allowed, words, name)
        for kind, (label, intents) in offered_kinds.items()
    ]
    open_kinds = [item for item in kinds if item["allowed"]]
    if not kinds:
        state = ALLOWED if permitted is not None else BLOCKED
    elif len(open_kinds) == len(kinds):
        state = ALLOWED
    elif open_kinds:
        state = PARTIAL
    else:
        state = BLOCKED if any(item["state"] == BLOCKED for item in kinds) else UNKNOWN
    # The row leads with its strongest reason; a blocked row leads with a block, an allowed row with its grant.
    shut = [item for item in kinds if not item["allowed"]]
    lead = min(shut, key=_lead_rank) if shut else (kinds[0] if kinds else None)
    if lead is None:
        reason = None if state == ALLOWED else _GENERIC_REASON
        rule = None
    else:
        reason, rule = lead["reason"], lead["rule"]
    differs = len({(item["state"], item["reason"]) for item in kinds}) > 1
    targets = dict.fromkeys(str(target) for target in (row.get("resolves_to") or {}).values())
    return {
        "id": str(row["id"]),
        "name": name,
        "alias": bool(row.get("alias")),
        "group": str(row.get("capability") or (kinds[0]["kind"] if kinds else "")),
        "group_label": str(row.get("capability_title") or (kinds[0]["label"] if kinds else "")),
        "state": state,
        "reason": reason,
        "rule": rule,
        "points_to": [{"id": target, "name": words.item(target)} for target in targets] if row.get("alias") else [],
        # Listed when the entry offers more than one kind and they differ.
        "kinds": kinds if len(kinds) > 1 and (state == PARTIAL or differs) else [],
    }


def build_access_preview(
    *,
    offered: Mapping[str, Any],
    permitted: Mapping[str, Any],
    denied: Sequence[Capability],
    allowed: Sequence[Capability] = (),
    plan_names: Mapping[str, str] | None = None,
    category_names: Mapping[str, str] | None = None,
    detail: str = FULL,
) -> dict[str, Any]:
    """Compare the catalog of everything offered with the catalog the plan sees.

    An entry only in ``permitted`` (a model whose alias is blocked) is allowed.
    ``denied`` and ``allowed`` are the capabilities the policy view refused and
    passed; they supply the deciding rule and never decide state.
    ``detail`` is ``full`` for the owner or ``limited`` for an administrator.
    """
    permitted_rows = {_key(row): row for row in _rows(permitted)}
    seen = {_key(row) for row in _rows(offered)}
    all_rows = [*_rows(offered), *(row for key, row in permitted_rows.items() if key not in seen)]
    item_names: dict[str, str] = {}
    for row in all_rows:
        item_names.setdefault(str(row["id"]), str(row.get("display_name") or row["id"]))
        if row.get("workflow"):
            item_names.setdefault(str(row["workflow"]), str(row.get("display_name") or row["workflow"]))
    words = _Words(plan_names or {}, category_names or {}, item_names, detail)
    refused, passed = _by_name(denied), _by_name(allowed)
    entries = [_entry(row, permitted_rows.get(_key(row)), refused, passed, words) for row in _rows(offered)]
    entries.extend(_entry(row, row, refused, passed, words) for key, row in permitted_rows.items() if key not in seen)

    def order(entry: Mapping[str, Any]) -> tuple[int, str, bool, str]:
        group = entry["group"]
        rank = GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)
        return rank, entry["group_label"], not entry["alias"], entry["name"].lower()

    entries.sort(key=order)
    groups: list[dict[str, Any]] = []
    for entry in entries:
        group = next((g for g in groups if g["group"] == entry["group"]), None)
        if group is None:
            group = {"group": entry["group"], "label": entry["group_label"],
                     "total": 0, "usable": 0, "blocked": 0, "unknown": 0}
            groups.append(group)
        group["total"] += 1
        bucket = {BLOCKED: "blocked", UNKNOWN: "unknown"}.get(entry["state"], "usable")
        group[bucket] += 1
    summary = {
        "usable": sum(group["usable"] for group in groups),
        "total": len(entries),
        "blocked": sum(group["blocked"] for group in groups),
        "unknown": sum(group["unknown"] for group in groups),
    }
    return {"summary": summary, "groups": groups, "entries": entries}
