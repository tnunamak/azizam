"""Bounded, nonblocking metrics submissions to one SQLite owner thread.

Samples and maintenance share a FIFO so a rollup sees earlier accepted samples.
Only the owner initializes, writes, checkpoints and closes the database. Readers
keep their own short-lived connections. Call flush/close off the event loop.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Literal

import metrics_store


class MetricsWriter:
    def __init__(self, path: Path, *, capacity: int = 4096):
        if capacity < 1:
            raise ValueError("metrics queue capacity must be positive")
        self._path = path
        self._queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)
        self._submitted = 0
        self._completed = 0
        self._accepting = True
        self._error: Exception | None = None
        self._dropped_samples = 0
        self._dropped_commands = 0
        self._write_errors = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="azizam-metrics-writer", daemon=True)
        self._thread.start()

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "accepting": self._accepting,
                "queued_commands": self._queue.qsize(),
                "dropped_samples": self._dropped_samples,
                "dropped_commands": self._dropped_commands,
                "write_errors": self._write_errors,
            }

    def _submit(self, command: tuple, sample_count: int = 0) -> bool:
        # This lock only protects queue admission and counters, never SQLite I/O.
        with self._lock:
            if self._accepting:
                try:
                    self._queue.put_nowait(command)
                    self._submitted += 1
                    return True
                except queue.Full:
                    pass
            self._dropped_samples += sample_count
            self._dropped_commands += 1
            return False

    def record_samples(self, samples: Iterable[tuple]) -> bool:
        rows = tuple(samples)
        if not rows:
            return True
        return self._submit(("samples", rows), len(rows))

    def record_sample(self, ts: int, metric: str, value: float, tags: dict | None = None) -> bool:
        return self.record_samples(((int(ts), metric, metrics_store._serialize_tags(tags), float(value)),))

    def maintain(self, operation: Literal["minute", "hour", "prune"], now: int) -> bool:
        return self._submit((operation, int(now)))

    def flush(self) -> None:
        """Wait for accepted work; raises if persistence failed. Off-loop only."""
        with self._drained:
            target = self._submitted
            self._drained.wait_for(lambda: self._completed >= target)
            if self._error is not None:
                raise RuntimeError("metrics persistence failed") from self._error

    def wait_ready(self) -> None:
        """Wait for schema migration and connection creation. Off-loop only."""
        self._ready.wait()
        if self._error is not None:
            raise RuntimeError("metrics writer could not start") from self._error

    def close(self) -> None:
        """Stop admission, drain FIFO, checkpoint and close on the owner thread."""
        with self._lock:
            self._accepting = False
        self._thread.join()
        if self._error is not None:
            raise RuntimeError("metrics persistence failed during shutdown") from self._error

    def _failed(self, error: Exception) -> None:
        with self._lock:
            self._write_errors += 1
            if self._error is None:
                self._error = error
        # Do not log sample tags (which can contain request identities).
        print(f"[metrics_writer] persistence failed: {type(error).__name__}", flush=True)

    def _finish(self) -> None:
        self._queue.task_done()
        with self._drained:
            self._completed += 1
            self._drained.notify_all()

    def _execute(self, conn: sqlite3.Connection, command: tuple) -> None:
        operation, payload = command
        while True:
            try:
                conn.execute("BEGIN")
                if operation == "samples":
                    metrics_store.record_samples(conn, payload)
                elif operation == "minute":
                    metrics_store.downsample_minute(conn, now=payload)
                elif operation == "hour":
                    metrics_store.downsample_hour(conn, now=payload)
                elif operation == "prune":
                    counts = metrics_store.prune_old(conn, now=payload)
                    print(f"[metrics_sampler] prune: {counts}", flush=True)
                else:
                    raise ValueError("unknown metrics operation")
                conn.commit()
                return
            except sqlite3.OperationalError as error:
                conn.rollback()
                # Retry temporary writer contention without losing accepted rows.
                if getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    time.sleep(0.1)
                    continue
                raise
            except Exception:
                conn.rollback()
                raise

    def _run(self) -> None:
        conn = None
        try:
            metrics_store.init_db(self._path)
            conn = metrics_store.open_writer(self._path)
            self._ready.set()
            while True:
                try:
                    command = self._queue.get(timeout=0.1)
                except queue.Empty:
                    with self._lock:
                        # A producer may have admitted work after get() timed
                        # out and before close() stopped admission. Recheck
                        # emptiness under the same lock used by both of them.
                        if not self._accepting and self._queue.empty():
                            break
                    continue
                try:
                    self._execute(conn, command)
                except Exception as error:
                    self._failed(error)
                finally:
                    self._finish()
        except Exception as error:
            self._failed(error)
        finally:
            self._ready.set()
            with self._lock:
                self._accepting = False
            # Initialization failures must also release flush waiters.
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                self._finish()
            if conn is not None:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except Exception as error:
                    self._failed(error)
                finally:
                    try:
                        conn.close()
                    except Exception as error:
                        self._failed(error)


_writer: MetricsWriter | None = None
_writer_lock = threading.Lock()


def get_writer() -> MetricsWriter:
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = MetricsWriter(metrics_store.DB_PATH)
        return _writer


def start_writer() -> MetricsWriter:
    """Start a new lifecycle; closed writers never reopen from request handlers."""
    global _writer
    with _writer_lock:
        if _writer is None or not _writer.stats["accepting"]:
            if _writer is not None and _writer._thread.is_alive():
                raise RuntimeError("metrics writer is still closing")
            _writer = MetricsWriter(metrics_store.DB_PATH)
        return _writer


def close_writer() -> None:
    with _writer_lock:
        writer = _writer
    if writer is not None:
        writer.close()


def writer_stats() -> dict | None:
    with _writer_lock:
        writer = _writer
    return writer.stats if writer is not None else None
