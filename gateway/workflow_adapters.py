"""The adapters a workflow binding can run through, found by the binding's key.

The run core (``workflow_run``) asks the main module three things about a binding: can this deployment run it, what
does a job pin about the code and the service behind it, and run it. A runner registered here answers all three for one
binding key. Registering one is the only step to make a binding key runnable; the core and the main module name no
runner. A binding key with no runner here goes the way it always went (the reviewed protocol adapters).
"""

from __future__ import annotations

from typing import Any, Protocol

from gateway.workflow_run import AdapterIdentity, RunContext


class BindingRunner(Protocol):
    """One reviewed way to run a binding."""

    binding_key: str
    adapter_id: str

    def binding_problem(self, binding: Any) -> str | None:
        """Why this deployment cannot run ``binding`` (a ``BindingDef``) now, or None. Reads no job and calls no service."""

    def identity(self, entry: Any, binding_key: str, binding: Any) -> AdapterIdentity:
        """What a job for ``entry`` pins about the code and the service. Raises ``ValueError`` when it cannot run."""

    async def run(self, ctx: RunContext) -> None:
        """Run the job from its pin. Returns when the result is committed; raises to fail the job."""


_RUNNERS: dict[str, BindingRunner] = {}


def register(runner: BindingRunner) -> None:
    """Make ``runner.binding_key`` runnable. A second runner for the same key replaces the first."""
    _RUNNERS[runner.binding_key] = runner


def for_binding(binding_key: str) -> BindingRunner | None:
    return _RUNNERS.get(binding_key)


def for_adapter(adapter_id: str) -> BindingRunner | None:
    """The runner whose pinned adapter id is ``adapter_id`` (what a job's envelope holds)."""
    return next((runner for runner in _RUNNERS.values() if runner.adapter_id == adapter_id), None)


def clear() -> None:
    """Forget every runner. For tests."""
    _RUNNERS.clear()
