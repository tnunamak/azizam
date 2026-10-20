"""Bounded, engine-agnostic execution primitives for long STT uploads.

This module deliberately knows only about multipart audio and the stable OpenAI
transcription shape.  Backend choice, authorization, route policy, and receipt
ownership remain at the gateway edge.  The small interface here gives that edge
an immutable execution configuration, a disk-backed multipart parser, and a
safe chunk execution result without exposing media or transcript contents to
observability.
"""

from __future__ import annotations

import asyncio
import functools
import json
import math
import os
import shutil
import tempfile
import time
from array import array
from collections.abc import Awaitable, Callable, Mapping, Sequence
from itertools import accumulate
from operator import mul
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeVar

import httpx
import yaml
from openai.types.audio.transcription import (
    Transcription,
    UsageDuration,
    UsageTokens,
    UsageTokensInputTokenDetails,
)
from starlette.datastructures import FormData, Headers, UploadFile
from starlette.formparsers import (
    MultiPartException,
    MultiPartParser,
    MultipartPart,
    _user_safe_decode,
    parse_options_header,
)
from starlette.requests import ClientDisconnect

from gateway import private_tmp
from gateway.alias_policy import transform_specs
from gateway.resource_claims import (
    AdmissionDecision,
    AdmissionRequest,
    CapacitySnapshot,
    QueueClass,
    ResourceClaim,
    evaluate_admission,
)
from gateway.resource_decisions import CapacityFact
from gateway.transforms import run_response_pipeline


_T = TypeVar("_T")
# Cuts are chosen on 20 ms frames of the decoded 16 kHz mono signal, one frame every 10 ms.
_PLANNING_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 320
_FRAME_HOP_SAMPLES = 160
# The last segment may run a quarter target past the target, so a file never ends in a sliver.
_TAIL_FRACTION_OF_TARGET = 0.25
_MIN_SEGMENT_SECONDS = 0.5
# Boundary line, part headers and CRLFs around one multipart part, with room for long filenames.
_PART_FRAMING_BYTES = 8192
_CHUNKING_DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "registry" / "stt_chunking_defaults.yml"
_HOP_BY_HOP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "content-type",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_DECODED_RESPONSE_REPRESENTATION_HEADERS = frozenset(
    {
        "accept-ranges",
        "content-encoding",
        "content-length",
        "content-md5",
        "content-range",
        "digest",
        "etag",
        "vary",
    }
)

# Deliberately read from the installed SDK type instead of duplicating its
# top-level contract in a hand-maintained list.  ``logprobs`` is recognized but
# not safely mergeable without response-specific timestamp semantics.
_SDK_TRANSCRIPTION_FIELDS = frozenset(Transcription.__annotations__)
_MERGEABLE_TRANSCRIPTION_FIELDS = frozenset({"text", "usage"})
_SDK_DURATION_USAGE_FIELDS = frozenset(UsageDuration.__annotations__)
_SDK_TOKEN_USAGE_FIELDS = frozenset(UsageTokens.__annotations__)
_SDK_TOKEN_DETAIL_FIELDS = frozenset(UsageTokensInputTokenDetails.__annotations__)


class SttExecutionError(RuntimeError):
    """Base class for safe STT execution failures."""


class SttConfigurationError(SttExecutionError):
    """The effective service record does not define a safe execution budget."""


class SttCancelled(SttExecutionError):
    """A client disconnect or correlated request cancellation won a race."""


class SttDeadlineExceeded(SttExecutionError):
    """The immutable outer STT deadline elapsed."""


class SttResourceLimitExceeded(SttExecutionError):
    """Storage, upload, decode, segment, or concurrency budget was exceeded."""

    def __init__(self, message: str, *, decision: AdmissionDecision | None = None) -> None:
        super().__init__(message)
        self.decision = decision


class SttChunkResponseError(SttExecutionError):
    """A started chunk did not return the safely mergeable response shape."""


class SttChunkAttemptError(SttExecutionError):
    """A downstream chunk attempt failed after its body started sending."""


class SttSetupFailure(SttExecutionError):
    """Media setup failed before a downstream chunk attempt started."""


class SttInvalidAudio(SttExecutionError):
    """The uploaded file is not audio the decoder can read (the caller's input, not a server fault)."""


class SttAudioNotSplittable(SttExecutionError):
    """A file cannot be verified or cut to fit the engine's hard maximum, so the gateway refuses it.

    ``reason`` is one of a fixed set (never text from a tool or an engine): ``unknown_duration`` when the probe
    could not establish a length, ``split_failed`` when
    a local step failed, or ``unsupported_request`` when cutting would change the response contract.
    """

    def __init__(self, reason: Literal["unknown_duration", "split_failed", "unsupported_request"], *, limit_seconds: float) -> None:
        super().__init__(reason)
        self.reason = reason
        self.limit_seconds = limit_seconds


class SttMediaCommandFailed(SttSetupFailure):
    """A local media utility ran and exited non-zero: its input could not be read."""


class SttBackendTimeout(SttExecutionError):
    """The selected backend did not complete one bounded request in time."""


class SttBackendUnavailable(SttExecutionError):
    """The selected backend could not accept one direct STT request."""


class SttBackendConnectionLost(SttBackendUnavailable):
    """The backend accepted the request, then dropped the connection (its process died mid-request)."""


class SttResponseLimitExceeded(SttExecutionError):
    """A backend or local media utility exceeded its configured output bound."""


class SttCleanupFailure(SttExecutionError):
    """A mandatory, already-attempted cleanup action failed."""


@functools.lru_cache(maxsize=4)
def _read_chunking_defaults(path: Path, modified_ns: int) -> Mapping[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = document.get("defaults")
    if not isinstance(defaults, Mapping):
        raise SttConfigurationError("The bundled STT chunking defaults are missing")
    return MappingProxyType(dict(defaults))


def bundled_chunking_defaults(path: Path = _CHUNKING_DEFAULTS_PATH) -> Mapping[str, Any]:
    """The long-audio values a record falls back to, from the bundled data file ``registry/stt_chunking_defaults.yml``."""
    return _read_chunking_defaults(path, path.stat().st_mtime_ns)


@dataclass(frozen=True)
class SttExecutionConfig:
    """Validated, immutable execution budget captured from one service record."""

    service_id: str
    host: str
    timeout_seconds: float
    deadline_seconds: float
    chunking_enabled: bool
    threshold_seconds: float
    target_seconds: float
    search_seconds: float
    max_segment_seconds: float
    max_segment_declared: bool
    native_long_audio_offline: bool
    native_long_audio_streaming: bool
    max_input_bytes: int
    max_decoded_bytes: int
    max_duration_seconds: float
    max_sample_rate: int
    max_channels: int
    max_segments: int
    max_files: int
    max_fields: int
    max_field_bytes: int
    max_response_bytes: int
    max_media_output_bytes: int
    spool_dir: Path
    min_free_bytes: int
    global_concurrency: int
    setup_failure_policy: Literal["passthrough", "error"]
    default_model: str
    backend_config: Mapping[str, Any]

    @classmethod
    def from_service(
        cls,
        *,
        service_id: str,
        service_config: Mapping[str, Any],
        config_directory: Path,
    ) -> "SttExecutionConfig":
        # ``config_directory`` is kept for callers: a relative spool_dir no longer resolves against it.
        raw = dict(service_config)
        chunking = raw.get("chunking") or {}
        if not isinstance(chunking, Mapping):
            raise SttConfigurationError("STT chunking configuration must be an object")

        def positive_int(name: str, default: int) -> int:
            value = chunking.get(name, default)
            try:
                result = int(value)
            except (TypeError, ValueError) as error:
                raise SttConfigurationError(f"STT {name} must be an integer") from error
            if result <= 0:
                raise SttConfigurationError(f"STT {name} must be positive")
            return result

        def positive_float(name: str, default: Any, *, allow_zero: bool = False) -> float:
            value = chunking.get(name, default)
            try:
                result = float(value)
            except (TypeError, ValueError) as error:
                raise SttConfigurationError(f"STT {name} must be a number") from error
            if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
                qualification = "non-negative" if allow_zero else "positive"
                raise SttConfigurationError(f"STT {name} must be finite and {qualification}")
            return result

        host = str(raw.get("host") or "127.0.0.1").strip()
        try:
            port = int(raw.get("port", 5092))
        except (TypeError, ValueError) as error:
            raise SttConfigurationError("STT port must be an integer") from error
        if not host or not 0 < port < 65536:
            raise SttConfigurationError("STT service host and port are invalid")

        fallback = bundled_chunking_defaults()
        threshold = positive_float("threshold_seconds", fallback["threshold_seconds"])
        target = positive_float("target_seconds", fallback["target_seconds"])
        if target >= threshold:
            raise SttConfigurationError("STT target_seconds must be less than threshold_seconds")
        search = min(positive_float("search_seconds", fallback["search_seconds"], allow_zero=True), target / 2)
        # A declared maximum is the only value that authorizes over-limit refusal. The planner still uses the
        # bundled derived ceiling to choose bounded pieces when a record has no measured maximum.
        max_segment_declared = chunking.get("max_segment_seconds") is not None
        max_segment_fallback = fallback.get("max_segment_seconds")
        derived = target + 2.0 * search
        if max_segment_declared:
            max_segment = positive_float("max_segment_seconds", derived)
        elif max_segment_fallback in (None, "derive"):
            max_segment = derived
        else:
            try:
                max_segment = float(max_segment_fallback)
            except (TypeError, ValueError) as error:
                raise SttConfigurationError("STT max_segment_seconds must be a number") from error
        if not math.isfinite(max_segment) or max_segment <= 0:
            raise SttConfigurationError("STT max_segment_seconds must be finite and positive")
        if max_segment < target:
            raise SttConfigurationError("STT max_segment_seconds must be at least target_seconds")

        policy = str(chunking.get("setup_failure_policy", "passthrough")).strip().lower()
        if policy not in {"passthrough", "error"}:
            raise SttConfigurationError("STT setup_failure_policy must be passthrough or error")

        spool_raw = str(chunking.get("spool_dir", "stt")).strip()
        if not spool_raw:
            raise SttConfigurationError("STT spool_dir must be configured")
        spool_dir = Path(spool_raw)
        # A relative name lives in the gateway's private temporary directory, which is emptied at start and holds
        # every other temporary file of the process (docs/design/REQUEST_RETENTION.md 5.1). An absolute path is the
        # operator's explicit choice and sits outside that guarantee; ``spool_is_private`` says which one applies.
        if not spool_dir.is_absolute():
            spool_dir = private_tmp.current() / spool_dir
        spool_dir = spool_dir.resolve()
        # This protects the known RAM-backed temporary root.  It does not try
        # to infer every platform's filesystem topology; operators explicitly
        # declare the durable spool root in data.
        if spool_dir == Path("/tmp") or Path("/tmp") in spool_dir.parents:
            raise SttConfigurationError("STT spool_dir must not be under /tmp")

        timeout = positive_float("backend_timeout_seconds", float(raw.get("timeout", 120)))
        deadline = positive_float("deadline_seconds", max(timeout, 300.0))
        if timeout > deadline:
            raise SttConfigurationError("STT backend_timeout_seconds cannot exceed deadline_seconds")

        return cls(
            service_id=service_id,
            host=f"http://{host}:{port}",
            timeout_seconds=timeout,
            deadline_seconds=deadline,
            chunking_enabled=_as_bool(chunking.get("enabled", True)),
            threshold_seconds=threshold,
            target_seconds=target,
            search_seconds=search,
            max_segment_seconds=max_segment,
            max_segment_declared=max_segment_declared,
            native_long_audio_offline=_as_bool(
                chunking.get("native_long_audio_offline", False)
            ),
            native_long_audio_streaming=_as_bool(
                chunking.get("native_long_audio_streaming", False)
            ),
            max_input_bytes=positive_int("max_input_bytes", 536_870_912),
            max_decoded_bytes=positive_int("max_decoded_bytes", 1_073_741_824),
            max_duration_seconds=positive_float("max_duration_seconds", 14_400.0),
            max_sample_rate=positive_int("max_sample_rate", 48_000),
            max_channels=positive_int("max_channels", 2),
            max_segments=positive_int("max_segments", 16),
            max_files=positive_int("max_files", 8),
            max_fields=positive_int("max_fields", 100),
            max_field_bytes=positive_int("max_field_bytes", 65_536),
            max_response_bytes=positive_int("max_response_bytes", 16 * 1024 * 1024),
            max_media_output_bytes=positive_int("max_media_output_bytes", 8 * 1024 * 1024),
            spool_dir=spool_dir,
            min_free_bytes=positive_int("min_free_bytes", 67_108_864),
            global_concurrency=positive_int("global_concurrency", 2),
            setup_failure_policy=policy,
            default_model=str(raw.get("default_model") or "").strip(),
            backend_config=_freeze_mapping(raw),
        )

    @property
    def spool_is_private(self) -> bool:
        """True when this service's workspaces sit in the gateway's private temporary directory."""
        return private_tmp.inside(self.spool_dir)

    def request_claim(self) -> ResourceClaim:
        """Represent the bounded disk reservation with the shared claim type."""
        return ResourceClaim(
            disk_mb=math.ceil((self.max_input_bytes + self.max_decoded_bytes) / (1024 * 1024)),
            queue_class=QueueClass.INTERACTIVE,
            reasons=("bounded STT upload and decoded media workspace",),
        )

    def chunking_view(self) -> dict[str, Any]:
        """What the owner is shown about long-audio cutting: the numbers in force and which come from the bundled defaults."""
        saved = _thaw(self.backend_config.get("chunking")) or {}
        from_defaults = [
            key for key in ("threshold_seconds", "target_seconds", "search_seconds", "max_segment_seconds")
            if saved.get(key) is None
        ]
        return {
            "enabled": self.chunking_enabled,
            "cut_files_longer_than_seconds": self.chunk_above_seconds,
            "target_seconds": self.target_seconds,
            "search_seconds": self.search_seconds,
            "max_segment_seconds": self.max_segment_seconds,
            "native_long_audio_offline": self.native_long_audio_offline,
            "native_long_audio_streaming": self.native_long_audio_streaming,
            "from_bundled_defaults": from_defaults,
            "note": (
                "Values listed in from_bundled_defaults are not saved on this engine; they come from the bundled "
                "defaults (registry/stt_chunking_defaults.yml). Save them on the engine to fix them."
                if from_defaults else "All values are saved on this engine."
            ),
        }

    @property
    def chunk_above_seconds(self) -> float:
        """Files longer than this are cut when the request shape can be preserved."""
        return min(self.threshold_seconds, self.max_segment_seconds) if self.max_segment_declared else self.threshold_seconds

    @property
    def replay_limit_bytes(self) -> int:
        """Largest raw multipart body a fallback replay may keep: audio plus per-part framing."""
        parts = self.max_files + self.max_fields
        return self.max_input_bytes + self.max_fields * self.max_field_bytes + parts * _PART_FRAMING_BYTES

    def replay_claim(self) -> ResourceClaim:
        """Disk only: the replay copy is held beside, not instead of, an attempt's own claim."""
        return ResourceClaim(
            disk_mb=math.ceil(self.replay_limit_bytes / (1024 * 1024)),
            queue_class=QueueClass.INTERACTIVE,
            reasons=("bounded STT replay copy of the request body",),
        )


@dataclass(frozen=True)
class ProbedAudio:
    duration_seconds: float
    sample_rate: int
    channels: int

    @property
    def decoded_bytes(self) -> int:
        return math.ceil(self.duration_seconds * self.sample_rate * self.channels * 2)


@dataclass(frozen=True)
class UpstreamResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]


@dataclass(frozen=True)
class ChunkingOutcome:
    """A result that never contains more observability than the final response."""

    mode: Literal["passthrough", "chunked", "upstream_error"]
    attempts_started: int
    fallback_reason: str | None = None
    merged_payload: Mapping[str, Any] | None = None
    upstream_response: UpstreamResponse | None = None


@dataclass
class ParsedMultipart:
    form: FormData
    parser: "DiskBoundedMultiPartParser"
    workspace: Path

    async def close_form(self) -> None:
        await self.form.close()

    async def aclose(self) -> None:
        """Convenience cleanup for non-route callers; attempt each release."""
        failures: list[Exception] = []
        for action in (self.close_form, self.parser.remove_spool_files, lambda: remove_request_workspace(self.workspace)):
            try:
                result = action()
                if result is not None:
                    await result
            except Exception as error:
                failures.append(error)
        if failures:
            raise failures[0]


class DiskBoundedMultiPartParser(MultiPartParser):
    """Starlette multipart parser that writes file parts directly to a known disk root.

    Starlette's stock parser uses ``SpooledTemporaryFile`` and therefore picks
    the process temporary directory.  This subclass keeps its parsing behavior
    but replaces only the file allocation decision: file content is bounded
    before it can be materialized and each part is a named file in the
    operator-configured workspace.
    """

    def __init__(
        self,
        headers: Headers,
        stream: Any,
        *,
        config: SttExecutionConfig,
        workspace: Path,
    ) -> None:
        super().__init__(
            headers,
            stream,
            max_files=config.max_files,
            max_fields=config.max_fields,
            max_part_size=config.max_field_bytes,
        )
        self._config = config
        self._workspace = workspace
        self._uploaded_bytes = 0
        self.max_parsed_field_bytes = 0
        self._spool_paths: set[Path] = set()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        message_bytes = data[start:end]
        if self._current_part.file is not None:
            self._uploaded_bytes += len(message_bytes)
            if self._uploaded_bytes > self._config.max_input_bytes:
                raise MultiPartException("Uploaded audio exceeds the configured STT limit.")
        super().on_part_data(data, start, end)
        if self._current_part.file is None:
            self.max_parsed_field_bytes = max(self.max_parsed_field_bytes, len(self._current_part.data))

    def on_headers_finished(self) -> None:
        disposition, options = parse_options_header(self._current_part.content_disposition)
        del disposition
        try:
            self._current_part.field_name = _user_safe_decode(options[b"name"], self._charset)
        except KeyError as error:
            raise MultiPartException('The Content-Disposition header field "name" must be provided.') from error
        if b"filename" not in options:
            self._current_fields += 1
            if self._current_fields > self.max_fields:
                raise MultiPartException(f"Too many fields. Maximum number of fields is {self.max_fields}.")
            self._current_part.file = None
            return

        self._current_files += 1
        if self._current_files > self.max_files:
            raise MultiPartException(f"Too many files. Maximum number of files is {self.max_files}.")
        filename = _user_safe_decode(options[b"filename"], self._charset)
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix="upload-",
                suffix=".part",
                dir=self._workspace,
                delete=False,
            )
        except OSError as error:
            raise MultiPartException("Unable to allocate configured STT spool storage.") from error
        path = Path(handle.name)
        self._spool_paths.add(path)
        self._files_to_close_on_error.append(handle)
        self._current_part.file = UploadFile(
            file=handle,
            size=0,
            filename=filename,
            headers=Headers(raw=self._current_part.item_headers),
        )

    def remove_spool_files(self) -> None:
        failures: list[OSError] = []
        for handle in self._files_to_close_on_error:
            try:
                handle.close()
            except OSError as error:
                failures.append(error)
        for path in self._spool_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                failures.append(error)
        self._spool_paths.clear()
        if failures:
            raise failures[0]


async def parse_bounded_multipart(
    request: Any,
    config: SttExecutionConfig,
    *,
    workspace: Path | None = None,
    cleanup: "SttCleanupOwner | None" = None,
    deadline_at: float | None = None,
) -> ParsedMultipart:
    """Parse the request body directly to bounded disk files, never bytes buffers."""
    try:
        config.spool_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SttResourceLimitExceeded("Configured STT spool storage is unavailable") from error
    owns_workspace = workspace is None
    active_workspace = workspace or create_request_workspace(config)
    parser = DiskBoundedMultiPartParser(
        request.headers,
        request.stream(),
        config=config,
        workspace=active_workspace,
    )
    if cleanup is not None:
        cleanup.add("multipart_parser_spool_files", parser.remove_spool_files)
    try:
        form = await await_with_cancellation(parser.parse(), deadline_at=deadline_at)
    except ClientDisconnect as error:
        if cleanup is None:
            _remove_spool_files_after_parse_failure(parser)
        if owns_workspace:
            _remove_workspace_after_parse_failure(active_workspace)
        raise SttCancelled("STT upload cancelled by client disconnect") from error
    except asyncio.CancelledError:
        if cleanup is None:
            _remove_spool_files_after_parse_failure(parser)
        if owns_workspace:
            _remove_workspace_after_parse_failure(active_workspace)
        raise
    except Exception:
        if cleanup is None:
            _remove_spool_files_after_parse_failure(parser)
        if owns_workspace:
            _remove_workspace_after_parse_failure(active_workspace)
        raise
    return ParsedMultipart(form=form, parser=parser, workspace=active_workspace)


def create_request_workspace(config: SttExecutionConfig) -> Path:
    """Allocate one private disk-backed directory beneath the configured spool root."""
    try:
        config.spool_dir.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="request-", dir=config.spool_dir))
    except OSError as error:
        raise SttResourceLimitExceeded("Configured STT spool storage is unavailable") from error


def remove_request_workspace(workspace: Path) -> None:
    """Remove a request workspace after all independently owned children released."""
    shutil.rmtree(workspace)


def _remove_spool_files_after_parse_failure(parser: DiskBoundedMultiPartParser) -> None:
    try:
        parser.remove_spool_files()
    except OSError as error:
        _log_cleanup_failure("multipart_spool", error)


def _remove_workspace_after_parse_failure(workspace: Path) -> None:
    try:
        remove_request_workspace(workspace)
    except OSError as error:
        _log_cleanup_failure("multipart_workspace", error)


class SttResourceLease:
    def __init__(self, gate: "SttResourceGate", reserved_bytes: int, decision: AdmissionDecision) -> None:
        self.decision = decision
        self._gate = gate
        self._reserved_bytes = reserved_bytes
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._gate._active -= 1
        self._gate._reserved_bytes -= self._reserved_bytes


class SttReplayReservation:
    """Disk held for a request's replay copy from the first attempt until the dispatcher is done.

    It counts toward reserved disk only. The attempt lease carries the request's one concurrency slot.
    ``record_written`` reports bytes the copy has already put on the disk of ``device``: the
    filesystem's free space no longer includes them, so the gate does not subtract them a second time.
    """

    def __init__(self, gate: "SttResourceGate", reserved_bytes: int, decision: AdmissionDecision, device: int) -> None:
        self.decision = decision
        self.reserved_bytes = reserved_bytes
        self.device = device
        self.written_bytes = 0
        self._gate = gate
        self._released = False

    def record_written(self, size: int) -> None:
        self.written_bytes = min(self.reserved_bytes, self.written_bytes + size)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._gate._replays.discard(self)
        self._gate._reserved_bytes -= self.reserved_bytes


def resource_evidence(lease: "SttResourceLease", replay: "SttReplayReservation | None") -> dict[str, object]:
    """Receipt evidence for one attempt: its lease decision plus the replay disk held beside it."""
    evidence = lease.decision.details.as_dict(lease.decision.status.value)
    if replay is not None:
        evidence["facts"].append(asdict(CapacityFact(
            "replay_reserved_disk_mb", "disk", math.ceil(replay.reserved_bytes / (1024 * 1024)), time.time(), "declared",
        )))
    return evidence


class SttResourceGate:
    """Immediate, bounded admission backed by the shared claim model.

    Upload admission deliberately rejects contention instead of waiting in a
    queue before the ASGI body reader owns ``receive()``.  A queued request
    could otherwise be disconnected while the gateway has no safe way to
    observe its receive channel.  The small synchronous critical section is
    atomic with respect to one asyncio event loop and reserves the full
    upload-plus-decode workspace before multipart parsing begins.
    """

    def __init__(self) -> None:
        self._active = 0
        self._reserved_bytes = 0
        self._replays: set[SttReplayReservation] = set()

    @property
    def active_count(self) -> int:
        """Number of live requests currently holding a transcription slot."""
        return self._active

    def acquire(
        self,
        *,
        request_id: str,
        config: SttExecutionConfig,
    ) -> SttResourceLease:
        reservation = config.max_input_bytes + config.max_decoded_bytes
        decision = self._admit(request_id, config, config.request_claim(), reservation, limit_concurrency=True)
        self._active += 1
        self._reserved_bytes += reservation
        return SttResourceLease(self, reservation, decision)

    def reserve_replay(self, *, request_id: str, config: SttExecutionConfig) -> SttReplayReservation:
        """Reserve disk for the replay copy without taking a concurrency slot."""
        reservation = config.replay_limit_bytes
        decision = self._admit(request_id, config, config.replay_claim(), reservation, limit_concurrency=False)
        replay = SttReplayReservation(self, reservation, decision, _device_of(config.spool_dir))
        self._reserved_bytes += reservation
        self._replays.add(replay)
        return replay

    def _admit(
        self,
        request_id: str,
        config: SttExecutionConfig,
        claim: ResourceClaim,
        reservation: int,
        *,
        limit_concurrency: bool,
    ) -> AdmissionDecision:
        try:
            config.spool_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SttResourceLimitExceeded(
                "Configured STT spool storage is unavailable"
            ) from error
        live_free_bytes = _disk_free_bytes(config.spool_dir)
        if live_free_bytes < reservation + config.min_free_bytes:
            raise SttResourceLimitExceeded(
                "Configured STT spool storage has insufficient free capacity"
            )
        # Live free space already excludes replay bytes written to this filesystem, yet their
        # reservations are still held in full: add those bytes back so each is counted once.
        device = _device_of(config.spool_dir)
        free_bytes = live_free_bytes + sum(r.written_bytes for r in self._replays if r.device == device)
        decision = evaluate_admission(
            AdmissionRequest(request_id=request_id, claim=claim),
            CapacitySnapshot(
                observed_at=time.time(),
                fact_qualities={"total_disk_mb": "measured", "queue_active": "measured"},
                total_disk_mb=max(0, free_bytes // (1024 * 1024)),
                used_disk_mb=math.ceil(self._reserved_bytes / (1024 * 1024)),
                active_by_queue={QueueClass.INTERACTIVE: self._active} if limit_concurrency else {},
                queue_limits={QueueClass.INTERACTIVE: config.global_concurrency} if limit_concurrency else {},
            ),
        )
        if not decision.accepted:
            raise SttResourceLimitExceeded("STT concurrency limit is currently exhausted", decision=decision)
        if free_bytes - self._reserved_bytes < reservation + config.min_free_bytes:
            raise SttResourceLimitExceeded(
                "Configured STT spool storage has insufficient reserved capacity"
            )
        return decision


GLOBAL_STT_RESOURCE_GATE = SttResourceGate()


async def await_with_cancellation(
    awaitable: Awaitable[_T],
    *,
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]] = (),
    deadline_at: float | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> _T:
    """Await one operation while disconnect/cancellation always wins ties."""
    if deadline_at is not None and time.monotonic() >= deadline_at:
        raise SttDeadlineExceeded("STT request deadline exceeded")
    operation = asyncio.ensure_future(awaitable)
    waiters = [asyncio.create_task(waiter()) for waiter in cancellation_waiters]
    deadline_task: asyncio.Task[None] | None = None
    if deadline_at is not None:
        deadline_task = asyncio.create_task(asyncio.sleep(max(0.0, deadline_at - time.monotonic())))
        waiters.append(deadline_task)
    try:
        if not waiters:
            return await operation
        done, _ = await asyncio.wait({operation, *waiters}, return_when=asyncio.FIRST_COMPLETED)
        cancelled = any(waiter in done for waiter in waiters if waiter is not deadline_task)
        expired = deadline_task in done if deadline_task is not None else False
        if cancelled or expired:
            if cancelled and on_cancel is not None:
                on_cancel()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if cancelled:
                raise SttCancelled("STT request cancelled by client disconnect")
            raise SttDeadlineExceeded("STT request deadline exceeded")
        return operation.result()
    except asyncio.CancelledError:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        raise
    except Exception:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        raise
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        if waiters:
            await asyncio.gather(*waiters, return_exceptions=True)


async def run_media_command(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]],
    deadline_at: float,
    on_cancel: Callable[[], None] | None,
    max_output_bytes: int = 8 * 1024 * 1024,
) -> tuple[bytes, bytes]:
    """Run a child process and always kill/reap it on cancellation or timeout."""
    process: asyncio.subprocess.Process | None = None
    try:
        process = await await_with_cancellation(
            asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            cancellation_waiters=cancellation_waiters,
            deadline_at=deadline_at,
            on_cancel=on_cancel,
        )
        try:
            stdout, stderr = await await_with_cancellation(
                asyncio.wait_for(
                    _read_media_output(process, max_output_bytes=max_output_bytes),
                    timeout=timeout_seconds,
                ),
                cancellation_waiters=cancellation_waiters,
                deadline_at=deadline_at,
                on_cancel=on_cancel,
            )
        except asyncio.TimeoutError as error:
            raise SttSetupFailure("STT media command timed out") from error
        if process.returncode != 0:
            raise SttMediaCommandFailed("STT media command failed")
        return stdout, stderr
    except (asyncio.CancelledError, SttCancelled, SttDeadlineExceeded, SttResponseLimitExceeded):
        raise
    except FileNotFoundError as error:
        raise SttSetupFailure("Required local media utility is unavailable") from error
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.shield(process.wait())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _log_cleanup_failure("media_process", error)


async def _read_media_output(
    process: asyncio.subprocess.Process,
    *,
    max_output_bytes: int,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise SttSetupFailure("STT media command did not expose output streams")
    stdout, stderr = await asyncio.gather(
        _read_limited_stream(process.stdout, max_output_bytes),
        _read_limited_stream(process.stderr, max_output_bytes),
    )
    await process.wait()
    return stdout, stderr


async def _read_limited_stream(reader: asyncio.StreamReader, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await reader.read(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise SttResponseLimitExceeded("STT media command output exceeds the configured limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def probe_audio(
    source_path: Path,
    *,
    config: SttExecutionConfig,
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]],
    deadline_at: float,
    on_cancel: Callable[[], None] | None,
) -> ProbedAudio:
    stdout, _ = await run_media_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,sample_rate,channels",
            "-of",
            "json",
            os.fspath(source_path),
        ],
        timeout_seconds=min(30.0, config.timeout_seconds),
        cancellation_waiters=cancellation_waiters,
        deadline_at=deadline_at,
        on_cancel=on_cancel,
        max_output_bytes=config.max_media_output_bytes,
    )
    try:
        payload = json.loads(stdout.decode("utf-8"))
        audio_streams = [
            stream for stream in (payload.get("streams") or [])
            if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
        ]
        stream = audio_streams[0]
        sample_rate = int(stream.get("sample_rate"))
        channels = int(stream.get("channels"))
        duration = _finite_positive_or_none(
            (payload.get("format") or {}).get("duration")
        )
    except (IndexError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SttMediaCommandFailed("STT media probe returned invalid audio metadata") from error
    if duration is None:
        # Some containers (notably a Chrome MediaRecorder webm whose recording
        # session ended without a clean stop) carry audio/video sample data but
        # never get a trailing Cues/duration element written. ffprobe's cheap
        # format-level read then reports no duration at all even though the
        # stream decodes fine. Recover the true length from packet timestamps
        # instead of assuming the file is unusable: this is what lets duration
        # gate the chunking decision below instead of forcing every such file
        # through a single unbounded passthrough request.
        duration = await _probe_duration_from_packets(
            source_path,
            config=config,
            cancellation_waiters=cancellation_waiters,
            deadline_at=deadline_at,
            on_cancel=on_cancel,
        )
    if duration is None or sample_rate <= 0 or channels <= 0:
        raise SttMediaCommandFailed("STT media probe returned invalid audio metadata")
    probed = ProbedAudio(duration, sample_rate, channels)
    if duration > config.max_duration_seconds:
        raise SttResourceLimitExceeded("Audio duration exceeds the configured STT limit")
    if sample_rate > config.max_sample_rate or channels > config.max_channels:
        raise SttResourceLimitExceeded("Audio format exceeds the configured STT limit")
    if probed.decoded_bytes > config.max_decoded_bytes:
        raise SttResourceLimitExceeded("Decoded audio exceeds the configured STT limit")
    return probed


def _finite_positive_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


async def _probe_duration_from_packets(
    source_path: Path,
    *,
    config: SttExecutionConfig,
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]],
    deadline_at: float,
    on_cancel: Callable[[], None] | None,
) -> float | None:
    """Recover duration from the last audio packet's timestamp plus its length.

    Cheaper than a full decode (``-count_frames``): ffprobe only needs to
    demux packet headers, not decode every frame. Malformed trailing packets
    (a truncated final Opus frame from an interrupted recording) print a
    decoder warning on stderr but do not stop packet enumeration, so this
    still recovers a usable duration for files with minor trailing damage.
    """
    try:
        stdout, _ = await run_media_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "packet=pts_time,duration_time",
                "-of",
                "csv=p=0",
                os.fspath(source_path),
            ],
            timeout_seconds=min(60.0, config.timeout_seconds),
            cancellation_waiters=cancellation_waiters,
            deadline_at=deadline_at,
            on_cancel=on_cancel,
            max_output_bytes=config.max_media_output_bytes,
        )
    except SttSetupFailure:
        return None
    last_end: float | None = None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.strip().split(",")
        if len(parts) != 2:
            continue
        pts = _finite_positive_or_none(parts[0])
        packet_duration = _finite_positive_or_none(parts[1])
        if pts is None:
            continue
        end = pts + packet_duration if packet_duration is not None else pts
        if last_end is None or end > last_end:
            last_end = end
    return last_end


async def decode_planning_pcm(
    source_path: Path,
    destination_path: Path,
    *,
    config: SttExecutionConfig,
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]],
    deadline_at: float,
    on_cancel: Callable[[], None] | None,
) -> None:
    """Decode once within the upload's disk, duration, deadline and cancellation budgets."""
    bytes_per_second = _PLANNING_SAMPLE_RATE * 2
    max_pcm_bytes = min(config.max_decoded_bytes, int(config.max_duration_seconds * bytes_per_second))
    # Ask for one extra sample to detect an over-limit stream without decoding an unbounded input.
    await run_media_command(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", os.fspath(source_path),
            "-vn", "-ac", "1", "-ar", str(_PLANNING_SAMPLE_RATE), "-f", "s16le",
            "-t", str((max_pcm_bytes + 2) / bytes_per_second), "-fs", str(max_pcm_bytes + 2),
            os.fspath(destination_path),
        ],
        timeout_seconds=config.timeout_seconds,
        cancellation_waiters=cancellation_waiters,
        deadline_at=deadline_at,
        on_cancel=on_cancel,
        max_output_bytes=config.max_media_output_bytes,
    )
    try:
        size = destination_path.stat().st_size
    except OSError as error:
        raise SttSetupFailure("STT planning decode did not create audio") from error
    if size <= 0:
        raise SttMediaCommandFailed("STT planning decode holds no audio")
    if size > max_pcm_bytes:
        raise SttResourceLimitExceeded("Decoded audio exceeds the configured STT limit")


def quietest_frame_reader(pcm_path: Path) -> Callable[[float, float, float], float]:
    """Return ``quietest(lo, hi, near)``: the centre time of the lowest-energy 20 ms frame centred in [lo, hi].

    Frames start every 10 ms. Energy is the sum of squared samples, so ties (digital silence, a flat signal) go to
    the frame nearest ``near``. The reader opens the file for each call and holds only that window in memory.
    """
    rate = _PLANNING_SAMPLE_RATE
    half = _FRAME_SAMPLES // 2

    def quietest(lo: float, hi: float, near: float) -> float:
        first = max(0, math.ceil((lo * rate - half) / _FRAME_HOP_SAMPLES))
        last = math.floor((hi * rate - half) / _FRAME_HOP_SAMPLES)
        if last < first:
            return min(max(near, lo), hi)
        # A service record may ask for a wide search. Read one second of frames
        # at a time so the memory cost does not grow with that width.
        best: tuple[int, float] | None = None
        best_centre = near
        near_sample = near * rate
        with pcm_path.open("rb") as handle:
            for block_first in range(first, last + 1, 100):
                block_last = min(last, block_first + 99)
                handle.seek(block_first * _FRAME_HOP_SAMPLES * 2)
                samples = array("h")
                samples.frombytes(handle.read(((block_last - block_first) * _FRAME_HOP_SAMPLES + _FRAME_SAMPLES) * 2))
                if len(samples) < _FRAME_SAMPLES:
                    break
                squares = list(accumulate(map(mul, samples, samples), initial=0))
                frames = (len(samples) - _FRAME_SAMPLES) // _FRAME_HOP_SAMPLES + 1
                for index in range(frames):
                    centre_sample = (block_first + index) * _FRAME_HOP_SAMPLES + half
                    score = (squares[index * _FRAME_HOP_SAMPLES + _FRAME_SAMPLES] - squares[index * _FRAME_HOP_SAMPLES],
                             abs(centre_sample - near_sample))
                    if best is None or score < best:
                        best, best_centre = score, centre_sample / rate
        return min(max(best_centre, lo), hi)

    return quietest


def plan_quietest_frame_segments(
    *,
    duration_seconds: float,
    quietest_frame: Callable[[float, float, float], float],
    target_seconds: float,
    search_seconds: float,
    max_segment_seconds: float,
    max_segments: int,
) -> list[tuple[float, float]]:
    """Cut at the quietest frame near each multiple of the target; no overlap, no segment above the hard maximum.

    The cut for target ``k * T`` is searched in ``k * T +- W`` (W is the record's search width). The
    range is clipped to what keeps the segment ending at the cut within ``max_segment_seconds``, so the maximum is
    kept even when the signal gives no quiet place. Cutting stops when the rest of the file fits one segment of at
    most ``T`` plus a quarter of ``T`` (and never more than the hard maximum). Segments tile the file exactly.
    """
    if max_segment_seconds < target_seconds:
        raise SttConfigurationError("STT max_segment_seconds must be at least target_seconds")
    width = min(search_seconds, target_seconds / 2)
    min_segment_seconds = min(_MIN_SEGMENT_SECONDS, target_seconds / 2)
    end_limit = min(max_segment_seconds, target_seconds * (1.0 + _TAIL_FRACTION_OF_TARGET))
    cuts: list[float] = []
    previous = 0.0
    while duration_seconds - previous > end_limit:
        if len(cuts) + 2 > max_segments:
            raise SttResourceLimitExceeded("Audio requires more STT segments than configured")
        target = target_seconds * (len(cuts) + 1)
        highest = min(previous + max_segment_seconds, duration_seconds - min_segment_seconds, target + width)
        lowest = max(previous + min_segment_seconds, target - width)
        if lowest > highest:
            raise SttSetupFailure("No legal STT cut lies inside the target search window")
        cut = min(max(quietest_frame(lowest, highest, min(max(target, lowest), highest)), lowest), highest)
        cuts.append(cut)
        previous = cut
    boundaries = [0.0, *cuts, duration_seconds]
    return list(zip(boundaries[:-1], boundaries[1:]))


async def extract_segment(
    source_path: Path,
    destination_path: Path,
    *,
    start_seconds: float,
    end_seconds: float,
    config: SttExecutionConfig,
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]],
    deadline_at: float,
    on_cancel: Callable[[], None] | None,
) -> None:
    expected = end_seconds - start_seconds
    await run_media_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            os.fspath(source_path),
            "-ss",
            f"{start_seconds}",
            "-t",
            f"{expected}",
            "-vn",
            "-acodec",
            "pcm_s16le",
            os.fspath(destination_path),
        ],
        timeout_seconds=config.timeout_seconds,
        cancellation_waiters=cancellation_waiters,
        deadline_at=deadline_at,
        on_cancel=on_cancel,
        max_output_bytes=config.max_media_output_bytes,
    )
    # WAV is fixed PCM16; this postcondition makes the disk bound concrete even
    # if a media utility emits an unexpected container or duration.
    try:
        size = destination_path.stat().st_size
    except OSError as error:
        raise SttSetupFailure("STT segment extraction did not create audio") from error
    if size <= 44 or size > config.max_decoded_bytes:
        raise SttResourceLimitExceeded("Extracted STT segment exceeds the configured limit")
    actual = await probe_audio(
        destination_path,
        config=config,
        cancellation_waiters=cancellation_waiters,
        deadline_at=deadline_at,
        on_cancel=on_cancel,
    )
    if abs(actual.duration_seconds - expected) > 0.1:
        raise SttSetupFailure("STT segment extraction has an unexpected duration")
    if actual.duration_seconds > config.max_segment_seconds:
        raise SttSetupFailure("STT segment extraction exceeds the engine's hard maximum")


def request_supports_chunking(fields: Sequence[tuple[str, str]], *, file_count: int) -> bool:
    """Return true only for the OpenAI envelope this module can preserve."""
    if file_count != 1:
        return False
    names = [name for name, _ in fields]
    values = {name: value for name, value in fields}
    response_format = values.get("response_format", "json").strip().lower()
    if response_format != "json" or _as_bool(values.get("stream", False)):
        return False
    if "chunking_strategy" in names:
        return False
    return not any(
        name == "include"
        or name.startswith("include[")
        or name == "timestamp_granularities"
        or name.startswith("timestamp_granularities[")
        for name in names
    )


async def guard_uncut_uploads(
    paths: Sequence[Path], *, config: SttExecutionConfig,
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]], deadline_at: float,
    on_cancel: Callable[[], None] | None, streaming: bool = False,
) -> None:
    """Refuse an over-limit file when the request envelope cannot be cut, if its record has a known maximum."""
    native = config.native_long_audio_streaming if streaming else config.native_long_audio_offline
    if native or not config.max_segment_declared:
        return
    for index, path in enumerate(paths):
        pcm_path = path.parent / f"uncut-{index}-16k.pcm"
        try:
            await probe_audio(path, config=config, cancellation_waiters=cancellation_waiters,
                              deadline_at=deadline_at, on_cancel=on_cancel)
            await decode_planning_pcm(path, pcm_path, config=config, cancellation_waiters=cancellation_waiters,
                                      deadline_at=deadline_at, on_cancel=on_cancel)
            duration = pcm_path.stat().st_size / (_PLANNING_SAMPLE_RATE * 2)
        except SttMediaCommandFailed as error:
            raise SttInvalidAudio("STT input is not readable audio") from error
        finally:
            _unlink_chunk_path(pcm_path)
        if duration > config.max_segment_seconds:
            raise SttAudioNotSplittable("unsupported_request", limit_seconds=config.max_segment_seconds)


def _may_send_whole(config: SttExecutionConfig, probed: ProbedAudio | None, *, duration_verified: bool) -> bool:
    """Whether a file whose cut failed may go whole under the setup policy and recorded limit."""
    if config.setup_failure_policy != "passthrough":
        return False
    return (
        config.native_long_audio_offline
        or not config.max_segment_declared
        or (duration_verified and probed is not None and probed.duration_seconds <= config.max_segment_seconds)
    )


def _unplanned_outcome(
    config: SttExecutionConfig, probed: ProbedAudio | None, error: SttSetupFailure, *, duration_verified: bool = False,
) -> ChunkingOutcome:
    """The cut could not be planned: send the file whole only when the engine can take it, else refuse it."""
    if _may_send_whole(config, probed, duration_verified=duration_verified):
        return ChunkingOutcome("passthrough", 0, "setup_failed_before_attempt")
    if config.setup_failure_policy != "passthrough" or config.native_long_audio_offline:
        raise error
    if probed is None:
        reason = "unknown_duration"
    else:
        reason = "split_failed"
    raise SttAudioNotSplittable(reason, limit_seconds=config.max_segment_seconds) from error


async def execute_chunking(
    *,
    source_path: Path,
    source_stem: str,
    fields: Sequence[tuple[str, str]],
    config: SttExecutionConfig,
    post_file: Callable[[Path, str], Awaitable[UpstreamResponse]],
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]],
    deadline_at: float,
    on_cancel: Callable[[], None] | None,
    on_attempt_started: Callable[[int], None] | None = None,
    cleanup: "SttCleanupOwner | None" = None,
) -> ChunkingOutcome:
    """Probe, plan, extract, and submit chunks without a whole-file replay."""
    if not config.chunking_enabled or not request_supports_chunking(fields, file_count=1):
        return ChunkingOutcome("passthrough", 0, "ineligible_envelope")

    probed: ProbedAudio | None = None
    planning_duration: float | None = None
    pcm_path = source_path.parent / "planning-16k.pcm"
    try:
        probed = await probe_audio(
            source_path,
            config=config,
            cancellation_waiters=cancellation_waiters,
            deadline_at=deadline_at,
            on_cancel=on_cancel,
        )
        if cleanup is not None:
            cleanup.add("planning_pcm", lambda: _unlink_chunk_path(pcm_path))
        try:
            await decode_planning_pcm(
                source_path,
                pcm_path,
                config=config,
                cancellation_waiters=cancellation_waiters,
                deadline_at=deadline_at,
                on_cancel=on_cancel,
            )
            planning_duration = pcm_path.stat().st_size / (_PLANNING_SAMPLE_RATE * 2)
            if planning_duration <= 0:
                raise SttMediaCommandFailed("STT planning decode holds no audio")
        except SttMediaCommandFailed as error:
            raise SttInvalidAudio("STT input is not readable audio") from error
        probed = ProbedAudio(planning_duration, _PLANNING_SAMPLE_RATE, 1)
        if planning_duration <= config.chunk_above_seconds:
            return ChunkingOutcome("passthrough", 0, "below_threshold")
        segments = await asyncio.to_thread(
            plan_quietest_frame_segments,
            duration_seconds=planning_duration,
            quietest_frame=quietest_frame_reader(pcm_path),
            target_seconds=config.target_seconds,
            search_seconds=config.search_seconds,
            max_segment_seconds=config.max_segment_seconds,
            max_segments=config.max_segments,
        )
    except (
        asyncio.CancelledError,
        SttCancelled,
        SttDeadlineExceeded,
        SttResourceLimitExceeded,
        SttBackendTimeout,
        SttBackendUnavailable,
    ):
        raise
    except SttSetupFailure as error:
        return _unplanned_outcome(config, probed, error, duration_verified=planning_duration is not None)
    except OSError:
        return _unplanned_outcome(config, probed, SttSetupFailure("STT planning could not read the decoded audio"),
                                  duration_verified=planning_duration is not None)
    finally:
        if cleanup is None:
            _unlink_chunk_path(pcm_path)
        else:
            await cleanup.release("planning_pcm", lambda: _unlink_chunk_path(pcm_path))

    planned_duration = planning_duration if planning_duration is not None else probed.duration_seconds
    if config.max_segment_declared and planned_duration > config.max_segment_seconds and (
        not segments or len(segments) <= 1 or any(end - start > config.max_segment_seconds for start, end in segments)
    ):
        raise SttAudioNotSplittable("split_failed", limit_seconds=config.max_segment_seconds)
    if not segments or len(segments) <= 1:
        return ChunkingOutcome("passthrough", 0, "single_segment_plan")
    if len(segments) > config.max_segments:
        raise SttResourceLimitExceeded("Audio requires more STT segments than configured")

    # A service declares per-chunk fields its engine adds beside the text (such as
    # a {"timing": ...} diagnostic) as response transform rows; they run on each
    # chunk before the merge check. A row that cannot run is a setup failure,
    # found by a trial run before any chunk is uploaded.
    response_transforms = transform_specs(_thaw(config.backend_config.get("transforms")))
    try:
        run_response_pipeline(response_transforms, {"text": ""})
    except (KeyError, TypeError, ValueError) as error:
        if _may_send_whole(config, probed, duration_verified=planning_duration is not None):
            return ChunkingOutcome("passthrough", 0, "setup_failed_before_attempt")
        raise SttSetupFailure("STT service response transforms cannot run") from error
    payloads: list[Mapping[str, Any]] = []
    attempts_started = 0
    for index, (start, end) in enumerate(segments):
        chunk_path = source_path.parent / f"segment-{index:03d}.wav"
        if cleanup is not None:
            cleanup.add(f"segment_{index:03d}", lambda path=chunk_path: _unlink_chunk_path(path))
        try:
            try:
                await extract_segment(
                    source_path,
                    chunk_path,
                    start_seconds=start,
                    end_seconds=end,
                    config=config,
                    cancellation_waiters=cancellation_waiters,
                    deadline_at=deadline_at,
                    on_cancel=on_cancel,
                )
            except SttSetupFailure as error:
                # Extraction failure before the first backend attempt may use
                # the configured explicit setup policy.  After progress it can
                # never silently replay the original audio.
                if attempts_started == 0:
                    if _may_send_whole(config, probed, duration_verified=planning_duration is not None):
                        return ChunkingOutcome("passthrough", 0, "setup_failed_before_attempt")
                    if config.setup_failure_policy == "passthrough":
                        raise SttAudioNotSplittable("split_failed", limit_seconds=config.max_segment_seconds) from error
                raise SttChunkAttemptError("STT segment extraction failed after chunk progress") from error

            attempts_started += 1
            if on_attempt_started is not None:
                on_attempt_started(attempts_started)
            try:
                response = await post_file(chunk_path, f"{source_stem}.chunk{index:03d}.wav")
            except (
                asyncio.CancelledError,
                SttCancelled,
                SttDeadlineExceeded,
                SttBackendTimeout,
                SttBackendUnavailable,
            ):
                raise
            except Exception as error:
                raise SttChunkAttemptError("STT chunk request failed") from error
            if not upstream_response_succeeded(response.status_code):
                return ChunkingOutcome(
                    "upstream_error",
                    attempts_started,
                    upstream_response=response,
                )
            payloads.append(_parse_mergeable_chunk_payload(response.body, response_transforms))
        finally:
            if cleanup is None:
                _unlink_chunk_path(chunk_path)
            else:
                await cleanup.release(f"segment_{index:03d}", lambda path=chunk_path: _unlink_chunk_path(path))

    return ChunkingOutcome(
        "chunked",
        attempts_started,
        merged_payload=merge_transcription_payloads(payloads),
    )


def merge_transcription_payloads(payloads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not payloads:
        raise SttChunkResponseError("STT chunks returned no transcription payload")
    texts = [payload["text"].strip() for payload in payloads if payload["text"].strip()]
    merged: dict[str, Any] = {"text": " ".join(texts)}
    usage = _merge_usage([payload.get("usage") for payload in payloads])
    if usage is not None:
        merged["usage"] = usage
    return merged


def _parse_mergeable_chunk_payload(body: bytes, response_transforms: Sequence[Any] = ()) -> Mapping[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SttChunkResponseError("STT chunk returned non-JSON output") from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("text"), str):
        raise SttChunkResponseError("STT chunk returned no text")
    payload = run_response_pipeline(list(response_transforms), dict(payload))
    unknown = set(payload) - _SDK_TRANSCRIPTION_FIELDS
    unsupported = set(payload) - _MERGEABLE_TRANSCRIPTION_FIELDS
    if unknown or unsupported:
        raise SttChunkResponseError("STT chunk returned a response shape that cannot be merged")
    if payload.get("usage") is not None:
        _validate_usage(payload["usage"])
    return payload


def _merge_usage(values: Sequence[Any]) -> dict[str, Any] | None:
    if not any(value is not None for value in values):
        return None
    if any(value is None for value in values) or any(not isinstance(value, Mapping) for value in values):
        raise SttChunkResponseError("STT chunk usage is invalid")
    usages = [dict(value) for value in values]
    for usage in usages:
        _validate_usage(usage)
    kinds = {str(usage.get("type") or "") for usage in usages}
    if len(kinds) != 1:
        raise SttChunkResponseError("STT chunk usage formats differ")
    kind = kinds.pop()
    if kind == "duration":
        return {"type": "duration", "seconds": _sum_number(usages, "seconds")}
    if kind == "tokens":
        merged: dict[str, Any] = {
            "type": "tokens",
            "input_tokens": _sum_number(usages, "input_tokens"),
            "output_tokens": _sum_number(usages, "output_tokens"),
            "total_tokens": _sum_number(usages, "total_tokens"),
        }
        details = [usage.get("input_token_details") for usage in usages]
        if all(detail is None for detail in details):
            return merged
        if any(not isinstance(detail, Mapping) for detail in details):
            raise SttChunkResponseError("STT chunk token usage details differ")
        detail_mappings = [dict(detail) for detail in details]
        detail_shapes = {frozenset(detail) for detail in detail_mappings}
        if len(detail_shapes) != 1:
            raise SttChunkResponseError("STT chunk token usage details differ")
        merged_details: dict[str, int] = {}
        for field in detail_shapes.pop():
            counters = [detail[field] for detail in detail_mappings]
            if any(counter is None for counter in counters):
                if any(counter is not None for counter in counters):
                    raise SttChunkResponseError("STT chunk token usage details differ")
                continue
            merged_details[field] = _sum_integer(counters)
        if merged_details:
            merged["input_token_details"] = merged_details
        return merged
    raise SttChunkResponseError("STT chunk usage format is unsupported")


def _validate_usage(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise SttChunkResponseError("STT chunk usage is invalid")
    kind = value.get("type")
    if kind == "duration":
        _validate_usage_mapping(value, _SDK_DURATION_USAGE_FIELDS, required={"type", "seconds"})
        _validate_duration_seconds(value["seconds"])
        return
    if kind == "tokens":
        _validate_usage_mapping(
            value,
            _SDK_TOKEN_USAGE_FIELDS,
            required={"type", "input_tokens", "output_tokens", "total_tokens"},
        )
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            _validate_integer(value[field])
        details = value.get("input_token_details")
        if details is not None:
            _validate_usage_mapping(details, _SDK_TOKEN_DETAIL_FIELDS, required=set())
            for field in _SDK_TOKEN_DETAIL_FIELDS:
                if field in details and details[field] is not None:
                    _validate_integer(details[field])
        return
    raise SttChunkResponseError("STT chunk usage format is unsupported")


def _validate_usage_mapping(
    value: Mapping[str, Any],
    fields: frozenset[str],
    *,
    required: set[str],
) -> None:
    if set(value) - fields or required - set(value):
        raise SttChunkResponseError("STT chunk usage has unsupported fields")


def _validate_duration_seconds(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SttChunkResponseError("STT chunk usage is invalid")


def _validate_integer(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SttChunkResponseError("STT chunk usage is invalid")


def filtered_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Filter headers after ``httpx.aiter_bytes`` has decoded the body.

    HTTPX exposes decoded content here.  Keeping validators or encodings for
    the upstream representation would lie about the bytes sent to the caller.
    """
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS | _DECODED_RESPONSE_REPRESENTATION_HEADERS
    }


def upstream_response_succeeded(status_code: int) -> bool:
    """The shared terminal-success contract for direct and chunked STT calls."""
    return 200 <= status_code < 300


async def read_bounded_response(
    response: httpx.Response,
    *,
    max_bytes: int,
    cancellation_waiters: Sequence[Callable[[], Awaitable[None]]],
    deadline_at: float,
    on_cancel: Callable[[], None] | None,
) -> bytes:
    """Read one upstream body without allowing it to become an unbounded buffer."""
    chunks: list[bytes] = []
    total = 0
    iterator = response.aiter_bytes().__aiter__()
    while True:
        try:
            chunk = await await_with_cancellation(
                iterator.__anext__(),
                cancellation_waiters=cancellation_waiters,
                deadline_at=deadline_at,
                on_cancel=on_cancel,
            )
        except StopAsyncIteration:
            break
        total += len(chunk)
        if total > max_bytes:
            raise SttResponseLimitExceeded("STT backend response exceeds the configured limit")
        chunks.append(chunk)
    return b"".join(chunks)


class SttCleanupOwner:
    """One route-local owner that attempts every cleanup and records safe failures."""

    def __init__(self) -> None:
        self._actions: list[tuple[str, Callable[[], Awaitable[None] | None]]] = []
        self.failed = False

    def add(self, name: str, action: Callable[[], Awaitable[None] | None]) -> None:
        self._actions.append((name, action))

    async def aclose(self) -> None:
        cancelled = False
        while self._actions:
            name, action = self._actions.pop()
            try:
                await self._run(name, action)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def release(self, name: str, action: Callable[[], Awaitable[None] | None]) -> None:
        for index in range(len(self._actions) - 1, -1, -1):
            registered_name, registered_action = self._actions[index]
            if registered_name == name:
                self._actions.pop(index)
                await self._run(registered_name, registered_action)
                return
        await self._run(name, action)

    async def _run(self, name: str, action: Callable[[], Awaitable[None] | None]) -> None:
        try:
            result = action()
            if result is not None:
                await _await_cleanup(result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failed = True
            _log_cleanup_failure(name, error)


async def _await_cleanup(awaitable: Awaitable[None]) -> None:
    task = asyncio.ensure_future(awaitable)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception as error:
            _log_cleanup_failure("cancelled_cleanup", error)
        raise


def _log_cleanup_failure(resource: str, error: Exception) -> None:
    print(
        f"[gateway] STT cleanup failed for {resource}: {type(error).__name__}",
        flush=True,
    )


def upload_file_parts(form: FormData) -> tuple[list[tuple[str, str]], list[tuple[str, UploadFile]]]:
    fields: list[tuple[str, str]] = []
    files: list[tuple[str, UploadFile]] = []
    for name, value in form.multi_items():
        if isinstance(value, UploadFile):
            files.append((name, value))
        else:
            fields.append((name, str(value)))
    return fields, files


def replace_model_field(
    fields: Sequence[tuple[str, str]],
    *,
    model: str,
) -> list[tuple[str, str]]:
    result = [(name, value) for name, value in fields if name != "model"]
    result.append(("model", model))
    return result


def source_path(upload: UploadFile) -> Path:
    path = getattr(upload.file, "name", None)
    if not isinstance(path, str) or not path:
        raise SttSetupFailure("STT upload did not spool to a file")
    return Path(path)


def _sum_number(items: Sequence[Mapping[str, Any]], key: str) -> int | float:
    values: list[int | float] = []
    for item in items:
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SttChunkResponseError("STT chunk usage is invalid")
        values.append(value)
    total = sum(values)
    return int(total) if all(isinstance(value, int) for value in values) else total


def _sum_integer(values: Sequence[Any]) -> int:
    for value in values:
        _validate_integer(value)
    return sum(values)


def _unlink_chunk_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _disk_free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError as error:
        raise SttResourceLimitExceeded("Configured STT spool storage is unavailable") from error


def _device_of(path: Path) -> int:
    try:
        return os.stat(path).st_dev
    except OSError as error:
        raise SttResourceLimitExceeded("Configured STT spool storage is unavailable") from error


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        frozen[str(key)] = _freeze_value(item)
    return MappingProxyType(frozen)


def _thaw(value: Any) -> Any:
    """A plain copy of a frozen config value, for code that expects dicts and lists."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    return value
