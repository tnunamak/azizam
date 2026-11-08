"""The frozen interfaces of code-to-data S4 to S8 (docs/design/CODE_TO_DATA_S4_S8.md, section 3).

Types and protocols only: no behaviour, no I/O, no imports of the main module. The shared pure rules that more than one
builder must apply the same way (canonical names, generated names, the execution digest, selector matching) are code in
gateway/workflow_kernel.py, not here. A change to this file is a design change: it is made by the design owner and
announced to every builder.

I1 RegistrySnapshot / WorkflowEntry / NameView / SnapshotProvider   the immutable view of what can run, one per request
I2 RegistryStore / Commit / HistoryEntry / RegistryConflict ...     edits, checks, history (S4, builder A)
I3 Projections / ExecutionProfile / OutputShape / ReceiptIdentity   pure functions of a snapshot (S5, builder B)
I4 RunRequest / MediaRef / RunJobView / RunRefused / RunCore /      the run core and the job reads (S6, builder C)
   JobService / RecoveryReport
I5 FrontendRow / FrontendShape / WorkflowSelector / RouteSpec       routes are rows over reviewed wire shapes (S7, D)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from gateway.manifests import ContractError, Provenance
from gateway.workflow_contract import Report

if TYPE_CHECKING:  # the HTTP types are only named, never constructed here
    from starlette.requests import Request
    from starlette.responses import Response

EntryState = Literal["active", "disabled"]
# The job store's own spelling (gateway/media_jobs.py): "canceled", one l.
JobStatus = Literal["queued", "running", "succeeded", "failed", "canceled"]
OutputKind = Literal["json", "assets"]          # S3 D5: from the output's media_type; count is separate
Disposition = Literal["new", "replay"]
NameKind = Literal["workflow", "workflow_alias", "model", "model_alias", "frontend", "route", "operation", "component"]

# The caller's authenticated context (the main module's auth context). Core and frontends pass it to the injected main-
# module hooks and never read it.
CallerContext = Any

# Refusal and failure codes the store, the run core and the frontends share (the error vocabulary of this design).
WORKFLOW_DISABLED = "workflow_disabled"
WORKFLOW_DELETED = "workflow_deleted"
WORKFLOW_EXISTS = "workflow_exists"
WORKFLOW_NOT_FOUND = "workflow_not_found"
ID_CLASH = "id_clash"
BOUND_EXCEEDED = "workflow_bound_exceeded"
GENERATION_MISMATCH = "generation_mismatch"
SPOOL_MISSING = "spool_missing"
PIN_NOT_RECORDED = "pin_not_recorded"
SUBMISSION_UNKNOWN = "submission_unknown"
ENVELOPE_VERSION_UNSUPPORTED = "envelope_version_unsupported"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"


# ---------------------------------------------------------------------------------------------------------- I1


@dataclass(frozen=True)
class WorkflowFacts:
    """The record-level policy facts of one workflow: the four S3 facts plus ``resource_class`` (the admission and queue
    class). All are stated; a workflow that cannot state them has no facts and needs configuration (fails closed)."""

    exposure: str
    trust_tier: str
    resource_tier: str
    resource_class: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemplateRef:
    """A template is inline (a stored workflow) or a bundled path; either way the loader puts its body in the
    definition store and this carries its digest."""

    digest: str
    inline: Mapping[str, Any] | None = None
    path: str | None = None


@dataclass(frozen=True)
class BindingDef:
    adapter_id: str
    binding: Mapping[str, Any]
    binding_digest: str
    template: TemplateRef | None = None


@dataclass(frozen=True)
class WorkflowEntry:
    """One workflow at one record revision. Every mapping in it is read-only (the snapshot builder wraps them).

    ``contract_digest`` is the execution digest (``workflow_kernel.execution_digest``: annotations excluded).
    ``record_digest`` is the digest of the whole stored record (contract with annotations, facts, state, published,
    overrides); history and restore address it. ``incarnation`` is set when the id is created and never changes; a
    delete and a later create of the same id give a new one, so a job queued before the delete never runs as the new
    workflow."""

    id: str
    contract: Mapping[str, Any]
    contract_digest: str
    record_digest: str
    incarnation: str
    bindings: Mapping[str, BindingDef]
    facts: WorkflowFacts | None
    provenance: Provenance
    state: EntryState
    published: bool
    record_revision: int
    overrides: str | None = None

    @property
    def intent(self) -> str:
        """For display, listings and receipts only. Core never branches on it."""
        return str(self.contract.get("intent") or "")

    @property
    def needs_configuration(self) -> bool:
        """Policy is incomplete. Adapter availability is a separate projection (I3 ``entry_problems``)."""
        return self.facts is None


@dataclass(frozen=True)
class NameOwner:
    kind: NameKind
    id: str            # the workflow, model, frontend or route that holds the name


class NameView(Protocol):
    """Every name a request can address at one revision, keyed by ``workflow_kernel.canonical_token``: workflow ids
    and aliases, model ids and model aliases (from the service registry's services and dynamic aliases), frontend ids,
    route paths (method and canonical path) and the generated OpenAPI operation and component names. Built from the
    same service-registry document as the workflows, so it has the same revision."""

    def owner(self, kind: NameKind, name: str) -> NameOwner | None: ...
    def owners(self, name: str) -> tuple[NameOwner, ...]: ...
    def items(self) -> tuple[tuple[str, NameOwner], ...]: ...


class RegistrySnapshot(Protocol):
    """One immutable view of the service-registry document at generation ``revision`` (os-N) plus the bundled
    release. ``digest`` covers the workflows, the frontend rows and the name view, so a frontend-only or alias-only
    edit changes it."""

    revision: int
    digest: str
    workflows: Mapping[str, WorkflowEntry]
    frontends: Mapping[str, FrontendRow]
    names: NameView

    def get(self, workflow_id: str) -> WorkflowEntry | None: ...
    def by_intent(self, intent: str) -> tuple[WorkflowEntry, ...]: ...   # listings a frontend declares; never dispatch


class SnapshotProvider(Protocol):
    """The one way code reaches the registry. A request takes ``current()`` once (the request-scope hook stores it on
    ``request.state``) and uses that snapshot for every read of that request. No module keeps a registry in a closure
    or a global."""

    def current(self) -> RegistrySnapshot: ...


# ---------------------------------------------------------------------------------------------------------- I2


@dataclass(frozen=True)
class Actor:
    kind: str
    id: str
    label: str | None = None


@dataclass(frozen=True)
class Commit:
    revision: int
    workflow_id: str
    record_revision: int
    contract_digest: str | None
    record_digest: str | None


@dataclass(frozen=True)
class HistoryEntry:
    record_revision: int
    record_digest: str
    contract_digest: str
    saved_at_s: int
    actor: Actor
    change: Literal["create", "update", "state", "publish", "restore", "delete"]


class Keep(Enum):
    """``facts=KEEP`` (the default) keeps the stored facts; ``facts=None`` clears them (needs configuration)."""

    KEEP = "keep"


KEEP = Keep.KEEP


class RegistryConflict(Exception):
    """``if_match`` is not the current revision (HTTP 412 ``generation_mismatch``), or a create found the id (409
    ``workflow_exists``)."""

    def __init__(self, current_revision: int, code: str = GENERATION_MISMATCH) -> None:
        super().__init__(code)
        self.current_revision = current_revision
        self.code = code


class RegistryRefused(Exception):
    """A save check failed (HTTP 422). ``problems`` are every error with its path, in the S3 ``ContractError`` shape."""

    def __init__(self, problems: tuple[ContractError, ...]) -> None:
        super().__init__("; ".join(str(p) for p in problems))
        self.problems = problems


class RegistryStore(Protocol):
    """Every write is one compare-and-swap on the os-N generation of the service-registry document, and every write of
    that document (this store, the whole-document PUT, engine and alias edits) runs the same name check over the whole
    prospective revision (``workflow_kernel.name_clashes``)."""

    def snapshot(self) -> RegistrySnapshot: ...
    def check(self, doc: Mapping[str, Any], *, workflow_id: str | None, facts: WorkflowFacts | None | Keep = KEEP,
              overrides: str | None = None, snapshot: RegistrySnapshot) -> Report: ...
    def put_workflow(self, doc: Mapping[str, Any], *, if_match: int | None, actor: Actor,
                     facts: WorkflowFacts | None | Keep = KEEP, overrides: str | None = None) -> Commit: ...
    def set_state(self, workflow_id: str, state: EntryState, *, if_match: int, actor: Actor) -> Commit: ...
    def set_published(self, workflow_id: str, published: bool, *, if_match: int, actor: Actor) -> Commit: ...
    def delete_workflow(self, workflow_id: str, *, if_match: int, actor: Actor) -> Commit: ...
    def history(self, workflow_id: str, *, limit: int = 20) -> tuple[HistoryEntry, ...]: ...
    def restore(self, workflow_id: str, record_revision: int, *, if_match: int, actor: Actor) -> Commit: ...
    def subscribe(self, listener: Callable[[RegistrySnapshot, RegistrySnapshot], None]) -> Callable[[], None]:
        """Called with (old, new) after a commit is live. A listener that raises is logged and does not stop the swap
        or the other listeners. Returns the unsubscribe callable."""
        ...


# ---------------------------------------------------------------------------------------------------------- I3


@dataclass(frozen=True)
class MediaAccept:
    media_types: tuple[str, ...]
    max_bytes: int | None = None
    min_items: int = 1
    max_items: int = 1


@dataclass(frozen=True)
class OutputShape:
    kind: OutputKind
    media_type: str
    min_items: int
    max_items: int


@dataclass(frozen=True)
class ExecutionProfile:
    adapter_id: str
    adapter_facet: str
    resource_class: str
    accepts: Mapping[str, MediaAccept] = field(default_factory=dict)
    produces: Mapping[str, OutputShape] = field(default_factory=dict)


@dataclass(frozen=True)
class ReceiptIdentity:
    workflow_id: str
    record_revision: int
    contract_digest: str
    binding_key: str
    intent: str


class Projections(Protocol):
    """Pure functions of their arguments (builder B). No projection reads a global, the live config or the clock.
    Viewer-specific and live data (authorization, availability, recent activity, base URL) are applied by the caller
    after the static projection, never folded into it."""

    def execution_profile(self, entry: WorkflowEntry, binding_key: str) -> ExecutionProfile: ...
    def receipt_identity(self, entry: WorkflowEntry, binding_key: str) -> ReceiptIdentity: ...
    def entry_problems(self, entry: WorkflowEntry, adapter_ids: frozenset[str]) -> tuple[str, ...]:
        """Stable reason codes why an entry cannot run here: ``needs_policy`` (no facts), ``adapter_missing`` (a
        binding's adapter has no registered implementation), and the like. Empty means runnable as far as the registry
        knows. Display and the run core both use it; an entry with problems never runs."""
        ...
    def catalog_static(self, snapshot: RegistrySnapshot) -> tuple[Mapping[str, Any], ...]: ...
    def openapi_paths(self, snapshot: RegistrySnapshot) -> Mapping[str, Any]: ...
    def input_form(self, entry: WorkflowEntry) -> Mapping[str, Any]: ...


# ---------------------------------------------------------------------------------------------------------- I4


@dataclass(frozen=True)
class MediaRef:
    """One media input. Exactly one of ``asset_id`` (a library asset the caller may read) or ``spool_key`` (an upload
    the frontend staged in this request's spool) is set. ``submit_run`` adopts staged uploads into the new job's spool
    in the same step that creates the job; staged files that no job adopted are deleted when the request ends. A
    replay adopts nothing."""

    digest: str
    media_type: str
    size: int
    filename: str | None = None
    asset_id: str | None = None
    spool_key: str | None = None


@dataclass(frozen=True)
class RunRequest:
    workflow_id: str
    inputs: Mapping[str, Any]
    media: Mapping[str, tuple[MediaRef, ...]] = field(default_factory=dict)
    binding_key: str | None = None
    idempotency_key: str | None = None
    wait_s: float = 0.0
    frontend_id: str = "workflows"


@dataclass(frozen=True)
class RunFailure:
    code: str
    category: str                 # the job store's failure category (client, policy, upstream, internal, ...)
    message: str
    retryable: bool = False
    upstream_status: int | None = None
    problems: tuple[ContractError, ...] = ()


@dataclass(frozen=True)
class ResultItem:
    output: str
    media_type: str
    size: int | None = None
    asset_id: str | None = None
    value: Any = None             # the JSON value, for a json output


@dataclass(frozen=True)
class RunJobView:
    job_id: str
    status: JobStatus
    disposition: Disposition
    workflow_id: str
    record_revision: int
    binding_key: str | None
    created_at_s: int
    updated_at_s: int
    failure: RunFailure | None = None
    results: tuple[ResultItem, ...] | None = None    # set once the result bundle is published


@dataclass(frozen=True)
class RunRefused:
    """``submit_run`` refused before creating a job. The frontend renders it in its own error vocabulary."""

    http_status: int
    code: str
    message: str
    problems: tuple[ContractError, ...] = ()
    retry_after_s: int | None = None


RecoveryOutcome = Literal["terminal", "replay", "poll", "submission_unknown", "resume", "interrupted_before_submit",
                          "envelope_version_unsupported", "pin_not_recorded"]


@dataclass(frozen=True)
class RecoveryReport:
    counts: Mapping[RecoveryOutcome, int]
    failed_job_ids: tuple[str, ...] = ()


class RunCore(Protocol):
    async def submit_run(self, req: RunRequest, caller: CallerContext, *,
                         snapshot: RegistrySnapshot) -> RunJobView | RunRefused: ...
    async def execute_run(self, job_id: str) -> None: ...
    def recover_runs(self, now_s: int) -> RecoveryReport: ...


class JobService(Protocol):
    """Reads and controls one workflow job for its caller (the existing media-jobs rules decide who may read and
    cancel). Frontends and the media-jobs routes use it; they never read the job store directly."""

    def get(self, job_id: str, caller: CallerContext) -> RunJobView | None: ...
    async def wait(self, job_id: str, caller: CallerContext, timeout_s: float) -> RunJobView: ...
    def cancel(self, job_id: str, caller: CallerContext) -> RunJobView: ...
    def open_result(self, job_id: str, item: ResultItem, caller: CallerContext) -> bytes: ...


# ---------------------------------------------------------------------------------------------------------- I5


@dataclass(frozen=True)
class RouteSpec:
    methods: tuple[str, ...]
    canonical: str
    aliases: tuple[str, ...] = ()            # path aliases of this route; model aliases live in the name view
    runtime_policy: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowSelector:
    """Explicit ids, or a match in the closed selector schema (``workflow_kernel.selector_matches``): required input
    names with their type or media type, and output kinds with their media type. Never an intent word."""

    ids: tuple[str, ...] = ()
    match: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FrontendRow:
    id: str
    shape: str
    routes: tuple[RouteSpec, ...]
    selects: WorkflowSelector
    mode: Literal["sync", "job"]
    receipt_protocol: str
    strictness: Literal["exact", "lenient"] = "exact"
    lists_as_model: bool = False


class FrontendShape(Protocol):
    """A reviewed wire shape (code). It turns its wire request into a ``RunRequest`` (staging uploads through the
    request spool) and renders a job view or a refusal in its own vocabulary; for ``mode: sync`` it waits through
    ``JobService.wait`` first. Authentication and the runtime route policy run before ``to_run_request``."""

    shape_id: str

    def profile(self, contract: Mapping[str, Any]) -> tuple[str, ...]: ...      # reasons it cannot serve the contract
    async def to_run_request(self, request: Request, row: FrontendRow,
                             snapshot: RegistrySnapshot) -> RunRequest | Response: ...
    async def to_response(self, outcome: RunJobView | RunRefused, row: FrontendRow, *, jobs: JobService,
                          caller: CallerContext) -> Response: ...
