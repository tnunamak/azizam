"""The frozen interfaces of code-to-data S4 to S8 (docs/design/CODE_TO_DATA_S4_S8.md, section 3).

Types and protocols only: no behaviour, no I/O, no imports of the main module. Every S4-S8 builder codes against these
names. A change here is a design change: it is made by the design owner and announced to every builder.

I1 RegistrySnapshot / WorkflowEntry / BindingDef      the immutable view of what can run
I2 RegistryStore / Commit / RegistryConflict / ...    edits, checks, history (S4)
I3 ExecutionProfile / ReceiptIdentity                 projections are pure functions of a snapshot (S5)
I4 RunRequest / RunAccepted / RunCore                 the run core (S6)
I5 FrontendRow / FrontendShape / WorkflowSelector     routes are rows over reviewed wire shapes (S7)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from gateway.manifests import ContractError, Provenance
from gateway.workflow_contract import Report

if TYPE_CHECKING:  # the HTTP and auth types are only named, never constructed here
    from starlette.requests import Request
    from starlette.responses import Response

EntryState = Literal["active", "disabled"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
OutputKind = Literal["json", "asset", "list"]

# Refusal codes the store, the run core and the frontends share (the error vocabulary of this design).
WORKFLOW_DISABLED = "workflow_disabled"
WORKFLOW_DELETED = "workflow_deleted"
WORKFLOW_EXISTS = "workflow_exists"
ID_CLASH = "id_clash"
BOUND_EXCEEDED = "workflow_bound_exceeded"
RESTART_NO_PIN = "restart_no_pin"
ENVELOPE_VERSION_UNSUPPORTED = "envelope_version_unsupported"
GENERATION_MISMATCH = "generation_mismatch"


# ---------------------------------------------------------------------------------------------------------- I1


@dataclass(frozen=True)
class WorkflowFacts:
    """The record-level policy facts of one workflow. The four S3 facts plus ``resource_class`` (the admission and queue
    class). All are stated; a workflow that cannot state them has no facts and needs configuration."""

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
    id: str
    contract: Mapping[str, Any]
    contract_digest: str
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
        return self.facts is None


class RegistrySnapshot(Protocol):
    revision: int
    digest: str
    workflows: Mapping[str, WorkflowEntry]
    frontends: Mapping[str, FrontendRow]

    def get(self, workflow_id: str) -> WorkflowEntry | None: ...
    def by_intent(self, intent: str) -> tuple[WorkflowEntry, ...]: ...
    def runnable(self, workflow_id: str) -> WorkflowEntry | None: ...


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


@dataclass(frozen=True)
class HistoryEntry:
    record_revision: int
    contract_digest: str
    saved_at_s: int
    actor: Actor


class RegistryConflict(Exception):
    """``if_match`` is not the current revision (HTTP 412 ``generation_mismatch``), or a create found the id."""

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
    def snapshot(self) -> RegistrySnapshot: ...
    def check(self, doc: Mapping[str, Any], *, workflow_id: str | None, snapshot: RegistrySnapshot) -> Report: ...
    def put_workflow(self, doc: Mapping[str, Any], *, if_match: int | None, actor: Actor,
                     facts: WorkflowFacts | None = None) -> Commit: ...
    def set_state(self, workflow_id: str, state: EntryState, *, if_match: int, actor: Actor) -> Commit: ...
    def set_published(self, workflow_id: str, published: bool, *, if_match: int, actor: Actor) -> Commit: ...
    def delete_workflow(self, workflow_id: str, *, if_match: int, actor: Actor) -> Commit: ...
    def history(self, workflow_id: str, *, limit: int = 20) -> tuple[HistoryEntry, ...]: ...
    def restore(self, workflow_id: str, contract_digest: str, *, if_match: int, actor: Actor) -> Commit: ...
    def subscribe(self, listener: Callable[[RegistrySnapshot, RegistrySnapshot], None]) -> None: ...


# ---------------------------------------------------------------------------------------------------------- I3


@dataclass(frozen=True)
class MediaAccept:
    media_types: tuple[str, ...]
    max_bytes: int | None = None
    max_items: int = 1


@dataclass(frozen=True)
class ExecutionProfile:
    adapter_id: str
    adapter_facet: str
    resource_class: str
    accepts: Mapping[str, MediaAccept] = field(default_factory=dict)
    produces: Mapping[str, OutputKind] = field(default_factory=dict)
    admission: Literal["job"] = "job"


@dataclass(frozen=True)
class ReceiptIdentity:
    workflow_id: str
    record_revision: int
    contract_digest: str
    binding_key: str
    intent: str


# ---------------------------------------------------------------------------------------------------------- I4


@dataclass(frozen=True)
class MediaInput:
    """An uploaded part (spooled, with its sniffed type and size) or a library asset reference."""

    asset_id: str | None = None
    spool_path: str | None = None
    media_type: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class RunRequest:
    workflow_id: str
    inputs: Mapping[str, Any]
    media: Mapping[str, MediaInput] = field(default_factory=dict)
    binding_key: str | None = None
    idempotency_key: str | None = None
    wait_s: float = 0.0
    frontend_id: str = "workflows"


@dataclass(frozen=True)
class RunAccepted:
    job_id: str
    status: JobStatus
    workflow_id: str
    record_revision: int
    result: Mapping[str, Any] | None = None   # the published result bundle's JSON, when finished within wait_s


class RunCore(Protocol):
    async def submit_run(self, req: RunRequest, ctx: Any, *, snapshot: RegistrySnapshot) -> RunAccepted | Response: ...
    async def execute_run(self, job_id: str) -> None: ...
    def recover_runs(self, now_s: int) -> Mapping[str, int]: ...   # outcome -> count


# ---------------------------------------------------------------------------------------------------------- I5


@dataclass(frozen=True)
class RouteSpec:
    methods: tuple[str, ...]
    canonical: str
    aliases: tuple[str, ...] = ()
    runtime_policy: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowSelector:
    """Explicit ids, or a validated predicate over the contract (inputs present and typed, output kinds). Never an
    intent word alone."""

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
    shape_id: str

    def profile(self, contract: Mapping[str, Any]) -> list[str]: ...
    async def to_run_request(self, request: Request, row: FrontendRow,
                             snapshot: RegistrySnapshot) -> RunRequest | Response: ...
    def to_response(self, accepted: RunAccepted, row: FrontendRow) -> Response: ...
