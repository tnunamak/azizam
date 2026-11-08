"""In-memory fakes of the S4-S8 interfaces (gateway/workflow_runtime_types.py), for builders that code against an
interface another builder owns. They hold data and record calls, and they keep the contracts the real modules keep
(fail-closed facts, state kept across edits, annotation-free execution digests, read-only views, listener isolation,
canonical name lookup, the name check on save, replay by request identity, terminal jobs stay terminal). They decide no
policy and run nothing: FakeStore.check runs only the name rule, and FakeRunCore admits every workflow the snapshot
holds and refuses nothing else."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from gateway.manifests import BUNDLED_PROVENANCE, ContractError
from gateway.workflow_contract import Report
from gateway.workflow_kernel import (
    canonical_token, digest, execution_digest, generated_names, name_clashes, route_key, run_identity,
)
from gateway.workflow_runtime_types import (
    IDEMPOTENCY_CONFLICT, KEEP, Actor, BindingDef, Commit, EntryState, FrontendRow, HistoryEntry, JobEvent,
    JobNotFound, Keep, NameKind, NameOwner, RecoveryReport, RegistryConflict, RegistryRefused, ResultContent,
    ResultRef, ResultUnavailable, RunJobView, RunRefused, RunRequest, WorkflowEntry, WorkflowFacts, WORKFLOW_EXISTS,
    WORKFLOW_NOT_FOUND,
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


def _token(kind: str, name: str) -> str:
    return name if kind == "route" else canonical_token(name)


@dataclass(frozen=True)
class FakeNames:
    """A NameView: lookups go by canonical token (route keys are already canonical)."""

    pairs: tuple[tuple[str, NameOwner], ...] = ()

    def owner(self, kind: NameKind, name: str) -> NameOwner | None:
        token = _token(kind, name)
        return next((o for n, o in self.pairs if o.kind == kind and _token(o.kind, n) == token), None)

    def owners(self, name: str) -> tuple[NameOwner, ...]:
        token = canonical_token(name)
        return tuple(dict.fromkeys(o for n, o in self.pairs if o.kind != "route" and canonical_token(n) == token))

    def items(self) -> tuple[tuple[str, NameOwner], ...]:
        return self.pairs


def workflow_names(e: WorkflowEntry) -> list[tuple[str, NameOwner]]:
    pairs = [(e.id, NameOwner("workflow", e.id))]
    pairs += [(str(alias), NameOwner("workflow_alias", e.id)) for alias in (e.contract.get("aliases") or ())]
    pairs += [(name, NameOwner(kind, e.id)) for kind, name in generated_names(e.id).items()]
    return pairs


def frontend_names(row: FrontendRow) -> list[tuple[str, NameOwner]]:
    pairs = [(row.id, NameOwner("frontend", row.id))]
    for spec in row.routes:
        for path in (spec.canonical, *spec.aliases):
            pairs += [(route_key(method, path), NameOwner("route", row.id)) for method in spec.methods]
    return pairs


def names_of(workflows: Mapping[str, WorkflowEntry], frontends: Mapping[str, FrontendRow] = MappingProxyType({}),
             extra: tuple[tuple[str, NameOwner], ...] = ()) -> FakeNames:
    """The whole revision's names: ``extra`` holds model ids and model aliases (the services section)."""
    pairs = list(extra)
    for e in workflows.values():
        pairs += workflow_names(e)
    for row in frontends.values():
        pairs += frontend_names(row)
    return FakeNames(tuple(pairs))


@dataclass(frozen=True)
class FakeSnapshot:
    revision: int
    workflows: Mapping[str, WorkflowEntry]
    frontends: Mapping[str, FrontendRow] = field(default_factory=lambda: MappingProxyType({}))
    models: tuple[tuple[str, NameOwner], ...] = ()      # construction input only; readers use ``names``

    @property
    def names(self) -> FakeNames:
        return names_of(self.workflows, self.frontends, self.models)

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
                 models: tuple[tuple[str, NameOwner], ...] = (),
                 frontends: Mapping[str, FrontendRow] = MappingProxyType({})) -> None:
        self._snap = FakeSnapshot(revision, MappingProxyType(dict(entries)), MappingProxyType(dict(frontends)), models)
        self._history: dict[str, list[tuple[HistoryEntry, WorkflowEntry]]] = {}
        self._listeners: list[Callable[[Any, Any], None]] = []

    def snapshot(self) -> FakeSnapshot:
        return self._snap

    def check(self, doc: Mapping[str, Any], *, workflow_id: str | None, facts: Any = KEEP,
              overrides: str | None = None, snapshot: Any) -> Report:
        """Only the name rule, over the public NameView: the revision's names without the saved workflow's own, plus
        the names the document would hold. The real store runs every save check."""
        wid = str(doc.get("id"))
        current = snapshot.get(wid)
        own = set(workflow_names(current)) if current is not None else set()
        pairs = [p for p in snapshot.names.items() if p not in own] + workflow_names(entry(doc))
        problems = name_clashes(pairs)
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
        report = self.check(doc, workflow_id=wid, facts=facts, overrides=overrides, snapshot=self._snap)
        if report.errors:
            raise RegistryRefused(tuple(report.errors))
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
    """Records submits. The key is bound to ``workflow_kernel.run_identity`` (the caller is the owner scope and maker):
    the same identity replays, another identity is ``idempotency_conflict``. A refusal binds nothing. execute_run marks a
    queued or running job succeeded with one fixed JSON result; a terminal job stays as it is. Sequential only: the
    real core's claim (section 7.2) is what makes concurrent submits safe."""

    def __init__(self) -> None:
        self.submitted: list[RunRequest] = []
        self.jobs: dict[str, RunJobView] = {}
        self.contents: dict[tuple[str, str, int], ResultContent] = {}
        self._keys: dict[tuple[str, str], tuple[str, str]] = {}

    async def submit_run(self, req: RunRequest, caller: Any, *, snapshot: Any) -> RunJobView | RunRefused:
        scope = str(caller)
        identity = run_identity(scope, scope, req)
        claim = (scope, req.idempotency_key) if req.idempotency_key else None
        if claim and claim in self._keys:
            job_id, first = self._keys[claim]
            if first != identity:
                return RunRefused(409, IDEMPOTENCY_CONFLICT, "the key names another request")
            return replace(self.jobs[job_id], disposition="replay")
        e = snapshot.get(req.workflow_id)
        if e is None:
            return RunRefused(404, WORKFLOW_NOT_FOUND, f"no workflow {req.workflow_id}")
        self.submitted.append(req)
        job_id = f"job-{len(self.submitted)}"
        view = RunJobView(job_id, "queued", "new", req.workflow_id, e.record_revision,
                          req.binding_key or next(iter(e.bindings), None), 0, 0)
        self.jobs[job_id] = view
        if claim:
            self._keys[claim] = (job_id, identity)
        return view

    async def execute_run(self, job_id: str) -> None:
        view = self.jobs[job_id]
        if view.status not in ("queued", "running"):
            return
        self.contents[(job_id, "result", 0)] = ResultContent("application/json", value={"ok": True})
        self.jobs[job_id] = replace(view, status="succeeded", results=(ResultRef("result", 0, "application/json"),))

    def recover_runs(self, now_s: int) -> RecoveryReport:
        return RecoveryReport({})


class FakeJobService:
    """Reads the fake core's jobs. ``wait`` returns at once (the fake has no clock)."""

    def __init__(self, core: FakeRunCore) -> None:
        self.core = core

    def _view(self, job_id: str) -> RunJobView:
        view = self.core.jobs.get(job_id)
        if view is None:
            raise JobNotFound(job_id)
        return view

    def get(self, job_id: str, caller: Any) -> RunJobView | None:
        return self.core.jobs.get(job_id)

    async def wait(self, job_id: str, caller: Any, timeout_s: float) -> RunJobView:
        return self._view(job_id)

    def cancel(self, job_id: str, caller: Any) -> RunJobView:
        view = self._view(job_id)
        if view.status in ("queued", "running"):
            view = replace(view, status="canceled")
            self.core.jobs[job_id] = view
        return view

    def events(self, job_id: str, caller: Any, *, limit: int, after_id: int | None = None,
               after_sequence: int | None = None) -> tuple[JobEvent, ...]:
        self._view(job_id)
        return ()

    def open_result(self, job_id: str, ref: ResultRef, caller: Any) -> ResultContent:
        self._view(job_id)
        content = self.core.contents.get((job_id, ref.output, ref.index))
        if content is None:
            raise ResultUnavailable("not_published")
        return content
