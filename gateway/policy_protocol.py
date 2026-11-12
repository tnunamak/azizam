"""The policy protocol of an intent or a workflow, read from the rows that declare it.

A workflow is judged under the protocol of the frontend row that selects it (``selects`` of
``registry/protocol_frontends.yml``); two workflows that share an intent but differ in inputs or outputs may be selected by
different rows. An intent with no workflow is judged under the protocol its route rows declare, or the one its capability
row declares. A name that no row declares raises ``ProtocolUnresolved``: it never becomes chat. The one default is the
capability convention of ``registry/capability_words.yml``: a known capability with no ``policy_protocol`` is a chat-family
capability.
"""

from __future__ import annotations

import functools
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import yaml

from gateway import frontend_shapes
from gateway.access_plans import capability_for_intent
from gateway.capability_ids import POLICY_PROTOCOLS, PROTOCOL_CAPABILITIES
from gateway.domain_rows import FRONTENDS_PATH
from gateway.workflow_kernel import selector_matches

# The protocol of a capability row that declares none (chat, decisions and reranking read as the chat family).
CHAT_FAMILY_PROTOCOL = "openai.chat"


class ProtocolUnresolved(LookupError):
    """No row declares the policy protocol of this intent or workflow."""


@functools.lru_cache(maxsize=1)
def _row_protocols() -> dict[str, frozenset[str]]:
    """Every protocol the shipped rows declare in a route policy, by the intent that policy names."""
    found: dict[str, set[str]] = defaultdict(set)

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            if isinstance(node.get("intent"), str) and isinstance(node.get("protocol"), str):
                found[node["intent"]].add(node["protocol"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(yaml.safe_load(FRONTENDS_PATH.read_text(encoding="utf-8")))
    return {intent: frozenset(protocols) for intent, protocols in found.items()}


def protocol_for_intent(intent: str) -> str:
    """The protocol the rows declare for ``intent``; ``ProtocolUnresolved`` when none does."""
    candidates = _row_protocols().get(intent, frozenset())
    capability = capability_for_intent(intent)
    declared = POLICY_PROTOCOLS.get(capability) if capability else None
    if len(candidates) == 1:
        return next(iter(candidates))
    if candidates:
        chosen = [p for p in candidates if p == declared or PROTOCOL_CAPABILITIES.get(p.replace(".", "-")) == capability]
        if len(chosen) == 1:
            return chosen[0]
        raise ProtocolUnresolved(f"the rows declare {sorted(candidates)} for intent {intent!r} and none is chosen by its capability")
    if capability is None:
        raise ProtocolUnresolved(f"no row declares a protocol for intent {intent!r}")
    return declared or CHAT_FAMILY_PROTOCOL


def protocol_for_workflow(workflow_id: str, contract: Mapping[str, Any]) -> str:
    """The protocol of the first frontend row that selects this workflow; else the protocol of its intent."""
    for row in frontend_shapes.bundled_rows():
        try:
            if not selector_matches(row.selects, workflow_id, contract):
                continue
        except (AttributeError, TypeError):
            continue
        protocols = {route.runtime_policy["protocol"] for route in row.routes if route.runtime_policy.get("protocol")}
        if len(protocols) == 1:
            return next(iter(protocols))
    return protocol_for_intent(str(contract.get("intent") or ""))


@functools.lru_cache(maxsize=1)
def _model_listed_intents() -> frozenset[str]:
    """The intents of the routes of every frontend row that lists its workflows as models."""
    return frozenset(
        route.runtime_policy["intent"]
        for row in frontend_shapes.bundled_rows() if row.lists_as_model
        for route in row.routes if route.runtime_policy.get("intent")
    )


def lists_as_model(workflow_id: str, contract: Mapping[str, Any]) -> bool:
    """Whether a frontend row that lists its workflows as models serves this workflow's intent.

    The rows' contract selectors are looser than the listing (a video edit workflow also has a prompt and a video output),
    so the intent of the row's route, not the selector, decides membership: any intent a listing row serves is listed, with
    every binding of it.
    """
    return str(contract.get("intent") or "") in _model_listed_intents()
