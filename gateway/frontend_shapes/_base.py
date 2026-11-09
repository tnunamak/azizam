"""The skeleton every wire shape shares: read the wire request (JSON or multipart), choose the workflow the row selects,
fill the inputs, and render what the core or the job service answers. A concrete shape sets a few class attributes and
overrides ``render_success``; the rest is common.

A shape is code because a wire is code: its field names, its status codes, its error vocabulary. It names no workflow,
no model and no engine; the input names it sets come from the chosen workflow's contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway import client_errors, job_retry_hint
from gateway.frontend_shapes import _wire
from gateway.workflow_kernel import selector_matches  # noqa: F401  (re-exported for shape tests)
from gateway.workflow_runtime_types import (
    CallerContext,
    FrontendRow,
    JobNotFound,
    JobService,
    MediaRef,
    RegistrySnapshot,
    RequestSpool,
    ResultRef,
    ResultUnavailable,
    RunFailure,
    RunJobView,
    RunRefused,
    RunRequest,
    WorkflowEntry,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
TERMINAL = ("succeeded", "failed", "canceled")
# How a job store failure category is spelled on the wire.
_STATUS_BY_CATEGORY = {"client": 400, "policy": 403, "capacity": 503, "upstream": 502, "timeout": 504}


@dataclass(frozen=True)
class LegacyRoute:
    """What ``Shape.to_run_request`` returns for a request the data path does not serve yet: the name of the registered
    legacy handler and the body the shape already read (the request stream is spent). The row server answers it by calling
    that handler. Every site that returns one is counted by ``tests/test_legacy_video_ratchet.py``, which may only fall."""

    name: str
    body: Mapping[str, Any]


def coerce(entry: WorkflowEntry, name: str, value: Any) -> Any:
    """A form value (always text) read as the type the contract declares for the input. A value that does not read is
    left as sent, and the core's validation names it."""
    spec = (entry.contract.get("inputs") or {}).get(name)
    if not isinstance(value, str) or not isinstance(spec, Mapping):
        return value
    kind = spec.get("type")
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
    except ValueError:
        return value
    if kind == "boolean" and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def failure_response(failure: RunFailure | None, *, fallback: str, retry_after_s: int | None = None,
                     wire_codes: frozenset[str] | None = None, null_keyed: frozenset[str] = frozenset(),
                     extra: Mapping[str, Any] | None = None) -> Response:
    if failure is None:
        return _wire.error_response(500, fallback, kind="api_error")
    status = failure.upstream_status if failure.category == "upstream" and failure.upstream_status else \
        _STATUS_BY_CATEGORY.get(failure.category, 500)
    response = _wire.error_response(status if status >= 400 else 502, failure.message,
                                    kind="invalid_request_error" if status < 500 else "api_error",
                                    code=failure.code if wire_codes is None or failure.code in wire_codes else None,
                                    null_code=failure.code in null_keyed,
                                    **({"param": None} if failure.code in null_keyed else {}), **(extra or {}))
    if retry_after_s is not None and failure.retryable:
        seconds = client_errors.retry_after(str(retry_after_s))
        if seconds:
            response.headers["Retry-After"] = seconds
    return response


def job_object(view: RunJobView, *, base: str = "/gateway/v1/media/jobs") -> dict[str, Any]:
    """The job as the media-jobs vocabulary shows it, from the view alone: a view holds references, never a result."""
    body: dict[str, Any] = {
        "object": "media.job", "id": view.job_id, "job_id": view.job_id, "status": view.status,
        "workflow_id": view.workflow_id, "created_at": view.created_at_s, "updated_at": view.updated_at_s,
        "poll_url": f"{base}/{view.job_id}", "content_url": f"{base}/{view.job_id}/content", "error": None,
    }
    if view.failure is not None:
        body["error"] = {"code": view.failure.code, "message": view.failure.message}
    return body


class Shape:
    """One reviewed wire. Class attributes say what it carries; ``render_success`` says how it answers."""

    shape_id: ClassVar[str] = ""
    body: ClassVar[str] = "json"                # "json" or "multipart"
    files: ClassVar[str] = "none"               # the file parts the wire carries: "none", "one" or "any"
    output_prefix: ClassVar[str] = ""           # the media-type prefix of the one output it answers with
    transport_fields: ClassVar[frozenset[str]] = frozenset({"model", "response_format", "user"})
    honours_idempotency_key: ClassVar[bool] = True
    timeout_code: ClassVar[str] = "generation_timeout"
    # The failure codes this wire carries. None: every code the core gives. A wire whose clients never saw the core's
    # codes lists the few it did send; any other failure is told by its status and message alone.
    wire_codes: ClassVar[frozenset[str] | None] = None
    # Failure codes whose answer carries ``"code": null`` and ``"param": null`` (the wire's clients saw those keys).
    null_keyed_codes: ClassVar[frozenset[str]] = frozenset()
    # Result facts a failed job's answer repeats as fields of its error (the wire's clients read them there).
    failure_fields: ClassVar[tuple[str, ...]] = ()
    # Whether a workflow this shape refuses is refused at save, when no other row serves it (a single-purpose wire).
    save_gate: ClassVar[bool] = False
    # The answer forms the wire lets a caller ask for, and the wire field that carries the choice. None: no choice.
    answer_forms: ClassVar[frozenset[str]] = frozenset()
    answer_field: ClassVar[str] = ""
    # What a JSON body that is not an object is told, in the words this wire's clients saw.
    not_object_text: ClassVar[str] = "The request body must be a JSON object"

    def __init__(self, *, wait_s: float = 300.0) -> None:
        self.wait_s = wait_s

    def profile(self, contract: Mapping[str, Any]) -> tuple[str, ...]:
        return _wire.profile_problems(contract, files=self.files, outputs=self.output_prefix)

    # -- request ------------------------------------------------------------------------------------------------

    def file_inputs(self, entry: WorkflowEntry, part_names: list[str]) -> Mapping[str, str] | Response:
        """Which input each file part fills. A part named for a declared asset input fills it; the first part of a
        one-file wire fills the required asset input; any other part fills the next asset input not yet used."""
        assets = _wire.asset_input_names(entry)
        taken: dict[str, str] = {}
        for part in part_names:
            if part in assets and part not in taken.values():
                taken[part] = part
        free = [name for name in assets if name not in taken.values()]
        for part in part_names:
            if part not in taken:
                if not free:
                    return _wire.error_response(400, f"The file field {part!r} has no input to fill", param=part)
                taken[part] = free.pop(0)
        return taken

    def form_limits(self, row: FrontendRow, snapshot: RegistrySnapshot, spool: RequestSpool) -> _wire.FormLimits:
        """The most one form may hold: as many file parts as the wire carries (one, or as many as the most asset inputs
        a workflow the row selects declares), each as large as the spool accepts."""
        # Every asset input a selected workflow declares can arrive as a file part (a required source and its optional
        # mask or references): the body budget counts them all. A one-file wire still has room for its one file.
        file_slots = max((len(_wire.asset_input_names(entry)) for entry in _wire.candidates(snapshot, row)), default=0)
        if self.files == "one":
            file_slots = max(1, file_slots)
        return _wire.FormLimits(file_slots=file_slots, max_file_bytes=getattr(spool, "max_file_bytes", None))

    async def to_run_request(self, request: Request, row: FrontendRow, snapshot: RegistrySnapshot, *,
                             spool: RequestSpool) -> RunRequest | Response:
        uploads: list[tuple[str, Any]] = []
        request.state.input_notes = _wire.begin_notes()
        request.state.receipt_facts = _wire.begin_facts()
        if self.body == "multipart":
            read = await _wire.split_form(request, self.form_limits(row, snapshot, spool), self.unreadable_form)
            if isinstance(read, Response):
                return read
            fields, uploads = read
        else:
            read = await _wire.json_object(request, not_object=self.not_object_text)
            if isinstance(read, Response):
                return read
            fields = read
        read_wire = self.read_wire(fields, [name for name, _ in uploads])
        if isinstance(read_wire, Response):
            return read_wire
        options, answer = read_wire
        if row.default_answer and not answer.get(self.answer_field):
            answer = {**answer, self.answer_field: row.default_answer}
        request.state.answer_form = answer
        refused = self.precheck(fields, uploads)
        if refused is not None:
            return refused
        named = _wire.pop_str(fields, "model")
        # A workflow the row's service has no binding for (another service runs it) is no workflow of this row.
        backend = next((route.runtime_policy["backend"] for route in row.routes if route.runtime_policy.get("backend")), None)
        servable = lambda entry: not self.profile(entry.contract) and (backend is None or backend in entry.bindings)  # noqa: E731
        target = self.refine_target(named or self.default_target(), named=bool(named), uploads=uploads,
                                    exists=lambda name: not isinstance(
                                        _wire.choose_workflow(snapshot, row, target=name, supplied=[], servable=servable), Response))
        pool = _wire.candidates(snapshot, row, servable)
        supplied = [*fields, *(name for entry in pool for name in _wire.asset_input_names(entry) if uploads)]
        chosen = _wire.choose_workflow(snapshot, row, target=target, supplied=supplied, servable=servable,
                                       not_found=self.no_such_model)
        if isinstance(chosen, Response):
            return chosen
        refused = self.postcheck(chosen, fields, uploads)
        if refused is not None:
            return refused
        prepared = await self.prepare_uploads(chosen, uploads, request)
        if isinstance(prepared, Response):
            return prepared
        uploads = prepared
        mapping = self.file_inputs(chosen, [name for name, _ in uploads])
        if isinstance(mapping, Response):
            return mapping
        media = await _wire.stage_uploads(spool, uploads, mapping)
        if isinstance(media, Response):
            return media
        inputs = {name: coerce(chosen, name, value) for name, value in fields.items() if name not in self.transport_fields}
        inputs = self.fill_inputs(chosen, inputs, fields)
        key = request.headers.get(IDEMPOTENCY_HEADER) if self.honours_idempotency_key else None
        return RunRequest(workflow_id=chosen.id, inputs=inputs, media=media, options=options, idempotency_key=key or None,
                          frontend_id=row.id, failure_text=row.job_failure_text, retries=row.job_retries)

    def read_wire(self, fields: dict[str, Any], upload_names: Sequence[str] = ()) -> tuple[dict[str, Any], dict[str, Any]] | Response:
        """Hook: the options this request asks of the run (they are part of its identity and reach the adapter) and the
        answer form it asks for (how this request wants the result shown; it never enters the job). A bad value is a
        shaped refusal. The fields are read, not removed: ``transport_fields`` keeps them out of the inputs."""
        return {}, {}

    def unreadable_form(self) -> Response:
        """Hook: what a multipart body that does not parse is told."""
        return _wire.error_response(400, "The request body is not valid multipart/form-data", code="invalid_request_body")

    def precheck(self, fields: Mapping[str, Any], uploads: Sequence[tuple[str, Any]]) -> Response | None:
        """Hook: refusals the wire made before it looked up any workflow."""
        return None

    def refine_target(self, target: str | None, *, named: bool, uploads: Sequence[tuple[str, Any]],
                      exists: Callable[[str], bool]) -> str | None:
        """Hook: the workflow name a request runs, from the name it sent (or the default) and the files it carries."""
        return target

    def no_such_model(self, target: str) -> Response | None:
        """Hook: how the wire tells that a name selects nothing (None: the common answer)."""
        return None

    def postcheck(self, entry: WorkflowEntry, fields: Mapping[str, Any], uploads: Sequence[tuple[str, Any]]) -> Response | None:
        """Hook: refusals the wire made once the workflow is known."""
        return None

    async def prepare_uploads(self, entry: WorkflowEntry, uploads: list[tuple[str, Any]], request: Request
                              ) -> list[tuple[str, Any]] | Response:
        """Hook: the file parts the run is given, once the workflow is known (a wire may fold one part into another)."""
        return uploads

    def fill_inputs(self, entry: WorkflowEntry, inputs: dict[str, Any], wire: Mapping[str, Any]) -> dict[str, Any]:
        """Hook: a shape sets inputs the wire spells differently (``size`` as width and height)."""
        return inputs

    async def legacy_route(self, request: Any, row: FrontendRow, snapshot: RegistrySnapshot) -> "LegacyRoute | None":
        """Hook: a request the old pipeline serves, asked before the row enforces the route policy or opens a receipt (the old
        handler owns both). None: the data path serves it."""
        return None

    async def after_submit(self, request: Any, view: RunJobView, caller: CallerContext) -> None:
        """Hook: what the wire does once a job was submitted (``view.disposition`` tells a new job from a replay)."""
        return None

    def default_target(self) -> str | None:
        """The workflow a request that names no model runs: the deployment's default for this wire, or None."""
        return None

    # -- response -----------------------------------------------------------------------------------------------

    async def to_response(self, outcome: RunJobView | RunRefused, row: FrontendRow, *, jobs: JobService,
                          caller: CallerContext, request: Any = None) -> Response:
        if isinstance(outcome, RunRefused):
            return _wire.refused_response(outcome, row.refusal_bodies.get(outcome.code))
        if row.mode == "job":
            return self.render_job(outcome)
        try:
            view = outcome if outcome.status in TERMINAL else await jobs.wait(outcome.job_id, caller, self.wait_s)
        except JobNotFound:
            return _wire.error_response(404, "Job not found", code="job_not_found")
        if view.status == "failed":
            return failure_response(view.failure, fallback="The job failed",
                                    retry_after_s=self.retry_after(view, jobs, caller), wire_codes=self.wire_codes,
                                    null_keyed=self.null_keyed_codes,
                                    extra={name: view.result_metadata[name] for name in self.failure_fields if name in view.result_metadata})
        if view.status == "canceled":
            return _wire.error_response(409, "The job was canceled", code="job_canceled")
        if view.status != "succeeded":
            return _wire.error_response(504, "The job did not finish in time", kind="api_error", code=self.timeout_code)
        try:
            answer = {**(getattr(getattr(request, "state", None), "answer_form", None) or {}), "request": request}
            response = await self.render_success(view, jobs, caller, answer)
            response.headers.update(_wire.result_headers(row.result_headers, {**view.result_metadata, **answer}))
            return response
        except (JobNotFound, ResultUnavailable):
            return _wire.error_response(404, "The result is not available", code="result_unavailable")

    @staticmethod
    def retry_after(view: RunJobView, jobs: JobService, caller: CallerContext) -> int | None:
        """The delay a busy resource asked for, from the job's events; None when the job holds none."""
        if view.failure is None or not view.failure.retryable:
            return None
        try:
            return job_retry_hint.read(jobs.events(view.job_id, caller, limit=200))
        except JobNotFound:
            return None

    def render_job(self, view: RunJobView) -> Response:
        status = 200 if view.status in TERMINAL else 202
        return JSONResponse(job_object(view), status_code=status, headers={"Location": f"/gateway/v1/media/jobs/{view.job_id}"})

    async def render_success(self, view: RunJobView, jobs: JobService, caller: CallerContext,
                             answer: Mapping[str, Any] | None = None) -> Response:
        raise NotImplementedError

    def render_content(self, view: RunJobView, jobs: JobService, caller: CallerContext, request: Any) -> Response | None:
        """Hook: how a finished job of this wire is read at the jobs route; None: as the file it holds."""
        return None

    @staticmethod
    def read(view: RunJobView, ref: ResultRef, jobs: JobService, caller: CallerContext) -> bytes:
        content = jobs.open_result(view.job_id, ref, caller)
        return content.body or b""

    @staticmethod
    def media_refs(view: RunJobView) -> tuple[ResultRef, ...]:
        return tuple(ref for ref in (view.results or ()) if ref.media_type != "application/json")
