"""In-memory fakes of the S4-S8 interfaces (gateway/workflow_runtime_types.py), for builders that code against an
interface another builder owns. They hold data and record calls; they decide nothing the real modules decide."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from gateway.manifests import BUNDLED_PROVENANCE
from gateway.workflow_runtime_types import (
    Actor, BindingDef, Commit, EntryState, FrontendRow, HistoryEntry, RegistryConflict, RunAccepted, RunRequest,
    WorkflowEntry, WorkflowFacts, WORKFLOW_EXISTS,
)
from gateway.workflow_contract import Report


def digest_of(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


FACTS = WorkflowFacts(exposure="default", trust_tier="stable", resource_tier="batch", resource_class="fake")


def entry(contract: Mapping[str, Any], *, revision: int = 1, state: EntryState = "active", published: bool = True,
          facts: WorkflowFacts | None = FACTS) -> WorkflowEntry:
    bindings = {key: BindingDef(adapter_id=key, binding=body, binding_digest=digest_of(body))
                for key, body in (contract.get("bindings") or {}).items()}
    return WorkflowEntry(id=str(contract["id"]), contract=contract, contract_digest=digest_of(contract), bindings=bindings,
                         facts=facts, provenance=BUNDLED_PROVENANCE, state=state, published=published,
                         record_revision=revision)


@dataclass(frozen=True)
class FakeSnapshot:
    revision: int
    workflows: Mapping[str, WorkflowEntry]
    frontends: Mapping[str, FrontendRow] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return digest_of(sorted((e.id, e.record_revision, e.contract_digest, e.state, e.published)
                                for e in self.workflows.values()))

    def get(self, workflow_id: str) -> WorkflowEntry | None:
        return self.workflows.get(workflow_id)

    def by_intent(self, intent: str) -> tuple[WorkflowEntry, ...]:
        return tuple(e for e in self.workflows.values() if e.intent == intent)

    def runnable(self, workflow_id: str) -> WorkflowEntry | None:
        e = self.workflows.get(workflow_id)
        return e if e is not None and e.state == "active" and not e.needs_configuration else None


class FakeStore:
    """A RegistryStore over a dict: compare-and-swap on one revision, history by digest, listeners after commit."""

    def __init__(self, entries: Mapping[str, WorkflowEntry] = (), revision: int = 1) -> None:
        self._snap = FakeSnapshot(revision, dict(entries))
        self._history: dict[str, list[tuple[HistoryEntry, Mapping[str, Any]]]] = {}
        self._listeners: list[Callable[[Any, Any], None]] = []

    def snapshot(self) -> FakeSnapshot:
        return self._snap

    def check(self, doc: Mapping[str, Any], *, workflow_id: str | None, snapshot: Any) -> Report:
        return Report()

    def _commit(self, workflows: dict[str, WorkflowEntry], workflow_id: str, digest: str | None) -> Commit:
        old, rev = self._snap, self._snap.revision + 1
        if workflow_id in workflows:
            workflows[workflow_id] = replace(workflows[workflow_id], record_revision=rev)
        self._snap = FakeSnapshot(rev, workflows, old.frontends)
        for listener in self._listeners:
            listener(old, self._snap)
        return Commit(rev, workflow_id, rev, digest)

    def _match(self, if_match: int | None) -> None:
        if if_match != self._snap.revision:
            raise RegistryConflict(self._snap.revision)

    def put_workflow(self, doc: Mapping[str, Any], *, if_match: int | None, actor: Actor,
                     facts: WorkflowFacts | None = None) -> Commit:
        wid = str(doc["id"])
        if if_match is None:
            if wid in self._snap.workflows:
                raise RegistryConflict(self._snap.revision, WORKFLOW_EXISTS)
        else:
            self._match(if_match)
        new = entry(doc, facts=facts or FACTS, published=False if wid not in self._snap.workflows
                    else self._snap.workflows[wid].published)
        self._history.setdefault(wid, []).insert(0, (HistoryEntry(self._snap.revision + 1, new.contract_digest, 0, actor), doc))
        return self._commit({**self._snap.workflows, wid: new}, wid, new.contract_digest)

    def set_state(self, workflow_id: str, state: EntryState, *, if_match: int, actor: Actor) -> Commit:
        self._match(if_match)
        e = self._snap.workflows[workflow_id]
        return self._commit({**self._snap.workflows, workflow_id: replace(e, state=state)}, workflow_id, e.contract_digest)

    def set_published(self, workflow_id: str, published: bool, *, if_match: int, actor: Actor) -> Commit:
        self._match(if_match)
        e = self._snap.workflows[workflow_id]
        return self._commit({**self._snap.workflows, workflow_id: replace(e, published=published)}, workflow_id,
                            e.contract_digest)

    def delete_workflow(self, workflow_id: str, *, if_match: int, actor: Actor) -> Commit:
        self._match(if_match)
        rest = {k: v for k, v in self._snap.workflows.items() if k != workflow_id}
        return self._commit(rest, workflow_id, None)

    def history(self, workflow_id: str, *, limit: int = 20) -> tuple[HistoryEntry, ...]:
        return tuple(h for h, _ in self._history.get(workflow_id, [])[:limit])

    def restore(self, workflow_id: str, contract_digest: str, *, if_match: int, actor: Actor) -> Commit:
        doc = next(d for h, d in self._history[workflow_id] if h.contract_digest == contract_digest)
        return self.put_workflow(doc, if_match=if_match, actor=actor)

    def subscribe(self, listener: Callable[[Any, Any], None]) -> None:
        self._listeners.append(listener)


class FakeRunCore:
    """Records submits; every run is accepted as queued, and execute_run marks it succeeded with a fixed bundle."""

    def __init__(self) -> None:
        self.submitted: list[RunRequest] = []
        self.status: dict[str, str] = {}

    async def submit_run(self, req: RunRequest, ctx: Any, *, snapshot: Any) -> RunAccepted:
        self.submitted.append(req)
        job_id = f"job-{len(self.submitted)}"
        self.status[job_id] = "queued"
        e = snapshot.get(req.workflow_id)
        return RunAccepted(job_id, "queued", req.workflow_id, e.record_revision if e else 0)

    async def execute_run(self, job_id: str) -> None:
        self.status[job_id] = "succeeded"

    def recover_runs(self, now_s: int) -> Mapping[str, int]:
        return {}
