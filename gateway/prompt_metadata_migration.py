"""Resumable removal of embedded metadata from pre-upgrade cached assets.

Asset filenames remain opaque handles. The progress database stores only file
fingerprints and error codes, never media metadata. Call ``strip_cached_assets``
off the event loop for delete-all/kill-switch; incomplete results must be shown
as pending. ``background_migration`` retries failures and replaced files.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from gateway import media_metadata

# Share this lock with in-process cache writers/deleters to make the final
# stat/replace indivisible with respect to their mutations. An external writer
# must likewise coordinate; filesystem rename does not provide compare-and-swap.
CACHE_MUTATION_LOCK = threading.RLock()
_SCAN_LOCK = threading.Lock()
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_SCRATCH = re.compile(r"^[0-9a-f]{32}\.(?:in|out|probe)\.[a-z0-9]+$")
_STAGING = re.compile(r"^([0-9a-f]{64})\.([a-z0-9]+)\.tmp$")
_ASSET_ID = re.compile(r"^(?:img|aud|vid)_([0-9a-f]{64})$")
_DDL = """CREATE TABLE IF NOT EXISTS prompt_metadata_migration (
    path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, version INTEGER NOT NULL
)"""
_VERSION = 1
_ATTEMPTS_DDL = """CREATE TABLE IF NOT EXISTS prompt_metadata_migration_attempts (
    path TEXT PRIMARY KEY, attempted_at INTEGER NOT NULL
)"""


@dataclass
class MigrationResult:
    scanned: int = 0
    stripped: int = 0
    already_clean: int = 0
    pending: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.pending == 0


def _fingerprint(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def _strip_file(path: Path, before: os.stat_result) -> str | None:
    """Return the new fingerprint, or None if a concurrent mutation won."""
    # O_NOFOLLOW rejects symlinks swapped in since the directory scan.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        if _fingerprint(os.fstat(source.fileno())) != _fingerprint(before):
            return None
        content = source.read()
        if _fingerprint(os.fstat(source.fileno())) != _fingerprint(before):
            return None
    if media_metadata.media_kind(path.suffix) in {"png", "jpeg", "webp"}:
        from io import BytesIO
        from PIL import Image
        try:
            with Image.open(BytesIO(content)) as image:
                image.verify()
        except (OSError, ValueError, SyntaxError) as exc:
            raise media_metadata.MediaMetadataError("invalid cached image") from exc
    clean = media_metadata.strip_metadata(content, path.suffix)
    if clean == content:
        with CACHE_MUTATION_LOCK:
            current = path.stat(follow_symlinks=False)
            return _fingerprint(current) if _fingerprint(current) == _fingerprint(before) else None
    # Only already-stripped bytes may enter a named cache scratch file.
    fd, name = tempfile.mkstemp(prefix=".metadata-clean-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), stat.S_IMODE(before.st_mode))
            target.write(clean)
            target.flush()
            os.fsync(target.fileno())
        with CACHE_MUTATION_LOCK:
            if _fingerprint(path.stat(follow_symlinks=False)) != _fingerprint(before):
                return None
            os.replace(temp, path)
            return _fingerprint(path.stat(follow_symlinks=False))
    finally:
        temp.unlink(missing_ok=True)


def purge_legacy_scratch(workdir: Path) -> MigrationResult:
    """Remove only the old metadata helper's recognizable anonymous filenames.

    New versions use memfd exclusively, so these names have no active writer.
    Errors are counted without including prompt-bearing error messages.
    """
    result = MigrationResult()
    if not Path(workdir).exists():
        return result
    with CACHE_MUTATION_LOCK:
        for path in Path(workdir).iterdir():
            if not _LEGACY_SCRATCH.fullmatch(path.name):
                continue
            result.scanned += 1
            try:
                path.unlink(missing_ok=True)
                result.stripped += 1
            except OSError as exc:
                result.pending += 1
                result.errors[path.name] = type(exc).__name__
    return result


def strip_cached_assets(
    cache_dir: Path,
    state_path: Path,
    *,
    asset_ids: Iterable[str] | None = None,
    legacy_workdir: Path | None = None,
    throttle_seconds: float = 0.0,
    max_files: int | None = None,
    stop_event: threading.Event | None = None,
) -> MigrationResult:
    """Strip all cached assets, or the caller's already-authorized asset IDs.

    Progress commits after each clean file. Failure leaves the source intact
    and reports a pending item; subsequent calls retry it. Deleted files are
    harmless. Every run rescans, so new/replaced files cannot hide behind an
    earlier completion marker. ``max_files`` bounds work, not the pending count.
    """
    cache_dir, state_path = Path(cache_dir), Path(state_path)
    digests = None
    if asset_ids is not None:
        digests = set()
        for asset_id in asset_ids:
            match = _ASSET_ID.fullmatch(str(asset_id))
            if match is None:
                raise ValueError("invalid asset ID")
            digests.add(match[1])
    result = purge_legacy_scratch(legacy_workdir or cache_dir / "media_metadata")
    if not cache_dir.exists():
        return result
    if throttle_seconds < 0 or (max_files is not None and max_files < 0):
        raise ValueError("migration limits cannot be negative")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with _SCAN_LOCK, sqlite3.connect(state_path, timeout=30) as db:
        db.execute(_DDL)
        db.execute(_ATTEMPTS_DDL)
        attempts = dict(db.execute("SELECT path, attempted_at FROM prompt_metadata_migration_attempts"))
        processed = 0
        # Permanent failures must not starve later files in bounded batches.
        paths = sorted(cache_dir.iterdir(), key=lambda path: (attempts.get(str(path.absolute()), 0), path.name))
        for path in paths:
            staging = _STAGING.fullmatch(path.name)
            if staging and (digests is None or staging[1] in digests):
                if media_metadata.media_kind("." + staging[2]) is None:
                    continue
                with CACHE_MUTATION_LOCK:
                    try:
                        info = path.stat(follow_symlinks=False)
                        result.scanned += 1
                        if time.time() - info.st_mtime < 3600:
                            result.pending += 1
                            result.errors[path.name] = "staging_file_recent"
                        else:
                            path.unlink()
                            result.stripped += 1
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        result.pending += 1
                        result.errors[path.name] = type(exc).__name__
                continue
            if not _DIGEST.fullmatch(path.stem) or (digests is not None and path.stem not in digests):
                continue
            # Sidecars and signing secrets are not media. Recognize GIF/PCM
            # explicitly so unsupported tagged GIFs stay honestly pending.
            kind = media_metadata.media_kind(path.suffix)
            if kind is None and path.suffix not in {".gif", ".pcm"}:
                continue
            try:
                before = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(before.st_mode):
                result.pending += 1
                result.errors[path.name] = "not_regular_file"
                continue
            result.scanned += 1
            row = db.execute("SELECT fingerprint, version FROM prompt_metadata_migration WHERE path=?", (str(path.absolute()),)).fetchone()
            if row == (_fingerprint(before), _VERSION):
                result.already_clean += 1
                continue
            if (max_files is not None and processed >= max_files) or (stop_event is not None and stop_event.is_set()):
                result.pending += 1
                continue
            processed += 1
            db.execute("INSERT OR REPLACE INTO prompt_metadata_migration_attempts VALUES (?, ?)",
                       (str(path.absolute()), time.time_ns()))
            db.commit()
            try:
                if path.suffix == ".gif":
                    raise media_metadata.MediaMetadataError("unsupported cached format")
                fingerprint = _strip_file(path, before)
                if fingerprint is None:
                    result.pending += 1
                    result.errors[path.name] = "concurrent_change"
                    continue
                db.execute("INSERT OR REPLACE INTO prompt_metadata_migration VALUES (?, ?, ?)", (str(path.absolute()), fingerprint, _VERSION))
                db.commit()
                result.stripped += 1
            except FileNotFoundError:
                pass  # Concurrent deletion already removed all prompt bytes.
            except (OSError, media_metadata.MediaMetadataError) as exc:
                result.pending += 1
                result.errors[path.name] = type(exc).__name__
            finally:
                if throttle_seconds:
                    if stop_event is not None:
                        stop_event.wait(throttle_seconds)
                    else:
                        time.sleep(throttle_seconds)
    return result


def _low_priority_scan(cache_dir: Path, state_path: Path, stop_event: threading.Event,
                       throttle_seconds: float, batch_size: int, legacy_workdir: Path | None) -> MigrationResult:
    # Linux applies this to the worker thread, not the event-loop thread.
    thread_id = threading.get_native_id()
    previous = os.getpriority(os.PRIO_PROCESS, thread_id)
    try:
        os.setpriority(os.PRIO_PROCESS, thread_id, max(previous, 10))
    except OSError:
        pass
    return strip_cached_assets(cache_dir, state_path, throttle_seconds=throttle_seconds,
                               max_files=batch_size, stop_event=stop_event, legacy_workdir=legacy_workdir)


async def background_migration(cache_dir: Path, state_path: Path, *,
                               throttle_seconds: float = 0.05,
                               retry_seconds: float = 60,
                               batch_size: int = 100,
                               legacy_workdir: Path | None = None) -> MigrationResult:
    """Run throttled batches in a dedicated low-priority thread until clean.

    Cancellation signals the worker to stop after the current atomic strip.
    The caller can inspect synchronous results on privacy settings mutations.
    """
    from concurrent.futures import ThreadPoolExecutor

    if batch_size < 1 or throttle_seconds < 0 or retry_seconds < 0:
        raise ValueError("invalid background migration limits")
    stop_event = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prompt-metadata")
    loop = asyncio.get_running_loop()
    try:
        while True:
            result = await loop.run_in_executor(executor, _low_priority_scan, Path(cache_dir),
                                               Path(state_path), stop_event, throttle_seconds, batch_size, legacy_workdir)
            if result.complete:
                return result
            await asyncio.sleep(retry_seconds if result.errors and result.stripped == 0 else 0)
    finally:
        stop_event.set()
        # Never block the loop waiting for an ffmpeg child. The worker cleans
        # its memfds/clean scratch in finally; the executor has one private thread.
        executor.shutdown(wait=False, cancel_futures=True)
