"""What every wire shape shares: reading a request body, staging uploads, choosing the workflow a row serves, and
rendering a refusal. Nothing here names an intent, a workflow, a model or an engine (CODE_TO_DATA_S4_S8 I4, I5)."""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway import client_errors
from gateway.workflow_kernel import selector_matches
from gateway.workflow_runtime_types import (
    MEDIA_PATH_UNSUPPORTED,
    WORKFLOW_NOT_FOUND,
    FrontendRow,
    MediaRef,
    RegistrySnapshot,
    RequestSpool,
    RunRefused,
    WorkflowEntry,
)

CHUNK = 1 << 16
FORM_MAX_FIELDS = 100               # the text parts of one form; a form with more is refused while it is read
FORM_MAX_FILES = 100                # the file parts the parser takes; a part with no input to fill is then named in a 400
FORM_PART_BYTES = 1 << 20           # the most one text part may hold, and all the text parts together
FORM_PART_HEAD_BYTES = 8 << 10      # the boundary and headers around one part
WORKFLOW_AMBIGUOUS = "workflow_ambiguous"


_NOTES: contextvars.ContextVar[list[tuple[str, str, str | None]]] = contextvars.ContextVar("input_notes")


def begin_notes() -> list[tuple[str, str, str | None]]:
    """Start the list of inputs this request's shape accepts, ignores or refuses; the row server writes it to the receipt."""
    notes: list[tuple[str, str, str | None]] = []
    _NOTES.set(notes)
    return notes


_FACTS: contextvars.ContextVar[list[tuple[str, Any, str | None]]] = contextvars.ContextVar("receipt_facts")


def begin_facts() -> list[tuple[str, Any, str | None]]:
    """Start the list of facts this request's shape puts on the receipt (a value to keep, and a warning to raise)."""
    facts: list[tuple[str, Any, str | None]] = []
    _FACTS.set(facts)
    return facts


def fact(name: str, value: Any, warning: str | None = None) -> None:
    """The shape adjusted something the caller asked for: ``name`` holds ``value`` on the receipt, with ``warning`` if any."""
    facts = _FACTS.get(None)
    if facts is not None:
        facts.append((name, value, warning))


def result_headers(names: Mapping[str, str], facts: Mapping[str, Any]) -> dict[str, str]:
    """The headers a row shows of a run's facts: one per fact the run returned, under the name the row gives it."""
    headers: dict[str, str] = {}
    for key, header in names.items():
        value = facts.get(key)
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, bool):
            headers[header] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            headers[header] = "; ".join(str(item) for item in value)
        else:
            headers[header] = str(value)
    return headers


def note(name: str, *, rejected: str | None = None, ignored: str | None = None) -> None:
    """The shape met input ``name``: accepted, or refused (``rejected`` is the reason), or ignored (``ignored``)."""
    notes = _NOTES.get(None)
    if notes is not None:
        notes.append((name, "rejected" if rejected else "ignored" if ignored else "accepted", rejected or ignored))


def error_response(status: int, message: str, *, kind: str = "invalid_request_error", code: str | None = None,
                   headers: Mapping[str, str] | None = None, null_code: bool = False, **extra: Any) -> Response:
    """The OpenAI-shaped error body: ``{"error": {"message", "type", "code"?}}``."""
    body: dict[str, Any] = {"message": message, "type": kind}
    if code:
        body["code"] = code
    elif null_code:             # a wire whose clients saw ``"code": null`` on this refusal
        body["code"] = None
    body.update(extra)
    return JSONResponse({"error": body}, status_code=status, headers=dict(headers or {}))


def refused_response(refused: RunRefused, words: Mapping[str, str] | None = None) -> Response:
    """A ``RunRefused`` in the OpenAI error vocabulary: the status and code of the refusal, the problems by path,
    ``Retry-After`` when the core says when to come back."""
    extra: dict[str, Any] = {}
    if refused.problems:
        extra["problems"] = [problem.as_dict() for problem in refused.problems]
    seconds = client_errors.retry_after(str(refused.retry_after_s))  # None when the core names no time
    headers = {"Retry-After": seconds} if seconds else None
    if words:               # the row's own words for this refusal: its message and type, and no code
        return error_response(refused.http_status, words["message"], kind=words["type"], headers=headers)
    kind = "invalid_request_error" if refused.http_status < 500 else "api_error"
    return error_response(refused.http_status, refused.message, kind=kind, code=refused.code, headers=headers, **extra)


async def json_object(request: Request, *, not_object: str = "The request body must be a JSON object") -> dict[str, Any] | Response:
    """The request body as a JSON object, or a 400 in the shape's vocabulary."""
    try:
        parsed = json.loads(await request.body() or b"")
    except (ValueError, UnicodeDecodeError, RecursionError):  # a body nested past the parser's depth is invalid too
        return error_response(400, "Invalid JSON body")
    if not isinstance(parsed, dict):
        return error_response(400, not_object)
    return parsed


async def _chunks(upload: Any) -> AsyncIterator[bytes]:
    while True:
        block = await upload.read(CHUNK)
        if not block:
            return
        yield block


def _is_upload(value: Any) -> bool:
    return hasattr(value, "filename") and hasattr(value, "read")


def nested_part(name: str) -> bool:
    """A file part addressed below a top-level input name (``a.b``, ``a[0]``, ``a/b``)."""
    return any(mark in name for mark in ".[]/")


@dataclass(frozen=True)
class FormLimits:
    """What one multipart form may hold. The body budget follows from the three: every file part at its largest, every
    text part at its largest, and the framing around each part."""

    file_slots: int                 # the file parts the wire can use; the byte budget has room for this many
    max_file_bytes: int | None      # None: the shape's spool states no per-file size, so only the counts bound the read
    max_fields: int = FORM_MAX_FIELDS
    max_files: int = FORM_MAX_FILES

    @property
    def body_budget(self) -> float:
        """Every file part at its largest, the text parts together, and the framing around every part the parser takes."""
        framing = (self.max_files + self.max_fields) * FORM_PART_HEAD_BYTES
        if self.max_file_bytes is None:
            return float("inf")
        return self.file_slots * self.max_file_bytes + FORM_PART_BYTES + framing


class _FormTooLarge(Exception):
    pass


class _TextBoundedParser(MultiPartParser):
    """The form parser, refusing once the text parts together pass ``FORM_PART_BYTES``. File parts spool to disk and
    are bounded by the body budget; text parts are buffered in memory, so their total is counted on its own, before
    a chunk is added to the part (a file budget never pays for text)."""

    _text_taken = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is None:
            self._text_taken += end - start
            if self._text_taken > FORM_PART_BYTES:
                raise _FormTooLarge
        super().on_part_data(data, start, end)


def _close_spills(parser: MultiPartParser) -> None:
    for handle in parser._files_to_close_on_error:  # starlette private; re-check on a Starlette upgrade (request_forms)
        handle.close()


def _bounded(request: Request, budget: float) -> Request:
    """The same request with a body that raises once more than ``budget`` bytes have been taken from the stream.
    Content-Length is never consulted: a chunked body declares nothing."""
    taken = 0

    async def receive() -> Any:
        nonlocal taken
        message = await request.receive()
        if message["type"] == "http.request":
            taken += len(message.get("body", b""))
            if taken > budget:
                raise _FormTooLarge
        return message

    return Request(request.scope, receive)


async def split_form(request: Request, limits: FormLimits, unreadable: Callable[[], Response] | None = None) -> tuple[dict[str, Any], list[tuple[str, Any]]] | Response:
    """A multipart body as its fields and its file parts (name, upload) in the order received. The body is bounded
    while it is read: its bytes by ``limits.body_budget``, its file and text parts by their counts. A file part whose
    name is addressed below a top-level input is refused (``media_path_unsupported``, 422)."""
    bounded = _bounded(request, limits.body_budget)
    parser = _TextBoundedParser(bounded.headers, bounded.stream(), max_files=limits.max_files,
                                max_fields=limits.max_fields, max_part_size=FORM_PART_BYTES)
    try:
        form = await parser.parse()
    except _FormTooLarge:
        _close_spills(parser)
        return error_response(413, "The request body is larger than this route accepts", code="payload_too_large")
    except (MultiPartException, HTTPException, ValueError):  # the parser's own error is a ValueError
        _close_spills(parser)
        return unreadable() if unreadable is not None else error_response(
            400, "The request body is not valid multipart/form-data", code="invalid_request_body")
    fields: dict[str, Any] = {}
    uploads: list[tuple[str, Any]] = []
    for name, value in form.multi_items():
        if not _is_upload(value):
            fields[name] = value
        elif nested_part(name):
            return error_response(422, f"The file part {name!r} is addressed below a top-level input; send it as a "
                                       "top-level part", code=MEDIA_PATH_UNSUPPORTED, param=name)
        else:
            uploads.append((name, value))
    return fields, uploads


async def stage_uploads(spool: RequestSpool, uploads: Sequence[tuple[str, Any]], file_inputs: Mapping[str, str]
                        ) -> dict[str, tuple[MediaRef, ...]] | Response:
    """Stage each file part in the spool under the input ``file_inputs`` maps its part name to, with the content type
    the caller declared (it reaches the wire as ``MediaRef.wire_media_type``)."""
    media: dict[str, list[MediaRef]] = {}
    for name, upload in uploads:
        target = file_inputs.get(name)
        if target is None:
            return error_response(400, f"Unknown file field {name!r}", param=name)
        staged = await spool.stage(target, _chunks(upload), filename=upload.filename or None,
                                   wire_media_type=upload.content_type or None)
        if isinstance(staged, RunRefused):
            return refused_response(staged)
        media.setdefault(target, []).append(staged)
    return {name: tuple(items) for name, items in media.items()}


def candidates(snapshot: RegistrySnapshot, row: FrontendRow, servable: Callable[[WorkflowEntry], bool] | None = None
               ) -> list[WorkflowEntry]:
    """The active, published workflows the row's selector selects (and ``servable`` accepts), in id order."""
    return sorted((entry for entry in snapshot.workflows.values()
                   if entry.state == "active" and entry.published and selector_matches(row.selects, entry.id, entry.contract)
                   and (servable is None or servable(entry))),
                  key=lambda entry: entry.id)


def required_inputs(entry: WorkflowEntry) -> set[str]:
    return {name for name, spec in (entry.contract.get("inputs") or {}).items()
            if isinstance(spec, Mapping) and spec.get("required") is True}


def choose_workflow(snapshot: RegistrySnapshot, row: FrontendRow, *, target: str | None, supplied: Sequence[str],
                    servable: Callable[[WorkflowEntry], bool] | None = None,
                    not_found: Callable[[str], Response | None] | None = None) -> WorkflowEntry | Response:
    """The workflow this request runs. A named target resolves through the snapshot's name view (a workflow id or
    alias) and must be one the row selects. With no target the row's selection narrows to the workflows whose
    required inputs the request supplies; exactly one must remain."""
    pool = candidates(snapshot, row, servable)
    if target:
        for kind in ("workflow", "workflow_alias"):
            owner = snapshot.names.owner(kind, target)  # type: ignore[arg-type]
            if owner is not None:
                chosen = next((entry for entry in pool if entry.id == owner.id), None)
                if chosen is not None:
                    return chosen
        short = [entry for entry in pool if entry.id == f"{entry.contract.get('intent')}.{target}"]
        if len(short) == 1:        # a workflow is also addressed by its id without the intent that starts it
            return short[0]
        return (not_found(target) if not_found is not None else None) or error_response(
            404, f"The model {target!r} does not exist or is not available on this route", code=WORKFLOW_NOT_FOUND, param="model")
    fitting = [entry for entry in pool if required_inputs(entry) <= set(supplied)]
    if len(fitting) == 1:
        return fitting[0]
    if not fitting and pool:
        missing = sorted(min((required_inputs(entry) - set(supplied) for entry in pool), key=len))
        return error_response(400, "Missing required input: " + ", ".join(missing), code="missing_input", param=missing[0])
    if not fitting:
        return error_response(404, "No workflow on this route accepts these inputs", code=WORKFLOW_NOT_FOUND)
    return error_response(400, "Name a model: more than one workflow on this route accepts these inputs",
                          code=WORKFLOW_AMBIGUOUS, param="model")


def asset_input_names(entry: WorkflowEntry) -> list[str]:
    """Asset inputs of a contract, required ones first, then in declared order."""
    names = [name for name, spec in (entry.contract.get("inputs") or {}).items()
             if isinstance(spec, Mapping) and spec.get("type") == "asset"]
    return sorted(names, key=lambda name: entry_required(entry, name) is not True)


def entry_required(entry: WorkflowEntry, name: str) -> Any:
    return ((entry.contract.get("inputs") or {}).get(name) or {}).get("required")


def declared(entry: WorkflowEntry, name: str) -> bool:
    return isinstance((entry.contract.get("inputs") or {}).get(name), Mapping)


def pop_str(fields: dict[str, Any], key: str) -> str | None:
    value = fields.pop(key, None)
    return value if isinstance(value, str) and value else None


def profile_problems(contract: Mapping[str, Any], *, files: str, outputs: str) -> tuple[str, ...]:
    """Structural reasons a wire cannot carry a contract. ``files`` is ``none`` (the wire carries no file),
    ``one`` (exactly one required asset input; others optional) or ``any``. ``outputs`` is a media-type prefix such as
    ``image/`` that the single output must have; empty means any. A contract of any shape gets an answer, never an
    exception."""
    if not isinstance(contract, Mapping):
        return ("the contract must be a mapping",)
    problems: list[str] = []
    inputs = contract.get("inputs") if isinstance(contract.get("inputs"), Mapping) else {}
    assets = [name for name, spec in inputs.items() if isinstance(spec, Mapping) and spec.get("type") == "asset"]
    required_assets = [name for name in assets if inputs[name].get("required") is True]
    if files == "none" and required_assets:
        problems.append("the wire carries no file, so a required asset input cannot be filled")
    if files == "one" and len(required_assets) != 1:
        problems.append("the wire carries one file: exactly one asset input must be required")
    out = contract.get("outputs") if isinstance(contract.get("outputs"), Mapping) else {}
    if len(out) != 1:
        problems.append("the wire answers with one output")
    elif outputs:
        media = next(iter(out.values())).get("media_type") if isinstance(next(iter(out.values())), Mapping) else None
        if not isinstance(media, str) or not media.startswith(outputs):
            problems.append(f"the output must be {outputs}*")
    return tuple(problems)
