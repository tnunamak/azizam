"""The request spool: where one request's uploads wait until a job adopts them (CODE_TO_DATA_S4_S8 section 7.1).

One ``FileRequestSpool`` belongs to one request. A frontend ``stage``s each top-level file input; ``submit_run`` adopts
the staged files of a NEW job into that job's directory before the job row commits (``JobSpool.adopt``); ``release``
deletes whatever no job adopted, on every path. A refusal, a replay or an error adopts nothing.

The module names no input, media family, workflow or engine. The type of a file is measured by an injected function
(``measure``), so the reviewed type knowledge stays with the caller that supplies it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from pathlib import Path

from gateway.workflow_call import sniff
from gateway.workflow_runtime_types import MEDIA_PATH_UNSUPPORTED, MediaRef, RunRefused, RunRequest

log = logging.getLogger(__name__)

HEAD_BYTES = 4096                       # what the type measurement may read
_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")

# measure(head bytes, caller-declared type) -> the measured media type, or None when the bytes are not a known type.
Measure = Callable[[bytes, "str | None"], "str | None"]


def measured_type(head: bytes, wire_media_type: str | None) -> str | None:
    """The default measurement: the type the bytes show (the sniffer every upload route uses), never the caller's word.
    Bytes of no known type measure as the unidentified type, which only an input that names it accepts. The declared
    type is not read here; the ``MediaRef`` keeps it as ``wire_media_type`` for the wire."""
    return sniff(head, None)[0]


def _safe_key(key: str) -> str:
    if not _KEY.match(key) or key in (".", ".."):
        raise ValueError("unsafe spool key")
    return key


class JobSpool:
    """The directories of adopted files: ``<root>/jobs/<job id>/<key>``. Staged files wait in
    ``<root>/staging/<request>/<key>``; a ``spool_key`` is ``<request>-<key>`` while staged and ``<key>`` once adopted."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "jobs"
        self.staging = Path(root) / "staging"

    def directory(self, job_id: str) -> Path:
        return self.root / _safe_key(job_id)

    def path(self, job_id: str, spool_key: str) -> Path:
        return self.directory(job_id) / _safe_key(spool_key)

    def delete(self, job_id: str) -> None:
        shutil.rmtree(self.directory(job_id), ignore_errors=True)

    def has(self, job_id: str, spool_key: str) -> bool:
        return self.path(job_id, spool_key).is_file()

    def adopt(self, job_id: str, refs: Mapping[str, tuple[MediaRef, ...]]) -> dict[str, tuple[MediaRef, ...]]:
        """Move the staged files of ``refs`` into the job's directory (one filesystem: a rename) and return the refs
        with their adopted keys. Library-asset refs pass through. A file that is not staged raises ``KeyError``; files
        already moved stay in the job directory, which the caller deletes (``delete``) when the job row does not commit."""
        target = self.directory(job_id)
        moved: dict[str, tuple[MediaRef, ...]] = {}
        for name, items in refs.items():
            out: list[MediaRef] = []
            for ref in items:
                if ref.spool_key is None:
                    out.append(ref)
                    continue
                request_part, _, file_part = ref.spool_key.partition("-")
                source = self.staging / _safe_key(request_part) / _safe_key(file_part)
                if not source.is_file():
                    raise KeyError(ref.spool_key)
                target.mkdir(parents=True, exist_ok=True)
                os.replace(source, target / file_part)
                out.append(replace(ref, spool_key=file_part))
            moved[name] = tuple(out)
        return moved


class FileRequestSpool:
    """One request's staging directory. Not shared between requests."""

    def __init__(self, root: Path, *, max_bytes: int, measure: Measure = measured_type,
                 request_id: str | None = None) -> None:
        self._request = request_id or uuid.uuid4().hex
        self._dir = Path(root) / "staging" / self._request
        self._max_bytes = max_bytes
        self._measure = measure
        self._released = False

    @property
    def max_file_bytes(self) -> int:
        return self._max_bytes

    async def stage(self, input_name: str, chunks: AsyncIterator[bytes], *, filename: str | None,
                    wire_media_type: str | None) -> MediaRef | RunRefused:
        """Stream one file in and measure it. Over-size (413) and an unknown type (415) keep no bytes."""
        if self._released:
            raise RuntimeError("the request spool is released")
        key = uuid.uuid4().hex
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / key
        digest, size, head = hashlib.sha256(), 0, b""
        try:
            with open(path, "xb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self._max_bytes:
                        path.unlink(missing_ok=True)
                        return RunRefused(413, "payload_too_large",
                                          f"{input_name}: the file is larger than {self._max_bytes} bytes")
                    if len(head) < HEAD_BYTES:
                        head += chunk[:HEAD_BYTES - len(head)]
                    digest.update(chunk)
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        measured = self._measure(head, wire_media_type)
        if measured is None:
            path.unlink(missing_ok=True)
            return RunRefused(415, "unsupported_media_type", f"{input_name}: the file type is not recognised")
        return MediaRef(digest="sha256:" + digest.hexdigest(), media_type=measured, size=size, filename=filename,
                        wire_media_type=wire_media_type, spool_key=f"{self._request}-{key}")

    def release(self) -> None:
        """Delete every staged file that no job adopted. Safe to call twice."""
        self._released = True
        shutil.rmtree(self._dir, ignore_errors=True)


@contextlib.contextmanager
def request_scope(request, spool: FileRequestSpool):
    """The request-scope hook: the spool is on ``request.state.spool`` for the request and released on every path."""
    request.state.spool = spool
    try:
        yield spool
    finally:
        spool.release()


# ------------------------------------------------------------------------------------------------- the run request

_NESTED = re.compile(r"[.\[\]/]")
_BODY_KEYS = frozenset({"inputs", "binding"})
_CHUNK = 64 * 1024


def _invalid(code: str, message: str, status: int = 422) -> RunRefused:
    return RunRefused(status, code, message)


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


async def _upload_chunks(upload) -> AsyncIterator[bytes]:
    while chunk := await upload.read(_CHUNK):
        yield chunk


async def read_run_request(request, workflow_id: str, *, spool: FileRequestSpool, library_ref,
                           idempotency_key: str | None, wait_s: float) -> RunRequest | RunRefused:
    """The wire request of the generic run route as a ``RunRequest``.

    JSON: ``{"inputs": {...}, "binding": "<key>"?}``. Multipart: a text part ``inputs`` holding that JSON object, a text
    part ``binding``, and one file part per media input (a repeated name is a multi-item input). A media input may be a
    library asset ``{"asset_id": "..."}`` in ``inputs``; ``library_ref(asset_id)`` returns its ``MediaRef`` or None when
    the caller may not read it. A file part addressed below a top-level name is ``media_path_unsupported``.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    media: dict[str, list[MediaRef]] = {}
    if content_type == "multipart/form-data":
        from gateway import request_forms
        form = await request_forms.read_form(request)
        raw_inputs, binding = _text(form.get("inputs")), _text(form.get("binding"))
        for name in form.keys():
            if name in _BODY_KEYS:
                continue
            for part in form.getlist(name):
                if _text(part) is not None:
                    return _invalid("invalid_request", f"{name}: a value part must be named inputs or binding")
                if _NESTED.search(name):
                    return _invalid(MEDIA_PATH_UNSUPPORTED, f"{name}: a file part is named by a top-level input")
                staged = await spool.stage(name, _upload_chunks(part), filename=getattr(part, "filename", None),
                                           wire_media_type=getattr(part, "content_type", None))
                if isinstance(staged, RunRefused):
                    return staged
                media.setdefault(name, []).append(staged)
        try:
            inputs = json.loads(raw_inputs) if raw_inputs else {}
        except (ValueError, RecursionError):  # nested past the parser's depth is not valid JSON either
            return _invalid("invalid_request", "inputs is not valid JSON")
    elif content_type in ("application/json", ""):
        try:
            body = await request.json()
        except (ValueError, RecursionError):
            return _invalid("invalid_request", "the body is not valid JSON")
        if not isinstance(body, Mapping) or set(body) - _BODY_KEYS:
            return _invalid("invalid_request", "the body takes only inputs and binding")
        inputs, binding = body.get("inputs", {}), body.get("binding")
        if binding is not None and not isinstance(binding, str):
            return _invalid("invalid_request", "binding must be a string")
    else:
        return _invalid("unsupported_media_type", "send JSON or multipart/form-data", status=415)
    if not isinstance(inputs, Mapping):
        return _invalid("invalid_request", "inputs must be an object")
    plain: dict = {}
    for name, value in inputs.items():
        if isinstance(value, Mapping) and set(value) == {"asset_id"}:
            ref = library_ref(str(value["asset_id"]))
            if ref is None:
                return RunRefused(404, "asset_not_found", f"{name}: the library asset was not found")
            media.setdefault(str(name), []).append(ref)
        else:
            plain[str(name)] = value
    return RunRequest(workflow_id=workflow_id, inputs=plain, media={n: tuple(r) for n, r in media.items()},
                      binding_key=binding or None, idempotency_key=idempotency_key or None, wait_s=wait_s)
