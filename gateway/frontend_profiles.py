"""The check a frontend runs on a workflow it serves, before the workflow is saved (CODE_TO_DATA_S3, D9; S7).

The general grammar says what any workflow may be. A frontend's wire is narrower: a route may carry one file part and
answer with one body, for example. Each reviewed wire shape states that as a profile, a function of the validated contract
that returns the reasons it cannot serve it (``gateway.frontend_shapes``). The bundled frontend rows say which workflows
each shape serves, by a selector over the contract's inputs and outputs. A contract that no row selects passes; one that
rows select is refused when none of them can serve it. The frontend checks the same function again when it loads the
record. This module names no intent, workflow, model or engine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway.manifests import refusal

PROFILE_REFUSED = "frontend_profile_refused"


def check(contract: Mapping[str, Any]) -> None:
    """Raise ``ContractErrors`` when the bundled rows that select the contract all refuse it."""
    from gateway import frontend_shapes

    problems = frontend_shapes.profile_problems(contract)
    if problems:
        raise refusal(("intent",), PROFILE_REFUSED, "the frontend that serves this intent cannot serve this workflow: " + "; ".join(problems))
