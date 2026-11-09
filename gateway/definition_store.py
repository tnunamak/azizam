"""Content-addressed definition blobs with reference counts, and the per-job envelope (S2 design, D2).

The three tables (``definition_blob``, ``definition_ref``, ``job_envelope``) live in the media jobs database, which
``gateway/media_jobs.py`` owns: its schema, its pragmas (``secure_delete`` is on) and its connections. The ``*_in``
functions take the caller's connection and never begin or end a transaction, so a later change can record or release
an envelope in the same transaction as the job row. ``DefinitionStore`` wraps them, one short transaction per call.

A blob is the bytes of a contract, binding or template that an envelope pins. A job's envelope row and its references
are written in one transaction, so a reference never points at a missing blob. Releasing a job deletes its envelope and
references in one transaction. ``collect_garbage_in`` deletes only blobs that have no reference and are older than a
grace period. ``put_blob_in`` refreshes a blob's ``created`` time on every put, so a blob put again after a sweep is
young again; ``record_envelope_in`` also accepts the blob bodies and writes them in its own transaction, which leaves
no gap at all.

A reference belongs to a job (``holder_kind`` ``job``) or to a holder that is not a job, such as ``registry:<revision>``
(``holder_kind`` ``holder``). The two kinds never share an id: releasing a holder cannot strip a job's references.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from gateway import generated_defaults, media_jobs
from gateway.workflow_envelope import Envelope, definition_shas, parse_envelope


class DefinitionUnavailable(Exception):
    """A pinned definition blob is missing, or a stored envelope cannot be read. The caller fails the job; there is no
    fallback to a live file."""


class EnvelopeConflict(Exception):
    """The job already has a different envelope. An envelope is built once."""


class HolderIdError(ValueError):
    """A job id was used as a holder id, or a holder id as a job id."""


def _digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _canonical(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _is_job_id(conn: sqlite3.Connection, owner_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM job_envelope WHERE job_id = ? "
            "UNION ALL SELECT 1 FROM definition_ref WHERE job_id = ? AND holder_kind = 'job' LIMIT 1",
            (owner_id, owner_id),
        ).fetchone()
        is not None
    )


def _is_holder_id(conn: sqlite3.Connection, owner_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM definition_ref WHERE job_id = ? AND holder_kind = 'holder' LIMIT 1", (owner_id,)
    ).fetchone()
    return row is not None


def _require_blobs(conn: sqlite3.Connection, shas: Iterable[str]) -> None:
    for sha in sorted(set(shas)):
        if conn.execute("SELECT 1 FROM definition_blob WHERE sha = ?", (sha,)).fetchone() is None:
            raise DefinitionUnavailable(f"definition {sha} is not stored")


def _add_refs(conn: sqlite3.Connection, owner_id: str, kind: str, shas: Iterable[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO definition_ref (blob_sha, job_id, holder_kind) VALUES (?, ?, ?)",
        [(sha, owner_id, kind) for sha in sorted(set(shas))],
    )


def put_blob_in(conn: sqlite3.Connection, body: bytes, *, now: float) -> str:
    """Store ``body`` and return its ``sha256:<hex>``. Idempotent by hash. ``created`` moves forward to ``now`` and
    never back, so a blob put again after a sweep is inside the grace period again."""
    sha = _digest_of(body)
    conn.execute(
        "INSERT INTO definition_blob (sha, body, created) VALUES (?, ?, ?) "
        "ON CONFLICT(sha) DO UPDATE SET created = max(created, excluded.created)",
        (sha, body, now),
    )
    return sha


def read_blob_in(conn: sqlite3.Connection, sha: str) -> bytes:
    row = conn.execute("SELECT body FROM definition_blob WHERE sha = ?", (sha,)).fetchone()
    if row is None:
        raise DefinitionUnavailable(f"definition {sha} is not stored")
    return bytes(row[0])


def _require_transaction(conn: sqlite3.Connection) -> None:
    """The write functions check, then write. They need the caller's write transaction (``BEGIN IMMEDIATE``): in a
    deferred one another writer can commit between the check and the write."""
    if not conn.in_transaction:
        raise RuntimeError("definition store writes need the caller's transaction (BEGIN IMMEDIATE)")


def record_envelope_in(
    conn: sqlite3.Connection,
    job_id: str,
    envelope: Envelope,
    *,
    generators: Mapping[str, str],
    now: float,
    blobs: Iterable[bytes] = (),
) -> None:
    """Check the envelope, then write it, one reference per pinned blob, and any ``blobs``.

    The envelope is parsed again from its JSON against ``generators`` (the reviewed generator map), so a value built
    by hand cannot get past the grammar; a failure raises ``ValueError`` and writes nothing. Recording the same
    envelope again is a no-op. A different envelope for the job raises ``EnvelopeConflict``. A holder id raises
    ``HolderIdError``. A missing blob raises ``DefinitionUnavailable``.
    """
    _require_transaction(conn)
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id: expected a non-empty string")
    checked = parse_envelope(envelope.to_json(), generators)
    body = _canonical(checked.to_json())
    if _is_holder_id(conn, job_id):
        raise HolderIdError(f"{job_id!r} is a holder id, not a job id")
    row = conn.execute("SELECT envelope_json FROM job_envelope WHERE job_id = ?", (job_id,)).fetchone()
    if row is not None:
        if row[0] == body:
            return
        raise EnvelopeConflict(f"job {job_id}: a different envelope is already recorded")
    for blob in blobs:
        put_blob_in(conn, blob, now=now)
    shas = definition_shas(checked)
    _require_blobs(conn, shas)
    conn.execute("INSERT INTO job_envelope (job_id, envelope_json, created) VALUES (?, ?, ?)", (job_id, body, now))
    _add_refs(conn, job_id, "job", shas)


def has_envelope_in(conn: sqlite3.Connection, job_id: str) -> bool:
    """Whether ``job_id`` recorded an envelope row at accept, readable or not."""
    return conn.execute("SELECT 1 FROM job_envelope WHERE job_id = ?", (job_id,)).fetchone() is not None


def envelope_in(conn: sqlite3.Connection, job_id: str, generators: Mapping[str, str] | None = None) -> Envelope | None:
    """The job's envelope, or None. ``generators`` is the reviewed map that ``parse_envelope`` needs while the envelope
    holds generated values; with none given, the reviewed generators' own map is used. An envelope that no longer parses (the map changed, or the row is damaged) raises
    ``DefinitionUnavailable``."""
    row = conn.execute("SELECT envelope_json FROM job_envelope WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    try:
        return parse_envelope(json.loads(row[0]), generated_defaults.persistence() if generators is None else generators)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise DefinitionUnavailable(f"job {job_id}: the stored envelope cannot be read: {exc}") from exc


def set_bundle_in(conn: sqlite3.Connection, job_id: str, bundle_ref: str) -> bool:
    """Record the job's result bundle in its envelope (``results.bundle``), in the caller's transaction.

    Returns False when the job has no envelope (a job accepted without a pin still publishes a bundle). Setting the
    same reference again is a no-op; a different one raises ``EnvelopeConflict``: a job publishes one bundle.
    """
    _require_transaction(conn)
    row = conn.execute("SELECT envelope_json FROM job_envelope WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return False
    try:
        data = json.loads(row[0])
        recorded = data["results"]["bundle"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DefinitionUnavailable(f"job {job_id}: the stored envelope cannot be read: {exc}") from exc
    if recorded == bundle_ref:
        return True
    if recorded is not None:
        raise EnvelopeConflict(f"job {job_id}: a different result bundle is already recorded")
    data["results"]["bundle"] = bundle_ref
    conn.execute("UPDATE job_envelope SET envelope_json = ? WHERE job_id = ?", (_canonical(data), job_id))
    return True


def clear_generated_in(conn: sqlite3.Connection, job_id: str) -> None:
    """Erase the values in the job's envelope ``generated`` block, in the caller's transaction. No-op without an envelope.

    Each entry keeps its generator and gains ``erased: true`` in place of its value (``generated_defaults.erased_block``),
    so a later read can tell an erased value from a job that never kept one: the first never runs, the second is a job
    made before values were kept. The pinned definitions stay (``release_in`` removes those with the job). Idempotent.

    A stored envelope that cannot be read is released instead (``release_in``): erasure must not fail on a damaged
    row, and a row that cannot be parsed cannot be partly cleared. Recording the original envelope again after this
    raises ``EnvelopeConflict``, because the stored one no longer matches.
    """
    _require_transaction(conn)
    row = conn.execute("SELECT envelope_json FROM job_envelope WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return
    try:
        data = json.loads(row[0])
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("generated"), dict):
        release_in(conn, job_id)
        return
    data["generated"] = generated_defaults.erased_block(data["generated"])
    conn.execute("UPDATE job_envelope SET envelope_json = ? WHERE job_id = ?", (_canonical(data), job_id))


def release_in(conn: sqlite3.Connection, job_id: str) -> None:
    """Delete the job's envelope and its job references. Idempotent. A holder with the same id keeps its references.

    Every path that deletes a job row calls this in the same transaction (today only ``delete_succeeded_in``;
    retention, erasure and the kill switch rewrite job rows and delete none, see tests/test_job_envelope_release.py).
    """
    _require_transaction(conn)
    conn.execute("DELETE FROM job_envelope WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM definition_ref WHERE job_id = ? AND holder_kind = 'job'", (job_id,))


def hold_in(conn: sqlite3.Connection, holder_id: str, shas: Iterable[str]) -> None:
    """References for a holder that is not a job (for example ``registry:<revision>``). A missing blob refuses all.
    A job id raises ``HolderIdError``."""
    _require_transaction(conn)
    shas = list(shas)
    if _is_job_id(conn, holder_id):
        raise HolderIdError(f"{holder_id!r} is a job id, not a holder id")
    _require_blobs(conn, shas)
    _add_refs(conn, holder_id, "holder", shas)


def release_holder_in(conn: sqlite3.Connection, holder_id: str) -> None:
    """Delete a holder's references. Idempotent. A job id raises ``HolderIdError`` and nothing is deleted."""
    _require_transaction(conn)
    if _is_job_id(conn, holder_id):
        raise HolderIdError(f"{holder_id!r} is a job id, not a holder id")
    conn.execute("DELETE FROM definition_ref WHERE job_id = ? AND holder_kind = 'holder'", (holder_id,))


def holder_shas_in(conn: sqlite3.Connection, holder_id: str) -> tuple[str, ...]:
    """The blobs a holder (not a job) references, in sha order. Empty for an unknown holder."""
    rows = conn.execute(
        "SELECT blob_sha FROM definition_ref WHERE job_id = ? AND holder_kind = 'holder' ORDER BY blob_sha", (holder_id,)
    ).fetchall()
    return tuple(row[0] for row in rows)


def replace_holder_in(conn: sqlite3.Connection, holder_id: str, shas: Iterable[str]) -> None:
    """Make a holder reference exactly ``shas``: the others it held are released, in this one transaction. A missing blob
    refuses all and changes nothing. A job id raises ``HolderIdError``."""
    _require_transaction(conn)
    shas = list(shas)
    if _is_job_id(conn, holder_id):
        raise HolderIdError(f"{holder_id!r} is a job id, not a holder id")
    _require_blobs(conn, shas)
    conn.execute("DELETE FROM definition_ref WHERE job_id = ? AND holder_kind = 'holder'", (holder_id,))
    _add_refs(conn, holder_id, "holder", shas)


def collect_garbage_in(conn: sqlite3.Connection, *, cutoff: float, limit: int | None = None) -> int:
    """Delete blobs created before ``cutoff`` that have no reference. Returns how many were deleted.

    ``limit`` bounds one call's work: at most that many blobs go, oldest first, and the next call takes the rest.
    """
    _require_transaction(conn)
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit: expected a positive integer")
    cursor = conn.execute(
        "DELETE FROM definition_blob WHERE sha IN ("
        "SELECT sha FROM definition_blob WHERE created < ? "
        "AND NOT EXISTS (SELECT 1 FROM definition_ref WHERE blob_sha = definition_blob.sha) "
        "ORDER BY created LIMIT ?)",
        (cutoff, -1 if limit is None else limit),
    )
    return cursor.rowcount


class DefinitionStore:
    """The ``*_in`` functions on a jobs database path, one short transaction per call."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        media_jobs.init_db(self._path)

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction. On a busy database the wait is bounded by the connection's busy
        timeout, then ``sqlite3.OperationalError`` ("database is locked") is raised and nothing is written. That is a
        transient error: a sweep skips and runs again; a caller recording an envelope retries or fails the request
        as retryable, never as ``submission_unknown``."""
        with media_jobs.connection(self._path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:  # the original error must surface, not a rollback failure
                    conn.rollback()
                raise
            conn.commit()

    def put_blob(self, body: bytes) -> str:
        with self._write() as conn:
            return put_blob_in(conn, body, now=self._clock())

    def read_blob(self, sha: str) -> bytes:
        with media_jobs.connection(self._path) as conn:
            return read_blob_in(conn, sha)

    def record_envelope(
        self, job_id: str, envelope: Envelope, *, generators: Mapping[str, str], blobs: Iterable[bytes] = ()
    ) -> None:
        with self._write() as conn:
            record_envelope_in(conn, job_id, envelope, generators=generators, now=self._clock(), blobs=blobs)

    def has_envelope(self, job_id: str) -> bool:
        with media_jobs.connection(self._path) as conn:
            return has_envelope_in(conn, job_id)

    def envelope(self, job_id: str, generators: Mapping[str, str] | None = None) -> Envelope | None:
        with media_jobs.connection(self._path) as conn:
            return envelope_in(conn, job_id, generators)

    def clear_generated(self, job_id: str) -> None:
        with self._write() as conn:
            clear_generated_in(conn, job_id)

    def hold(self, holder_id: str, shas: Iterable[str]) -> None:
        with self._write() as conn:
            hold_in(conn, holder_id, shas)

    def holder_shas(self, holder_id: str) -> tuple[str, ...]:
        with media_jobs.connection(self._path) as conn:
            return holder_shas_in(conn, holder_id)

    def replace_holder(self, holder_id: str, shas: Iterable[str]) -> None:
        with self._write() as conn:
            replace_holder_in(conn, holder_id, shas)

    def release_holder(self, holder_id: str) -> None:
        with self._write() as conn:
            release_holder_in(conn, holder_id)

    def release(self, job_id: str) -> None:
        with self._write() as conn:
            release_in(conn, job_id)

    def collect_garbage(self, grace_s: float = 3600, *, limit: int | None = None) -> int:
        """Delete unreferenced blobs older than ``grace_s``, at most ``limit`` of them. Returns how many were deleted."""
        with self._write() as conn:
            return collect_garbage_in(conn, cutoff=self._clock() - grace_s, limit=limit)
