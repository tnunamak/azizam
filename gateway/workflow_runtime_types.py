"""The frozen interfaces of code-to-data S4 to S8 (docs/design/CODE_TO_DATA_S4_S8.md, section 3).

Types and protocols only: no behaviour, no I/O, no imports of the main module. The shared pure rules that more than one
builder must apply the same way (canonical names, generated names, the execution digest, selector matching) are code in
gateway/workflow_kernel.py, not here. A change to this file is a design change: it is made by the design owner and
announced to every builder.

I1 RegistrySnapshot / WorkflowEntry / NameView / SnapshotProvider   the immutable view of what can run, one per request
I2 RegistryStore / Commit / HistoryEntry / RegistryConflict ...     edits, checks, history (S4, builder A)
I3 Projections / ExecutionProfile / OutputShape / ReceiptIdentity   pure functions of a snapshot (S5, builder B)
I4 RunRequest / MediaRef / RequestSpool / RunJobView / ResultRef /  the run core and the job reads (S6, builder C)
   RunRefused / RunCore / JobService / RecoveryReport
I5 FrontendRow / FrontendShape / RouteSpec                          routes are rows over reviewed wire shapes (S7, D)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from gateway.manifests import ContractError, Provenance
from gateway.result_bundle import ResultLimits
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
IDEMPOTENCY_CONFLICT = "idempotency_conflict"          # the key was used for a different request identity (409)
IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"    # the key's first request has not finished deciding (409, retry)
JOB_NOT_FOUND = "job_not_found"                        # absent, or not readable by this caller (the same answer)
RESULT_UNAVAILABLE = "result_unavailable"              # erased, expired, not yet published, or not the caller's to read
MEDIA_PATH_UNSUPPORTED = "media_path_unsupported"      # a file part addressed below a top-level input name
# Reasons ``AdapterAvailability.binding_problem`` may return for the live state of the service behind a binding (S7 parity
# M4). ``target_gate`` turns each into a 503 ``RunRefused`` before a job exists; ``binding_retry_after_s`` gives the delay.
SERVICE_DISABLED = "service_disabled"
SERVICE_UNHEALTHY = "service_unhealthy"
SERVICE_NOT_ADMITTING = "service_not_admitting"
SERVICE_REASONS = frozenset({SERVICE_DISABLED, SERVICE_UNHEALTHY, SERVICE_NOT_ADMITTING})


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

    # A v1 job (queued before this train) carries no incarnation: dispatch check 2 compares presence only, so a delete and
    # a create of the same id between its queueing and its dispatch is not detected. The deploy refuses to start while a
    # v1 workflow job is queued (section 5.5), so the exception is closed operationally, not by this type.

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
    """Who holds a name. ``kind`` is how the name addresses its owner; ``id`` is the owning entity's id (for an alias,
    the workflow or model it names). Two names with one owner never clash: ``workflow_kernel.owner_entity`` maps
    ``workflow_alias`` to ``workflow`` and ``model_alias`` to ``model``. A name that must reach several workflows (an
    alias that picks among them by the caller's inputs) is a frontend row's id or route, owned by that row, never an alias
    with several owners."""

    kind: NameKind
    id: str


class NameView(Protocol):
    """Every name a request can address at one revision, keyed by ``workflow_kernel.canonical_token``: workflow ids
    and aliases, model ids and model aliases (from the service registry's services and dynamic aliases), frontend ids,
    route paths (method and canonical path) and the generated OpenAPI operation and component names. Built from the
    same service-registry document as the workflows, so it has the same revision."""

    def owner(self, kind: NameKind, name: str) -> NameOwner | None:
        """The owner of ``name`` (any spelling with the same canonical token) among names of this kind."""
        ...
    def owners(self, name: str) -> tuple[NameOwner, ...]:
        """Every (kind, owner) holding the canonical token of ``name``, across kinds. In a valid revision the target
        kinds name at most one owning entity."""
        ...
    def items(self) -> tuple[tuple[str, NameOwner], ...]:
        """Every (name as written, owner) pair; ``workflow_kernel.name_clashes`` over these is empty."""
        ...


class RegistrySnapshot(Protocol):
    """One immutable view of the service-registry document at generation ``revision`` (os-N) plus the bundled
    release. ``digest`` covers the workflows, the frontend rows and the name view, so a frontend-only or alias-only
    edit changes it."""

    revision: int
    digest: str
    workflows: Mapping[str, WorkflowEntry]
    frontends: Mapping[str, FrontendRow]
    names: NameView
    # Stored workflows left out because their names clash with names the document already held, with the clashes.
    held_out: Mapping[str, tuple[Any, ...]]

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
    # The browser sign-in that asked for the change (a ``session_guard.BrowserSession``), or None for a key. The registry
    # write checks it inside its transaction, so a sign-out that lands first refuses the write (decision D41).
    browser_session: Any = field(default=None, compare=False, repr=False)


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
    """An output's count is resolved: a contract that declares no count gets S3's ``effective_count`` (min 1, max the
    ``limits.max_assets`` passed to ``execution_profile``)."""

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
    """``record_revision`` and ``binding_key`` are None for a v1 job (no such fields in its envelope); a receipt shows
    them as unknown and never fills them from the live registry."""

    workflow_id: str
    record_revision: int | None
    contract_digest: str
    binding_key: str | None
    intent: str


class AdapterAvailability(Protocol):
    """Whether this deployment can run one binding: its adapter is registered and every reviewed role the binding
    needs is present (S3). Supplied by the run core's port (the binding registry); projections only read it."""

    def binding_problem(self, binding: BindingDef) -> str | None: ...    # a stable reason code, or None when runnable
    def binding_retry_after_s(self, binding: BindingDef) -> int | None:
        """Seconds a caller should wait before retrying after ``binding_problem`` answered a ``SERVICE_*`` reason
        (``service_not_admitting`` has one); None when there is no advice."""
        ...


class Projections(Protocol):
    """Pure functions of their arguments (builder B). No projection reads a global, the live config or the clock.
    Viewer-specific and live data (authorization, availability, recent activity, base URL) are applied by the caller
    after the static projection, never folded into it."""

    def execution_profile(self, entry: WorkflowEntry, binding_key: str, *, limits: ResultLimits) -> ExecutionProfile:
        """``limits`` is the deployment's effective result limits (``result_bundle.ResultLimits``), passed in by the
        caller from its snapshot of the config, so an absent output count resolves the same way in B and C."""
        ...
    def receipt_identity(self, entry: WorkflowEntry, binding_key: str) -> ReceiptIdentity: ...
    def entry_problems(self, entry: WorkflowEntry, available: AdapterAvailability, *,
                       binding_key: str | None = None) -> tuple[str, ...]:
        """For listings and new submissions: ``needs_policy`` (no facts), then the ``binding_problem`` of the selected
        binding: ``binding_key`` (``RunRequest.binding_key`` at submit), or the entry's default binding when None (a
        listing). An unknown key is ``binding_unknown``. Empty means a new run on that binding can start."""
        ...
    def binding_problems(self, binding: BindingDef, available: AdapterAvailability) -> tuple[str, ...]:
        """For dispatch of a queued job: only the PINNED binding's availability (dispatch check 5). A later change of
        the live entry's bindings never decides whether a pinned job can run."""
        ...
    def catalog_static(self, snapshot: RegistrySnapshot) -> tuple[Mapping[str, Any], ...]: ...
    def openapi_paths(self, snapshot: RegistrySnapshot) -> Mapping[str, Any]: ...
    def input_form(self, entry: WorkflowEntry) -> Mapping[str, Any]: ...


# ---------------------------------------------------------------------------------------------------------- I4


@dataclass(frozen=True)
class MediaRef:
    """One media input. Exactly one of ``asset_id`` (a library asset the caller may read) or ``spool_key`` (an upload
    staged in this request's ``RequestSpool``) is set. ``media_type`` is measured from the bytes and is what validation
    uses; ``wire_media_type`` is the type the caller declared or the route fixes, kept only so an adapter can send the
    established wire shape (S2.6b). Request identity (``workflow_kernel.run_identity``) uses ``digest`` and never
    ``spool_key``."""

    digest: str
    media_type: str
    size: int
    filename: str | None = None
    wire_media_type: str | None = None
    asset_id: str | None = None
    spool_key: str | None = None


class RequestSpool(Protocol):
    """The uploads of one request (builder C writes it; the request-scope hook creates one per request and releases
    it when the request ends). A frontend stages each top-level file input here; ``submit_run`` adopts the staged files
    of a NEW job into that job's spool before the job row commits, and deletes the job spool again if the commit fails.
    A refusal, a replay or an error adopts nothing, and ``release`` deletes every file not adopted. A file part
    addressed below a top-level input name is refused by the shape with ``media_path_unsupported``."""

    async def stage(self, input_name: str, chunks: AsyncIterator[bytes], *, filename: str | None,
                    wire_media_type: str | None) -> MediaRef | RunRefused:
        """Stream one file in; measures type, size and digest. Refuses (413 or 415) without keeping the bytes."""
        ...
    def release(self) -> None: ...


@dataclass(frozen=True)
class RunRequest:
    workflow_id: str
    inputs: Mapping[str, Any]
    media: Mapping[str, tuple[MediaRef, ...]] = field(default_factory=dict)
    binding_key: str | None = None
    idempotency_key: str | None = None
    wait_s: float = 0.0
    # The request controls, by namespace (``extensions`` on the wire): the prompt-memory control reaches the job's
    # payload, where the prompt-memory rules decide what is kept. Not part of run identity.
    extensions: Mapping[str, Any] = field(default_factory=dict)
    frontend_id: str = "workflows"
    # The request receipt this run closes when its job ends (a row that answered with the job). None: no receipt.
    request_id: str | None = None
    # Transport options the caller chose on the wire that change what the engine is asked for (S7 parity M2): the count,
    # size, output format and the like. The shape fills it; it is part of run identity and travels to the adapter in the
    # flat body under ``options``. An answer form that only changes rendering is not an option.
    options: Mapping[str, Any] = field(default_factory=dict)
    # The wire fields the caller sent, in the order the shape read them, for the receipt's ``accepted_inputs`` (S7 parity
    # M1). A wire field that is neither an input nor an option (an answer form, the model name) is listed here too. Not
    # part of run identity. Empty: the receipt lists the options, then the inputs, then the media.
    accepted_inputs: tuple[str, ...] = ()
    # Which text a failed job stores: "adapter" (the adapter's own message) or "category_copy" (the fixed sentence for the
    # failure's category). It is data of the row (``runtime_policy.job_failure_text``) and not part of run identity.
    failure_text: str = "adapter"
    # "binding" or "none": see ``FrontendRow.job_retries``. Not part of run identity.
    retries: str = "binding"
    # The intent the job row is filed under: the one the matched route declares (``runtime_policy.intent``), the same value
    # the request receipt records. Empty: the generic run intent. Not part of run identity.
    intent: str = ""


@dataclass(frozen=True)
class RunFailure:
    code: str
    category: str                 # the job store's failure category (client, policy, upstream, internal, ...)
    message: str
    retryable: bool = False
    upstream_status: int | None = None
    problems: tuple[ContractError, ...] = ()


@dataclass(frozen=True)
class ResultRef:
    """One published result item, by reference. A job view never carries a JSON value or bytes: S2 keeps a JSON
    result private to its owner and maker, so reading content is ``JobService.open_result``, which applies the stronger
    result-reader rule."""

    output: str
    index: int
    media_type: str
    size: int | None = None
    asset_id: str | None = None


@dataclass(frozen=True)
class ResultContent:
    media_type: str
    value: Any = None             # the JSON value, for a json output
    body: bytes | None = None     # the bytes, for an asset output


@dataclass(frozen=True)
class JobEvent:
    id: int
    sequence: int
    type: str
    at_s: int
    payload: Mapping[str, Any] = field(default_factory=dict)


class JobNotFound(Exception):
    """``job_not_found`` (404): absent, or not readable by this caller. One answer for both, so ids do not leak."""


class JobDeleteConflict(Exception):
    """``job_delete_conflict`` (409): the job could not be canceled first (a running job whose engine has not confirmed
    the cancel), so it was not deleted and is unchanged."""


class ResultUnavailable(Exception):
    """``result_unavailable`` (404 or 410 by ``reason``): not yet published, erased, expired, or not the caller's to
    read under the result-reader rule."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RunJobView:
    job_id: str
    status: JobStatus
    disposition: Disposition
    workflow_id: str
    record_revision: int | None                      # None for a v1 job: unknown, never filled from the live registry
    binding_key: str | None                          # None for a v1 job
    created_at_s: int
    updated_at_s: int
    failure: RunFailure | None = None
    results: tuple[ResultRef, ...] | None = None     # set once the result bundle is published; references only
    # Facts the adapter returned with its result (S7 parity M5): a plain mapping the core stores on the job and never
    # interprets. Shapes render headers from it and the receipt writer copies fields from it. Empty until the job succeeds.
    result_metadata: Mapping[str, Any] = field(default_factory=dict)


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
                         snapshot: RegistrySnapshot) -> RunJobView | RunRefused:
        """Section 7.2. With an idempotency key the FIRST step claims the key for ``run_identity`` in one transaction;
        only the claim holder validates, admits, draws and creates. The claim row carries a random ``holder`` token;
        every later transition is conditional on that token and on ``job_id`` being null (7.2): takeover replaces the
        token, so an old holder's draw-and-create transaction and its release both match nothing. A refusal releases
        only this holder's undecided claim; a committed job mapping is never released. A second request with
        the same identity replays the job the claim names, or answers 409 ``idempotency_in_progress`` with
        ``retry_after_s`` while the claim is undecided; another identity is 409 ``idempotency_conflict``."""
        ...
    async def execute_run(self, job_id: str) -> None: ...
    def recover_runs(self, now_s: int) -> RecoveryReport: ...


class JobService(Protocol):
    """Reads and controls one workflow job for its caller (the existing media-jobs rules decide who may read and
    cancel). Frontends and the media-jobs routes use it; they never read the job store directly."""

    def get(self, job_id: str, caller: CallerContext) -> RunJobView | None: ...     # None: JOB_NOT_FOUND
    async def wait(self, job_id: str, caller: CallerContext, timeout_s: float) -> RunJobView:
        """The view when the job is terminal or ``timeout_s`` passed (status then queued or running). JobNotFound."""
        ...
    def cancel(self, job_id: str, caller: CallerContext) -> RunJobView:
        """Today's rules: the caller who made it or the owner; a terminal job is answered unchanged. JobNotFound."""
        ...
    def events(self, job_id: str, caller: CallerContext, *, limit: int, after_id: int | None = None,
               after_sequence: int | None = None) -> tuple[JobEvent, ...]:
        """The existing event list and its cursors (media_jobs.events). JobNotFound."""
        ...
    def open_result(self, job_id: str, ref: ResultRef, caller: CallerContext) -> ResultContent:
        """The content under the result-reader rule. JobNotFound, ResultUnavailable."""
        ...
    def delete(self, job_id: str, caller: CallerContext) -> None:
        """Delete one job (S7 parity 6.7). The caller who may cancel it may delete it. Cancels first; when the cancel is
        not confirmed, raises JobDeleteConflict and keeps the job. Then persists a tombstone: the job is gone from get,
        events, open_result and listing at once and after a restart, and is reaped after the retention window. A
        job already deleted, absent or not the caller's raises JobNotFound."""
        ...


# ---------------------------------------------------------------------------------------------------------- I5


@dataclass(frozen=True)
class RouteSpec:
    methods: tuple[str, ...]
    canonical: str
    aliases: tuple[str, ...] = ()            # path aliases of this route; model aliases live in the name view
    runtime_policy: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FrontendRow:
    id: str
    shape: str
    routes: tuple[RouteSpec, ...]
    # The kernel's closed selector schema, as read from the row: {"ids": [...]} or {"match": {...}}. One representation:
    # rows are validated with ``workflow_kernel.selector_problems`` at load and matched with ``selector_matches``.
    selects: Mapping[str, Any]
    mode: Literal["sync", "job"]
    receipt_protocol: str
    strictness: Literal["exact", "lenient"] = "exact"
    lists_as_model: bool = False
    # Where the receipt opens (S7 parity M1): "before_policy" leaves a closed ``rejected`` receipt when the route policy
    # denies the request; "after_policy" (default) leaves none.
    receipt_opens: Literal["before_policy", "after_policy"] = "after_policy"
    # Which text a failed job of this row stores (see ``RunRequest.failure_text``); one value for all of the row's routes.
    job_failure_text: Literal["adapter", "category_copy"] = "adapter"
    # How many tries a run of this row gets: "binding" (the binding's retry rule) or "none" (one try: a caller who waits
    # on the answer gets the first failure). Data of the row (``runtime_policy.job_retries``).
    job_retries: Literal["binding", "none"] = "binding"
    # The answer form a request that names none gets (``runtime_policy.default_answer``); one of the forms the row's shape
    # accepts. None: the shape's own rule.
    default_answer: str | None = None


class FrontendShape(Protocol):
    """A reviewed wire shape (code). It turns its wire request into a ``RunRequest`` (staging uploads through the
    request spool) and renders a job view or a refusal in its own vocabulary; for ``mode: sync`` it waits through
    ``JobService.wait`` and reads content through ``JobService.open_result`` with the caller. Authentication and the runtime route policy run before ``to_run_request``."""

    shape_id: str

    def profile(self, contract: Mapping[str, Any]) -> tuple[str, ...]: ...      # reasons it cannot serve the contract
    async def to_run_request(self, request: Request, row: FrontendRow, snapshot: RegistrySnapshot, *,
                             spool: RequestSpool) -> RunRequest | Response: ...
    async def to_response(self, outcome: RunJobView | RunRefused, row: FrontendRow, *, jobs: JobService,
                          caller: CallerContext, request: Request) -> Response:
        """``request`` is the wire request ``to_run_request`` read: the answer form a caller chose on the wire (an
        images ``response_format``, an audio container) is read from it here, never from the job (integration
        addendum, CODE_TO_DATA_S4_S8 13.2)."""
        ...
