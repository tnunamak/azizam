"""The in-tree transcription client, behind the adapter interface (``executor: adapter:chunked-stt``).

This reviewed adapter owns the wire of an OpenAI-shaped transcription engine
(the Parakeet, Whisper and Canary servers are what it was written for). It
moves *where the requests leave the gateway*, not what they send: every
request is the httpx call the gateway always made, with the same URL, keyword
arguments and timeouts, and each failure surfaces as the same typed error.

Core (``proxy.py``) keeps the policy: admission, the resource gate and its
lease, receipts, the request deadline, cancellation signalling, the multipart
parse and the cleanup owner. It hands them over in one typed ``SttRun``. The
adapter registers the resources it opens on that cleanup owner, under the names
and in the order core always used, and never closes the owner itself.

Operations (``invoke``); the operation names the route:
- ``stt.transcribe`` (``/v1/audio/transcriptions``) and ``stt.translate``
  (``/v1/audio/translations``).

  With an ``SttRun``: one client for the run; the 16 kHz WAV preparation when
  the engine accepts only that; the chunk plan, the chunk posts and the merge
  (``stt_chunking.execute_chunking``); then, when no chunked answer settled it,
  the relay of the engine's event stream (``run.stream``) or one direct post.
  ``run.on_outcome`` sees the chunking outcome before any later request. The
  answer is a ``TranscriptReply`` or, for a relayed stream, a ``StreamReply``
  whose chunks are read under the run's cancellation, deadline and byte budget.
  httpx timeouts, refused connections and dropped connections become
  ``SttBackendTimeout``, ``SttBackendUnavailable`` and
  ``SttBackendConnectionLost``.

  With an ``SttUpload``: one buffered multipart post of audio core already
  holds in memory. The answer is a ``WireReply``; httpx errors become
  ``TransportFailure`` (the original error is its ``__cause__``).

``health`` is unknown: readiness of these engines belongs to the lifecycle
driver. Inventory, jobs and recovery are not implemented.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Literal, Mapping, Sequence

import httpx

from gateway import stt_chunking
from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION
from gateway.engine_protocol.description import ProtocolDescription
from gateway.engine_protocol.errors import UnknownOperation, UnsupportedByAdapter
from gateway.engine_protocol.httpx_wire import failure, lower_headers, reply_from
from gateway.engine_protocol.types import (
    AssetRef, CanonicalRequest, Connection, Health, Inventory, JobEvent, JobRef, JobStatus, Reply, StreamReply,
)

ADAPTER_ID = "chunked-stt"
ROUTES = {"stt.transcribe": "/v1/audio/transcriptions", "stt.translate": "/v1/audio/translations"}
OPERATIONS = tuple(ROUTES)
_DEFAULT_LABEL = "STT backend"
_NO_JOBS = "A transcription is answered in one request; it has no job."


@dataclass(kw_only=True)
class SttUpload(CanonicalRequest):
    """One buffered transcription of audio core holds in memory.

    ``parts`` are the multipart file parts (httpx ``files=``); ``body`` holds the form fields (httpx ``data=``).
    """

    parts: Mapping[str, tuple[str, bytes, str]]


@dataclass(kw_only=True)
class SttRun(CanonicalRequest):
    """One transcription run. Core decides every value here; the adapter spends it and decides nothing.

    ``headers`` (inherited) are the prepared downstream headers. ``fields`` are the caller's form fields after the
    model swap (they decide whether the run may be chunked); ``multipart_fields`` are the fields the engine is sent.
    ``uploads`` are the parsed, spooled file parts. The cleanup owner, the cancellation waiters, the deadline and the
    hooks are core's: the adapter registers what it opens on ``cleanup`` and reports progress through the hooks.
    """

    execution: stt_chunking.SttExecutionConfig
    fields: Sequence[tuple[str, str]]
    multipart_fields: Mapping[str, str | list[str]]
    uploads: Sequence[tuple[str, Any]]
    workspace: Path
    cleanup: stt_chunking.SttCleanupOwner
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]]
    deadline_at: float
    on_cancel: Callable[[], None]
    on_attempt_started: Callable[[int], None]
    on_outcome: Callable[[stt_chunking.ChunkingOutcome], None]
    on_whole_file_attempt: Callable[[], None] | None = None   # called when the whole upload is sent in one request
    wav16k_input: bool = False      # the engine reads only 16 kHz mono WAV: decode the upload first
    stream: bool = False            # relay the engine's event stream when no chunked answer settled the run


@dataclass(frozen=True)
class TranscriptReply(Reply):
    """A buffered transcription answer.

    ``mode``: ``chunked`` (``body`` is the merged transcript), ``upstream_error`` (a chunk was refused),
    ``stream_refused`` (the engine refused to stream) or ``direct`` (the answer to one direct post). Except for
    ``chunked``, ``status``, ``headers`` and ``raw`` are the engine's, untouched.
    """

    mode: Literal["chunked", "upstream_error", "stream_refused", "direct"] = "direct"


def _transcript(response: stt_chunking.UpstreamResponse, mode: Literal["upstream_error", "stream_refused", "direct"]) -> TranscriptReply:
    return TranscriptReply(response.status_code, response.headers, raw=response.body, mode=mode)


class _BoundedRelay:
    """The engine's event stream, raw chunk by raw chunk, read under the run's cancellation, deadline and byte budget.

    The upstream response is closed by core's cleanup owner (it was registered there), so ``aclose`` has nothing to
    release.
    """

    def __init__(self, label: str, upstream: httpx.Response, run: SttRun) -> None:
        self._label = label
        self._upstream = upstream
        self._run = run
        self._iterator: AsyncIterator[bytes] | None = None
        self._total = 0

    def __aiter__(self) -> "_BoundedRelay":
        return self

    async def __anext__(self) -> bytes:
        run = self._run
        if self._iterator is None:
            self._iterator = self._upstream.aiter_raw().__aiter__()
        chunk = await stt_chunking.await_with_cancellation(
            self._iterator.__anext__(),
            cancellation_waiters=run.cancellation_waiters,
            deadline_at=run.deadline_at,
            on_cancel=run.on_cancel,
        )
        self._total += len(chunk)
        if self._total > run.execution.max_response_bytes:
            raise stt_chunking.SttResponseLimitExceeded(f"{self._label} response exceeds the configured limit")
        return chunk

    async def aclose(self) -> None:
        return None


async def _nothing_to_close() -> None:
    return None


@contextlib.contextmanager
def _backend_errors(label: str) -> Iterator[None]:
    """Map an httpx failure talking to the engine onto the gateway's typed backend errors."""
    try:
        yield
    except httpx.TimeoutException as error:
        raise stt_chunking.SttBackendTimeout(f"{label} timed out") from error
    except httpx.ConnectError as error:
        raise stt_chunking.SttBackendUnavailable(f"{label} is unavailable") from error
    except httpx.TransportError as error:
        # Reset/closed/protocol error after the connection was made: the engine died
        # mid-request (typically a GPU out-of-memory kill). Another engine may serve it.
        raise stt_chunking.SttBackendConnectionLost(f"{label} dropped the connection") from error


class _RunWire:
    """The requests of one run, over the run's one client."""

    def __init__(self, label: str, client: httpx.AsyncClient, url: str, run: SttRun) -> None:
        self._label = label
        self._client = client
        self._url = url
        self._run = run

    async def send_files(self, files: list[tuple[str, tuple[str, Any, str]]]) -> stt_chunking.UpstreamResponse:
        run, client = self._run, self._client
        with _backend_errors(self._label):
            outgoing = client.build_request("POST", self._url, headers=run.headers, data=run.multipart_fields, files=files)
            upstream = await stt_chunking.await_with_cancellation(
                client.send(outgoing, stream=True),
                cancellation_waiters=run.cancellation_waiters, deadline_at=run.deadline_at, on_cancel=run.on_cancel,
            )
            run.cleanup.add("upstream_response", upstream.aclose)
            try:
                body = await stt_chunking.read_bounded_response(
                    upstream, max_bytes=run.execution.max_response_bytes,
                    cancellation_waiters=run.cancellation_waiters, deadline_at=run.deadline_at, on_cancel=run.on_cancel,
                )
            finally:
                await run.cleanup.release("upstream_response", upstream.aclose)
            return stt_chunking.UpstreamResponse(upstream.status_code, body, dict(upstream.headers))

    async def post_file(self, path: Path, filename: str, media_type: str) -> stt_chunking.UpstreamResponse:
        handle = path.open("rb")
        self._run.cleanup.add("chunk_upload_handle", handle.close)
        try:
            return await self.send_files([("file", (filename, handle, media_type))])
        finally:
            await self._run.cleanup.release("chunk_upload_handle", handle.close)

    async def post_uploads(self) -> stt_chunking.UpstreamResponse:
        opened: list[tuple[str, Any, str, str]] = []
        cleanup = self._run.cleanup
        try:
            for index, (name, upload) in enumerate(self._run.uploads):
                handle = stt_chunking.source_path(upload).open("rb")
                opened.append((name, handle, upload.filename or "audio", upload.content_type or "application/octet-stream"))
                cleanup.add(f"direct_upload_handle_{index}", handle.close)
            return await self.send_files([(name, (filename, handle, media_type)) for name, handle, filename, media_type in opened])
        finally:
            for index, (_name, handle, _filename, _media_type) in enumerate(opened):
                await cleanup.release(f"direct_upload_handle_{index}", handle.close)

    async def prepared_wav(self, upload: Any) -> Path:
        """Decode the upload to 16 kHz mono WAV, the only input audio.cpp engines accept."""
        run = self._run
        destination = run.workspace / "normalized-16k.wav"
        run.cleanup.add("normalized_wav", lambda: destination.unlink(missing_ok=True))
        try:
            await stt_chunking.run_media_command(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", os.fspath(stt_chunking.source_path(upload)),
                    "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
                    os.fspath(destination),
                ],
                timeout_seconds=run.execution.timeout_seconds,
                cancellation_waiters=run.cancellation_waiters,
                deadline_at=run.deadline_at,
                on_cancel=run.on_cancel,
                max_output_bytes=run.execution.max_media_output_bytes,
            )
        except stt_chunking.SttMediaCommandFailed as error:
            # ffmpeg ran and refused the file: the caller sent something that is not audio.
            raise stt_chunking.SttInvalidAudio("STT input is not readable audio") from error
        size = destination.stat().st_size if destination.exists() else 0
        if size <= 44:
            raise stt_chunking.SttInvalidAudio("STT input holds no decodable audio")
        if size > run.execution.max_decoded_bytes:
            raise stt_chunking.SttResourceLimitExceeded("Decoded audio exceeds the configured STT limit")
        return destination

    async def open_stream(self, path: Path, filename: str) -> TranscriptReply | StreamReply:
        """Start the engine's event stream; a refusal is read (bounded) and answered buffered."""
        run, client = self._run, self._client
        handle = path.open("rb")
        run.cleanup.add("stream_upload_handle", handle.close)
        outgoing = client.build_request(
            "POST", self._url, headers=run.headers, data=run.multipart_fields,
            files=[("file", (filename, handle, "audio/wav" if run.wav16k_input else "application/octet-stream"))],
        )
        # Before the first event byte nothing has reached the caller, so a lost connection
        # still lets the tier hand the whole upload to its next engine.
        with _backend_errors(self._label):
            upstream = await stt_chunking.await_with_cancellation(
                client.send(outgoing, stream=True),
                cancellation_waiters=run.cancellation_waiters, deadline_at=run.deadline_at, on_cancel=run.on_cancel,
            )
        run.cleanup.add("upstream_response", upstream.aclose)
        if upstream.status_code != 200:
            with _backend_errors(self._label):
                body = await stt_chunking.read_bounded_response(
                    upstream, max_bytes=run.execution.max_response_bytes,
                    cancellation_waiters=run.cancellation_waiters, deadline_at=run.deadline_at, on_cancel=run.on_cancel,
                )
            return _transcript(stt_chunking.UpstreamResponse(upstream.status_code, body, dict(upstream.headers)), "stream_refused")
        return StreamReply(upstream.status_code, lower_headers(upstream.headers), _BoundedRelay(self._label, upstream, run),
                           _nothing_to_close)


class ChunkedSttAdapter:
    interface_version = ADAPTER_INTERFACE_VERSION

    def __init__(self, description: ProtocolDescription | None = None) -> None:
        self.label = (description.label if description is not None else "") or _DEFAULT_LABEL

    async def health(self, conn: Connection) -> Health:
        return Health("unknown", "Readiness of this engine is observed by its lifecycle driver, not by this adapter.")

    async def inventory(self, conn: Connection) -> Inventory:
        raise UnsupportedByAdapter("The transcription adapter lists no inventory.")

    async def invoke(self, conn: Connection, op: str, request: CanonicalRequest) -> Reply | StreamReply:
        route = ROUTES.get(op)
        if route is None:
            raise UnknownOperation(f"The transcription adapter has no operation '{op}'.")
        url = f"{conn.address}{route}"
        timeout = httpx.Timeout(conn.timeouts.first_byte_s, connect=conn.timeouts.connect_s)
        if isinstance(request, SttRun):
            return await self._run(url, timeout, request)
        if isinstance(request, SttUpload):
            return await self._post_once(url, timeout, request)
        raise UnsupportedByAdapter("A transcription takes an SttRun or an SttUpload request.")

    @staticmethod
    async def _post_once(url: str, timeout: httpx.Timeout, request: SttUpload) -> Reply:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, files=request.parts, data=request.body)
        except httpx.HTTPError as error:
            raise failure(error) from error
        return reply_from(resp)

    async def _run(self, url: str, timeout: httpx.Timeout, run: SttRun) -> TranscriptReply | StreamReply:
        client = httpx.AsyncClient(timeout=timeout)
        run.cleanup.add("http_client", client.aclose)
        wire = _RunWire(self.label, client, url, run)
        uploads = run.uploads
        prepared_source: Path | None = None
        if len(uploads) == 1:
            _name, upload = uploads[0]
            if run.wav16k_input:
                prepared_source = await wire.prepared_wav(upload)
            outcome = await stt_chunking.execute_chunking(
                source_path=prepared_source or stt_chunking.source_path(upload),
                source_stem=Path(upload.filename or "audio").stem or "audio",
                fields=run.fields,
                config=run.execution,
                post_file=lambda path, filename: wire.post_file(path, filename, "audio/wav"),
                cancellation_waiters=run.cancellation_waiters,
                deadline_at=run.deadline_at,
                on_cancel=run.on_cancel,
                on_attempt_started=run.on_attempt_started,
                cleanup=run.cleanup,
            )
        else:
            outcome = stt_chunking.ChunkingOutcome("passthrough", 0, "multiple_files")
        run.on_outcome(outcome)
        if outcome.mode == "chunked":
            return TranscriptReply(200, {}, body=outcome.merged_payload, mode="chunked")
        if outcome.mode == "upstream_error" and outcome.upstream_response is not None:
            return _transcript(outcome.upstream_response, "upstream_error")
        if outcome.fallback_reason in {"ineligible_envelope", "multiple_files"}:
            await stt_chunking.guard_uncut_uploads(
                [prepared_source or stt_chunking.source_path(upload) for _name, upload in uploads],
                config=run.execution, cancellation_waiters=run.cancellation_waiters,
                deadline_at=run.deadline_at, on_cancel=run.on_cancel,
            )
        if run.on_whole_file_attempt is not None:
            run.on_whole_file_attempt()
        if run.stream and len(uploads) == 1:
            return await wire.open_stream(
                prepared_source or stt_chunking.source_path(uploads[0][1]),
                "audio.wav" if prepared_source else (uploads[0][1].filename or "audio"),
            )
        if prepared_source is not None:
            direct = await wire.post_file(prepared_source, "audio.wav", "audio/wav")
        else:
            direct = await wire.post_uploads()
        return _transcript(direct, "direct")

    async def submit(self, conn: Connection, op: str, request: CanonicalRequest) -> JobRef:
        raise UnsupportedByAdapter(_NO_JOBS)

    async def poll(self, conn: Connection, ref: JobRef) -> JobStatus:
        raise UnsupportedByAdapter(_NO_JOBS)

    async def events(self, conn: Connection, ref: JobRef) -> AsyncIterator[JobEvent]:
        raise UnsupportedByAdapter(_NO_JOBS)
        yield  # pragma: no cover

    async def cancel(self, conn: Connection, ref: JobRef) -> None:
        raise UnsupportedByAdapter(_NO_JOBS)

    async def recover(self, conn: Connection, ref: JobRef) -> JobStatus | None:
        raise UnsupportedByAdapter(_NO_JOBS)

    async def fetch_assets(self, conn: Connection, ref: JobRef, status: JobStatus) -> tuple[AssetRef, ...]:
        raise UnsupportedByAdapter(_NO_JOBS)
