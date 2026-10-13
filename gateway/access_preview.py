"""Everything one access plan can and cannot use (admin "Check access").

The preview does not decide anything itself. The caller evaluates every
capability the gateway offers with the same policy view discovery uses
(:func:`proxy._capability_for_access`: ``evaluate`` plus the category gate),
then builds the Models catalog twice: once over every capability ("offered")
and once over the permitted ones ("permitted"). This module only compares the
two, so an entry is shown as blocked exactly when the real catalog would not
list it, and the reason is the one the decision recorded.

Pure: no I/O, no engine names.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from gateway.capabilities import Capability

ALLOWED = "allowed"
BLOCKED = "blocked"
PARTIAL = "partial"

# The order the Models page shows its groups in.
GROUP_ORDER = (
    "chat", "image", "video", "music", "sfx", "speech", "audio",
    "transcribe", "decide", "embed", "rerank",
)

_CATEGORY_PREFIX = "category rules:"
_CATEGORY_HIDDEN = "CATEGORY_HIDDEN"
_GENERIC_REASON = "This access plan doesn't include it."


def plain_reason(reason: str) -> str:
    """A decision reason as a sentence an owner can read.

    Plan reasons are already sentences. Category-rule reasons carry a stable
    code and ids; say what happened without the code.
    """
    text = str(reason).strip()
    if not text.startswith(_CATEGORY_PREFIX):
        return text
    if _CATEGORY_HIDDEN in text:
        return "It is in a restricted category that this plan is not listed for."
    return "The category rules block it."


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


def _names(row: Mapping[str, Any]) -> set[str]:
    """Every name a denied capability may carry for this row."""
    names = {str(row["id"]), *map(str, row.get("use_names") or ())}
    names.update(str(target) for target in (row.get("resolves_to") or {}).values())
    if row.get("workflow"):
        names.add(str(row["workflow"]))
    return names


def _reasons_by_name(denied: Iterable[Capability]) -> dict[str, dict[str, str]]:
    """Name -> intent -> first recorded reason, from capabilities the plan refused."""
    found: dict[str, dict[str, str]] = {}
    for capability in denied:
        if not capability.policy_reasons:
            continue
        reason = plain_reason(capability.policy_reasons[0])
        for name in (capability.model, capability.workflow, capability.resolved_workflow):
            if name:
                found.setdefault(str(name), {}).setdefault(capability.intent, reason)
    return found


def _reason_for(
    row: Mapping[str, Any], blocked_intents: Sequence[str], reasons: Mapping[str, Mapping[str, str]],
) -> str:
    """The recorded reason for the blocked intents of one row, else a generic one."""
    for name in sorted(_names(row)):
        by_intent = reasons.get(name, {})
        for intent in blocked_intents:
            if intent in by_intent:
                return by_intent[intent]
    return _GENERIC_REASON


def _entry(
    row: Mapping[str, Any],
    permitted: Mapping[str, Any] | None,
    reasons: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    offered_kinds = _kinds(row)
    permitted_kinds = _kinds(permitted) if permitted is not None else {}
    kinds = []
    for kind, (label, intents) in offered_kinds.items():
        allowed_intents = permitted_kinds.get(kind, (label, set()))[1]
        blocked_intents = sorted(intents - allowed_intents)
        reason = _reason_for(row, blocked_intents, reasons) if blocked_intents else None
        kinds.append({"kind": kind, "label": label, "allowed": not blocked_intents, "reason": reason})
    open_kinds = [item for item in kinds if item["allowed"]]
    if not kinds:
        state = ALLOWED if permitted is not None else BLOCKED
    else:
        state = ALLOWED if len(open_kinds) == len(kinds) else (PARTIAL if open_kinds else BLOCKED)
    first_reason = next((item["reason"] for item in kinds if item["reason"]), None)
    return {
        "id": str(row["id"]),
        "name": str(row.get("display_name") or row["id"]),
        "alias": bool(row.get("alias")),
        "group": str(row.get("capability") or (kinds[0]["kind"] if kinds else "")),
        "group_label": str(row.get("capability_title") or (kinds[0]["label"] if kinds else "")),
        "state": state,
        "reason": first_reason,
        # Listed only when the entry offers more than one kind and they differ.
        "kinds": kinds if len(kinds) > 1 and state == PARTIAL else [],
    }


def build_access_preview(
    *,
    offered: Mapping[str, Any],
    permitted: Mapping[str, Any],
    denied: Sequence[Capability],
) -> dict[str, Any]:
    """Compare the catalog of everything offered with the catalog the plan sees.

    An entry only in ``permitted`` (a model whose alias is blocked) is allowed.
    ``denied`` supplies reasons; it never decides state.
    """
    permitted_rows = {_key(row): row for row in _rows(permitted)}
    reasons = _reasons_by_name(denied)
    entries = [_entry(row, permitted_rows.get(_key(row)), reasons) for row in _rows(offered)]
    seen = {_key(row) for row in _rows(offered)}
    entries.extend(_entry(row, row, reasons) for key, row in permitted_rows.items() if key not in seen)

    def order(entry: Mapping[str, Any]) -> tuple[int, str, bool, str]:
        group = entry["group"]
        rank = GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)
        return rank, entry["group_label"], not entry["alias"], entry["name"].lower()

    entries.sort(key=order)
    usable = sum(1 for entry in entries if entry["state"] != BLOCKED)
    return {"summary": {"usable": usable, "total": len(entries)}, "entries": entries}
