"""The run core, the job service and adapter availability of code-to-data S6 (docs/design/CODE_TO_DATA_S4_S8.md,
sections 5 and 7).

``WorkflowRunCore`` accepts a run (``submit_run``, section 7.2), runs a queued job from what it pinned
(``execute_run``, the dispatch checks of 5.2) and decides at start what each unfinished job needs (``recover_runs``,
5.4). ``WorkflowJobService`` is the one reader and controller of a workflow job for its caller. ``BindingAvailability``
answers whether this deployment can run a binding.

The module names no intent, capability, adapter, engine or model: what the workflow is and how it runs are data in the
pinned contract and binding. Everything that belongs to the main module (who the caller is, plan and exposure policy,
admission, the execution spool of a request body, how a job is scheduled and the engine adapters themselves) is reached
through ``RunPort``, a narrow injected interface. This module never imports the main module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gateway import generated_defaults, log_scrub, media_jobs, result_bundle
from gateway.definition_store import DefinitionUnavailable
from gateway.frontend_shapes import media_job as media_job_shape
from gateway.job_envelope import UNTRACKED, build_job_envelope
from gateway.manifests import ContractError, ManifestError, validate_inputs
from gateway.pinned_definitions import PinnedDefinitions, load_in
from gateway.request_spool import JobSpool
from gateway.result_bundle import Reader, ResultLimits, ResultRefused
from gateway.workflow_envelope import Envelope
from gateway.workflow_kernel import execution_contract, run_identity
from gateway.workflow_runtime_types import (
    ENVELOPE_VERSION_UNSUPPORTED, IDEMPOTENCY_CONFLICT, IDEMPOTENCY_IN_PROGRESS, PIN_NOT_RECORDED, SPOOL_MISSING,
    SERVICE_REASONS, SUBMISSION_UNKNOWN, WORKFLOW_DELETED, WORKFLOW_DISABLED, WORKFLOW_NOT_FOUND, AdapterAvailability, BindingDef,
    CallerContext, JobDeleteConflict, JobEvent, JobNotFound, MediaRef, Projections, RecoveryReport, ResultContent, ResultRef,
    ResultUnavailable, RunFailure, RunJobView, RunRefused, RunRequest, SnapshotProvider, WorkflowEntry,
)

log = logging.getLogger(__name__)

# The job store's intent and mode for a job made by this core. The job's workflow id is the workflow it runs.
RUN_INTENT = "workflow.run"


def is_run_job(job: Mapping[str, Any] | None) -> bool:
    """A job the run core owns. Its row intent is the route's declared one (or ``RUN_INTENT``); the mode is always the run's."""
    if not job:
        return False
    return job.get("intent") == RUN_INTENT or (job.get("metadata") or {}).get("mode") == RUN_INTENT
# How a job's terminal status closes the request receipt it carries (status, reason); a failed job uses its error code.
RECEIPT_OUTCOMES = {
    media_jobs.STATUS_SUCCEEDED: ("succeeded", "succeeded"),
    media_jobs.STATUS_FAILED: ("failed", "failed"),
    media_jobs.STATUS_CANCELED: ("cancelled", "cancelled"),
}
RESULT_METADATA_KEY = "result_metadata"      # where the adapter's result metadata sits in the job's metadata
CLAIM_LEASE_S = 120.0       # the request timeout plus a margin: it only decides when a takeover may happen
CLAIM_WAIT_S = 2.0          # how long a second request waits for the first to decide (section 7.2, step 1)
CLAIM_POLL_S = 0.05
RETRY_AFTER_S = 2
WAIT_POLL_S = 0.1
TOMBSTONE_RETENTION_S = 24 * 3600     # a deleted job is hidden at once and removed after this long

# I7: the code's value of each key is its ceiling; the runtime config (section ``workflow_execution``) may only lower it.
CONFIG_SECTION = "workflow_execution"
RUN_CEILINGS = {"max_wait_s": 60, "history_depth": 20,
                "bounds": {"max_template_bytes": 512 * 1024, "max_nodes": 2000, "max_depth": 32}}


@dataclass(frozen=True)
class RunLimits:
    max_wait_s: int = RUN_CEILINGS["max_wait_s"]
    history_depth: int = RUN_CEILINGS["history_depth"]
    max_template_bytes: int = RUN_CEILINGS["bounds"]["max_template_bytes"]
    max_nodes: int = RUN_CEILINGS["bounds"]["max_nodes"]
    max_depth: int = RUN_CEILINGS["bounds"]["max_depth"]


def _lowered(raw: Any, ceiling: int) -> int:
    """A positive whole number cut to ``ceiling``; anything else keeps the ceiling."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return min(raw, ceiling)
    return ceiling


def run_limits_from_config(config: Mapping[str, Any]) -> RunLimits:
    """The limits of I7 from the runtime config, as ``result_bundle.limits_from_config`` reads ``result_limits``, with the
    code's value as the ceiling."""
    section = config.get(CONFIG_SECTION) if isinstance(config, Mapping) else None
    section = section if isinstance(section, Mapping) else {}
    bounds = section.get("bounds") if isinstance(section.get("bounds"), Mapping) else {}
    ceilings = RUN_CEILINGS["bounds"]
    return RunLimits(
        max_wait_s=_lowered(section.get("max_wait_s"), RUN_CEILINGS["max_wait_s"]),
        history_depth=_lowered(section.get("history_depth"), RUN_CEILINGS["history_depth"]),
        max_template_bytes=_lowered(bounds.get("max_template_bytes"), ceilings["max_template_bytes"]),
        max_nodes=_lowered(bounds.get("max_nodes"), ceilings["max_nodes"]),
        max_depth=_lowered(bounds.get("max_depth"), ceilings["max_depth"]))


INVALID_INPUTS = "invalid_inputs"
BINDING_UNKNOWN = "binding_unknown"
NEEDS_POLICY = "needs_policy"
CALLER_UNAVAILABLE = "caller_unavailable"
BINDING_UNAVAILABLE = "binding_unavailable"
INTERNAL_ERROR = "internal_error"
JOB_DELETED = "job_deleted"
FAILED_FALLBACK = "The job failed."

# What a restart says about a job it cannot finish (S7 parity M12), keyed by the reason. One table for every kind of job.
RESTART_PAYLOAD_MISSING = "Image media job payload missing after proxy restart"
RESTART_RECOVERY_OFF = "The gateway restarted with job recovery turned off"
RESTART_CANCEL_UNCONFIRMED = "The gateway restarted before cancellation could be confirmed"
RESTART_NOT_RUN_AGAIN = "The gateway restarted before completion could be confirmed; this job was not run again"
RECOVERY_FAILURE_TEXT: Mapping[str, str] = {
    "interrupted_before_submit": RESTART_PAYLOAD_MISSING,
    "recovery_off": RESTART_RECOVERY_OFF,
    "cancel_unconfirmed": RESTART_CANCEL_UNCONFIRMED,
    "submission_unknown": RESTART_NOT_RUN_AGAIN,
}


RETRY_CANCEL_UNCONFIRMED = "Cancellation was requested; the backend stop could not be confirmed"
# The job-retry rule the old image routes used (S7 parity M13). A binding overrides each value under ``retry``.
RETRY_MAX_ATTEMPTS, RETRY_BASE_S, RETRY_MAX_S = 3, 5.0, 30.0


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = RETRY_MAX_ATTEMPTS
    base_s: float = RETRY_BASE_S
    max_s: float = RETRY_MAX_S

    def delay_after(self, exc: BaseException, attempt: int) -> float | None:
        """Seconds to wait before the next attempt, or None when this failure settles the job: the adapter did not name a
        ``retry_reason`` (``retryable`` alone tells the caller it may ask again; it does not retry inside the job), or
        ``attempt`` was the last. An engine's ``retry_after_s`` wins over the doubling backoff; both stop at ``max_s``."""
        reason = getattr(exc, "retry_reason", None)
        if attempt >= self.max_attempts or not (isinstance(reason, str) and reason):
            return None
        given = getattr(exc, "retry_after_s", None)
        if isinstance(given, (int, float)) and not isinstance(given, bool) and given >= 0:
            return min(float(given), self.max_s)
        return min(self.base_s * (2 ** max(0, attempt - 1)), self.max_s)


def retry_policy(binding: Mapping[str, Any]) -> RetryPolicy:
    """The retry rule a pinned binding states under ``retry`` (``max_attempts``, ``base_s``, ``max_s``); a value that is
    missing or not a sensible number keeps the default."""
    given = binding.get("retry") if isinstance(binding, Mapping) else None
    given = given if isinstance(given, Mapping) else {}

    def number(name: str, default: float, minimum: float) -> float:
        value = given.get(name)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum else default

    return RetryPolicy(max_attempts=int(number("max_attempts", RETRY_MAX_ATTEMPTS, 1)),
                       base_s=number("base_s", RETRY_BASE_S, 0), max_s=number("max_s", RETRY_MAX_S, 0))


# ------------------------------------------------------------------------------------------------------- the port


@dataclass(frozen=True)
class AdapterIdentity:
    """What a job pins about the code and the engine that will run it. ``origin`` is digested into the envelope, never
    stored."""

    protocol_id: str
    protocol_revision: str | int
    adapter_id: str
    interface_version: int
    service_id: str
    origin: str


@dataclass(frozen=True)
class RunContext:
    """Everything ``RunPort.run_adapter`` needs to run one job from its pin. Nothing in it is read from the live
    registry. ``caller`` is None only when a poll resumes a job whose caller can no longer be rebuilt."""

    job_id: str
    caller: CallerContext | None
    pin: PinnedDefinitions
    payload: Mapping[str, Any]
    inputs: Mapping[str, Any]
    media: Mapping[str, tuple[MediaRef, ...]]
    generated: Mapping[str, Any]
    spool: JobSpool
    request_id: str | None = None      # the request the job answers (the Library asset origin names it)
    # The adapter calls it once, with its result metadata, before it commits the bundle (S7 parity M5).
    record_result_metadata: Callable[[Mapping[str, Any]], None] = lambda metadata: None


class RunPort(Protocol):
    """The main module's side of the run core. Each method is one decision or one effect the main module owns."""

    available: AdapterAvailability

    # who is asking
    def owner_scope(self, caller: CallerContext) -> str: ...
    def maker(self, caller: CallerContext) -> str: ...
    def is_owner(self, caller: CallerContext) -> bool: ...
    def reader(self, caller: CallerContext) -> Reader: ...
    def audience(self, caller: CallerContext) -> str: ...
    def can_read(self, job: Mapping[str, Any], caller: CallerContext) -> bool: ...
    def can_cancel(self, job: Mapping[str, Any], caller: CallerContext) -> bool: ...
    def job_owner(self, caller: CallerContext) -> tuple[str | None, str | None, str | None]:
        """(owner key hash, App id, key name) to stamp on the job."""
        ...

    # policy and admission
    def target_refusal(self, entry: WorkflowEntry, caller: CallerContext) -> RunRefused | None:
        """Exposure and plan for a new run (``published`` is checked by the core)."""
        ...
    def dispatch_target_problem(self, entry: WorkflowEntry, caller: CallerContext) -> str | None:
        """The same decision at dispatch; a stable code or None. ``published`` is not asked again."""
        ...
    def admission_refusal(self, resource_class: str, caller: CallerContext, *, count_queue: bool) -> RunRefused | None: ...
    def result_limits(self) -> ResultLimits: ...
    def resume_on_start(self) -> bool:
        """Whether a restart resumes unfinished jobs (the operator's switch). False fails them with the recovery text."""
        ...

    # what the job pins
    def adapter_identity(self, entry: WorkflowEntry, binding_key: str, binding: BindingDef) -> AdapterIdentity: ...
    def template_bytes(self, binding: BindingDef) -> bytes | None: ...

    # the execution payload of a request (the prompt-memory rules decide whether it reaches disk)
    def caller_payload(self, caller: CallerContext) -> Mapping[str, Any]:
        """The identity block ``caller_from_payload`` rebuilds the caller from. No secret: a key hash and ids."""
        ...
    def save_payload(self, job_id: str, payload: Mapping[str, Any], caller: CallerContext) -> None: ...
    def load_payload(self, job_id: str) -> Mapping[str, Any] | None: ...
    def drop_payload(self, job_id: str) -> None: ...
    def caller_from_payload(self, payload: Mapping[str, Any]) -> CallerContext | None:
        """Dispatch check 1: the caller rebuilt from the admin store, or None (the job then fails)."""
        ...

    # running
    def schedule(self, job_id: str) -> None: ...
    async def run_adapter(self, ctx: RunContext) -> None:
        """Run or poll the job through its pinned adapter. On success the result bundle is committed and the job is
        terminal. Raises for a failure. These attributes of the exception become the job's failure: ``code``,
        ``category``, ``retryable``, ``upstream_status`` and ``client_message`` (the text the caller reads; the exception's
        own text never reaches the job).
        The adapter gives its result metadata (S7 parity M5) to ``ctx.record_result_metadata`` BEFORE it commits the
        bundle, so a waiter that sees the terminal job also sees the metadata."""
        ...
    def read_asset(self, asset_id: str, caller: CallerContext) -> bytes | None: ...


# ------------------------------------------------------------------------------------------------ availability


class BindingAvailability:
    """Whether this deployment can run one binding (I3 ``AdapterAvailability``). ``check`` is the main module's decision
    (the adapter is registered and every reviewed role is present); this class gives it the interface projections read."""

    def __init__(self, check: Callable[[BindingDef], str | None],
                 retry_after: Callable[[BindingDef], int | None] | None = None) -> None:
        self._check = check
        self._retry_after = retry_after

    def binding_problem(self, binding: BindingDef) -> str | None:
        return self._check(binding)

    def binding_retry_after_s(self, binding: BindingDef) -> int | None:
        return self._retry_after(binding) if self._retry_after is not None else None


# ---------------------------------------------------------------------------------------------------- helpers


def asset_origin(contract: Mapping[str, Any], request_id: str | None) -> dict[str, Any]:
    """What the Library shows about a file a run made (S7 parity M3): the request, the workflow's display name and whether
    the workflow takes words from the caller. An empty value is left out, except ``takes_text``."""
    takes_text = any(isinstance(spec, Mapping) and spec.get("type") == "string"
                     for spec in (contract.get("inputs") or {}).values())
    origin = {"workflow_name": contract.get("display_name"), "request_id": request_id, "takes_text": takes_text}
    return {key: value for key, value in origin.items() if value or key == "takes_text"}


def receipt_writes(entry: WorkflowEntry, req: RunRequest, result_metadata: Mapping[str, Any]) -> dict[str, Any]:
    """The fields a request receipt takes from a run (S7 parity M1): what the request supplied (the shape's
    ``accepted_inputs`` when it states them, else the wire options in the order the shape put them, then the inputs, then
    the media), the workflow it resolved to and that workflow's manifest
    identity, and ``metadata``: the mapping the adapter named ``receipt`` in its result metadata, copied as it is. The core
    does not read inside it."""
    from gateway.manifests import ManifestError, manifest_identity

    contract = plain(entry.contract)
    try:
        identity = manifest_identity(dict(contract))
        manifest_hash = identity.manifest_hash
    except ManifestError:
        manifest_hash = None
    accepted = list(req.accepted_inputs) or [*req.options, *req.inputs, *req.media]
    return {"accepted_inputs": accepted, "resolved_workflow": entry.id,
            "manifest_id": entry.id, "manifest_version": contract.get("version"), "manifest_hash": manifest_hash,
            "metadata": dict(result_metadata.get("receipt") or {})}


def plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def _failure(code: str, category: str, message: str, *, retryable: bool = False) -> RunFailure:
    return RunFailure(code=code, category=category, message=message, retryable=retryable)


def _published_results(job_id: str, path: Path) -> tuple[ResultRef, ...] | None:
    """The published result items of a job, by reference, or None while nothing is published."""
    with media_jobs.connection(path) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_stage'").fetchone() is None:
            return None
        rows = conn.execute(
            "SELECT name, kind, count, bytes, items_json FROM result_stage WHERE job_id=? AND state='published' "
            "ORDER BY name", (job_id,)).fetchall()
    if not rows:
        return None
    refs: list[ResultRef] = []
    for name, kind, count, size, items_json in rows:
        if kind == "json":
            refs.append(ResultRef(name, 0, result_bundle.JSON_MEDIA_TYPE, size))
            continue
        for index, item in enumerate(json.loads(items_json or "[]")):
            refs.append(ResultRef(name, index, item["media_type"], item.get("size_bytes"), asset_id=item["asset_ref"]))
    return tuple(refs)


def _envelope_facts(job_id: str, path: Path) -> tuple[int | None, str | None]:
    """(record_revision, binding_key) from the job's envelope; None for a version 1 job or none at all. Never filled from
    the live registry."""
    try:
        with media_jobs.connection(path) as conn:
            pin = load_envelope(conn, job_id)
    except DefinitionUnavailable:
        return None, None
    if isinstance(pin, Envelope):
        return pin.record_revision, pin.binding_key
    return None, None


def load_envelope(conn: sqlite3.Connection, job_id: str) -> Envelope | None | str:
    """The job's envelope, None when it has none, or ``ENVELOPE_VERSION_UNSUPPORTED`` when it cannot be read as a known
    version."""
    from gateway.definition_store import envelope_in

    row = conn.execute("SELECT envelope_json FROM job_envelope WHERE job_id=?", (job_id,)).fetchone() \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_envelope'").fetchone() else None
    if row is None:
        return None
    try:
        return envelope_in(conn, job_id)
    except DefinitionUnavailable:
        try:
            version = json.loads(row[0]).get("envelope_version")
        except ValueError:
            version = None
        from gateway.workflow_envelope import SUPPORTED_VERSIONS
        if version not in SUPPORTED_VERSIONS:
            return ENVELOPE_VERSION_UNSUPPORTED
        raise


def job_view(job: Mapping[str, Any], path: Path, *, disposition: str = "new") -> RunJobView:
    """A job row as a ``RunJobView``: references only, never a value or bytes."""
    status = str(job["status"])
    failure = None
    if status == media_jobs.STATUS_FAILED:
        failure = RunFailure(code=str(job.get("error_code") or INTERNAL_ERROR),
                             category=str(job.get("error_category") or "internal"),
                             message=str(job.get("error") or "The job failed."),
                             retryable=bool(job.get("retryable")), upstream_status=job.get("upstream_http_status"))
    revision, binding_key = _envelope_facts(str(job["job_id"]), path)
    results = _published_results(str(job["job_id"]), path) if status == media_jobs.STATUS_SUCCEEDED else None
    return RunJobView(
        job_id=str(job["job_id"]), status=status, disposition=disposition,  # type: ignore[arg-type]
        workflow_id=str(job.get("workflow") or ""), record_revision=revision, binding_key=binding_key,
        created_at_s=int(job.get("created_at") or 0), updated_at_s=int(job.get("updated_at") or 0),
        failure=failure, results=results, result_metadata=dict((job.get("metadata") or {}).get(RESULT_METADATA_KEY) or {}))


# ----------------------------------------------------------------------------------------------------- the core


class WorkflowRunCore:
    """``RunCore`` over the job store. See the module docstring for what is injected."""

    def __init__(
        self, *, snapshots: SnapshotProvider, projections: Projections, port: RunPort, spool: JobSpool,
        path: Path | None = None, generate: Callable[..., Mapping[str, Any]] = generated_defaults.resolve_generated,
        claim_lease_s: float = CLAIM_LEASE_S, claim_wait_s: float = CLAIM_WAIT_S,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._sleep = sleep if sleep is not None else (lambda delay: asyncio.sleep(delay))     # resolved at call time
        self._snapshots = snapshots
        self._projections = projections
        self._port = port
        self._spool = spool
        self._path = path or media_jobs.DB_PATH
        self._generate = generate
        self._lease_s = claim_lease_s
        self._wait_s = claim_wait_s
        self._clock = clock

    # ------------------------------------------------------------------------------------------- submit

    async def submit_run(self, req: RunRequest, caller: CallerContext, *,
                         snapshot) -> RunJobView | RunRefused:
        port = self._port
        scope, maker = port.owner_scope(caller), port.maker(caller)
        fence = None
        if req.idempotency_key:
            fence = media_jobs.RunClaimFence(scope, maker, req.idempotency_key, secrets.token_hex(16))
            decided = await self._claim(fence, run_identity(scope, maker, req))
            if decided is not None:
                return decided
        try:
            created = await self._decide_and_create(req, caller, snapshot, fence)
        except media_jobs.ClaimLost:
            return await self._resolve_lost(fence)
        except BaseException:
            if fence is not None:
                with contextlib.suppress(Exception):
                    media_jobs.release_run_claim(fence, path=self._path)
            raise
        if isinstance(created, RunRefused):
            if fence is not None:
                media_jobs.release_run_claim(fence, path=self._path)
            return created
        # The job and its claim are final. Nothing from here deletes either of them.
        try:
            port.schedule(created["job_id"])
        except Exception:  # noqa: BLE001 - the job stays queued; recovery schedules it and a retry replays it
            log.exception("job %s was created but not scheduled", created["job_id"])
        return job_view(created, self._path, disposition="new")

    async def _claim(self, fence: media_jobs.RunClaimFence, identity: str) -> RunJobView | RunRefused | None:
        """Step 1. None means this request holds the claim."""
        deadline = time.monotonic() + self._wait_s
        while True:
            claim = await asyncio.to_thread(
                media_jobs.claim_run_key, owner_scope=fence.owner_scope, maker=fence.maker, key=fence.key,
                identity=identity, holder=fence.holder, lease_s=self._lease_s, now=self._clock(), path=self._path)
            if claim.outcome == "held":
                return None
            if claim.outcome == "conflict":
                return RunRefused(409, IDEMPOTENCY_CONFLICT, "this Idempotency-Key was used for a different request")
            if claim.outcome == "replay":
                return self._replay(claim.job_id)
            if time.monotonic() >= deadline:
                return RunRefused(409, IDEMPOTENCY_IN_PROGRESS,
                                  "the first request with this Idempotency-Key has not finished; retry",
                                  retry_after_s=RETRY_AFTER_S)
            await asyncio.sleep(CLAIM_POLL_S)

    def _replay(self, job_id: str | None) -> RunJobView | RunRefused:
        job = media_jobs.get(job_id or "", audience="internal", path=self._path)
        if job is None:
            return RunRefused(409, IDEMPOTENCY_IN_PROGRESS, "the job for this Idempotency-Key is not readable yet; retry",
                              retry_after_s=RETRY_AFTER_S)
        if media_jobs.tombstoned(str(job["job_id"]), path=self._path):     # deleted: the claim stays until the reaper
            return RunRefused(404, JOB_DELETED, "the job for this Idempotency-Key was deleted")
        return job_view(job, self._path, disposition="replay")

    async def _resolve_lost(self, fence: media_jobs.RunClaimFence | None) -> RunJobView | RunRefused:
        """This holder's claim was taken over or decided while it worked: it drew and stored nothing. Answer as the new
        holder's request would be answered, and leave the claim alone."""
        assert fence is not None
        state = media_jobs.get_run_claim(owner_scope=fence.owner_scope, maker=fence.maker, key=fence.key, path=self._path)
        if state is not None and state[1] is not None:
            return self._replay(state[1])
        return RunRefused(409, IDEMPOTENCY_IN_PROGRESS, "another request holds this Idempotency-Key; retry",
                          retry_after_s=RETRY_AFTER_S)

    def target_gate(self, workflow_id: str, caller: CallerContext, *, snapshot,
                    binding_key: str | None = None) -> RunRefused | None:
        """Step 2's entry and target policy alone: the route asks it before it reads or stages a body, so a caller who
        may not run the workflow is refused before any upload is accepted; ``submit_run`` asks it again. It also
        answers the live state of the service behind the binding (``binding_key``, or the entry's default): a
        ``SERVICE_*`` reason is a 503 with ``retry_after_s``, before any job exists (S7 parity M4)."""
        port = self._port
        entry = snapshot.get(workflow_id)
        if entry is None or (not entry.published and not port.is_owner(caller)):
            return RunRefused(404, WORKFLOW_NOT_FOUND, f"no workflow {workflow_id}")
        if entry.state != "active":
            return RunRefused(409, WORKFLOW_DISABLED, f"workflow {entry.id} is disabled")
        refused = port.target_refusal(entry, caller)
        if refused is not None:
            return refused
        binding = entry.bindings.get(binding_key) if binding_key else next(iter(entry.bindings.values()), None)
        reason = port.available.binding_problem(binding) if binding is not None else None
        if reason in SERVICE_REASONS:
            return RunRefused(503, str(reason), f"the service behind workflow {entry.id} cannot take a run now ({reason})",
                              retry_after_s=port.available.binding_retry_after_s(binding))
        return None

    async def _decide_and_create(self, req: RunRequest, caller: CallerContext, snapshot,
                                 fence: media_jobs.RunClaimFence | None) -> dict[str, Any] | RunRefused:
        """Steps 2 to 6. Returns the created job, or a refusal. Nothing here is visible until the fenced commit."""
        port = self._port
        refused = self.target_gate(req.workflow_id, caller, snapshot=snapshot, binding_key=req.binding_key)
        if refused is not None:
            return refused
        entry = snapshot.get(req.workflow_id)
        problems = self._projections.entry_problems(entry, port.available, binding_key=req.binding_key)
        if problems:
            first = problems[0]
            status = 422 if first == BINDING_UNKNOWN else 409 if first == NEEDS_POLICY else 503
            return RunRefused(status, first, f"workflow {entry.id} cannot start a run: {', '.join(problems)}")
        binding_key = req.binding_key or next(iter(entry.bindings))
        profile = self._projections.execution_profile(entry, binding_key, limits=port.result_limits())
        errors = self._validate(entry, req, profile)
        if errors:
            return RunRefused(422, INVALID_INPUTS, "the run request is not valid", problems=tuple(errors))
        refused = port.admission_refusal(profile.resource_class, caller, count_queue=True)
        if refused is not None:
            return refused
        try:
            return await asyncio.to_thread(self._create, req, caller, entry, binding_key, profile, fence)
        except (ManifestError, generated_defaults.GeneratedValueUnavailable) as exc:
            return RunRefused(422, INVALID_INPUTS, "a generated input could not be filled",
                              problems=(ContractError(("inputs",), "generated_value_invalid", str(exc)),))

    def _validate(self, entry: WorkflowEntry, req: RunRequest, profile) -> list[ContractError]:
        """Step 3: the inputs against the current contract and the media against the profile, every problem at once."""
        contract = plain(entry.contract)
        generated = generated_defaults.generated_inputs(contract.get("inputs") or {})
        supplied = dict(req.inputs)
        errors: list[ContractError] = []
        for name in req.media:
            if name in supplied:
                errors.append(ContractError(("inputs", name), "value_invalid", "given both as a value and as media"))
            supplied[name] = "upload"           # the placeholder: media is checked below against the profile
        try:
            result = validate_inputs(contract, supplied)
        except ManifestError as exc:
            return [ContractError(("contract",), "contract_invalid", str(exc))]
        for error in result.errors:
            # A generated input the caller left empty is filled at create; it is not missing.
            if error.code == "required_missing" and len(error.path) == 2 and error.path[1] in generated:
                continue
            errors.append(error)
        for name, items in sorted(req.media.items()):
            accept = profile.accepts.get(name)
            if accept is None:
                errors.append(ContractError(("media", name), "media_not_accepted", "this input takes no media"))
                continue
            if not accept.min_items <= len(items) <= accept.max_items:
                errors.append(ContractError(("media", name), "media_count",
                                            f"expected {accept.min_items} to {accept.max_items} items, got {len(items)}"))
            for index, ref in enumerate(items):
                if not _type_fits(ref.media_type, accept.media_types):
                    errors.append(ContractError(("media", name, index), "media_type", "this media type is not accepted"))
                if accept.max_bytes is not None and ref.size > accept.max_bytes:
                    errors.append(ContractError(("media", name, index), "media_size", "the file is larger than allowed"))
        for name, accept in profile.accepts.items():
            if accept.min_items > 0 and name not in req.media and name not in req.inputs:
                errors.append(ContractError(("media", name), "required_missing", "required media is missing"))
        return errors

    def _create(self, req: RunRequest, caller: CallerContext, entry: WorkflowEntry, binding_key: str, profile,
                fence: media_jobs.RunClaimFence | None) -> dict[str, Any]:
        """Steps 5 and 6: the fenced transaction. ``prepare`` runs inside it, after the claim is confirmed, so a holder
        that lost its claim draws and adopts nothing."""
        port = self._port
        binding = entry.bindings[binding_key]
        owner_hash, app_id, key_name = port.job_owner(caller)
        job = media_jobs.MediaJob(
            intent=req.intent or RUN_INTENT, workflow=entry.id, backend=profile.adapter_id, owner_key_hash=owner_hash, app_id=app_id,
            api_key_name=key_name, request_id=req.request_id or "",
            metadata={"mode": RUN_INTENT, "resource_class": profile.resource_class, "binding_key": binding_key,
                      "frontend": req.frontend_id, "failure_text": req.failure_text, "retries": req.retries})
        adopted: dict[str, tuple[MediaRef, ...]] = {}

        def prepare(record: media_jobs.MediaJob):
            contract = plain(entry.contract)
            generated = self._generate(contract.get("inputs") or {}, req.inputs)
            identity = port.adapter_identity(entry, binding_key, binding)
            envelope = build_job_envelope(
                source_kind="workflow", source_id=entry.id, contract=execution_contract(contract),
                binding=plain(binding.binding), template=port.template_bytes(binding),
                protocol_id=identity.protocol_id, protocol_revision=identity.protocol_revision,
                adapter_id=identity.adapter_id, interface_version=identity.interface_version,
                service_id=identity.service_id, origin=identity.origin, generated=generated,
                incarnation=entry.incarnation, binding_key=binding_key, record_revision=entry.record_revision)
            adopted.update(self._spool.adopt(record.job_id, dict(req.media)))
            port.save_payload(record.job_id, {
                "body": {"inputs": plain(req.inputs), **({"options": plain(req.options)} if req.options else {}),
                         **({"extensions": plain(req.extensions)} if req.extensions else {})}, "auth": port.caller_payload(caller),
                "media": {name: [_ref_dict(ref) for ref in refs] for name, refs in adopted.items()}}, caller)
            return envelope

        try:
            created = media_jobs.create(job, input_data=None, path=self._path, maker_from_request=True,
                                        require_envelope=True, claim=fence, prepare=prepare)
        except BaseException:
            self._spool.delete(job.job_id)
            port.drop_payload(job.job_id)
            raise
        with contextlib.suppress(Exception):
            media_jobs.append_event(created.job_id, "queued", {"phase": "queued", "status": media_jobs.STATUS_QUEUED,
                                                              "progress": 0}, path=self._path)
        return created.to_dict("internal")

    # ------------------------------------------------------------------------------------------ execute

    async def execute_run(self, job_id: str) -> None:
        """The worker. Decides from the job's state what it needs (the 5.4 table), then runs the checks of 5.2 in order,
        then the first effect."""
        try:
            await self._execute(job_id)
        finally:
            await self.close_receipt(job_id)

    async def _execute(self, job_id: str) -> None:
        job = media_jobs.get(job_id, audience="internal", path=self._path)
        if job is None or media_jobs.is_terminal(str(job["status"])):
            return
        action = self._classify(job)
        if action == "replay":
            self._finish_from_bundle(job_id)
        elif action in ("submission_unknown", "interrupted_before_submit"):
            self._fail_at_restart(job_id, action)
        else:
            await self._run(job_id, checks=action == "resume")

    async def close_receipt(self, job_id: str) -> None:
        """When the job is terminal, tell the port to close the request receipt the job carries. A job still running (the
        worker was cancelled) keeps it open; the port ignores a receipt it does not hold. Never raises."""
        close = getattr(self._port, "close_receipt", None)
        if close is None:
            return
        try:
            job = media_jobs.get(job_id, audience="internal", path=self._path)
            if job is None or not job.get("request_id") or not media_jobs.is_terminal(str(job["status"])):
                return
            status, reason = RECEIPT_OUTCOMES.get(str(job["status"]), ("failed", "failed"))
            if status == "failed":
                reason = str(job.get("error_code") or reason)
            await close(str(job["request_id"]), status, reason)
        except Exception:  # noqa: BLE001 - a receipt that does not close never changes the job
            log.exception("job %s ended but its receipt was not closed", job_id)

    def _classify(self, job: Mapping[str, Any]) -> str:
        from gateway.workflow_envelope import classify_recovery

        metadata = job.get("metadata") or {}
        return classify_recovery(
            terminal=media_jobs.is_terminal(str(job["status"])), submitting=bool(metadata.get("protocol_submitting")),
            protocol_job=bool(metadata.get("protocol_job")),
            bundle_committed=result_bundle.bundle_summary(str(job["job_id"]), path=self._path) is not None,
            spool_present=self._port.load_payload(str(job["job_id"])) is not None)

    async def _run(self, job_id: str, *, checks: bool) -> None:
        port = self._port
        job = media_jobs.get(job_id, audience="internal", path=self._path)
        if job is None:
            return
        if job["status"] == media_jobs.STATUS_QUEUED:
            try:
                media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, expected_status=media_jobs.STATUS_QUEUED,
                                         progress=5, metadata_patch={"started_at": int(time.time())}, path=self._path)
            except ValueError:      # canceled, or taken by another worker, between the read and the write
                return
            media_jobs.append_event(job_id, "started", {"phase": "running", "status": media_jobs.STATUS_RUNNING, "progress": 5},
                                    path=self._path)
        try:
            with media_jobs.connection(self._path) as conn:
                pin = load_in(conn, job_id)
        except DefinitionUnavailable:
            self._fail(job_id, PIN_NOT_RECORDED, "internal", "The job did not record what it runs, so it cannot start.")
            return
        payload = port.load_payload(job_id)
        if payload is None:
            self._fail(job_id, SPOOL_MISSING, "internal", "The job's request was not kept, so it cannot start.")
            return
        caller = port.caller_from_payload(payload)
        if checks:
            problem = self._dispatch_problem(job, pin, caller)
            if problem is not None:
                self._fail(job_id, *problem)
                return
        policy = retry_policy(pin.binding)
        if (job.get("metadata") or {}).get("retries") == "none":
            policy = dataclasses.replace(policy, max_attempts=1)
        attempt = int(job.get("attempt_number") or 1)       # the durable number: a recovered job keeps its budget
        while True:
            ctx = RunContext(
                job_id=job_id, caller=caller, pin=pin, payload=payload,
                inputs=dict((payload.get("body") or {}).get("inputs") or {}),
                media={name: tuple(MediaRef(**item) for item in items) for name, items in (payload.get("media") or {}).items()},
                generated={n: e["value"] for n, e in pin.envelope.generated.items() if "value" in e}, spool=self._spool,
                request_id=str(job.get("request_id") or "") or None,
                record_result_metadata=lambda metadata: self._record_result_metadata(job_id, metadata))
            media_jobs.append_event(
                job_id, "attempt", {"phase": "running", "status": media_jobs.STATUS_RUNNING, "progress": min(95, 5 + (attempt - 1) * 10),
                                    "attempt": attempt, "total": policy.max_attempts}, path=self._path)
            try:
                await port.run_adapter(ctx)
            except asyncio.CancelledError:
                raise
            except media_jobs.CancellationPending:
                with contextlib.suppress(Exception):
                    media_jobs.update_status(job_id, media_jobs.STATUS_CANCELED, error="Canceled by requester", path=self._path)
            except Exception as exc:  # noqa: BLE001 - settled as a failure; only the adapter's client_message reaches the job
                delay = policy.delay_after(exc, attempt)
                if delay is None:
                    category = str(getattr(exc, "category", "") or "internal")
                    text, code = media_job_shape.stored_failure(
                        str((job.get("metadata") or {}).get("failure_text") or "adapter"), category, _client_message(exc),
                        _code_of(exc), _upstream_status(exc))
                    self._fail(job_id, code, category, text, retryable=bool(getattr(exc, "retryable", False)),
                               upstream_status=_upstream_status(exc))
                    return
                caller, payload, problem = await self._before_retry(job_id, exc, attempt, delay, policy)
                if problem is not None:
                    self._fail(job_id, *problem)
                    return
                attempt += 1
                continue
            break
        self._settle(job_id)

    async def _before_retry(self, job_id: str, exc: BaseException, attempt: int, delay: float, policy: RetryPolicy) -> tuple[CallerContext | None, Mapping[str, Any] | None, tuple[str, str, str] | None]:
        """Between two attempts of one job (S7 parity M13): leave the old retry event and facts, wait, then check again
        that nobody asked for a cancel and that the caller can still use the gateway, and start the next attempt on the same
        job. Returns ``(caller, payload, None)`` for the next attempt, or ``(None, None, (code, category, message))``."""
        port = self._port
        status = _upstream_status(exc)
        progress = min(95, 10 + attempt * 10)
        with contextlib.suppress(ValueError):
            media_jobs.update_status(
                job_id, media_jobs.STATUS_RUNNING, progress=progress, path=self._path,
                metadata_patch={"attempts": attempt, "last_http_status": status,
                                "last_retry_reason": getattr(exc, "retry_reason", None) or _code_of(exc)})
        media_jobs.append_event(
            job_id, "retry",
            {"phase": "retrying", "status": media_jobs.STATUS_RUNNING, "progress": progress, "attempt": attempt,
             "total": policy.max_attempts, "http_status": status,
             "reason": getattr(exc, "retry_reason", None) or _code_of(exc), "retry_after_s": delay},
            path=self._path)
        with contextlib.suppress(Exception):
            result_bundle.abort(job_id, result_bundle.attempt_of(
                media_jobs.get(job_id, audience="internal", path=self._path) or {}), path=self._path)
        await self._sleep(delay)
        current = media_jobs.get(job_id, audience="internal", path=self._path) or {}
        if media_jobs.is_terminal(str(current.get("status") or "failed")):
            return None, None, ("job_settled", "internal", "The job ended while it waited to retry.")
        if (current.get("metadata") or {}).get("cancel_requested_at"):
            return None, None, ("cancel_unconfirmed", "capacity", RETRY_CANCEL_UNCONFIRMED)
        payload = port.load_payload(job_id)
        caller = port.caller_from_payload(payload) if payload is not None else None
        if payload is None:
            return None, None, (SPOOL_MISSING, "internal", "The job's request was not kept, so it cannot start.")
        if caller is None:       # a retry runs as the caller is now: a revoked key or connection stops it here
            return None, None, (CALLER_UNAVAILABLE, "authorization", "The account that asked for this job can no longer use the gateway.")
        try:       # refuses a terminal or cancel-pending job inside its own transaction: that stops the retry
            started = media_jobs.begin_attempt(job_id, path=self._path)
        except media_jobs.CancellationPending:
            return None, None, ("cancel_unconfirmed", "capacity", RETRY_CANCEL_UNCONFIRMED)
        except ValueError:
            return None, None, ("job_settled", "internal", "The job ended while it waited to retry.")
        if started is None:
            return None, None, ("job_settled", "internal", "The job ended while it waited to retry.")
        return caller, payload, None

    def _record_result_metadata(self, job_id: str, result_metadata: Mapping[str, Any]) -> None:
        """Keep the adapter's result metadata on the still-running job, as it gave it (the job store refuses unsafe
        content). It does nothing once the job is terminal."""
        try:
            media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, path=self._path,
                                     metadata_patch={RESULT_METADATA_KEY: plain(result_metadata)})
        except ValueError:
            log.warning("job %s result metadata was not stored", job_id, exc_info=True)

    def _dispatch_problem(self, job: Mapping[str, Any], pin: PinnedDefinitions,
                          caller: CallerContext | None) -> tuple[str, str, str] | None:
        """Checks 1 to 6 of 5.2, in order. The first that fails decides; none of them calls an engine."""
        port = self._port
        if caller is None:                                                             # 1. the caller
            return CALLER_UNAVAILABLE, "authorization", "The account that asked for this job can no longer use the gateway."
        envelope = pin.envelope
        entry = self._snapshots.current().get(envelope.source_id)
        tracked = envelope.incarnation not in (None, UNTRACKED)
        if entry is None or (tracked and entry.incarnation != envelope.incarnation):   # 2. the same incarnation
            return WORKFLOW_DELETED, "invalid_input", "The workflow this job was made for was deleted."
        if entry.state != "active":                                                    # 3. active
            return WORKFLOW_DISABLED, "invalid_input", "The workflow this job was made for is disabled."
        if entry.facts is None:                                                        # 4. policy, not ``published``
            return NEEDS_POLICY, "invalid_input", "The workflow has no policy facts."
        denied = port.dispatch_target_problem(entry, caller)
        if denied is not None:
            return denied, "authorization", "The workflow is not available to this account now."
        pinned = BindingDef(adapter_id=envelope.binding_key or "", binding=pin.binding, binding_digest=envelope.binding)
        problems = self._projections.binding_problems(pinned, port.available)         # 5. the PINNED binding
        if problems:
            return problems[0], "capacity", "The binding this job pinned cannot run here now."
        resource_class = str((job.get("metadata") or {}).get("resource_class") or "")
        refused = port.admission_refusal(resource_class, caller, count_queue=False)    # 6. re-admission, no queue count
        if refused is not None:
            return refused.code, "capacity", refused.message
        return None

    def _fail(self, job_id: str, code: str, category: str, message: str, *, retryable: bool = False,
              upstream_status: int | None = None) -> None:
        category = category if category in media_jobs.ERROR_CATEGORIES else "internal"
        try:
            media_jobs.update_status(
                job_id, media_jobs.STATUS_FAILED, error=message, path=self._path,
                error_info={"error_code": code, "error_category": category, "retryable": retryable,
                            "upstream_http_status": upstream_status})
        except ValueError:
            log.info("job %s was already settled when it failed with %s", job_id, code)
        else:
            with contextlib.suppress(Exception):
                media_jobs.append_event(job_id, "failed", {"phase": "failed", "status": media_jobs.STATUS_FAILED, "progress": 100,
                                                           "http_status": upstream_status or 0}, path=self._path)
        self._port.drop_payload(job_id)
        self._spool.delete(job_id)

    def _settle(self, job_id: str) -> None:
        """After ``run_adapter`` returns: a job still unfinished means the adapter ended without a bundle."""
        job = media_jobs.get(job_id, audience="internal", path=self._path)
        if job is None:
            return
        if media_jobs.is_terminal(str(job["status"])):
            self._port.drop_payload(job_id)
            self._spool.delete(job_id)
            return
        self._fail(job_id, INTERNAL_ERROR, "internal", "The job ended without a result.")

    def _finish_from_bundle(self, job_id: str) -> None:
        """Recovery row ``replay``: the bundle is committed; finish the job from it. No engine call."""
        with contextlib.suppress(ValueError):
            if (media_jobs.get(job_id, audience="internal", path=self._path) or {}).get("status") == media_jobs.STATUS_QUEUED:
                media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, expected_status=media_jobs.STATUS_QUEUED,
                                         path=self._path)
            media_jobs.update_status(job_id, media_jobs.STATUS_SUCCEEDED, progress=100, path=self._path)
        self._settle(job_id)

    # ------------------------------------------------------------------------------------------ recovery

    def _restart_reason(self, job: Mapping[str, Any], action: str) -> str | None:
        """Why a restart fails this job instead of running it (a key of ``RECOVERY_FAILURE_TEXT``), or None. The order is
        the old routes': the request was not kept; the operator turned recovery off; a cancel was waiting; the engine
        may have started it (a job that was running and never submitted is not run again either)."""
        if action == "interrupted_before_submit":
            return action
        if action == "poll":        # the engine already has the job: polling it is not a resubmit, so no restart text applies
            return None
        if not self._port.resume_on_start():
            return "recovery_off"
        if (job.get("metadata") or {}).get("cancel_requested_at"):
            return "cancel_unconfirmed"
        if action == "submission_unknown" or (action == "resume" and job.get("status") == media_jobs.STATUS_RUNNING):
            return "submission_unknown"
        return None

    def _fail_at_restart(self, job_id: str, reason: str) -> None:
        code = SPOOL_MISSING if reason == "interrupted_before_submit" else SUBMISSION_UNKNOWN if reason == "submission_unknown" else reason
        category = "internal" if reason == "interrupted_before_submit" else "upstream" if reason == "submission_unknown" else "capacity"
        self._fail(job_id, code, category, RECOVERY_FAILURE_TEXT[reason])

    def recover_runs(self, now_s: int) -> RecoveryReport:
        """Section 5.4: decide, for each unfinished workflow job, what restart does. Rows that need no engine are
        settled here; rows that run (``resume``, ``poll``) are scheduled."""
        counts: dict[str, int] = {}
        failed: list[str] = []

        def note(outcome: str) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1

        for job in media_jobs.unfinished(path=self._path):
            if not is_run_job(job):
                continue
            job_id = str(job["job_id"])
            with media_jobs.connection(self._path) as conn:
                try:
                    envelope = load_envelope(conn, job_id)
                except DefinitionUnavailable:
                    envelope = None
            if envelope is None:
                note("pin_not_recorded")
                self._fail(job_id, PIN_NOT_RECORDED, "internal", "The job did not record what it runs, so it cannot start.")
                failed.append(job_id)
                continue
            if envelope == ENVELOPE_VERSION_UNSUPPORTED:
                note("envelope_version_unsupported")
                self._fail(job_id, ENVELOPE_VERSION_UNSUPPORTED, "internal", "The job's record has a version this gateway cannot read.")
                failed.append(job_id)
                continue
            action = self._classify(job)
            note(action)
            if action == "replay":
                self._finish_from_bundle(job_id)
                continue
            reason = self._restart_reason(job, action)
            if reason is not None:
                self._fail_at_restart(job_id, reason)
                failed.append(job_id)
            elif action in ("resume", "poll"):
                try:
                    self._port.schedule(job_id)
                except Exception:  # noqa: BLE001
                    log.exception("job %s could not be scheduled at recovery", job_id)
        return RecoveryReport(counts=counts, failed_job_ids=tuple(failed))


def _type_fits(media_type: str, accepted: tuple[str, ...]) -> bool:
    family = media_type.split("/", 1)[0]
    return any(item == media_type or item == f"{family}/*" for item in accepted)


def _ref_dict(ref: MediaRef) -> dict[str, Any]:
    return {"digest": ref.digest, "media_type": ref.media_type, "size": ref.size, "filename": ref.filename,
            "wire_media_type": ref.wire_media_type, "asset_id": ref.asset_id, "spool_key": ref.spool_key}


def _client_message(exc: BaseException) -> str:
    """The text a failed job shows its caller: the adapter's own ``client_message``, else the fixed sentence. The text is
    stored and shown, so every registered secret value is removed from it first, whichever adapter wrote it."""
    message = getattr(exc, "client_message", None)
    if not isinstance(message, str) or not message.strip():
        return FAILED_FALLBACK
    return log_scrub.scrub(message, limit=2000)


def _upstream_status(exc: BaseException) -> int | None:
    """The engine's HTTP status. The job store keeps it only for an upstream failure, so another category drops it."""
    status = getattr(exc, "upstream_status", None)
    if getattr(exc, "category", None) != "upstream":
        return None
    return status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None


def _code_of(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and media_jobs.is_error_code(code) else INTERNAL_ERROR


# ------------------------------------------------------------------------------------------------- wire bodies


def view_body(view: RunJobView) -> dict[str, Any]:
    """The JSON body of a job view: references to results, never a value or bytes."""
    body: dict[str, Any] = {
        "job_id": view.job_id, "status": view.status, "disposition": view.disposition, "workflow_id": view.workflow_id,
        "record_revision": view.record_revision, "binding_key": view.binding_key,
        "created_at": view.created_at_s, "updated_at": view.updated_at_s}
    if view.failure is not None:
        failure = view.failure
        body["failure"] = {"code": failure.code, "category": failure.category, "message": failure.message,
                           "retryable": failure.retryable, "upstream_status": failure.upstream_status,
                           "problems": [{"path": e.pointer, "code": e.code, "message": e.message} for e in failure.problems]}
    if view.results is not None:
        body["results"] = [{"output": r.output, "index": r.index, "media_type": r.media_type, "size": r.size,
                            "asset_id": r.asset_id} for r in view.results]
    return body


def refusal_body(refused: RunRefused) -> dict[str, Any]:
    """The JSON body of a refusal (the gateway's error envelope), with every contract problem at its path."""
    error: dict[str, Any] = {"code": refused.code, "message": refused.message}
    if refused.problems:
        error["problems"] = [{"path": e.pointer, "code": e.code, "message": e.message} for e in refused.problems]
    if refused.retry_after_s is not None:
        error["retry_after_s"] = refused.retry_after_s
    return {"error": error}


# --------------------------------------------------------------------------------------------- the job service


class WorkflowJobService:
    """``JobService`` over the job store. Who may read or cancel is the port's decision (the existing media-job rules)."""

    def __init__(self, *, port: RunPort, path: Path | None = None,
                 tombstone_retention_s: float = TOMBSTONE_RETENTION_S, clock: Callable[[], float] = time.time) -> None:
        self._port = port
        self._path = path or media_jobs.DB_PATH
        self._retention_s = tombstone_retention_s
        self._clock = clock

    def _readable(self, job_id: str, caller: CallerContext) -> dict[str, Any]:
        job = media_jobs.get(job_id, audience="internal", path=self._path)
        if (job is None or not is_run_job(job) or not self._port.can_read(job, caller)
                or media_jobs.tombstoned(job_id, path=self._path)):
            raise JobNotFound(job_id)
        return job

    def get(self, job_id: str, caller: CallerContext) -> RunJobView | None:
        try:
            return job_view(self._readable(job_id, caller), self._path, disposition="replay")
        except JobNotFound:
            return None

    async def wait(self, job_id: str, caller: CallerContext, timeout_s: float) -> RunJobView:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            job = self._readable(job_id, caller)
            if media_jobs.is_terminal(str(job["status"])) or time.monotonic() >= deadline:
                return job_view(job, self._path, disposition="replay")
            await asyncio.sleep(WAIT_POLL_S)

    def cancel(self, job_id: str, caller: CallerContext) -> RunJobView:
        job = self._readable(job_id, caller)
        if not self._port.can_cancel(job, caller):
            raise JobNotFound(job_id)
        if not media_jobs.is_terminal(str(job["status"])):
            try:
                media_jobs.request_cancel(job_id, path=self._path)
            except ValueError:          # finished between the read and the cancel: answer the job as it is
                pass
            current = media_jobs.get(job_id, audience="internal", path=self._path) or job
            if media_jobs.is_terminal(str(current["status"])):
                self._port.drop_payload(job_id)
            job = current
        return job_view(job, self._path, disposition="replay")

    def delete(self, job_id: str, caller: CallerContext) -> None:
        job = self._readable(job_id, caller)
        if not self._port.can_cancel(job, caller):
            raise JobNotFound(job_id)
        if not media_jobs.is_terminal(str(job["status"])):
            with contextlib.suppress(ValueError):       # finished between the read and the cancel: the re-read decides
                media_jobs.request_cancel(job_id, path=self._path)
            job = media_jobs.get(job_id, audience="internal", path=self._path) or job
            if not media_jobs.is_terminal(str(job["status"])):
                raise JobDeleteConflict(job_id)
        media_jobs.tombstone_job(job_id, now=int(self._clock()), path=self._path)
        self._port.drop_payload(job_id)
        media_jobs.reap_tombstones(before=int(self._clock() - self._retention_s), path=self._path)

    def events(self, job_id: str, caller: CallerContext, *, limit: int, after_id: int | None = None,
               after_sequence: int | None = None) -> tuple[JobEvent, ...]:
        self._readable(job_id, caller)
        rows = media_jobs.events(job_id, audience=self._port.audience(caller), limit=limit, after_id=after_id,
                                 after_sequence=after_sequence, path=self._path)
        return tuple(JobEvent(id=int(r.get("id") or r["sequence"]), sequence=int(r["sequence"]), type=str(r["event_type"]),
                              at_s=int(r["created_at"]), payload=dict(r.get("payload") or {})) for r in rows)

    def open_result(self, job_id: str, ref: ResultRef, caller: CallerContext) -> ResultContent:
        self._readable(job_id, caller)
        published = _published_results(job_id, self._path) or ()
        if ref not in published:
            raise ResultUnavailable("not_published")
        if ref.asset_id is not None:
            body = self._port.read_asset(ref.asset_id, caller)
            if body is None:
                raise ResultUnavailable("asset_unavailable")
            return ResultContent(media_type=ref.media_type, body=body)
        read = result_bundle.read_json(job_id, ref.output, self._port.reader(caller), path=self._path)
        if read is None:
            raise ResultUnavailable("not_readable")
        return ResultContent(media_type=read.media_type, value=json.loads(read.body))


# ------------------------------------------------------------------------------------------- pre-deploy check


def queued_jobs_without_v2_envelope(path: Path | None = None) -> list[dict[str, Any]]:
    """Section 5.5, the pre-deploy check: every unfinished job whose envelope names a workflow and is not version 2.
    The deploy refuses to start while this list is not empty. A version 1 job has no incarnation, so dispatch check 2
    compares presence only for it; the list closes that gap by keeping such a job from being queued at the deploy.
    Each item shows ``record_revision`` and ``binding_key`` as None, as a v1 job's view does."""
    found: list[dict[str, Any]] = []
    for job in media_jobs.unfinished(path=path or media_jobs.DB_PATH):
        with media_jobs.connection(path or media_jobs.DB_PATH) as conn:
            try:
                envelope = load_envelope(conn, str(job["job_id"]))
            except DefinitionUnavailable:
                envelope = ENVELOPE_VERSION_UNSUPPORTED
        if envelope is None or (isinstance(envelope, Envelope) and envelope.source_kind != "workflow"):
            continue
        if isinstance(envelope, Envelope) and envelope.envelope_version >= 2:
            continue
        found.append({"job_id": job["job_id"], "workflow": job.get("workflow"), "status": job["status"],
                      "record_revision": None, "binding_key": None})
    return found
