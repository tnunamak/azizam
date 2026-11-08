"""The check a frontend runs on a workflow it serves, before the workflow is saved (CODE_TO_DATA_S3, D9).

The general grammar says what any workflow may be. A frontend's wire is narrower: a route may carry one file part and
answer with one body, for example. The frontend states that as a profile, a function of the validated contract that returns
the reasons it cannot serve it. A profile is registered under the intent its frontend serves; a workflow with that intent is
checked by it at save, and the frontend checks the same function again when it loads the record. This module names no
intent, workflow, model or engine: each frontend registers its own. S7 turns frontends into registry rows and replaces this.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from gateway.manifests import refusal

# intent -> profile(contract) -> reasons the frontend cannot serve the contract (empty when it can)
_PROFILES: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {}

PROFILE_REFUSED = "frontend_profile_refused"


def register(intent: str, profile: Callable[[Mapping[str, Any]], list[str]]) -> None:
    """Register (or replace) the profile of the frontend that serves ``intent``."""
    _PROFILES[intent] = profile


def check(contract: Mapping[str, Any]) -> None:
    """Raise ``ContractErrors`` when the profile registered for the contract's intent refuses it. A contract with no
    registered profile passes."""
    intent = contract.get("intent") if isinstance(contract, Mapping) else None
    profile = _PROFILES.get(intent) if isinstance(intent, str) else None
    if profile is None:
        return
    problems = profile(contract)
    if problems:
        raise refusal(("intent",), PROFILE_REFUSED, "the frontend that serves this intent cannot serve this workflow: " + "; ".join(problems))
