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

from gateway import job_wake, media_input_leases, request_connections, request_source
from gateway.compat import get_env
from gateway.sqlite_startup import connect, store_startup

import json
import logging
import re
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping
from uuid import uuid4
from gateway.ownership import stored_owner_key
from gateway.job_envelope import JobEnvelope

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

SCHEMA_VERSION = 6

JobStatus = Literal["queued", "running", "succeeded", "failed", "canceled"]
PHASE_FIELDS = ("accepted_at", "queued_at", "started_at", "first_output_at", "finished_at")
# Typed facts about a failure, beside the plain ``error`` sentence. The category
# says who refused: the gateway (authorization, invalid_input, capacity) or the
# work itself (upstream, timeout, safety); ``internal`` is the gateway's own bug. ``upstream_http_status`` is set only
# for an upstream failure. None means not recorded (older or unclassified jobs).
# The job metadata key, and the read-time field, that hold when an unreachable engine last answered (the failure code
# ``engine_unavailable``). The field is None unless that code is set and the gateway's health record knew a time.
LAST_HEALTHY_KEY = "last_healthy_at"
ERROR_FIELDS = ("error_code", "error_category", "retryable", "upstream_http_status")
ERROR_CATEGORIES = frozenset({"authorization", "invalid_input", "upstream", "capacity", "timeout", "safety",
                              "internal"})
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
        "queue_position",
        LAST_HEALTHY_KEY,
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
INTERNAL_ONLY_FIELDS = frozenset({
    "metadata", "owner_key_hash", "owner_scope", "input_hash", "creating_receipt_id",
    "maker_person_id", "maker_credential_id", "maker_connection_id",
})

# Metadata/event keys that smell like raw payloads, secrets, or media bytes.
# Sourced from ``gateway.redaction`` so receipts and media jobs share one
# vocabulary. Re-exported under the historical name for external callers
# (the test suite asserts the two names refer to the same frozenset).
_FORBIDDEN_KEYS = _SHARED_FORBIDDEN_KEYS  # noqa: F841 — re-exported for backward compat


def new_job_id() -> str:
    return f"mj_{uuid4().hex}"


_JOB_ID_FORM = re.compile(r"^mj_[0-9a-f]{32}$")


def is_job_id(raw: str) -> bool:
    """True when ``raw`` has the form ``new_job_id`` makes. Whether such a job exists, was deleted or was reaped does not
    change what an id of this form names: the job store."""
    return isinstance(raw, str) and _JOB_ID_FORM.match(raw) is not None


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
    # Who made the job, stamped once at creation from the authenticated request (never from the body or a parent
    # link). Reads authorize from these alone. A job with none of them has no maker and no reader.
    maker_person_id: str | None = None
    maker_credential_id: str | None = None
    maker_connection_id: str | None = None
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
            "maker_person_id": self.maker_person_id,
            "maker_credential_id": self.maker_credential_id,
            "maker_connection_id": self.maker_connection_id,
            "metadata": dict(self.metadata),
            "owner_scope": self.owner_scope,
            "input_hash": self.input_hash,
            "attempt_number": self.attempt_number,
            "previous_attempts": [dict(attempt) for attempt in self.previous_attempts],
            **{name: getattr(self, name) for name in PHASE_FIELDS},
            **{name: getattr(self, name) for name in ERROR_FIELDS},
        }
        return _filter_for_audience(data, audience)


_log = logging.getLogger(__name__)


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
    data = {**dict.fromkeys((*PHASE_FIELDS, *ERROR_FIELDS, "attempt_number", "queue_position", LAST_HEALTHY_KEY)), **data}
    data.setdefault("previous_attempts", [])
    data["cancel_requested"] = (
        holds_resources(data.get("status", STATUS_QUEUED))
        and bool((data.get("metadata") or {}).get("cancel_requested_at"))
    )
    if audience == "internal":
        return dict(data)
    # OWNER_VISIBILITY_STEP3: admin job content needs Own or a closed owner operational projection.
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

CREATE TABLE IF NOT EXISTS media_hidden_assets (
  person_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  hidden_at INTEGER NOT NULL,
  PRIMARY KEY (person_id, asset_id, job_id)
);
CREATE TABLE IF NOT EXISTS media_job_asset_origins (
  asset_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  origin_id TEXT NOT NULL,
  PRIMARY KEY (asset_id, job_id),
  FOREIGN KEY (job_id) REFERENCES media_jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS media_job_asset_origins_asset
  ON media_job_asset_origins(asset_id, origin_id);
CREATE TABLE IF NOT EXISTS media_library_erasure_fences (
  person_id TEXT NOT NULL,
  origin_id TEXT NOT NULL,
  erased_at INTEGER NOT NULL,
  PRIMARY KEY (person_id, origin_id)
);
CREATE TABLE IF NOT EXISTS media_jobs_schema_version (version INTEGER NOT NULL);

-- Facts are immutable metadata. References can outlive receipts when a kept record survives.
CREATE TABLE IF NOT EXISTS retention_fact_revisions (
  revision TEXT PRIMARY KEY,
  fact_json TEXT NOT NULL,
  first_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS retention_fact_references (
  source_kind TEXT NOT NULL CHECK(source_kind IN ('receipt', 'kept_record')),
  source_id TEXT NOT NULL,
  destination_id TEXT NOT NULL,
  revision TEXT NOT NULL REFERENCES retention_fact_revisions(revision),
  PRIMARY KEY(source_kind, source_id, destination_id, revision)
);
CREATE INDEX IF NOT EXISTS retention_fact_references_revision
  ON retention_fact_references(revision);
CREATE TABLE IF NOT EXISTS retention_fact_destinations (
  source_kind TEXT NOT NULL CHECK(source_kind IN ('receipt', 'kept_record')),
  source_id TEXT NOT NULL,
  destination_id TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(source_kind, source_id, destination_id)
);

-- Pinned definitions and each job's envelope (gateway/definition_store.py owns the rules). They live here so a later
-- change can write or delete an envelope in the same transaction as the job row.
CREATE TABLE IF NOT EXISTS definition_blob (
  sha TEXT PRIMARY KEY,
  body BLOB NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS definition_ref (
  blob_sha TEXT NOT NULL,
  job_id TEXT NOT NULL,
  holder_kind TEXT NOT NULL CHECK(holder_kind IN ('job', 'holder')),
  PRIMARY KEY(blob_sha, job_id)
);
CREATE INDEX IF NOT EXISTS definition_ref_job ON definition_ref(job_id);
CREATE TABLE IF NOT EXISTS job_envelope (
  job_id TEXT PRIMARY KEY,
  envelope_json TEXT NOT NULL,
  created REAL NOT NULL
);

-- Named results of a job: staged privately, then published with the job's terminal state (gateway/result_bundle.py owns
-- the rules). They live here so every path that deletes or erases a job row can release them in the same transaction.
CREATE TABLE IF NOT EXISTS result_stage (
  job_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  name TEXT NOT NULL,
  ref TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('staged', 'published', 'aborted')),
  kind TEXT NOT NULL CHECK(kind IN ('json', 'assets')),
  count INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  sha256 TEXT,
  items_json TEXT,
  created REAL NOT NULL,
  PRIMARY KEY(job_id, attempt, name)
);
CREATE INDEX IF NOT EXISTS result_stage_state ON result_stage(state, created);
CREATE TABLE IF NOT EXISTS result_json (
  ref TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  owner_scope TEXT NOT NULL,
  maker_person_id TEXT,
  maker_credential_id TEXT,
  maker_connection_id TEXT,
  value BLOB NOT NULL,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS result_json_job ON result_json(job_id);
CREATE TABLE IF NOT EXISTS result_bundle (
  job_id TEXT PRIMARY KEY,
  attempt INTEGER NOT NULL,
  bundle_ref TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  published_at REAL NOT NULL,
  receipt_linked_at REAL
);
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
    conn.executescript(media_input_leases.DDL)  # leases and ingest markers (gateway/media_input_leases.py)
    conn.executescript(RUN_CLAIM_DDL)
    conn.executescript(TOMBSTONE_DDL)
    row = conn.execute("SELECT version FROM media_jobs_schema_version LIMIT 1").fetchone()
    previous_version = int(row[0]) if row is not None else None
    if row is None:
        conn.execute(
            "INSERT INTO media_jobs_schema_version(version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
    elif previous_version not in (1, 2, 3, 4, 5, SCHEMA_VERSION):
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
    destination_columns = {r[1] for r in conn.execute("PRAGMA table_info(retention_fact_destinations)")}
    if "created_at" not in destination_columns:
        conn.execute("ALTER TABLE retention_fact_destinations ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0")
    # Version 6: the maker stamp. Rows made before it keep NULL, which means no maker and no reader (fail closed).
    job_columns = {r[1] for r in conn.execute("PRAGMA table_info(media_jobs)")}
    for column in ("maker_person_id", "maker_credential_id", "maker_connection_id"):
        if column not in job_columns:
            conn.execute(f"ALTER TABLE media_jobs ADD COLUMN {column} TEXT")
    # Read a person's jobs by their maker stamp as a range, not a table scan. Created after the columns exist.
    conn.execute("CREATE INDEX IF NOT EXISTS media_jobs_maker_person ON media_jobs(maker_person_id, updated_at, job_id)")
    if previous_version is None or previous_version < SCHEMA_VERSION:
        _backfill_asset_origins(conn)
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


def _asset_ids(job_data: Mapping[str, Any]) -> set[str]:
    assets = job_data.get("assets")
    if not isinstance(assets, list):
        return set()
    return {
        str(asset_id)
        for asset in assets if isinstance(asset, Mapping)
        for asset_id in (asset.get("asset_id") or asset.get("id"),)
        if asset_id
    }


def _backfill_asset_origins(conn: sqlite3.Connection) -> None:
    """Build the indexed asset-to-creating-request relation for an older store."""
    pending: list[tuple[str, str, str]] = []
    for job_id, job_json in conn.execute("SELECT job_id, job_json FROM media_jobs"):
        try:
            data = json.loads(job_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, Mapping):
            continue
        origin_id = _receipt_link_candidate(data)
        if not origin_id:
            continue
        pending.extend((asset_id, str(job_id), origin_id) for asset_id in _asset_ids(data))
        if len(pending) >= 1000:
            conn.executemany(
                "INSERT OR IGNORE INTO media_job_asset_origins(asset_id,job_id,origin_id) VALUES(?,?,?)",
                pending,
            )
            pending.clear()
    if pending:
        conn.executemany(
            "INSERT OR IGNORE INTO media_job_asset_origins(asset_id,job_id,origin_id) VALUES(?,?,?)",
            pending,
        )


def connection(path: Path = DB_PATH):
    """A connection to the jobs database with its pragmas, for a sibling store that shares its transactions.

    Same as the connection the job functions use: ``secure_delete`` is on, so deleted rows are overwritten.
    """
    return _conn(path)


def init_db(path: Path = DB_PATH) -> None:
    """Check existing data safely, then initialize the schema."""
    with store_startup(path):
        from gateway import private_io
        private_io.prepare_private_database(path)
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            _apply_pragmas(conn)
            _exec_schema(conn)
        finally:
            conn.close()


@contextmanager
def _conn(path: Path = DB_PATH, *, timeout: float = 5.0):
    def open_connection() -> sqlite3.Connection:
        scoped = request_connections.active()
        conn = connect(path, timeout=timeout, check_same_thread=not scoped)
        if scoped:
            conn.row_factory = sqlite3.Row  # prompt memory reads by column name; index access is unchanged
        try:
            _apply_pragmas(conn)
            conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout * 1000))}")
        except BaseException:
            conn.close()
            raise
        return conn

    namespace = "media_jobs" if timeout == 5.0 else "media_jobs_nowait"
    with request_connections.scoped(namespace, path, open_connection) as conn:
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
            maker_person_id=data.get("maker_person_id"),
            maker_credential_id=data.get("maker_credential_id"),
            maker_connection_id=data.get("maker_connection_id"),
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
    maker_from_request: bool = True,
) -> MediaJob:
    """Insert a job row or update the row with the same ``job_id``.

    Idempotent on ``job_id``: writing the same record twice is a no-op apart
    from updating ``updated_at`` and ``job_json``. A non-empty ``request_id``
    held by a different job raises ``sqlite3.IntegrityError`` and changes
    nothing; callers should look up by ``request_id`` first. An existing
    row's request ID is never restored after it was cleared.

    The maker follows ``create``: a job written under an authenticated request takes that request's maker, and
    ``maker_from_request=False`` keeps the maker the job carries. A row that already exists keeps the maker it was
    stamped with, whoever writes it again.
    """
    record = _coerce_to_job(job)
    if maker_from_request:
        record.maker_person_id, record.maker_credential_id, record.maker_connection_id = (
            request_source.job_maker(record.app_id))
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


class EnvelopeNotRecorded(RuntimeError):
    """``create(require_envelope=True)`` could not record the job's envelope; nothing was stored."""


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


RUN_CLAIM_DDL = """CREATE TABLE IF NOT EXISTS workflow_run_claim (
  owner_scope TEXT NOT NULL,
  maker TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  identity TEXT NOT NULL,
  holder TEXT NOT NULL,
  job_id TEXT,
  claimed_at REAL NOT NULL,
  PRIMARY KEY (owner_scope, maker, idem_key)
);
CREATE INDEX IF NOT EXISTS workflow_run_claim_job ON workflow_run_claim(job_id)"""


@dataclass(frozen=True)
class RunClaimFence:
    """The claim a fenced ``create`` must still hold: it is checked, and then set to name the job, inside the write
    transaction that inserts the job (CODE_TO_DATA_S4_S8 section 7.2)."""

    owner_scope: str
    maker: str
    key: str
    holder: str


@dataclass(frozen=True)
class RunClaim:
    """What ``claim_run_key`` found. ``held``: this holder owns an undecided claim. ``replay``: the key names ``job_id``.
    ``conflict``: another identity owns the key. ``busy``: the same identity is held by a live holder."""

    outcome: Literal["held", "replay", "conflict", "busy"]
    job_id: str | None = None


class ClaimLost(RuntimeError):
    """The fenced ``create`` found the claim taken over or decided. Nothing was drawn, adopted or stored."""


def claim_run_key(
    *, owner_scope: str, maker: str, key: str, identity: str, holder: str, lease_s: float,
    now: float | None = None, path: Path = DB_PATH,
) -> RunClaim:
    """Claim an idempotency key in one write transaction (section 7.2, step 1). A claim that names a job that is gone
    or past the replay window is stale and is replaced. An undecided claim older than ``lease_s`` is taken over by
    rewriting its ``holder``; the old holder's fence then matches nothing."""
    stamp = time.time() if now is None else now
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT identity, holder, job_id, claimed_at FROM workflow_run_claim "
                "WHERE owner_scope=? AND maker=? AND idem_key=?", (owner_scope, maker, key)).fetchone()
            if row is None:
                conn.execute("INSERT INTO workflow_run_claim VALUES (?,?,?,?,?,NULL,?)",
                             (owner_scope, maker, key, identity, holder, stamp))
                conn.commit()
                return RunClaim("held")
            known_identity, old_holder, job_id, claimed_at = row
            take_over = False
            if job_id is not None:
                job = conn.execute("SELECT status, updated_at FROM media_jobs WHERE job_id=?", (job_id,)).fetchone()
                if job is None or not _replays(job[0], job[1]):
                    take_over = True  # the job is gone or past the replay window: the key is free again
                elif known_identity != identity:
                    conn.rollback()
                    return RunClaim("conflict")
                else:
                    conn.rollback()
                    return RunClaim("replay", job_id)
            else:
                expired = stamp - claimed_at > lease_s
                if known_identity != identity and not expired:
                    conn.rollback()
                    return RunClaim("conflict")
                if not expired:
                    conn.rollback()
                    return RunClaim("busy")
                take_over = True
            if take_over:
                moved = conn.execute(
                    "UPDATE workflow_run_claim SET identity=?, holder=?, job_id=NULL, claimed_at=? "
                    "WHERE owner_scope=? AND maker=? AND idem_key=? AND holder=? AND job_id IS ?",
                    (identity, holder, stamp, owner_scope, maker, key, old_holder, job_id)).rowcount
                if moved != 1:
                    conn.rollback()
                    return RunClaim("busy")
            conn.commit()
            return RunClaim("held")
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise


def get_run_claim(*, owner_scope: str, maker: str, key: str, path: Path = DB_PATH) -> tuple[str, str | None, str] | None:
    """The (holder, job_id, identity) of a claim, read in one statement, or None when it is gone."""
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT holder, job_id, identity FROM workflow_run_claim WHERE owner_scope=? AND maker=? AND idem_key=?",
            (owner_scope, maker, key)).fetchone()
    return (row[0], row[1], row[2]) if row else None


def release_run_claim(fence: RunClaimFence, *, path: Path = DB_PATH) -> bool:
    """Drop this holder's undecided claim (step 7). A claim another holder took over, or one that names a job, stays."""
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        gone = conn.execute(
            "DELETE FROM workflow_run_claim WHERE owner_scope=? AND maker=? AND idem_key=? AND holder=? AND job_id IS NULL",
            (fence.owner_scope, fence.maker, fence.key, fence.holder)).rowcount
        conn.commit()
    return gone == 1


TOMBSTONE_DDL = """CREATE TABLE IF NOT EXISTS workflow_job_tombstone (
  job_id TEXT PRIMARY KEY,
  deleted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_job_tombstone_at ON workflow_job_tombstone(deleted_at)"""


def tombstone_job(job_id: str, *, now: int | None = None, path: Path = DB_PATH) -> None:
    """Mark a job deleted. Store reads hide it at once unless explicitly included, and
    ``reap_tombstones`` removes it after the retention window. Marking twice keeps the first time."""
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO workflow_job_tombstone(job_id, deleted_at) VALUES (?, ?)",
                     (job_id, _now() if now is None else int(now)))
        conn.commit()


def tombstoned(job_id: str, *, path: Path = DB_PATH) -> bool:
    with _conn(path) as conn:
        return conn.execute("SELECT 1 FROM workflow_job_tombstone WHERE job_id=?", (job_id,)).fetchone() is not None


def reap_tombstones(*, before: int, path: Path = DB_PATH) -> int:
    """Remove every job tombstoned at or before ``before`` (a unix time), with its events, envelope, results and claim.
    Returns how many jobs went."""
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [row[0] for row in conn.execute(
                "SELECT job_id FROM workflow_job_tombstone WHERE deleted_at <= ?", (int(before),))]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM media_job_events WHERE job_id IN ({marks})", chunk)
                conn.execute(f"DELETE FROM media_jobs WHERE job_id IN ({marks})", chunk)
                _release_envelopes_in(conn, chunk)
                conn.execute(f"DELETE FROM workflow_job_tombstone WHERE job_id IN ({marks})", chunk)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return len(ids)


def create(
    job: MediaJob | Mapping[str, Any],
    *,
    outstanding_limits: Iterable[OutstandingLimit] = (),
    input_data: Mapping[str, Any] | None = None,
    path: Path = DB_PATH,
    maker_from_request: bool = True,
    envelope: JobEnvelope | None = None,
    require_envelope: bool = False,
    claim: RunClaimFence | None = None,
    prepare: Callable[[MediaJob], JobEnvelope | None] | None = None,
) -> MediaJob:
    """Create a job; replay only matching owner, operation and submission facts.

    The job's maker is the authenticated request's maker. A caller that indexes work made by someone else passes
    ``maker_from_request=False``, and the maker it set on the job stands.

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

    ``envelope`` is what the job pinned (``gateway/job_envelope.py``). It is written in the same transaction as the
    job row, so a job never exists without it nor it without the job. By default recording is best effort: a refusal
    (a bad envelope, a store error) is logged and rolled back to a savepoint, and the job is created without it. A
    route whose jobs run from their pin passes ``require_envelope=True``: a missing envelope or a failed write raises
    ``EnvelopeNotRecorded`` (the cause is chained), the whole transaction rolls back and no job row is left. A replay
    of an existing job records nothing.

    ``claim`` fences the create (section 7.2): in the write transaction, before anything else, the claim row must
    still hold this holder and name no job, else ``ClaimLost`` is raised and nothing is stored. ``prepare(record)``
    runs after that check, in the same transaction, and returns the envelope: it draws generated values and adopts
    staged files, so a holder that lost its claim draws and adopts nothing. When the job row is written the claim is set
    to name it; the claim and the job commit together.
    """
    record = _coerce_to_job(job)
    # Auth-disabled submissions still record an explicit scope. Missing scope
    # on historical rows must not be mistaken for a verified unscoped request.
    record.owner_scope = stored_owner_key(record.owner_key_hash, app_id=record.app_id) or "unscoped"
    # Never accept a caller-supplied digest as proof that the input matches.
    record.input_hash = _input_hash(record, input_data)
    # The maker is the person and credential of the authenticated request, never a field of the submission.
    if maker_from_request:
        record.maker_person_id, record.maker_credential_id, record.maker_connection_id = (
            request_source.job_maker(record.app_id))
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if claim is not None:
            held = conn.execute(
                "SELECT 1 FROM workflow_run_claim WHERE owner_scope=? AND maker=? AND idem_key=? AND holder=? "
                "AND job_id IS NULL", (claim.owner_scope, claim.maker, claim.key, claim.holder)).fetchone()
            if held is None:
                conn.rollback()
                raise ClaimLost("the idempotency claim is no longer held by this request")
        if prepare is not None:
            try:
                envelope = prepare(record)
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
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
                # A replay returns the stored job, so it must be the same maker's job, or one maker reads another's.
                if (not previous.owner_scope or not previous.input_hash
                        or previous.owner_scope != record.owner_scope or previous.intent != record.intent
                        or previous.input_hash != record.input_hash
                        or (previous.maker_person_id or None) != (record.maker_person_id or None)):
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
        erased = _write_job(conn, record)
        if envelope is None and require_envelope:
            conn.rollback()
            raise EnvelopeNotRecorded("the job pinned no envelope")
        if envelope is not None:
            try:
                _record_envelope(conn, record.job_id, envelope, require=require_envelope, erase=erased)
            except EnvelopeNotRecorded:
                if conn.in_transaction:
                    conn.rollback()
                raise
        if claim is not None:
            named = conn.execute(
                "UPDATE workflow_run_claim SET job_id=? WHERE owner_scope=? AND maker=? AND idem_key=? AND holder=? "
                "AND job_id IS NULL", (record.job_id, claim.owner_scope, claim.maker, claim.key, claim.holder)).rowcount
            if named != 1:
                conn.rollback()
                raise ClaimLost("the idempotency claim could not name the job")
        conn.commit()
    return record


def _record_envelope(
    conn: sqlite3.Connection, job_id: str, envelope: JobEnvelope, *, require: bool, erase: bool = False,
) -> None:
    """Write the envelope in the caller's transaction, inside a savepoint: a failure leaves the job row untouched.

    A failure is logged and swallowed, or with ``require`` raised as ``EnvelopeNotRecorded`` for the caller to roll the
    whole transaction back.

    ``erase`` is True when the erasure cutoff covers the job (created at or before it): the job is stored, as its other
    prompt derivatives are cleared and not refused, and its generated values are written as erased markers."""
    from gateway import definition_store, generated_defaults  # import this module, so they cannot be imported at the top

    conn.execute("SAVEPOINT job_envelope")
    try:
        definition_store.record_envelope_in(
            conn, job_id, envelope.envelope, generators=generated_defaults.persistence(), now=time.time(), blobs=envelope.blobs,
        )
        if erase:
            definition_store.clear_generated_in(conn, job_id)
    except Exception as exc:  # noqa: BLE001 - without ``require`` the job is created as it always was
        if require:
            raise EnvelopeNotRecorded(f"{type(exc).__name__}: {exc}") from exc
        if not conn.in_transaction:
            # SQLite rolled back the whole transaction (disk full, I/O error): the job row is gone too, and the
            # savepoint with it. Raise the real cause, not "no such savepoint".
            raise
        conn.execute("ROLLBACK TO job_envelope")
        _log.warning("job %s: the envelope was not recorded: %s: %s", job_id, type(exc).__name__, exc)
    if conn.in_transaction:
        conn.execute("RELEASE job_envelope")


def _write_job(conn: sqlite3.Connection, job: MediaJob, *, new_attempt: bool = False) -> bool:
    """Write the job row. Returns True when the owner's or the wildcard erasure cutoff covered the job, so its prompt
    derivatives were cleared."""
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
        # The maker is stamped once, by create(). A later write, from a copy made before the stamp, keeps the stored one.
        job.maker_person_id = data.get("maker_person_id")
        job.maker_credential_id = data.get("maker_credential_id")
        job.maker_connection_id = data.get("maker_connection_id")
        preserved = ("accepted_at",) if new_attempt else (*PHASE_FIELDS, "attempt_number", "previous_attempts")
        stale_attempt = data.get("attempt_number") != job.attempt_number
        for name in preserved:
            if name in data and (data[name] is not None or (stale_attempt and not new_attempt)):
                setattr(job, name, data[name])
    cutoff = conn.execute(
        "SELECT MAX(cutoff_at) FROM media_job_prompt_erasure_cutoffs WHERE owner_key IN (?,?)",
        ("*", job.owner_key_hash),
    ).fetchone()[0]
    erased = cutoff is not None and job.created_at <= cutoff
    if erased:
        _clear_prompt_derivatives(job)
    # Update by job_id only. A request ID held by another job raises
    # IntegrityError instead of deleting that job, as INSERT OR REPLACE did.
    conn.execute(
        "INSERT INTO media_jobs "
        "(job_id, request_id, intent, workflow, backend, status, progress, error, "
        " created_at, updated_at, app_profile, api_key_name, parent_request_id, job_json, "
        " maker_person_id, maker_credential_id, maker_connection_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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
            job.maker_person_id,
            job.maker_credential_id,
            job.maker_connection_id,
        ),
    )
    if job.status in TERMINAL_STATUSES:
        # The job ended: its media input leases end with it, in this same transaction.
        media_input_leases.release_job_in(conn, job.job_id)
    conn.execute("DELETE FROM media_job_asset_origins WHERE job_id=?", (job.job_id,))
    if job.creating_receipt_id:
        conn.executemany(
            "INSERT INTO media_job_asset_origins(asset_id,job_id,origin_id) VALUES(?,?,?)",
            [(asset_id, job.job_id, job.creating_receipt_id) for asset_id in sorted(_asset_ids(job.to_dict("internal")))],
        )
    return erased


def _clear_prompt_derivatives(job: MediaJob) -> None:
    job.metadata.pop("request_hash", None)
    job.input_hash = None
    if job.request_id.startswith("imgjob_seeded_"):
        job.request_id = ""
    if (job.parent_request_id or "").startswith("imgjob_seeded_"):
        job.parent_request_id = None


def delete_succeeded_in(conn: sqlite3.Connection, job_ids: Iterable[str]) -> int:
    """Delete finished jobs and their events inside the caller's transaction; receipts are not touched.

    A job that has not succeeded stays, whatever the ids say. Returns how many jobs were deleted.
    """
    ids = sorted({job_id for job_id in job_ids if job_id})
    deleted = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        done = [row[0] for row in conn.execute(
            f"SELECT job_id FROM media_jobs WHERE status=? AND job_id IN ({marks})",
            [STATUS_SUCCEEDED, *chunk],
        )]
        if not done:
            continue
        marks = ",".join("?" for _ in done)
        conn.execute(f"DELETE FROM media_job_events WHERE job_id IN ({marks})", done)
        conn.execute(f"DELETE FROM media_jobs WHERE job_id IN ({marks})", done)
        _release_envelopes_in(conn, done)
        deleted += len(done)
    return deleted


def discard_failed(job_id: str, *, path: Path = DB_PATH) -> bool:
    """Delete one failed job, its events, envelope, results and idempotency claim. A job that did not fail stays. For a job
    whose engine never took it (the caller was told 502): nothing of it may be kept or replayed."""
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        found = conn.execute("SELECT 1 FROM media_jobs WHERE job_id=? AND status=?", (job_id, STATUS_FAILED)).fetchone()
        if found is None:
            conn.commit()
            return False
        conn.execute("DELETE FROM media_job_events WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM media_jobs WHERE job_id=?", (job_id,))
        _release_envelopes_in(conn, [job_id])
        conn.commit()
    return True


def _release_envelopes_in(conn: sqlite3.Connection, job_ids: Iterable[str]) -> None:
    """Release each deleted job's envelope, references and results, in the transaction that deleted the job row.

    Every function in this module that deletes ``media_jobs`` rows calls this
    (``tests/test_job_envelope_release.py`` scans the code for that). A database that predates the definition tables
    has no envelopes to release.
    """
    from gateway import definition_store, result_bundle  # import this module, so they cannot be imported at the top

    for job_id in job_ids:
        media_input_leases.release_job_in(conn, job_id)  # a job row deleted: its input leases end with it
        conn.execute("DELETE FROM workflow_run_claim WHERE job_id=?", (job_id,))  # and so does its idempotency claim
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_envelope'").fetchone() is None:
        return
    for job_id in job_ids:
        definition_store.release_in(conn, job_id)
        result_bundle.release_in(conn, job_id)


def delete_succeeded(job_ids: Iterable[str], *, path: Path = DB_PATH) -> int:
    """``delete_succeeded_in`` in its own transaction."""
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            deleted = delete_succeeded_in(conn, job_ids)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return deleted


HIDDEN_ASSETS_DDL = """CREATE TABLE IF NOT EXISTS media_hidden_assets (
  person_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  hidden_at INTEGER NOT NULL,
  PRIMARY KEY (person_id, asset_id, job_id)
)"""


def hide_assets_in(conn: sqlite3.Connection, person_id: str, pairs: Iterable[tuple[str, str]]) -> None:
    """Hide ``(asset_id, job_id)`` from one person's Library, inside the caller's transaction.

    The hide belongs to the asset as that job made it. The same bytes made again by another
    job are a new item and are not hidden.
    """
    conn.execute(HIDDEN_ASSETS_DDL)
    conn.executemany(
        "INSERT OR IGNORE INTO media_hidden_assets(person_id, asset_id, job_id, hidden_at) VALUES (?,?,?,?)",
        [(person_id, asset_id, job_id, _now()) for asset_id, job_id in pairs],
    )


def delete_person_hides_in(conn: sqlite3.Connection, person_id: str) -> int:
    """Delete one person's Library hides inside the caller's transaction; a database older than the table has none."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_hidden_assets'").fetchone() is None:
        return 0
    return conn.execute("DELETE FROM media_hidden_assets WHERE person_id=?", (person_id,)).rowcount


def request_ids_for_assets_in(conn: sqlite3.Connection, asset_ids: Iterable[str]) -> set[str]:
    """The creating receipt IDs of jobs that produced these assets, inside the caller's transaction."""
    ids = sorted({str(asset_id) for asset_id in asset_ids if asset_id})
    if not ids or conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_job_asset_origins'"
    ).fetchone() is None:
        return set()
    found: set[str] = set()
    for offset in range(0, len(ids), 500):
        chunk = ids[offset:offset + 500]
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT DISTINCT origin_id FROM media_job_asset_origins WHERE asset_id IN ({marks})",
            chunk,
        ).fetchall()
        found.update(str(row[0]) for row in rows if row[0])
    return found


def request_ids_for_jobs_in(conn: sqlite3.Connection, job_ids: Iterable[str]) -> set[str]:
    """The creating receipt IDs of these jobs, with a legacy fallback."""
    ids = sorted({str(job_id) for job_id in job_ids if job_id})
    if not ids or conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_jobs'"
    ).fetchone() is None:
        return set()
    found: set[str] = set()
    for offset in range(0, len(ids), 500):
        chunk = ids[offset:offset + 500]
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(f"SELECT job_json FROM media_jobs WHERE job_id IN ({marks})", chunk).fetchall()
        for row in rows:
            try:
                data = json.loads(row[0])
            except (TypeError, ValueError):
                continue
            if isinstance(data, Mapping):
                origin_id = _receipt_link_candidate(data)
                if origin_id:
                    found.add(origin_id)
    return found


def fence_library_origins_in(
    conn: sqlite3.Connection, person_id: str, origin_ids: Iterable[str], *, erased_at: int | None = None,
) -> int:
    """Persist per-item write fences in the caller's media transaction."""
    ids = sorted({str(origin_id) for origin_id in origin_ids if origin_id})
    if not person_id or not ids:
        return 0
    timestamp = _now() if erased_at is None else int(erased_at)
    conn.executemany(
        "INSERT INTO media_library_erasure_fences(person_id,origin_id,erased_at) VALUES(?,?,?) "
        "ON CONFLICT(person_id,origin_id) DO UPDATE SET erased_at=MAX(erased_at,excluded.erased_at)",
        [(person_id, origin_id, timestamp) for origin_id in ids],
    )
    return len(ids)


def library_origin_erased_in(conn: sqlite3.Connection, person_id: str, origin_id: str) -> bool:
    """Whether this person's Library deletion already fenced the creating request."""
    if not person_id or not origin_id:
        return False
    return conn.execute(
        "SELECT 1 FROM media_library_erasure_fences WHERE person_id=? AND origin_id=?",
        (person_id, origin_id),
    ).fetchone() is not None


# The connection already waits ``busy_timeout`` for a lock; these are the retries after that.
_HIDDEN_READ_RETRY_PAUSES_S = (0.05, 0.1, 0.2)


def hidden_assets(person_ids: Iterable[str], *, path: Path = DB_PATH) -> set[tuple[str, str]]:
    """The ``(asset_id, job_id)`` pairs these people hid."""
    ids = sorted(set(person_ids))
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    # A read that fails must fail the request: an empty answer would show hidden items again.
    with _conn(path) as conn:
        for pause in (*_HIDDEN_READ_RETRY_PAUSES_S, None):
            try:
                rows = conn.execute(
                    f"SELECT asset_id, job_id FROM media_hidden_assets WHERE person_id IN ({marks})", ids,
                ).fetchall()
                break
            except sqlite3.OperationalError as exc:
                message = str(exc)
                if "no such table: media_hidden_assets" in message:  # a database older than this table
                    return set()
                if pause is None or ("locked" not in message and "busy" not in message):
                    raise
                time.sleep(pause)
    return {(row[0], row[1]) for row in rows}


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
        media_input_leases.release_owners_in(conn, owner_keys)  # erasure: the owners' media input leases end
        conn.commit()
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_jobs'").fetchone() is None:
            return 0
        _erase_results(conn, owner_keys, before=int(before))
        cursor = ""
        while True:
            rows = conn.execute(
                "SELECT job_id, job_json FROM media_jobs WHERE job_id>? AND created_at<=? "
                "ORDER BY job_id LIMIT 100", (cursor, int(before)),
            ).fetchall()
            if not rows:
                break
            ids = []
            owned = []
            for job_id, payload in rows:
                data = _safe_loads(payload)
                if not isinstance(data, Mapping):
                    continue
                owner = stored_owner_key(data.get("owner_key_hash"), app_id=data.get("app_id"))
                if "*" not in owner_keys and owner not in owner_keys:
                    continue
                owned.append(job_id)
                if (data.get("input_hash") or "request_hash" in (data.get("metadata") or {})
                        or str(data.get("request_id") or "").startswith("imgjob_seeded_")
                        or str(data.get("parent_request_id") or "").startswith("imgjob_seeded_")):
                    ids.append(job_id)
            if owned:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _clear_generated_in(conn, owned)  # the values the gateway made for these jobs go with the body
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


def _clear_generated_in(conn: sqlite3.Connection, job_ids: Iterable[str]) -> None:
    """Empty each job's envelope ``generated`` block (S3 design, D6), in the caller's transaction. A database that
    predates the definition tables has none."""
    from gateway import definition_store  # imports this module, so it cannot be imported at the top

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_envelope'").fetchone() is None:
        return
    for job_id in job_ids:
        definition_store.clear_generated_in(conn, job_id)


def _erase_results(conn: sqlite3.Connection, owner_keys: set[str], *, before: int) -> None:
    """Erase the result values of these owners' jobs made up to ``before``, in one transaction (S2 design, D4)."""
    from gateway import result_bundle  # imports this module, so it cannot be imported at the top

    conn.execute("BEGIN IMMEDIATE")
    try:
        result_bundle.erase_in(conn, owner_keys, before=before)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def queue_position_in(conn: sqlite3.Connection, job_id: str) -> int | None:
    """Jobs ahead of a queued job in its queue, read from the job table; ``None`` unless the job is queued.

    A queue is the queued jobs of one engine, taken oldest first (creation time, then insertion order). Nothing is
    counted in memory, so a restarted process reads the same number. The gateway has no honest time estimate, so none
    is given.
    """
    row = conn.execute("SELECT rowid, status, created_at, COALESCE(backend, '') FROM media_jobs WHERE job_id = ?",
                       (job_id,)).fetchone()
    if row is None or row[1] != STATUS_QUEUED:
        return None
    return conn.execute(
        "SELECT COUNT(*) FROM media_jobs WHERE status = ? AND COALESCE(backend, '') = ? "
        "AND (created_at, rowid) < (?, ?)", (STATUS_QUEUED, row[3], row[2], row[0])).fetchone()[0]


def queue_position(job_id: str, *, path: Path = DB_PATH) -> int | None:
    with _conn(path) as conn:
        return queue_position_in(conn, job_id)


def _with_read_facts(conn: sqlite3.Connection, data: Mapping[str, Any]) -> Mapping[str, Any]:
    """The stored job plus its read-time facts: ``queue_position``, and ``last_healthy_at`` of a failed job whose engine
    could not be reached. Never the stored values, which were only defaults."""
    if not data.get("job_id"):
        return data
    position = queue_position_in(conn, str(data["job_id"])) if data.get("status") == STATUS_QUEUED else None
    healthy = (data.get("metadata") or {}).get(LAST_HEALTHY_KEY) if data.get("status") == STATUS_FAILED else None
    return {**data, "queue_position": position, LAST_HEALTHY_KEY: healthy}


def get(
    job_id: str,
    *,
    audience: str = "internal",
    include_deleted: bool = False,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Return the stored job filtered for ``audience``, or None. Deleted jobs are hidden unless explicitly included."""
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT job_json FROM media_jobs WHERE job_id = ?"
            + ("" if include_deleted else " AND job_id NOT IN (SELECT job_id FROM workflow_job_tombstone)"),
            (job_id,),
        ).fetchone()
        data = _safe_loads(row[0]) if row else None
        if isinstance(data, Mapping):
            data = _with_read_facts(conn, data)
    if not isinstance(data, Mapping):
        return None
    return _filter_for_audience(data, audience)


def get_many(
    job_ids: Iterable[str],
    *,
    audience: str = "internal",
    include_deleted: bool = False,
    path: Path = DB_PATH,
) -> dict[str, dict[str, Any]]:
    """Return ``{job_id: job}`` as ``get`` would, in one query per 500 ids. Deleted jobs are hidden by default."""
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    ids = sorted({value for value in job_ids if value})
    out: dict[str, dict[str, Any]] = {}
    with _conn(path) as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = conn.execute(
                f"SELECT job_id, job_json FROM media_jobs WHERE job_id IN ({','.join('?' for _ in chunk)})"
                + ("" if include_deleted else " AND job_id NOT IN (SELECT job_id FROM workflow_job_tombstone)"),
                chunk,
            ).fetchall()
            for job_id, raw in rows:
                data = _safe_loads(raw)
                if isinstance(data, Mapping):
                    out[job_id] = _filter_for_audience(_with_read_facts(conn, data), audience)
    return out


def get_by_request_id(
    request_id: str,
    *,
    audience: str = "internal",
    include_deleted: bool = False,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Return the job this request ID still replays; see ``REPLAY_WINDOW_S``. Deleted jobs are hidden by default."""
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    if not request_id:
        return None
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT job_json, status, updated_at FROM media_jobs WHERE request_id = ?"
            + ("" if include_deleted else " AND job_id NOT IN (SELECT job_id FROM workflow_job_tombstone)"),
            (request_id,),
        ).fetchone()
        data = _safe_loads(row[0]) if row and _replays(row[1], row[2]) else None
        if isinstance(data, Mapping):
            data = _with_read_facts(conn, data)
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
    job_ids: list[str] | None = None,
    request_ids: list[str] | None = None,
    maker_person_ids: list[str] | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` most-recently-updated jobs, newest first. ``job_ids``, ``request_ids`` and
    ``maker_person_ids`` are positive allow-lists."""
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    if limit <= 0:
        return []
    if app_ids is not None or owner_key_hashes is not None:
        if not app_ids and not owner_key_hashes:
            return []
    clauses: list[str] = ["job_id NOT IN (SELECT job_id FROM workflow_job_tombstone)"]
    params: list[Any] = []
    if app_ids is not None or owner_key_hashes is not None:
        ownership = []
        for field, values in (("app_id", app_ids), ("owner_key_hash", owner_key_hashes)):
            if values:
                ownership.append(f"json_extract(job_json, '$.{field}') IN ({','.join('?' for _ in values)})")
                params.extend(values)
        clauses.append("(" + " OR ".join(ownership) + ")")
    if maker_person_ids is not None:
        # The durable maker stamp, read from its own indexed column. An empty list matches nothing.
        if not maker_person_ids:
            return []
        clauses.append(f"maker_person_id IN ({','.join('?' for _ in maker_person_ids)})")
        params.extend(maker_person_ids)
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
    if job_ids is not None or request_ids is not None:
        # One allow-list of job ids or of their request ids; an empty list matches nothing.
        if not job_ids and not request_ids:
            return []
        if job_ids:
            clauses.append(f"job_id IN ({','.join('?' for _ in job_ids)})")
            params.extend(job_ids)
        if request_ids:
            clauses.append(f"request_id IN ({','.join('?' for _ in request_ids)})")
            params.extend(request_ids)
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
            out.append(_filter_for_audience(_with_read_facts(conn, data), audience))
    return out


def list_jobs(*args, **kwargs):
    """Alias for ``recent`` so call-sites can read more naturally."""
    return recent(*args, **kwargs)


def is_error_code(code: str) -> bool:
    """Whether ``code`` is a short lowercase token the job store keeps as a failure code."""
    return _ERROR_CODE_RE.fullmatch(code) is not None


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
    before_commit: Callable[[sqlite3.Connection, dict[str, Any]], object] | None = None,
    committed_bundle: bool = False,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Apply a status transition. Returns the updated internal job dict.

    ``before_commit(conn, job)`` runs in the same transaction, after the transition is checked and written and before
    the commit: the caller writes what must become visible with the new state (the result bundle, S2 design D4). If it
    raises, nothing is committed. It never runs for a refused transition (an illegal one, or a success that a pending
    cancel holds back).

    ``error_info`` holds the typed failure facts (``ERROR_FIELDS``) that go
    with ``error``; ``validate_error_info`` checks them.

    ``committed_bundle`` is for ``settle_committed`` alone: the result is already published, so a pending cancel no
    longer holds the success back (S7 addendum 6.1: a committed bundle wins).

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
        if (status == STATUS_SUCCEEDED and not committed_bundle
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
        if before_commit is not None:
            before_commit(conn, updated.to_dict("internal"))
        conn.commit()
    if is_terminal(status):
        job_wake.wake(job_id)
    return updated.to_dict("internal")


class CancellationPending(ValueError):
    """Output finished after cancellation; the worker must release before settling."""


def settle_committed(job_id: str, *, path: Path = DB_PATH) -> dict[str, Any] | None:
    """Recovery: the job's result bundle is committed, so it succeeds from the bundle, even when a cancel was asked
    (the ordinary pre-publication cancel guard does not apply to a published result). A terminal job is left alone."""
    job = get(job_id, audience="internal", path=path)
    if job is None or is_terminal(str(job["status"])):
        return job
    if job["status"] == STATUS_QUEUED:
        update_status(job_id, STATUS_RUNNING, expected_status=STATUS_QUEUED, path=path)
    return update_status(job_id, STATUS_SUCCEEDED, progress=100, committed_bundle=True, path=path)


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
        if (job.metadata or {}).get("cancel_requested_at"):
            raise CancellationPending("Cancel requested; no further attempt may start")
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
    # OWNER_VISIBILITY_STEP3: close trusted event types; inventory freezes today's writers without changing accepted values here.
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
    # OWNER_VISIBILITY_STEP3: admin event audiences still expose member job correlation; enforce job Own.
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


def save_fact_revisions(
    facts: list[dict[str, Any]], *, source_kind: str, source_id: str, destination_id: str,
    path: Path = DB_PATH, now: int | None = None,
    destination: Mapping[str, Any] | None = None,
) -> list[str]:
    """Append exact fact versions and link them to a receipt or kept record."""
    from gateway.retention_facts import validate_facts

    checked = [validate_facts([fact])[0] for fact in facts]
    return save_fact_snapshot(
        [{**(destination or {}), "id": destination_id, "facts": checked}],
        source_kind=source_kind, source_id=source_id, path=path, now=now,
    )


@dataclass(frozen=True)
class PreparedFactSnapshot:
    """Validated, hashed, serialized evidence reusable for one captured generation."""

    entries: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...]
    hashes: tuple[str, ...]


def prepare_fact_snapshot(destinations: list[Mapping[str, Any]]) -> PreparedFactSnapshot:
    from gateway.retention_facts import revision, validate_facts

    entries = []
    hashes = []
    for destination in destinations:
        destination_id = destination.get("id")
        if not isinstance(destination_id, str) or not destination_id or len(destination_id) > 128:
            raise ValueError("invalid fact revision reference")
        facts = destination.get("facts", [])
        if not isinstance(facts, list) or any(not isinstance(fact, Mapping) for fact in facts):
            raise ValueError("retention facts must be a list of mappings")
        checked = validate_facts([
            {key: value for key, value in fact.items()
             if key not in {"revision", "stale", "stale_label", "source"}}
            for fact in facts
        ])
        revisions = [revision(fact) for fact in checked]
        metadata = {key: destination[key] for key in ("name", "kind", "address", "reach")
                    if key in destination}
        if any(not isinstance(value, str) or len(value) > 500 for value in metadata.values()):
            raise ValueError("invalid retention destination metadata")
        if "facts_unavailable" in destination:
            if type(destination["facts_unavailable"]) is not bool:
                raise ValueError("invalid retention destination availability")
            metadata["facts_unavailable"] = destination["facts_unavailable"]
        entries.append((destination_id, json.dumps(metadata, ensure_ascii=False, sort_keys=True), tuple(
            (digest, json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            for digest, fact in zip(revisions, checked)
        )))
        hashes.extend(revisions)
    return PreparedFactSnapshot(tuple(entries), tuple(dict.fromkeys(hashes)))


def save_fact_snapshot(
    destinations: list[Mapping[str, Any]] | PreparedFactSnapshot, *, source_kind: str, source_id: str,
    path: Path = DB_PATH, now: int | None = None, timeout: float = 5.0,
) -> list[str]:
    """Save exact captured evidence atomically; request writers can decline a busy store."""
    if source_kind not in {"receipt", "kept_record"} or not source_id or len(source_id) > 128:
        raise ValueError("invalid fact revision reference")
    prepared = destinations if isinstance(destinations, PreparedFactSnapshot) else prepare_fact_snapshot(destinations)
    timestamp = int(time.time()) if now is None else now
    with _conn(path, timeout=timeout) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for destination_id, metadata_json, facts in prepared.entries:
                conn.execute(
                    "INSERT OR IGNORE INTO retention_fact_destinations(source_kind,source_id,destination_id,metadata_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (source_kind, source_id, destination_id, metadata_json, timestamp),
                )
                for digest, encoded in facts:
                    conn.execute(
                        "INSERT OR IGNORE INTO retention_fact_revisions(revision,fact_json,first_seen) VALUES(?,?,?)",
                        (digest, encoded, timestamp),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO retention_fact_references(source_kind,source_id,destination_id,revision) VALUES(?,?,?,?)",
                        (source_kind, source_id, destination_id, digest),
                    )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return list(prepared.hashes)


def fact_revisions(hashes: list[str], *, path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Read stored versions in receipt order; an unknown hash is an error, not today's fact."""
    if not hashes:
        return []
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT revision,fact_json FROM retention_fact_revisions WHERE revision IN (" +
            ",".join("?" for _ in hashes) + ")", tuple(hashes),
        ).fetchall()
    found = {row[0]: json.loads(row[1]) for row in rows}
    if any(digest not in found for digest in hashes):
        raise KeyError("a stored retention fact revision is missing")
    return [{**found[digest], "revision": digest} for digest in hashes]


def fact_revisions_for_source(
    source_kind: str, source_id: str, *, path: Path = DB_PATH,
) -> dict[str, list[dict[str, Any]]]:
    """Restore each saved fact to the destination it described at write time."""
    if source_kind not in {"receipt", "kept_record"}:
        raise ValueError("invalid fact revision reference")
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT r.destination_id, r.revision, f.fact_json FROM retention_fact_references r "
            "JOIN retention_fact_revisions f ON f.revision=r.revision "
            "WHERE r.source_kind=? AND r.source_id=? ORDER BY r.destination_id, r.rowid",
            (source_kind, source_id),
        ).fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for destination_id, digest, encoded in rows:
        result.setdefault(destination_id, []).append({**json.loads(encoded), "revision": digest})
    return result


def fact_destinations_for_source(
    source_kind: str, source_id: str, *, path: Path = DB_PATH,
) -> dict[str, dict[str, Any]]:
    """Return the public destination labels and reach captured with a request."""
    if source_kind not in {"receipt", "kept_record"}:
        raise ValueError("invalid fact revision reference")
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT destination_id,metadata_json FROM retention_fact_destinations "
            "WHERE source_kind=? AND source_id=?",
            (source_kind, source_id),
        ).fetchall()
    return {destination_id: json.loads(metadata) for destination_id, metadata in rows}


def receipt_fact_source_ids(*, after: str = "", limit: int = 500,
                            older_than: int | None = None, path: Path = DB_PATH) -> list[str]:
    """Page receipt sources, allowing a grace period for a pending receipt write."""
    with _conn(path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT source_id FROM ("
            "SELECT source_id FROM retention_fact_references WHERE source_kind='receipt' "
            "UNION SELECT source_id FROM retention_fact_destinations WHERE source_kind='receipt') AS sources "
            "WHERE source_id > ? AND (? IS NULL OR NOT EXISTS ("
            "SELECT 1 FROM retention_fact_destinations d WHERE d.source_kind='receipt' "
            "AND d.source_id=sources.source_id AND d.created_at > ?)) "
            "ORDER BY source_id LIMIT ?", (after, older_than, older_than, limit),
        )]


def release_fact_references(source_kind: str, source_id: str, *, path: Path = DB_PATH) -> int:
    """Release one source and prune only versions with no remaining references."""
    return release_fact_reference_batch(source_kind, [source_id], path=path)


def release_fact_reference_batch(source_kind: str, source_ids: list[str], *, path: Path = DB_PATH) -> int:
    """Release a bounded batch in one transaction, retaining shared versions."""
    if source_kind not in {"receipt", "kept_record"} or len(source_ids) > 500:
        raise ValueError("invalid fact revision reference batch")
    if not source_ids:
        return 0
    placeholders = ",".join("?" for _ in source_ids)
    with _conn(path) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("retention_fact_references", "retention_fact_destinations"):
            conn.execute(f"DELETE FROM {table} WHERE source_kind=? AND source_id IN ({placeholders})",
                         (source_kind, *source_ids))
        removed = conn.execute(
            "DELETE FROM retention_fact_revisions WHERE NOT EXISTS ("
            "SELECT 1 FROM retention_fact_references r WHERE r.revision=retention_fact_revisions.revision)"
        ).rowcount
    return removed


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
    "RunClaim",
    "RunClaimFence",
    "ClaimLost",
    "claim_run_key",
    "get_run_claim",
    "settle_committed",
    "release_run_claim",
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
    "connection",
    "save_fact_revisions",
    "save_fact_snapshot",
    "fact_revisions",
    "fact_revisions_for_source",
    "fact_destinations_for_source",
    "release_fact_references",
    "schema_version",
]
