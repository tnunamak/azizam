"""Generic media-job model and durable store.

Media jobs are the gateway-native abstraction behind durable, possibly
long-running media work: image edits routed through ComfyUI, video generation,
music/sound effects, transcription batches, anything that does not fit a
single synchronous request/response.

This module is intentionally pure with respect to backends: it does not know
how ComfyUI submits a workflow or how a TTS engine streams. It owns:

- A small typed record (``MediaJob``) describing one job.
- A SQLite store that persists job records and an append-only event log.
- Audience filtering so the same internal record can be projected for
  ``client``, ``admin``, and ``internal`` callers.
- A safety guard that rejects event payloads or metadata that look like raw
  prompts, image bytes, secrets, or request/response bodies.

The shape is parallel to ``gateway/receipt_store.py`` but distinct:

- Receipts keep routing, usage and spend facts.
- Jobs are mutable durable records about ongoing work. They progress through
  states, accrue events, and reference assets.

Wiring this into ``proxy.py``/``gateway/api.py`` is owned by a separate lane;
see the workstream note for suggested integration points.
"""

from __future__ import annotations

from gateway import request_connections
from gateway.compat import get_env
from gateway.sqlite_startup import connect, store_startup

import json
import re
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping
from uuid import uuid4
from gateway.ownership import stored_owner_key

from gateway.redaction import (
    FORBIDDEN_KEYS as _SHARED_FORBIDDEN_KEYS,
    assert_safe_mapping as _shared_assert_safe_mapping,
)

_default_db = Path(__file__).resolve().parent.parent / "data" / "media_jobs.db"
DB_PATH = Path(get_env("AZIZAM_MEDIA_JOBS_DB")) if get_env("AZIZAM_MEDIA_JOBS_DB") else _default_db

# A request ID replays its job while the job is active or changed in the last
# 14 days: the lifetime job records had before they were kept. A later repeat
# starts new work, and the old job keeps its history without the request ID.
REPLAY_WINDOW_S = 14 * 24 * 3600

SCHEMA_VERSION = 2

JobStatus = Literal["queued", "running", "succeeded", "failed", "canceled"]
PHASE_FIELDS = ("accepted_at", "queued_at", "started_at", "first_output_at", "finished_at")
# Typed facts about a failure, beside the plain ``error`` sentence. The category
# says who refused: the gateway (authorization, invalid_input, capacity) or the
# work itself (upstream, timeout, safety). ``upstream_http_status`` is set only
# for an upstream failure. None means not recorded (older or unclassified jobs).
ERROR_FIELDS = ("error_code", "error_category", "retryable", "upstream_http_status")
ERROR_CATEGORIES = frozenset({"authorization", "invalid_input", "upstream", "capacity", "timeout", "safety"})
_ERROR_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


# Status values. Mirrors the minimum lifecycle in CORE_CONTRACTS.md.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

ALL_STATUSES = frozenset({
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELED,
})

TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELED})


def is_terminal(status: str) -> bool:
    """Whether a job state is terminal; unknown states must keep waiting."""
    return status in TERMINAL_STATUSES


def _replays(status: str, updated_at: int) -> bool:
    return not is_terminal(status) or updated_at >= _now() - REPLAY_WINDOW_S


def holds_resources(status: JobStatus) -> bool:
    """Queued work reserves admission capacity until it becomes terminal."""
    return not is_terminal(status)

# Allowed forward transitions. Idempotent same-state writes are permitted
# regardless (e.g. running -> running progress updates).
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_QUEUED: frozenset({STATUS_QUEUED, STATUS_RUNNING, STATUS_FAILED, STATUS_CANCELED}),
    STATUS_RUNNING: frozenset({STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELED}),
    STATUS_SUCCEEDED: frozenset({STATUS_SUCCEEDED}),
    STATUS_FAILED: frozenset({STATUS_FAILED}),
    STATUS_CANCELED: frozenset({STATUS_CANCELED}),
}

_AUDIENCES = ("internal", "admin", "client")

# Fields safe to expose to the caller of a job. Anything not in this set is
# admin- or internal-only by default.
CLIENT_VISIBLE_FIELDS = frozenset(
    {
        "request_id",
        "job_id",
        "intent",
        "status",
        "progress",
        "assets",
        "error",
        "created_at",
        "updated_at",
        "parent_request_id",
        *PHASE_FIELDS,
        *ERROR_FIELDS,
        "attempt_number",
        "previous_attempts",
        "cancel_requested",
    }
)

# Backend identity, workflow alias, and ownership are admin/operator concerns.
# They are not secrets, but exposing them to arbitrary clients leaks the
# routing layer's internals. Audience filtering keeps that boundary explicit.
ADMIN_ONLY_FIELDS = frozenset(
    {
        "workflow",
        "backend",
        "app_profile",
        "app_id",
        "api_key_name",
    }
)

# Metadata bag and the raw event payloads are internal-only. They are the
# place where adapter-specific breadcrumbs live (prompt_id, queue position,
# probe response codes, etc.) and they have already passed the safety guard.
INTERNAL_ONLY_FIELDS = frozenset({"metadata", "owner_key_hash", "owner_scope", "input_hash", "creating_receipt_id"})

# Metadata/event keys that smell like raw payloads, secrets, or media bytes.
# Sourced from ``gateway.redaction`` so receipts and media jobs share one
# vocabulary. Re-exported under the historical name for external callers
# (the test suite asserts the two names refer to the same frozenset).
_FORBIDDEN_KEYS = _SHARED_FORBIDDEN_KEYS  # noqa: F841 — re-exported for backward compat


def new_job_id() -> str:
    return f"mj_{uuid4().hex}"


def _now() -> int:
    return int(time.time())


# The admin shape is an allowlist too: a new column stays out of every response
# until someone adds it here on purpose.
ADMIN_VISIBLE_FIELDS = CLIENT_VISIBLE_FIELDS | ADMIN_ONLY_FIELDS


@dataclass
class MediaJob:
    """Pure data record for one media job.

    ``request_id`` is the originating route receipt's request id. ``job_id`` is
    the durable handle clients poll. They are deliberately distinct: many
    requests may attempt to create or reattach to a single job (idempotency by
    request_id is enforced by the store).
    """

    intent: str
    workflow: str | None = None
    backend: str | None = None
    request_id: str = ""
    job_id: str = field(default_factory=new_job_id)
    status: str = STATUS_QUEUED
    progress: int = 0
    assets: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)
    app_profile: str | None = None
    api_key_name: str | None = None
    parent_request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Trusted submission code sets this from the authenticated App. Keep it
    # with the job so receipt retention cannot erase its ownership boundary.
    app_id: str | None = None
    owner_key_hash: str | None = None
    owner_scope: str | None = None
    input_hash: str | None = None
    # Unix seconds. None means unavailable, including pre-instrumentation rows.
    accepted_at: int | None = None
    queued_at: int | None = None
    started_at: int | None = None
    first_output_at: int | None = None
    finished_at: int | None = None
    attempt_number: int | None = None
    previous_attempts: list[dict[str, Any]] = field(default_factory=list)
    # Durable provenance, independent of the reusable request/idempotency key.
    creating_receipt_id: str | None = None
    error_code: str | None = None
    error_category: str | None = None
    retryable: bool | None = None
    upstream_http_status: int | None = None

    def __post_init__(self) -> None:
        if not self.intent:
            raise ValueError("MediaJob.intent is required")
        if self.status not in ALL_STATUSES:
            raise ValueError(
                f"MediaJob.status must be one of {sorted(ALL_STATUSES)}, got {self.status!r}"
            )
        self.progress = _clamp_progress(self.progress)
        _assert_safe_mapping("MediaJob.metadata", self.metadata)
        for asset in self.assets:
            if not isinstance(asset, Mapping):
                raise ValueError("MediaJob.assets entries must be mappings")
            _assert_safe_mapping("MediaJob.assets[*]", asset)
        for attempt in self.previous_attempts:
            if not isinstance(attempt, Mapping) or set(attempt) - {*PHASE_FIELDS, "attempt_number"}:
                raise ValueError("previous_attempts must contain only phase facts")
            if any(value is not None and (type(value) is not int or value < 0) for value in attempt.values()):
                raise ValueError("previous_attempts phase facts must be non-negative integers or null")

    def to_dict(self, audience: str = "internal") -> dict[str, Any]:
        if audience not in _AUDIENCES:
            raise ValueError(f"audience must be one of {_AUDIENCES}")
        data: dict[str, Any] = {
            "request_id": self.request_id,
            "job_id": self.job_id,
            "intent": self.intent,
            "workflow": self.workflow,
            "backend": self.backend,
            "status": self.status,
            "progress": self.progress,
            "assets": [dict(a) for a in self.assets],
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "app_profile": self.app_profile,
            "app_id": self.app_id,
            "owner_key_hash": self.owner_key_hash,
            "api_key_name": self.api_key_name,
            "parent_request_id": self.parent_request_id,
            "creating_receipt_id": self.creating_receipt_id,
            "metadata": dict(self.metadata),
            "owner_scope": self.owner_scope,
            "input_hash": self.input_hash,
            "attempt_number": self.attempt_number,
            "previous_attempts": [dict(attempt) for attempt in self.previous_attempts],
            **{name: getattr(self, name) for name in PHASE_FIELDS},
            **{name: getattr(self, name) for name in ERROR_FIELDS},
        }
        return _filter_for_audience(data, audience)


def _clamp_progress(value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"progress must be an int 0..100, got {value!r}") from e
    if v < 0 or v > 100:
        raise ValueError(f"progress must be in [0, 100], got {v}")
    return v


def _assert_safe_mapping(label: str, data: Any) -> None:
    """Reject obviously unsafe shapes before they hit disk.

    Thin wrapper over ``gateway.redaction.assert_safe_mapping`` kept so older
    call-sites in this module continue to read naturally.
    """
    _shared_assert_safe_mapping(label, data, recurse=True)


def for_audience(job: Mapping[str, Any], audience: str) -> dict[str, Any]:
    """Filter a job dict read with ``audience="internal"`` for another audience."""
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    return _filter_for_audience(job, audience)


def _filter_for_audience(data: Mapping[str, Any], audience: str) -> dict[str, Any]:
    data = {**dict.fromkeys((*PHASE_FIELDS, *ERROR_FIELDS, "attempt_number")), **data}
    data.setdefault("previous_attempts", [])
    data["cancel_requested"] = (
        holds_resources(data.get("status", STATUS_QUEUED))
        and bool((data.get("metadata") or {}).get("cancel_requested_at"))
    )
    if audience == "internal":
        return dict(data)
    if audience == "admin":
        return {k: data[k] for k in ADMIN_VISIBLE_FIELDS if k in data}
    return {k: data[k] for k in CLIENT_VISIBLE_FIELDS if k in data}


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_jobs (
  job_id TEXT PRIMARY KEY,
  request_id TEXT,
  intent TEXT NOT NULL,
  workflow TEXT,
  backend TEXT,
  status TEXT NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  app_profile TEXT,
  api_key_name TEXT,
  parent_request_id TEXT,
  job_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS media_jobs_created_at ON media_jobs(created_at);
CREATE INDEX IF NOT EXISTS media_jobs_status ON media_jobs(status);
CREATE INDEX IF NOT EXISTS media_jobs_updated_order ON media_jobs(updated_at, job_id);
CREATE INDEX IF NOT EXISTS media_jobs_status_updated_order ON media_jobs(status, updated_at, job_id);
CREATE INDEX IF NOT EXISTS media_jobs_app_profile ON media_jobs(app_profile);
CREATE INDEX IF NOT EXISTS media_jobs_request_id ON media_jobs(request_id);
CREATE UNIQUE INDEX IF NOT EXISTS media_jobs_unique_request_id
  ON media_jobs(request_id) WHERE request_id IS NOT NULL AND request_id != '';

CREATE TABLE IF NOT EXISTS media_job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  FOREIGN KEY (job_id) REFERENCES media_jobs(job_id)
);
CREATE INDEX IF NOT EXISTS media_job_events_job_id ON media_job_events(job_id, id);
CREATE INDEX IF NOT EXISTS media_job_events_created_at ON media_job_events(created_at);

CREATE TABLE IF NOT EXISTS media_job_prompt_erasure_cutoffs (
  owner_key TEXT PRIMARY KEY,
  cutoff_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS media_jobs_schema_version (version INTEGER NOT NULL);
"""


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA secure_delete=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")


def _exec_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM media_jobs_schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO media_jobs_schema_version(version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
    elif int(row[0]) not in (1, SCHEMA_VERSION):
        raise sqlite3.DatabaseError(
            f"media_jobs schema version mismatch: db={row[0]} code={SCHEMA_VERSION}"
        )
    # Migrate under a write lock: reopening an old store is safe and retains
    # every row. Historical events have unknown provenance, never trusted.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(media_job_events)")}
    if "sequence" not in columns:
        conn.execute("ALTER TABLE media_job_events ADD COLUMN sequence INTEGER")
    if "source" not in columns:
        conn.execute("ALTER TABLE media_job_events ADD COLUMN source TEXT NOT NULL DEFAULT 'unavailable'")
    if "attempt_number" not in columns:
        conn.execute("ALTER TABLE media_job_events ADD COLUMN attempt_number INTEGER")
    _repair_event_sequences(conn)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS media_job_event_sequence ON media_job_events(job_id, sequence)")
    # Baseline writers omit sequence. SQLite serializes this with their insert,
    # so a surviving old process cannot leave an unreadable event behind.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS media_job_event_assign_sequence
        AFTER INSERT ON media_job_events WHEN NEW.sequence IS NULL
        BEGIN
            UPDATE media_job_events SET sequence = (
                SELECT COALESCE(MAX(sequence), 0) + 1 FROM media_job_events WHERE job_id = NEW.job_id
            ) WHERE id = NEW.id;
        END""")
    conn.execute("UPDATE media_jobs_schema_version SET version=?", (SCHEMA_VERSION,))
    conn.commit()


def _repair_event_sequences(conn: sqlite3.Connection, job_id: str | None = None) -> None:
    """Assign missing cursors after published ones; caller holds the write lock."""
    where, params = ("AND job_id = ?", (job_id,)) if job_id is not None else ("", ())
    missing = conn.execute(
        f"SELECT id, job_id FROM media_job_events WHERE sequence IS NULL {where} ORDER BY id", params,
    ).fetchall()
    next_sequences: dict[str, int] = {}
    for event_id, owner_job_id in missing:
        if owner_job_id not in next_sequences:
            next_sequences[owner_job_id] = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM media_job_events WHERE job_id=?", (owner_job_id,),
            ).fetchone()[0]
        next_sequences[owner_job_id] += 1
        conn.execute("UPDATE media_job_events SET sequence=? WHERE id=?",
                     (next_sequences[owner_job_id], event_id))


def init_db(path: Path = DB_PATH) -> None:
    """Check existing data safely, then initialize the schema."""
    with store_startup(path):
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            _apply_pragmas(conn)
            _exec_schema(conn)
        finally:
            conn.close()


@contextmanager
def _conn(path: Path = DB_PATH):
    def open_connection() -> sqlite3.Connection:
        scoped = request_connections.active()
        conn = connect(path, timeout=5.0, check_same_thread=not scoped)
        if scoped:
            conn.row_factory = sqlite3.Row  # prompt memory reads by column name; index access is unchanged
        try:
            _apply_pragmas(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    with request_connections.scoped("media_jobs", path, open_connection) as conn:
        yield conn


def _coerce_to_job(job_or_dict: Any) -> MediaJob:
    if isinstance(job_or_dict, MediaJob):
        return job_or_dict
    if isinstance(job_or_dict, Mapping):
        data = dict(job_or_dict)
        return MediaJob(
            intent=data.get("intent", ""),
            workflow=data.get("workflow"),
            backend=data.get("backend"),
            request_id=data.get("request_id", ""),
            job_id=data.get("job_id") or new_job_id(),
            status=data.get("status", STATUS_QUEUED),
            progress=int(data.get("progress", 0) or 0),
            assets=list(data.get("assets") or []),
            error=data.get("error"),
            created_at=int(data.get("created_at") or _now()),
            updated_at=int(data.get("updated_at") or _now()),
            app_profile=data.get("app_profile"),
            app_id=data.get("app_id"),
            owner_key_hash=data.get("owner_key_hash"),
            api_key_name=data.get("api_key_name"),
            parent_request_id=data.get("parent_request_id"),
            creating_receipt_id=data.get("creating_receipt_id"),
            metadata=dict(data.get("metadata") or {}),
            owner_scope=data.get("owner_scope"),
            input_hash=data.get("input_hash"),
            attempt_number=data.get("attempt_number"),
            previous_attempts=list(data.get("previous_attempts") or []),
            **{name: data.get(name) for name in PHASE_FIELDS},
            **{name: data.get(name) for name in ERROR_FIELDS},
        )
    raise TypeError(f"job must be MediaJob or mapping, got {type(job_or_dict).__name__}")


def _job_payload(job: MediaJob) -> str:
    return json.dumps(job.to_dict("internal"), ensure_ascii=False, sort_keys=True)


def upsert(
    job: MediaJob | Mapping[str, Any],
    *,
    path: Path = DB_PATH,
) -> MediaJob:
    """Insert a job row or update the row with the same ``job_id``.

    Idempotent on ``job_id``: writing the same record twice is a no-op apart
    from updating ``updated_at`` and ``job_json``. A non-empty ``request_id``
    held by a different job raises ``sqlite3.IntegrityError`` and changes
    nothing; callers should look up by ``request_id`` first. An existing
    row's request ID is never restored after it was cleared.
    """
    record = _coerce_to_job(job)
    with _conn(path) as conn:
        _write_job(conn, record)
        conn.commit()
    return record


@dataclass(frozen=True)
class OutstandingLimit:
    """At most ``maximum`` queued or running jobs of one mode (and App)."""

    scope: str
    maximum: int
    mode: str
    app_id: str | None = None


class OutstandingLimitReached(Exception):
    def __init__(self, limit: OutstandingLimit) -> None:
        super().__init__(f"{limit.scope} outstanding job limit reached")
        self.limit = limit


class IdempotencyConflict(ValueError):
    """A request key cannot alias another owner, operation, or input."""

    status_code = 409
    code = "idempotency_conflict"

    def __init__(self) -> None:
        super().__init__("idempotency key conflicts with the owner, operation, or input")


def _input_hash(job: MediaJob, input_data: Mapping[str, Any] | None = None) -> str:
    # Capture immutable submission facts before workers add progress metadata.
    data = {"intent": job.intent, "workflow": job.workflow, "backend": job.backend,
            "metadata": job.metadata, "assets": job.assets}
    if input_data is not None:
        data["input"] = input_data
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _receipt_link_candidate(data: Mapping[str, Any]) -> str | None:
    """Keep an explicit receipt or parent before falling back to a legacy key.

    A fallback is only a lookup candidate: projections must load a real receipt.
    Seeded dedupe fingerprints are never receipts and must remain erasable.
    """
    for name in ("creating_receipt_id", "parent_request_id", "request_id"):
        value = data.get(name)
        if isinstance(value, str) and value and not value.startswith("imgjob_seeded_"):
            return value
    return None


def create(
    job: MediaJob | Mapping[str, Any],
    *,
    outstanding_limits: Iterable[OutstandingLimit] = (),
    input_data: Mapping[str, Any] | None = None,
    path: Path = DB_PATH,
) -> MediaJob:
    """Create a job; replay only matching owner, operation and submission facts.

    A finished job older than ``REPLAY_WINDOW_S`` gives up its request ID to
    the new job instead of replaying.

    This is the safe idempotent create primitive for handlers: clients may
    retry POST /v1/videos with the same Idempotency-Key/request_id and we
    must return the same job_id every time.

    ``outstanding_limits`` are checked in the same write transaction as the
    insert, so concurrent creates cannot overshoot them. A full limit raises
    ``OutstandingLimitReached`` and stores nothing.

    ``input_data`` covers inputs omitted from the safe job metadata. Only its
    digest is stored. Legacy rows and erased digests cannot prove the original
    input; those retries conflict rather than reconstructing proof from metadata.
    """
    record = _coerce_to_job(job)
    # Auth-disabled submissions still record an explicit scope. Missing scope
    # on historical rows must not be mistaken for a verified unscoped request.
    record.owner_scope = stored_owner_key(record.owner_key_hash, app_id=record.app_id) or "unscoped"
    # Never accept a caller-supplied digest as proof that the input matches.
    record.input_hash = _input_hash(record, input_data)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if record.request_id:
            existing = conn.execute(
                "SELECT job_json, status, updated_at, job_id FROM media_jobs WHERE request_id = ?",
                (record.request_id,),
            ).fetchone()
            if existing and not _replays(existing[1], existing[2]):
                retained = json.loads(existing[0])
                retained["creating_receipt_id"] = _receipt_link_candidate(retained)
                retained["request_id"] = ""
                conn.execute(
                    "UPDATE media_jobs SET request_id = NULL, job_json = ? WHERE job_id = ?",
                    (json.dumps(retained, ensure_ascii=False, sort_keys=True), existing[3]),
                )
                existing = None
            if existing:
                data = json.loads(existing[0])
                previous = _coerce_to_job(data)
                if (not previous.owner_scope or not previous.input_hash
                        or previous.owner_scope != record.owner_scope or previous.intent != record.intent
                        or previous.input_hash != record.input_hash):
                    raise IdempotencyConflict()
                conn.rollback()
                return _coerce_to_job(data)
        for limit in outstanding_limits:
            states = sorted(s for s in ALL_STATUSES if holds_resources(s))
            clauses = [f"status IN ({','.join('?' for _ in states)})", "json_extract(job_json, '$.metadata.mode') = ?"]
            params: list[Any] = [*states, limit.mode]
            if limit.app_id is not None:
                clauses.append("json_extract(job_json, '$.app_id') = ?")
                params.append(limit.app_id)
            outstanding = conn.execute(
                f"SELECT COUNT(*) FROM media_jobs WHERE {' AND '.join(clauses)}", params,
            ).fetchone()[0]
            if outstanding >= limit.maximum:
                conn.rollback()
                raise OutstandingLimitReached(limit)
        for name in PHASE_FIELDS:
            setattr(record, name, None)
        record.previous_attempts = []
        observed_at = _now()
        record.accepted_at = observed_at
        record.attempt_number = 1
        if record.status == STATUS_QUEUED:
            record.queued_at = observed_at
        elif record.status == STATUS_RUNNING:
            record.started_at = observed_at
        elif is_terminal(record.status):
            record.finished_at = observed_at
        if record.assets:
            record.first_output_at = observed_at
        _write_job(conn, record)
        conn.commit()
    return record


def _write_job(conn: sqlite3.Connection, job: MediaJob, *, new_attempt: bool = False) -> None:
    job.owner_key_hash = stored_owner_key(job.owner_key_hash, app_id=job.app_id)
    # Serialize cutoff checking with erasure. A worker holding an old job
    # object cannot put a forgotten prompt fingerprint back into the database.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    previous = conn.execute("SELECT job_json, request_id FROM media_jobs WHERE job_id=?", (job.job_id,)).fetchone()
    job.creating_receipt_id = _receipt_link_candidate(
        json.loads(previous[0]) if previous else job.to_dict("internal")
    )
    if previous:
        # A stored request ID only changes by being cleared (replay release or
        # erasure). A stale caller copy must not restore or re-point it.
        if job.request_id and job.request_id != previous[1]:
            job.request_id = previous[1] or ""
        data = json.loads(previous[0])
        preserved = ("accepted_at",) if new_attempt else (*PHASE_FIELDS, "attempt_number", "previous_attempts")
        stale_attempt = data.get("attempt_number") != job.attempt_number
        for name in preserved:
            if name in data and (data[name] is not None or (stale_attempt and not new_attempt)):
                setattr(job, name, data[name])
    cutoff = conn.execute(
        "SELECT MAX(cutoff_at) FROM media_job_prompt_erasure_cutoffs WHERE owner_key IN (?,?)",
        ("*", job.owner_key_hash),
    ).fetchone()[0]
    if cutoff is not None and job.created_at <= cutoff:
        _clear_prompt_derivatives(job)
    # Update by job_id only. A request ID held by another job raises
    # IntegrityError instead of deleting that job, as INSERT OR REPLACE did.
    conn.execute(
        "INSERT INTO media_jobs "
        "(job_id, request_id, intent, workflow, backend, status, progress, error, "
        " created_at, updated_at, app_profile, api_key_name, parent_request_id, job_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET request_id=excluded.request_id, intent=excluded.intent, "
        "workflow=excluded.workflow, backend=excluded.backend, status=excluded.status, "
        "progress=excluded.progress, error=excluded.error, created_at=excluded.created_at, "
        "updated_at=excluded.updated_at, app_profile=excluded.app_profile, "
        "api_key_name=excluded.api_key_name, parent_request_id=excluded.parent_request_id, "
        "job_json=excluded.job_json",
        (
            job.job_id,
            job.request_id or None,
            job.intent,
            job.workflow,
            job.backend,
            job.status,
            job.progress,
            job.error,
            job.created_at,
            job.updated_at,
            job.app_profile,
            job.api_key_name,
            job.parent_request_id,
            _job_payload(job),
        ),
    )


def _clear_prompt_derivatives(job: MediaJob) -> None:
    job.metadata.pop("request_hash", None)
    job.input_hash = None
    if job.request_id.startswith("imgjob_seeded_"):
        job.request_id = ""
    if (job.parent_request_id or "").startswith("imgjob_seeded_"):
        job.parent_request_id = None


def erase_prompt_derivatives(owner_keys: set[str], *, before: int, path: Path = DB_PATH) -> int:
    """Forget body hashes and seeded dedupe IDs, retaining the jobs and media.

    Commit retry protection first, then rewrite matching rows in batches. All
    later writes check the same cutoffs, including jobs not yet inserted when
    deletion began. App ownership is stable; the scope includes legacy jobs
    without a recorded maker. A household kill uses the wildcard owner key.
    """
    if not owner_keys:
        return 0
    count = 0
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Account removal can precede the first media-job store initialization.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS media_job_prompt_erasure_cutoffs ("
            "owner_key TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO media_job_prompt_erasure_cutoffs(owner_key,cutoff_at) VALUES(?,?) "
            "ON CONFLICT(owner_key) DO UPDATE SET cutoff_at=MAX(cutoff_at,excluded.cutoff_at)",
            [(key, int(before)) for key in owner_keys],
        )
        conn.commit()
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_jobs'").fetchone() is None:
            return 0
        cursor = ""
        while True:
            rows = conn.execute(
                "SELECT job_id, job_json FROM media_jobs WHERE job_id>? AND created_at<=? "
                "ORDER BY job_id LIMIT 100", (cursor, int(before)),
            ).fetchall()
            if not rows:
                break
            ids = []
            for job_id, payload in rows:
                data = _safe_loads(payload)
                if not isinstance(data, Mapping):
                    continue
                if (not data.get("input_hash") and "request_hash" not in (data.get("metadata") or {})
                        and not str(data.get("request_id") or "").startswith("imgjob_seeded_")
                        and not str(data.get("parent_request_id") or "").startswith("imgjob_seeded_")):
                    continue
                owner = stored_owner_key(data.get("owner_key_hash"), app_id=data.get("app_id"))
                if "*" in owner_keys or owner in owner_keys:
                    ids.append(job_id)
            if ids:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for job_id in ids:
                        # Re-read under the lock to retain concurrent progress.
                        row = conn.execute("SELECT job_json FROM media_jobs WHERE job_id=?", (job_id,)).fetchone()
                        if row is None:
                            continue
                        job = _coerce_to_job(json.loads(row[0]))
                        _write_job(conn, job)
                        count += 1
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            cursor = rows[-1][0]
    return count


def get(
    job_id: str,
    *,
    audience: str = "internal",
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Return the stored job filtered for ``audience``, or None."""
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT job_json FROM media_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if not row:
        return None
    data = _safe_loads(row[0])
    if not isinstance(data, Mapping):
        return None
    return _filter_for_audience(data, audience)


def get_many(
    job_ids: Iterable[str],
    *,
    audience: str = "internal",
    path: Path = DB_PATH,
) -> dict[str, dict[str, Any]]:
    """Return ``{job_id: job}`` as ``get`` would, in one query per 500 ids."""
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    ids = sorted({value for value in job_ids if value})
    out: dict[str, dict[str, Any]] = {}
    with _conn(path) as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = conn.execute(
                f"SELECT job_id, job_json FROM media_jobs WHERE job_id IN ({','.join('?' for _ in chunk)})",
                chunk,
            ).fetchall()
            for job_id, raw in rows:
                data = _safe_loads(raw)
                if isinstance(data, Mapping):
                    out[job_id] = _filter_for_audience(data, audience)
    return out


def get_by_request_id(
    request_id: str,
    *,
    audience: str = "internal",
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Return the job this request ID still replays; see ``REPLAY_WINDOW_S``."""
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    if not request_id:
        return None
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT job_json, status, updated_at FROM media_jobs WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    if not row or not _replays(row[1], row[2]):
        return None
    data = _safe_loads(row[0])
    if not isinstance(data, Mapping):
        return None
    return _filter_for_audience(data, audience)


def recent(
    limit: int = 50,
    *,
    audience: str = "internal",
    app_profile: str | None = None,
    app_ids: list[str] | None = None,
    owner_key_hashes: list[str] | None = None,
    status: str | None = None,
    since_s: int | None = None,
    updated_since: int | None = None,
    include_active: bool = False,
    before: tuple[int, str] | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` most-recently-updated jobs, newest first."""
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    if limit <= 0:
        return []
    if app_ids is not None or owner_key_hashes is not None:
        if not app_ids and not owner_key_hashes:
            return []
    clauses: list[str] = []
    params: list[Any] = []
    if app_ids is not None or owner_key_hashes is not None:
        ownership = []
        for field, values in (("app_id", app_ids), ("owner_key_hash", owner_key_hashes)):
            if values:
                ownership.append(f"json_extract(job_json, '$.{field}') IN ({','.join('?' for _ in values)})")
                params.extend(values)
        clauses.append("(" + " OR ".join(ownership) + ")")
    if app_profile is not None:
        clauses.append("app_profile = ?")
        params.append(app_profile)
    if status is not None:
        if status not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        clauses.append("status = ?")
        params.append(status)
    if since_s is not None:
        clauses.append("updated_at >= ?")
        params.append(int(time.time()) - int(since_s))
    if updated_since is not None:
        if include_active:
            clauses.append("(updated_at >= ? OR status IN (?, ?))")
            params.extend((updated_since, STATUS_QUEUED, STATUS_RUNNING))
        else:
            clauses.append("updated_at >= ?")
            params.append(updated_since)
    if before is not None:
        clauses.append("(updated_at, job_id) < (?, ?)")
        params.extend(before)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with _conn(path) as conn:
        rows = conn.execute(
            f"SELECT job_json FROM media_jobs{where} "
            "ORDER BY updated_at DESC, job_id DESC LIMIT ?",
            params,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for (job_json,) in rows:
        data = _safe_loads(job_json)
        if not isinstance(data, Mapping):
            continue
        out.append(_filter_for_audience(data, audience))
    return out


def list_jobs(*args, **kwargs):
    """Alias for ``recent`` so call-sites can read more naturally."""
    return recent(*args, **kwargs)


def validate_error_info(info: Mapping[str, Any]) -> dict[str, Any]:
    """Return the typed failure facts, or raise ValueError.

    Only known fields; a known category; a plain code token, never free text
    (which could carry a prompt); an HTTP status only for an upstream failure.
    """
    unknown = set(info) - set(ERROR_FIELDS)
    if unknown:
        raise ValueError(f"unknown error fields: {sorted(unknown)}")
    category = info.get("error_category")
    if category not in ERROR_CATEGORIES:
        raise ValueError(f"error_category must be one of {sorted(ERROR_CATEGORIES)}")
    code = info.get("error_code")
    if not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code):
        raise ValueError("error_code must be a short lowercase token")
    retryable = info.get("retryable")
    if not isinstance(retryable, bool):
        raise ValueError("retryable must be a boolean")
    upstream = info.get("upstream_http_status")
    if upstream is not None and (category != "upstream" or type(upstream) is not int
                                 or not 100 <= upstream <= 599):
        raise ValueError("upstream_http_status must be an HTTP status of an upstream failure")
    return {"error_code": code, "error_category": category, "retryable": retryable,
            "upstream_http_status": upstream}


def update_status(
    job_id: str,
    status: str,
    *,
    progress: int | None = None,
    error: str | None = None,
    error_info: Mapping[str, Any] | None = None,
    add_assets: Iterable[Mapping[str, Any]] | None = None,
    metadata_patch: Mapping[str, Any] | None = None,
    expected_status: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Apply a status transition. Returns the updated internal job dict.

    ``error_info`` holds the typed failure facts (``ERROR_FIELDS``) that go
    with ``error``; ``validate_error_info`` checks them.

    Rejects illegal transitions (e.g. succeeded -> running). Same-state writes
    are accepted so progress updates during ``running`` are idempotent. With
    ``expected_status``, the write applies only if the job is still in that
    status; otherwise it raises ``ValueError``.
    """
    if status not in ALL_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
    if progress is not None:
        progress = _clamp_progress(progress)
    if metadata_patch is not None:
        _assert_safe_mapping("metadata_patch", metadata_patch)
    error_facts = validate_error_info(error_info) if error_info is not None else None
    assets_list: list[dict[str, Any]] = []
    if add_assets is not None:
        for asset in add_assets:
            if not isinstance(asset, Mapping):
                raise ValueError("add_assets entries must be mappings")
            _assert_safe_mapping("add_assets[*]", asset)
            assets_list.append(dict(asset))
    ts = int(now if now is not None else _now())

    with _conn(path) as conn:
        # Read and write in one transaction so a concurrent transition cannot
        # slip between them.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT job_json FROM media_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            return None
        data = _safe_loads(row[0])
        if not isinstance(data, Mapping):
            return None
        current = dict(data)
        prev_status = current.get("status", STATUS_QUEUED)
        if expected_status is not None and prev_status != expected_status:
            raise ValueError(f"job {job_id!r} is {prev_status!r}, not {expected_status!r}")
        if status not in _ALLOWED_TRANSITIONS.get(prev_status, frozenset()):
            raise ValueError(
                f"illegal status transition {prev_status!r} -> {status!r} for job {job_id!r}"
            )
        if (status == STATUS_SUCCEEDED
                and (current.get("metadata") or {}).get("cancel_requested_at")):
            raise CancellationPending("Cancel requested; waiting for resources to be released")
        current["status"] = status
        if progress is not None:
            current["progress"] = progress
        elif status == STATUS_SUCCEEDED:
            current["progress"] = 100
        if error is not None:
            current["error"] = error
        if error_facts is not None:
            current.update(error_facts)
        if assets_list:
            existing_assets = list(current.get("assets") or [])
            existing_assets.extend(assets_list)
            current["assets"] = existing_assets
        if metadata_patch:
            existing_meta = dict(current.get("metadata") or {})
            existing_meta.update(metadata_patch)
            current["metadata"] = existing_meta
        current["updated_at"] = ts
        # Legacy attempts remain unavailable; do not invent prior phase facts.
        if current.get("attempt_number") is not None:
            if status == STATUS_RUNNING and current.get("started_at") is None:
                current["started_at"] = ts
            if assets_list and current.get("first_output_at") is None:
                current["first_output_at"] = ts
            if is_terminal(status) and current.get("finished_at") is None:
                current["finished_at"] = ts

        updated = _coerce_to_job(current)
        _write_job(conn, updated)
        conn.commit()
    return updated.to_dict("internal")


class CancellationPending(ValueError):
    """Output finished after cancellation; the worker must release before settling."""


def request_cancel(job_id: str, *, reason: str | None = None, path: Path = DB_PATH) -> dict[str, Any] | None:
    """Cancel unstarted work atomically, or retain running work's reservation."""
    job = get(job_id, path=path)
    if job is None:
        return None
    if is_terminal(job["status"]):
        raise ValueError("This job has already finished")
    try:
        return update_status(job_id, STATUS_CANCELED, expected_status=STATUS_QUEUED,
                             error=reason or "Canceled by requester", path=path)
    except ValueError:
        return update_status(job_id, STATUS_RUNNING, expected_status=STATUS_RUNNING,
                             metadata_patch={"cancel_requested_at": _now()}, path=path)


def active_count(*, app_id: str, intents: Iterable[str], path: Path = DB_PATH) -> int:
    """Count this app's queued/running work without a history scan limit."""
    intents = tuple(intents)
    with _conn(path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM media_jobs WHERE status IN (?, ?) "
            "AND json_extract(job_json, '$.app_id') = ? "
            f"AND intent IN ({','.join('?' for _ in intents)})",
            (STATUS_QUEUED, STATUS_RUNNING, app_id, *intents),
        ).fetchone()[0]


def unfinished(*, path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Startup snapshot, without the history endpoint's page-size ceiling."""
    with _conn(path) as conn:
        rows = conn.execute("SELECT job_json FROM media_jobs WHERE status IN (?, ?)",
                            (STATUS_QUEUED, STATUS_RUNNING)).fetchall()
    return [data for (raw,) in rows if isinstance(data := _safe_loads(raw), dict)]


def begin_attempt(job_id: str, *, now: int | None = None, path: Path = DB_PATH) -> dict[str, Any] | None:
    """Trusted worker retry: retain prior facts and start a new running attempt.

    Only call when dispatching work again, never on a progress update or a
    reconnect to the same upstream job. Legacy attempt numbers stay unknown.
    """
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT job_json FROM media_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        job = _coerce_to_job(json.loads(row[0]))
        if job.status != STATUS_RUNNING:
            raise ValueError("only a running job can begin another attempt")
        if job.attempt_number is None:
            return job.to_dict()
        job.previous_attempts.append({"attempt_number": job.attempt_number,
                                      **{name: getattr(job, name) for name in PHASE_FIELDS}})
        job.attempt_number += 1
        job.queued_at = None
        job.started_at = int(now if now is not None else _now())
        job.first_output_at = None
        job.finished_at = None
        job.updated_at = job.started_at
        _write_job(conn, job, new_attempt=True)
        conn.commit()
        return job.to_dict()


def append_event(
    job_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    now: int | None = None,
    path: Path = DB_PATH,
    _source: Literal["gateway", "client"] = "gateway",
) -> int:
    """Trusted adapter event write; returns a per-job cursor, never a row id.

    Event payloads are metadata-only. They must not carry raw prompts, image
    bytes, request/response bodies, or secrets — the safety guard rejects
    those shapes before they reach disk.
    """
    if not event_type or not isinstance(event_type, str):
        raise ValueError("event_type must be a non-empty string")
    safe_payload = dict(payload or {})
    _assert_safe_mapping("event payload", safe_payload)
    ts = int(now if now is not None else _now())
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT job_json FROM media_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            raise LookupError(f"unknown job_id {job_id!r}")
        attempt_number = json.loads(row[0]).get("attempt_number") if _source == "gateway" else None
        _repair_event_sequences(conn, job_id)
        sequence = conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM media_job_events WHERE job_id=?", (job_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO media_job_events(job_id, created_at, event_type, payload_json, sequence, source, attempt_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, ts, event_type, json.dumps(safe_payload, ensure_ascii=False, sort_keys=True), sequence, _source, attempt_number),
        )
        conn.commit()
        return int(sequence)


def validate_client_event(event_type: str, payload: Mapping[str, Any] | None) -> None:
    """Client notes cannot claim lifecycle authority, even for admin audiences."""
    if event_type not in {"progress", "note"}:
        raise ValueError("client event_type must be progress or note; lifecycle events require a trusted writer")
    if payload and {"phase", "status"}.intersection(payload):
        raise ValueError("client notes cannot set phase or status")
    for name in ("progress", "step", "total"):
        value = (payload or {}).get(name)
        if value is not None and (type(value) is not int or value < 0 or (name == "progress" and value > 100)):
            raise ValueError(f"{name} must be a non-negative integer" + (" up to 100" if name == "progress" else ""))


def append_client_event(job_id: str, event_type: str, payload: Mapping[str, Any] | None = None,
                        *, path: Path = DB_PATH) -> int:
    validate_client_event(event_type, payload)
    return append_event(job_id, event_type, payload, path=path, _source="client")


def events(
    job_id: str,
    *,
    audience: str = "internal",
    limit: int = 200,
    after_id: int | None = None,
    after_sequence: int | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return events using either a legacy global id or a per-job sequence.

    ``after_id`` retains its pre-migration global-row meaning and ordering.
    New clients use ``after_sequence`` with the response's ``sequence`` field.
    Repair appends missing sequences after published ones without renumbering.

    Audience filtering for events:

    - ``internal``: full payload included.
    - ``admin``: type and timestamp; payload omitted by default.
    - ``client``: type, timestamp, and minimal progress fields if present.
      A small allowlist (``progress``, ``step``, ``total``, ``phase``) lets a
      polling client see incremental progress without learning backend
      breadcrumbs.
    """
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    if after_id is not None and after_sequence is not None:
        raise ValueError("use either after_id or after_sequence, not both")
    if limit <= 0:
        return []
    clauses = ["job_id = ?"]
    params: list[Any] = [job_id]
    if after_id is not None:
        clauses.append("id > ?")
        params.append(int(after_id))
    if after_sequence is not None:
        clauses.append("sequence > ?")
        params.append(int(after_sequence))
    params.append(int(limit))
    where = " AND ".join(clauses)
    with _conn(path) as conn:
        # Repair stores already damaged by a mixed-version writer, including
        # readers on an open store that has not run initialization again.
        if conn.execute("SELECT 1 FROM media_job_events WHERE job_id=? AND sequence IS NULL LIMIT 1",
                        (job_id,)).fetchone():
            conn.execute("BEGIN IMMEDIATE")
            _repair_event_sequences(conn, job_id)
            conn.commit()
        order = "sequence" if after_sequence is not None else "id"
        rows = conn.execute(
            f"SELECT id, sequence, created_at, event_type, payload_json, source, attempt_number FROM media_job_events "
            f"WHERE {where} ORDER BY {order} ASC LIMIT ?",
            params,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for ev_id, sequence, created_at, event_type, payload_json, source, attempt_number in rows:
        payload = _safe_loads(payload_json) or {}
        out.append(_filter_event_for_audience(
            {
                "id": int(ev_id),
                "sequence": int(sequence),
                "job_id": job_id,
                "created_at": int(created_at),
                "event_type": event_type,
                "source": source,
                "attempt_number": attempt_number,
                "payload": payload if isinstance(payload, Mapping) else {},
            },
            audience,
        ))
    return out


_CLIENT_EVENT_PAYLOAD_KEYS = frozenset({"progress", "step", "total", "phase", "status"})


def _filter_event_for_audience(event: Mapping[str, Any], audience: str) -> dict[str, Any]:
    base = {
        "id": event.get("id"),
        "sequence": event.get("sequence"),
        "job_id": event.get("job_id"),
        "created_at": event.get("created_at"),
        "event_type": event.get("event_type"),
        "source": event.get("source", "unavailable"),
        "attempt_number": event.get("attempt_number"),
    }
    if audience == "internal":
        return {**base, "payload": dict(event.get("payload") or {})}
    if audience == "admin":
        # Admin sees that an event happened but not the freeform breadcrumb
        # bag. Operators who need the full payload bump to internal.
        return base
    # The global id is a household-wide counter: gaps between one caller's
    # ids reveal other people's activity, so callers page by per-job sequence.
    base.pop("id")
    payload = event.get("payload") or {}
    if not isinstance(payload, Mapping):
        return base
    filtered = {k: payload[k] for k in _CLIENT_EVENT_PAYLOAD_KEYS if k in payload}
    out = dict(base)
    if filtered:
        out["payload"] = filtered
    return out


def count(path: Path = DB_PATH) -> int:
    with _conn(path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()
        return int(row[0]) if row else 0


def schema_version(path: Path = DB_PATH) -> int | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT version FROM media_jobs_schema_version LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None


def _safe_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


__all__ = [
    "DB_PATH",
    "REPLAY_WINDOW_S",
    "SCHEMA_VERSION",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "STATUS_FAILED",
    "STATUS_CANCELED",
    "ALL_STATUSES",
    "TERMINAL_STATUSES",
    "JobStatus",
    "PHASE_FIELDS",
    "ERROR_FIELDS",
    "ERROR_CATEGORIES",
    "validate_error_info",
    "is_terminal",
    "holds_resources",
    "IdempotencyConflict",
    "CLIENT_VISIBLE_FIELDS",
    "ADMIN_ONLY_FIELDS",
    "INTERNAL_ONLY_FIELDS",
    "MediaJob",
    "new_job_id",
    "init_db",
    "create",
    "upsert",
    "get",
    "get_by_request_id",
    "recent",
    "list_jobs",
    "update_status",
    "begin_attempt",
    "append_event",
    "append_client_event",
    "validate_client_event",
    "events",
    "count",
    "schema_version",
]
