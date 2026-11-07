"""Leases and ingest markers for media inputs (S2 design, D7 "Effects"), kept in the media jobs database.

A media input is a file that a run reads: an asset the caller owns, an upload, or a download. ``gateway/media_inputs.py``
resolves one and holds a **lease** on it for a holder: a job, or one request. A lease lasts until its holder ends:
the job reaches a terminal state, its row is deleted, its owner is erased, or the request ends. Releasing only marks
the lease (``released_at``). The sweep removes the lease row, and removes the stored file when the ingest created it
and no other lease holds it. A release runs inside the transaction of the job write that caused it, so the job and its
leases never disagree; the file is removed later, outside that transaction.

An ingest that stores a file has its own marker (``media_input_effect``). The marker is written before the file, and
the lease is written in the transaction that deletes the marker. A crash between the two leaves a marker with no
lease, and the sweep removes the file the marker names. This is the pattern of the definition store
(``gateway/definition_store.py``): durable intent first, an idempotent sweep after.

The ``*_in`` functions take the caller's connection and write inside the caller's transaction. They name no workflow,
model, engine or modality.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

HOLDER_KINDS = ("job", "request")

DDL = """
CREATE TABLE IF NOT EXISTS media_input_effect (
  effect_id TEXT PRIMARY KEY,
  owner_key TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  created_asset INTEGER NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS media_input_lease (
  lease_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  owner_key TEXT NOT NULL,
  holder_kind TEXT NOT NULL CHECK(holder_kind IN ('job', 'request')),
  holder_id TEXT NOT NULL,
  created_asset INTEGER NOT NULL,
  created REAL NOT NULL,
  released_at REAL
);
CREATE INDEX IF NOT EXISTS media_input_lease_holder ON media_input_lease(holder_kind, holder_id);
CREATE INDEX IF NOT EXISTS media_input_lease_asset ON media_input_lease(asset_id);
"""


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError("media input writes need the caller's transaction (BEGIN IMMEDIATE)")


def _has_tables(conn: sqlite3.Connection) -> bool:
    """A database that predates these tables has no leases to release."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_input_lease'").fetchone() is not None


def begin_effect_in(conn: sqlite3.Connection, effect_id: str, *, owner_key: str, asset_id: str,
                    created_asset: bool, now: float) -> None:
    """The marker for one ingest, written before the file is stored. ``asset_id`` is the id the store will give."""
    _require_transaction(conn)
    conn.execute(
        "INSERT INTO media_input_effect (effect_id, owner_key, asset_id, created_asset, created) VALUES (?, ?, ?, ?, ?)",
        (effect_id, owner_key, asset_id, int(created_asset), now),
    )


def lease_in(conn: sqlite3.Connection, lease_id: str, *, asset_id: str, owner_key: str, holder_kind: str,
             holder_id: str, created_asset: bool, now: float, effect_id: str | None = None) -> None:
    """Hold ``asset_id`` for a holder. With ``effect_id``, the ingest marker is deleted in the same transaction."""
    _require_transaction(conn)
    if holder_kind not in HOLDER_KINDS:
        raise ValueError(f"holder_kind must be one of {HOLDER_KINDS}")
    if not holder_id:
        raise ValueError("holder_id: expected a non-empty string")
    # A file that another live ingest created is shared with it, so it also goes when the last of them lets it go.
    # Without this, the holder that created it could release first and nobody would remove the file.
    shared = conn.execute(
        "SELECT 1 FROM media_input_lease WHERE asset_id = ? AND released_at IS NULL AND created_asset = 1 "
        "UNION ALL SELECT 1 FROM media_input_effect WHERE asset_id = ? AND created_asset = 1 AND effect_id IS NOT ? LIMIT 1",
        (asset_id, asset_id, effect_id),
    ).fetchone() is not None
    if effect_id is not None:
        # The sweep may have found this file unheld and left it for this ingest (``discard_unheld``): the duty passes on.
        row = conn.execute("SELECT created_asset FROM media_input_effect WHERE effect_id = ?", (effect_id,)).fetchone()
        shared = shared or bool(row and row[0])
    conn.execute(
        "INSERT INTO media_input_lease (lease_id, asset_id, owner_key, holder_kind, holder_id, created_asset, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lease_id, asset_id, owner_key, holder_kind, holder_id, int(created_asset or shared), now),
    )
    if effect_id is not None:
        conn.execute("DELETE FROM media_input_effect WHERE effect_id = ?", (effect_id,))


def release_holder_in(conn: sqlite3.Connection, holder_kind: str, holder_id: str, *, now: float) -> int:
    """Release every lease the holder has. Idempotent. A database without the tables has none. Returns the count."""
    _require_transaction(conn)
    if not _has_tables(conn):
        return 0
    return conn.execute(
        "UPDATE media_input_lease SET released_at = ? WHERE holder_kind = ? AND holder_id = ? AND released_at IS NULL",
        (now, holder_kind, holder_id),
    ).rowcount


def release_job_in(conn: sqlite3.Connection, job_id: str, *, now: float | None = None) -> int:
    """The job ended, was deleted, or was erased: release its leases, in the transaction that caused it."""
    return release_holder_in(conn, "job", job_id, now=time.time() if now is None else now)


def release_owners_in(conn: sqlite3.Connection, owner_keys: Iterable[str], *, now: float | None = None) -> int:
    """Erasure: release every lease the owners hold. The key ``*`` (a household erasure) releases all of them."""
    _require_transaction(conn)
    if not _has_tables(conn):
        return 0
    keys = sorted(set(owner_keys))
    if not keys:
        return 0
    stamp = time.time() if now is None else now
    if "*" in keys:
        return conn.execute("UPDATE media_input_lease SET released_at = ? WHERE released_at IS NULL", (stamp,)).rowcount
    marks = ",".join("?" for _ in keys)
    return conn.execute(
        f"UPDATE media_input_lease SET released_at = ? WHERE released_at IS NULL AND owner_key IN ({marks})",
        (stamp, *keys),
    ).rowcount


@dataclass(frozen=True)
class Discard:
    """A stored file that nothing holds any more, and that the ingest created."""

    asset_id: str
    owner_key: str


def sweep_in(
    conn: sqlite3.Connection, *, now: float, effect_grace_s: float, request_ttl_s: float,
) -> list[Discard]:
    """Release what has no live holder, delete released lease rows and stale markers, and say which files to remove.

    - A marker older than ``effect_grace_s`` is a crash between the marker and the lease: the marker goes, and the file
      it named is a discard if the ingest created it and no lease holds it.
    - A request lease older than ``request_ttl_s`` is released: its request is gone.
    - A job lease whose job row does not exist, and is older than ``effect_grace_s``, is released: the job was never
      created, or was deleted by a path that did not release.
    - A released lease row is deleted. A file it created is a discard when no live lease holds it.

    The caller removes the files after this transaction commits, with ``MediaInputLedger.sweep``, which checks each file
    again in its own transaction before it removes it.
    """
    _require_transaction(conn)
    discards: dict[tuple[str, str], Discard] = {}

    def consider(asset_id: str, owner_key: str, created_asset: int) -> None:
        if not created_asset:
            return
        live = conn.execute(
            "SELECT 1 FROM media_input_lease WHERE asset_id = ? AND released_at IS NULL LIMIT 1", (asset_id,)
        ).fetchone()
        pending = conn.execute(
            "SELECT 1 FROM media_input_effect WHERE asset_id = ? LIMIT 1", (asset_id,)
        ).fetchone()
        if live is None and pending is None:
            discards[(asset_id, owner_key)] = Discard(asset_id, owner_key)

    for effect_id, owner_key, asset_id, created_asset in conn.execute(
        "SELECT effect_id, owner_key, asset_id, created_asset FROM media_input_effect WHERE created < ?",
        (now - effect_grace_s,),
    ).fetchall():
        conn.execute("DELETE FROM media_input_effect WHERE effect_id = ?", (effect_id,))
        consider(asset_id, owner_key, created_asset)
    conn.execute(
        "UPDATE media_input_lease SET released_at = ? WHERE released_at IS NULL AND holder_kind = 'request' AND created < ?",
        (now, now - request_ttl_s),
    )
    for lease_id, holder_id in conn.execute(
        "SELECT lease_id, holder_id FROM media_input_lease WHERE released_at IS NULL AND holder_kind = 'job' AND created < ?",
        (now - effect_grace_s,),
    ).fetchall():
        if conn.execute("SELECT 1 FROM media_jobs WHERE job_id = ?", (holder_id,)).fetchone() is None:
            conn.execute("UPDATE media_input_lease SET released_at = ? WHERE lease_id = ?", (now, lease_id))
    for lease_id, asset_id, owner_key, created_asset in conn.execute(
        "SELECT lease_id, asset_id, owner_key, created_asset FROM media_input_lease WHERE released_at IS NOT NULL"
    ).fetchall():
        conn.execute("DELETE FROM media_input_lease WHERE lease_id = ?", (lease_id,))
        consider(asset_id, owner_key, created_asset)
    return list(discards.values())


class MediaInputLedger:
    """The ``*_in`` functions on a jobs database path, one short transaction per call."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        from gateway import media_jobs

        self._path = Path(path)
        self._clock = clock
        self._media_jobs = media_jobs
        media_jobs.init_db(self._path)

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        with self._media_jobs.connection(self._path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            conn.commit()

    def begin_effect(self, effect_id: str, *, owner_key: str, asset_id: str, created_asset: bool) -> None:
        with self._write() as conn:
            begin_effect_in(conn, effect_id, owner_key=owner_key, asset_id=asset_id,
                            created_asset=created_asset, now=self._clock())

    def lease(self, lease_id: str, *, asset_id: str, owner_key: str, holder_kind: str, holder_id: str,
              created_asset: bool, effect_id: str | None = None) -> None:
        with self._write() as conn:
            lease_in(conn, lease_id, asset_id=asset_id, owner_key=owner_key, holder_kind=holder_kind,
                     holder_id=holder_id, created_asset=created_asset, now=self._clock(), effect_id=effect_id)

    def release_holder(self, holder_kind: str, holder_id: str) -> int:
        with self._write() as conn:
            return release_holder_in(conn, holder_kind, holder_id, now=self._clock())

    def release_owners(self, owner_keys: Iterable[str]) -> int:
        with self._write() as conn:
            return release_owners_in(conn, owner_keys, now=self._clock())

    def live_leases(self, holder_kind: str, holder_id: str) -> list[str]:
        """The asset ids that the holder holds now."""
        with self._media_jobs.connection(self._path) as conn:
            return [row[0] for row in conn.execute(
                "SELECT asset_id FROM media_input_lease WHERE holder_kind = ? AND holder_id = ? AND released_at IS NULL "
                "ORDER BY created, lease_id", (holder_kind, holder_id))]

    def sweep_scan(self, *, effect_grace_s: float = 3600, request_ttl_s: float = 3600) -> list[Discard]:
        """First half of a pass: release what has no live holder, commit, and name the files that looked unheld."""
        with self._write() as conn:
            return sweep_in(conn, now=self._clock(), effect_grace_s=effect_grace_s, request_ttl_s=request_ttl_s)

    def discard_unheld(self, found: Iterable[Discard], discard: Callable[[str, str], None]) -> int:
        """Second half: remove each file, but only if nothing holds it *now*. A lease or ingest marker can land after
        the scan committed, so the check and the removal share one write transaction per file: a writer that comes
        later waits for the lock and then sees the file gone, and one that came earlier is seen by the check. A file
        that is held after all stays, and its holder inherits the duty to remove it (``created_asset`` is set), so
        it is not left behind when the holder lets go. Returns how many files were discarded. A failed ``discard``
        raises and leaves the file; the rows of the files before it are already committed. Callers pass a
        ``discard`` that does not raise for a file that is already gone."""
        removed = 0
        for item in found:
            with self._write() as conn:
                held = conn.execute(
                    "UPDATE media_input_lease SET created_asset = 1 WHERE asset_id = ? AND released_at IS NULL",
                    (item.asset_id,),
                ).rowcount
                pending = conn.execute(
                    "UPDATE media_input_effect SET created_asset = 1 WHERE asset_id = ?", (item.asset_id,),
                ).rowcount
                if held or pending:
                    continue
                discard(item.asset_id, item.owner_key)
                removed += 1
        return removed

    def sweep(self, discard: Callable[[str, str], None], *, effect_grace_s: float = 3600,
              request_ttl_s: float = 3600) -> int:
        """One pass: release what has no live holder, then call ``discard(asset_id, owner_key)`` for each file that
        is still unheld when its turn comes. Returns how many files were discarded."""
        found = self.sweep_scan(effect_grace_s=effect_grace_s, request_ttl_s=request_ttl_s)
        return self.discard_unheld(found, discard)
