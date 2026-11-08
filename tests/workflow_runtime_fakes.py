"""In-memory fakes of the S4-S8 interfaces (gateway/workflow_runtime_types.py), for builders that code against an
interface another builder owns. They hold data and record calls, and they keep the contracts the real modules keep
(fail-closed facts, state kept across edits, annotation-free execution digests, read-only views, listener isolation,
replay by idempotency key). They decide no policy and run nothing."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from gateway.manifests import BUNDLED_PROVENANCE, ContractError
from gateway.workflow_contract import Report
from gateway.workflow_kernel import digest, execution_digest, generated_names, name_clashes
from gateway.workflow_runtime_types import (
    KEEP, Actor, BindingDef, Commit, EntryState, FrontendRow, HistoryEntry, Keep, NameKind, NameOwner,
    RegistryConflict, RegistryRefused, ResultItem, RunJobView, RunRefused, RunRequest, WorkflowEntry, WorkflowFacts,
    WORKFLOW_EXISTS, WORKFLOW_NOT_FOUND,
)

log = logging.getLogger(__name__)
FACTS = WorkflowFacts(exposure="default", trust_tier="stable", resource_tier="batch", resource_class="fake")


def frozen(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: frozen(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(frozen(v) for v in value)
    return value


def entry(contract: Mapping[str, Any], *, revision: int = 1, state: EntryState = "active", published: bool = True,
          facts: WorkflowFacts | None = FACTS, incarnation: str = "inc-1", overrides: str | None = None) -> WorkflowEntry:
    """A test entry. ``facts`` defaults to complete facts; pass ``facts=None`` for a workflow that needs policy."""
    bindings = {key: BindingDef(adapter_id=key, binding=frozen(body), binding_digest=digest(body))
                for key, body in (contract.get("bindings") or {}).items()}
    record = {"contract": contract, "facts": facts.__dict__ if facts else None, "state": state,
              "published": published, "overrides": overrides}
    return WorkflowEntry(id=str(contract["id"]), contract=frozen(contract), contract_digest=execution_digest(contract),
                         record_digest=digest(record), incarnation=incarnation, bindings=MappingProxyType(bindings),
                         facts=facts, provenance=BUNDLED_PROVENANCE, state=state, published=published,
                         record_revision=revision, overrides=overrides)


@dataclass(frozen=True)
class FakeNames:
    pairs: tuple[tuple[str, NameOwner], ...] = ()

    def owner(self, kind: NameKind, name: str) -> NameOwner | None:
        return next((o for n, o in self.pairs if o.kind == kind and n == name), None)

    def owners(self, name: str) -> tuple[NameOwner, ...]:
        return tuple(o for n, o in self.pairs if n == name)

    def items(self) -> tuple[tuple[str, NameOwner], ...]:
        return self.pairs


def names_of(workflows: Mapping[str, WorkflowEntry], extra: tuple[tuple[str, NameOwner], ...] = ()) -> FakeNames:
    pairs = list(extra)
    for e in workflows.values():
        pairs.append((e.id, NameOwner("workflow", e.id)))
        for kind, name in generated_names(e.id).items():
            pairs.append((name, NameOwner(kind, e.id)))
    return FakeNames(tuple(pairs))


@dataclass(frozen=True)
class FakeSnapshot:
    revision: int
    workflows: Mapping[str, WorkflowEntry]
    frontends: Mapping[str, FrontendRow] = field(default_factory=dict)
    models: tuple[tuple[str, NameOwner], ...] = ()      # model ids and aliases, as the name view holds them

    @property
    def names(self) -> FakeNames:
        return names_of(self.workflows, self.models)

    @property
    def digest(self) -> str:
        return digest({"workflows": sorted((e.id, e.record_digest, e.record_revision) for e in self.workflows.values()),
                       "frontends": sorted(repr(row) for row in self.frontends.values()),
                       "names": sorted((n, o.kind, o.id) for n, o in self.names.items())})

    def get(self, workflow_id: str) -> WorkflowEntry | None:
        return self.workflows.get(workflow_id)

    def by_intent(self, intent: str) -> tuple[WorkflowEntry, ...]:
        return tuple(e for e in self.workflows.values() if e.intent == intent)


class FakeStore:
    """A RegistryStore over a dict: compare-and-swap on one revision, full-record history, isolated listeners."""

    def __init__(self, entries: Mapping[str, WorkflowEntry] = (), revision: int = 1,
                 models: tuple[tuple[str, NameOwner], ...] = ()) -> None:
        self._snap = FakeSnapshot(revision, MappingProxyType(dict(entries)), models=models)
        self._history: dict[str, list[tuple[HistoryEntry, WorkflowEntry]]] = {}
        self._listeners: list[Callable[[Any, Any], None]] = []

    def snapshot(self) -> FakeSnapshot:
        return self._snap

    def check(self, doc: Mapping[str, Any], *, workflow_id: str | None, facts: Any = KEEP,
              overrides: str | None = None, snapshot: Any) -> Report:
        """Only the name rule; the real store runs every save check."""
        wid = str(doc.get("id"))
        others = {k: v for k, v in snapshot.workflows.items() if k != wid}
        problems = name_clashes(names_of({**others, wid: entry(doc)}, snapshot.models).items())
        return Report(errors=problems, primary=problems[0] if problems else None)

    def _commit(self, workflows: dict[str, WorkflowEntry], workflow_id: str, actor: Actor, change: str) -> Commit:
        old, rev = self._snap, self._snap.revision + 1
        current = workflows.get(workflow_id)
        if current is not None:
            current = replace(current, record_revision=rev)
            workflows[workflow_id] = current
            self._history.setdefault(workflow_id, []).insert(
                0, (HistoryEntry(rev, current.record_digest, current.contract_digest, 0, actor, change), current))
        self._snap = FakeSnapshot(rev, MappingProxyType(workflows), old.frontends, old.models)
        for listener in list(self._listeners):
            try:
                listener(old, self._snap)
            except Exception:  # a listener never stops the swap or the other listeners
                log.exception("registry listener failed")
        return Commit(rev, workflow_id, rev, current.contract_digest if current else None,
                      current.record_digest if current else None)

    def _match(self, if_match: int | None) -> None:
        if if_match != self._snap.revision:
            raise RegistryConflict(self._snap.revision)

    def _existing(self, workflow_id: str) -> WorkflowEntry:
        found = self._snap.workflows.get(workflow_id)
        if found is None:
            raise RegistryRefused((ContractError(("id",), WORKFLOW_NOT_FOUND, f"no workflow {workflow_id}"),))
        return found

    def put_workflow(self, doc: Mapping[str, Any], *, if_match: int | None, actor: Actor,
                     facts: WorkflowFacts | None | Keep = KEEP, overrides: str | None = None) -> Commit:
        wid = str(doc["id"])
        old = self._snap.workflows.get(wid)
        if if_match is None:
            if old is not None:
                raise RegistryConflict(self._snap.revision, WORKFLOW_EXISTS)
        else:
            self._match(if_match)
            self._existing(wid)
        kept = (old.facts if old else None) if facts is KEEP else facts
        new = entry(doc, facts=kept, state=old.state if old else "active",
                    published=old.published if old else False,
                    incarnation=old.incarnation if old else uuid.uuid4().hex, overrides=overrides)
        return self._commit({**self._snap.workflows, wid: new}, wid, actor, "update" if old else "create")

    def set_state(self, workflow_id: str, state: EntryState, *, if_match: int, actor: Actor) -> Commit:
        self._match(if_match)
        e = self._existing(workflow_id)
        return self._commit({**self._snap.workflows, workflow_id: entry(
            e.contract, facts=e.facts, state=state, published=e.published, incarnation=e.incarnation,
            overrides=e.overrides)}, workflow_id, actor, "state")

    def set_published(self, workflow_id: str, published: bool, *, if_match: int, actor: Actor) -> Commit:
        self._match(if_match)
        e = self._existing(workflow_id)
        return self._commit({**self._snap.workflows, workflow_id: entry(
            e.contract, facts=e.facts, state=e.state, published=published, incarnation=e.incarnation,
            overrides=e.overrides)}, workflow_id, actor, "publish")

    def delete_workflow(self, workflow_id: str, *, if_match: int, actor: Actor) -> Commit:
        self._match(if_match)
        self._existing(workflow_id)
        rest = {k: v for k, v in self._snap.workflows.items() if k != workflow_id}
        return self._commit(rest, workflow_id, actor, "delete")

    def history(self, workflow_id: str, *, limit: int = 20) -> tuple[HistoryEntry, ...]:
        return tuple(h for h, _ in self._history.get(workflow_id, [])[:limit])

    def restore(self, workflow_id: str, record_revision: int, *, if_match: int, actor: Actor) -> Commit:
        """The whole record at that revision (contract with annotations, facts, published), as a new revision. The
        current state and incarnation stay."""
        self._match(if_match)
        current = self._existing(workflow_id)
        _, old = next((h, e) for h, e in self._history[workflow_id] if h.record_revision == record_revision)
        back = entry(old.contract, facts=old.facts, state=current.state, published=old.published,
                     incarnation=current.incarnation, overrides=old.overrides)
        return self._commit({**self._snap.workflows, workflow_id: back}, workflow_id, actor, "restore")

    def subscribe(self, listener: Callable[[Any, Any], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)


class FakeRunCore:
    """Records submits. A new key makes a queued job; the same key and the same request replay it; execute_run marks
    it succeeded with one fixed JSON result. A workflow the snapshot does not hold is refused."""

    def __init__(self) -> None:
        self.submitted: list[RunRequest] = []
        self.jobs: dict[str, RunJobView] = {}
        self._keys: dict[str, tuple[str, RunRequest]] = {}

    async def submit_run(self, req: RunRequest, caller: Any, *, snapshot: Any) -> RunJobView | RunRefused:
        if req.idempotency_key and req.idempotency_key in self._keys:
            job_id, first = self._keys[req.idempotency_key]
            if first != replace(req, wait_s=first.wait_s):
                return RunRefused(409, "idempotency_conflict", "the key names another request")
            return replace(self.jobs[job_id], disposition="replay")
        e = snapshot.get(req.workflow_id)
        if e is None:
            return RunRefused(404, WORKFLOW_NOT_FOUND, f"no workflow {req.workflow_id}")
        self.submitted.append(req)
        job_id = f"job-{len(self.submitted)}"
        view = RunJobView(job_id, "queued", "new", req.workflow_id, e.record_revision,
                          req.binding_key or next(iter(e.bindings), None), 0, 0)
        self.jobs[job_id] = view
        if req.idempotency_key:
            self._keys[req.idempotency_key] = (job_id, req)
        return view

    async def execute_run(self, job_id: str) -> None:
        self.jobs[job_id] = replace(self.jobs[job_id], status="succeeded",
                                    results=(ResultItem("result", "application/json", value={"ok": True}),))

    def recover_runs(self, now_s: int) -> Any:
        from gateway.workflow_runtime_types import RecoveryReport
        return RecoveryReport({})


class FakeJobService:
    """Reads the fake core's jobs. ``wait`` returns at once (the fake has no clock)."""

    def __init__(self, core: FakeRunCore) -> None:
        self.core = core

    def get(self, job_id: str, caller: Any) -> RunJobView | None:
        return self.core.jobs.get(job_id)

    async def wait(self, job_id: str, caller: Any, timeout_s: float) -> RunJobView:
        return self.core.jobs[job_id]

    def cancel(self, job_id: str, caller: Any) -> RunJobView:
        view = self.core.jobs[job_id]
        if view.status in ("queued", "running"):
            view = replace(view, status="canceled")
            self.core.jobs[job_id] = view
        return view

    def open_result(self, job_id: str, item: ResultItem, caller: Any) -> bytes:
        return repr(item.value).encode()
