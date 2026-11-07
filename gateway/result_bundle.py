"""Named results of a job: staging, publication, the private JSON store, the completion receipt (S2 design, D4 and D6).

A job's results are a bundle: each result has a name and a kind, ``json`` (one value) or ``assets`` (a list of stored
files). A run stages its results first, privately, in ``result_stage``. Publication is one SQLite transaction that sets
the job's bundle reference, marks the job's stage rows ``published`` and moves the job to its terminal state
(``publish_in``, called from ``media_jobs.update_status(before_commit=...)``). It runs only after the limits pass and
after any cancel has settled: ``update_status`` refuses a success while a cancel is pending, before the hook runs.
After the commit, the completion receipt is linked, once per job (``link_receipt``). A crash between publication and
the link leaves a published bundle with no receipt; the startup scan (``startup_scan``) links it. The same scan deletes
the stage rows and values of every attempt that is neither running nor published.

JSON values live in the private store ``result_json``, with the maker stamp and owner scope of the job that made them.
``read_json`` answers only the maker of the job, under the owner scope of the job's App, and returns ``application/json``
bytes. Values go nowhere else: not job metadata, not events, not receipts. A receipt gets the envelope identity and each
result's name, kind, count and size, never a value.

The limits (D6) are deployment limits read from the trusted runtime config, never from a manifest. A JSON value is
checked while it is read: size, depth, item count, string length, non-finite numbers and duplicate keys. An asset
stream is checked while it is saved: ``LimitedAssetStore`` wraps any asset store and counts what it reads.

Every path that deletes or erases a job gives its results up in the job's own transaction: ``release_in`` (job delete)
and ``erase_in`` (erasure, forget and the kill switch). ``tests/test_result_bundle_release.py`` finds those paths in the
code.

This module names no workflow, engine, model, intent or modality: a result kind says how a value is held, and the
caller says what it is called.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from gateway import media_jobs
from gateway.manifests import RESULT_NAME_RE, RESULT_NAME_RULE

BUNDLE_VERSION = 1
KINDS = ("json", "assets")
JSON_MEDIA_TYPE = "application/json"
# The name under which a route that has no named results of its own mirrors the files it already lists.
DEFAULT_NAME = "assets"
CONFIG_SECTION = "workflow_execution"
CONFIG_KEY = "result_limits"
# A bundle published long ago and still unlinked is given up: its receipt has been pruned.
LINK_WINDOW_S = media_jobs.REPLAY_WINDOW_S
JSON_RETENTION_S = 30 * 24 * 3600


class ResultRefused(ValueError):
    """A result broke a limit or the bundle's shape. ``code`` names the rule; the message never holds a value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------------------------------------- limits

@dataclass(frozen=True)
class ResultLimits:
    """Trusted deployment limits (D6). The defaults are ceilings no ordinary result reaches."""

    max_assets: int = 64
    max_asset_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    json_max_bytes: int = 4 * 1024 * 1024
    json_max_depth: int = 32
    json_max_items: int = 100_000
    json_max_string_length: int = 1_000_000
    # Reading the engine's answer (D6): selectors in one binding, bytes of the history answer, seconds for one fetch,
    # and result fetches in flight at once.
    max_selectors: int = 16
    max_history_bytes: int = 8 * 1024 * 1024
    fetch_timeout_s: int = 300
    max_concurrent_fetches: int = 8


def limits_from_config(config: Mapping[str, Any]) -> ResultLimits:
    """The limits in ``workflow_execution.result_limits`` of the runtime config. A missing section, an unknown key or
    a value that is not a positive integer keeps the default for that field."""
    section = config.get(CONFIG_SECTION) if isinstance(config, Mapping) else None
    raw = section.get(CONFIG_KEY) if isinstance(section, Mapping) else None
    if not isinstance(raw, Mapping):
        return ResultLimits()
    defaults = ResultLimits()
    chosen = {
        name: value
        for name, value in raw.items()
        if hasattr(defaults, str(name)) and isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    return ResultLimits(**chosen)


def _refuse_non_finite(_token: str) -> Any:
    raise ResultRefused("json_non_finite", "a JSON value holds a number that is not finite")


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ResultRefused("json_non_finite", "a JSON value holds a number that is not finite")
    return number


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ResultRefused("json_duplicate_key", "a JSON object repeats a key")
        seen[key] = value
    return seen


def _depth_of(raw: bytes, limit: int) -> None:
    """Refuse before parsing if brackets nest deeper than ``limit``. The parser would recurse; this scan does not."""
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > limit:
                raise ResultRefused("json_too_deep", f"a JSON value nests deeper than {limit} levels")
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1


def check_json(raw: bytes, limits: ResultLimits) -> Any:
    """Parse ``raw`` as one JSON value under the limits, or raise ``ResultRefused``. Returns the parsed value."""
    if len(raw) > limits.json_max_bytes:
        raise ResultRefused("json_too_large", f"a JSON value is larger than {limits.json_max_bytes} bytes")
    _depth_of(bytes(raw), limits.json_max_depth)
    try:
        text = bytes(raw).decode("utf-8")
        value = json.loads(text, parse_constant=_refuse_non_finite, parse_float=_finite_float,
                           object_pairs_hook=_no_duplicates)
    except ResultRefused:
        raise
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise ResultRefused("json_malformed", "a JSON result cannot be read as JSON") from exc
    items = 0
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if len(node) > limits.json_max_string_length:
                raise ResultRefused("json_string_too_long", "a JSON string is longer than the limit")
        elif isinstance(node, dict):
            items += len(node)
            for key, child in node.items():
                if len(key) > limits.json_max_string_length:
                    raise ResultRefused("json_string_too_long", "a JSON string is longer than the limit")
                stack.append(child)
        elif isinstance(node, list):
            items += len(node)
            stack.extend(node)
        if items > limits.json_max_items:
            raise ResultRefused("json_too_many_items", f"a JSON value holds more than {limits.json_max_items} items")
    return value


def check_assets(limits: ResultLimits, sizes: Sequence[int]) -> None:
    """Refuse a set of files that has too many of them, one that is too large, or a total that is too large.
    Use before saving the files, when the sizes are known."""
    if len(sizes) > limits.max_assets:
        raise ResultRefused("too_many_assets", f"a result holds more than {limits.max_assets} assets")
    if any(size > limits.max_asset_bytes for size in sizes):
        raise ResultRefused("asset_too_large", f"an asset is larger than {limits.max_asset_bytes} bytes")
    if sum(sizes) > limits.max_total_bytes:
        raise ResultRefused("total_too_large", f"a result is larger than {limits.max_total_bytes} bytes in total")


class LimitedAssetStore:
    """An asset store that enforces the limits while it reads (D6): the stream stops at the first byte over a limit.

    It wraps any store with the engine protocol's ``save(chunks, content_type=, max_bytes=, name=)`` and ``read``. One
    instance serves one run: it counts the assets and the bytes of that run. Over a limit it raises ``ResultRefused``
    while the inner store is still reading. Whether the inner store keeps a partial file then is the inner store's
    behaviour, not this wrapper's: the caller must not record the asset, and an unrecorded file is the inner store's to
    clean up.
    """

    def __init__(self, inner: Any, limits: ResultLimits) -> None:
        self._inner = inner
        self._limits = limits
        self._assets = 0
        self._bytes = 0

    async def read(self, ref: Any, *, max_bytes: int) -> bytes:
        return await self._inner.read(ref, max_bytes=max_bytes)

    async def save(self, chunks: AsyncIterator[bytes], *, content_type: str, max_bytes: int, name: str | None = None) -> Any:
        if self._assets + 1 > self._limits.max_assets:
            raise ResultRefused("too_many_assets", f"a result holds more than {self._limits.max_assets} assets")
        asset_cap = min(max_bytes, self._limits.max_asset_bytes)
        room = self._limits.max_total_bytes - self._bytes
        seen = 0

        async def counted() -> AsyncIterator[bytes]:
            nonlocal seen
            async for chunk in chunks:
                seen += len(chunk)
                if seen > self._limits.max_asset_bytes:
                    raise ResultRefused("asset_too_large", f"an asset is larger than {self._limits.max_asset_bytes} bytes")
                if seen > room:
                    raise ResultRefused("total_too_large",
                                        f"a result is larger than {self._limits.max_total_bytes} bytes in total")
                yield chunk

        ref = await self._inner.save(counted(), content_type=content_type, max_bytes=asset_cap, name=name)
        self._assets += 1
        self._bytes += seen
        return ref


# ------------------------------------------------------------------------------------------------------- staging

def _now() -> float:
    return time.time()


def _stage_ref(job_id: str, attempt: int, name: str) -> str:
    return f"stage:{job_id}:{attempt}:{name}"


def _require_name(name: Any) -> str:
    if not isinstance(name, str) or not RESULT_NAME_RE.fullmatch(name):
        raise ResultRefused("bad_name", RESULT_NAME_RULE)
    return name


def _job_row(conn: sqlite3.Connection, job_id: str) -> Mapping[str, Any]:
    row = conn.execute("SELECT job_json FROM media_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise ResultRefused("no_job", "the job does not exist")
    data = json.loads(row[0])
    if not isinstance(data, Mapping):
        raise ResultRefused("no_job", "the job record cannot be read")
    return data


def attempt_of(job: Mapping[str, Any]) -> int:
    """The attempt a job's results belong to. A legacy job without an attempt number is attempt 1."""
    value = job.get("attempt_number")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def _erased(conn: sqlite3.Connection, job: Mapping[str, Any]) -> bool:
    """Whether erasure has covered this job, so a stale worker must not write a value for it."""
    from gateway.ownership import stored_owner_key

    owner = stored_owner_key(job.get("owner_key_hash"), app_id=job.get("app_id"))
    cutoff = conn.execute(
        "SELECT MAX(cutoff_at) FROM media_job_prompt_erasure_cutoffs WHERE owner_key IN (?, ?)", ("*", owner),
    ).fetchone()[0]
    created = job.get("created_at")
    return cutoff is not None and isinstance(created, int) and created <= cutoff


def stage_json_in(
    conn: sqlite3.Connection, job_id: str, attempt: int, name: str, raw: bytes, *, limits: ResultLimits,
    now: float | None = None,
) -> str:
    """Stage one JSON result: check ``raw`` under the limits, then write the value to the private store and a stage row.
    Returns the stage reference. Staging the same name again in the same attempt replaces the earlier stage.
    Needs the caller's write transaction."""
    name = _require_name(name)
    check_json(raw, limits)
    job = _job_row(conn, job_id)
    if _erased(conn, job):
        raise ResultRefused("erased", "the job's results were erased")
    created = _now() if now is None else now
    ref = _stage_ref(job_id, attempt, name)
    owner = job.get("owner_scope")
    if not isinstance(owner, str) or not owner:
        raise ResultRefused("no_owner", "the job has no owner scope")
    _drop_stage(conn, job_id, attempt, name)
    conn.execute(
        "INSERT INTO result_json (ref, job_id, owner_scope, maker_person_id, maker_credential_id, maker_connection_id, "
        "value, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ref, job_id, owner, job.get("maker_person_id"), job.get("maker_credential_id"),
         job.get("maker_connection_id"), bytes(raw), created),
    )
    conn.execute(
        "INSERT INTO result_stage (job_id, attempt, name, ref, state, kind, count, bytes, sha256, items_json, created) "
        "VALUES (?, ?, ?, ?, 'staged', 'json', 1, ?, ?, NULL, ?)",
        (job_id, attempt, name, ref, len(raw), "sha256:" + hashlib.sha256(raw).hexdigest(), created),
    )
    return ref


def stage_assets_in(
    conn: sqlite3.Connection, job_id: str, attempt: int, name: str, items: Sequence[Mapping[str, Any]], *,
    limits: ResultLimits, now: float | None = None,
) -> str:
    """Stage one list of stored files. Each item is ``{"asset_ref", "media_type", "size_bytes"}``. The files are
    already saved: this records which ones belong to the result. Needs the caller's write transaction."""
    name = _require_name(name)
    clean: list[dict[str, Any]] = []
    for item in items:
        ref, media_type, size = item.get("asset_ref"), item.get("media_type"), item.get("size_bytes")
        if (not isinstance(ref, str) or not ref or not isinstance(media_type, str)
                or not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise ResultRefused("bad_asset", "an asset entry needs asset_ref, media_type and size_bytes")
        clean.append({"asset_ref": ref, "media_type": media_type, "size_bytes": size})
    check_assets(limits, [item["size_bytes"] for item in clean])
    if _erased(conn, _job_row(conn, job_id)):
        raise ResultRefused("erased", "the job's results were erased")
    _drop_stage(conn, job_id, attempt, name)
    ref = _stage_ref(job_id, attempt, name)
    conn.execute(
        "INSERT INTO result_stage (job_id, attempt, name, ref, state, kind, count, bytes, sha256, items_json, created) "
        "VALUES (?, ?, ?, ?, 'staged', 'assets', ?, ?, NULL, ?, ?)",
        (job_id, attempt, name, ref, len(clean), sum(item["size_bytes"] for item in clean),
         json.dumps(clean, sort_keys=True, separators=(",", ":")), _now() if now is None else now),
    )
    return ref


def _drop_stage(conn: sqlite3.Connection, job_id: str, attempt: int, name: str) -> None:
    ref = _stage_ref(job_id, attempt, name)
    state = conn.execute(
        "SELECT state FROM result_stage WHERE job_id = ? AND attempt = ? AND name = ?", (job_id, attempt, name),
    ).fetchone()
    if state is not None and state[0] == "published":
        raise ResultRefused("published", "a published result cannot be staged again")
    conn.execute("DELETE FROM result_json WHERE ref = ?", (ref,))
    conn.execute("DELETE FROM result_stage WHERE job_id = ? AND attempt = ? AND name = ?", (job_id, attempt, name))


# ------------------------------------------------------------------------------------------------- publication

def _envelope_identity(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    from gateway.definition_store import DefinitionUnavailable, envelope_in

    try:
        envelope = envelope_in(conn, job_id)
    except DefinitionUnavailable:
        return None
    if envelope is None:
        return None
    identity: dict[str, Any] = {
        "source_kind": envelope.source_kind, "source_id": envelope.source_id,
        "source_revision": envelope.source_revision, "contract": envelope.contract, "binding": envelope.binding,
        "adapter_id": envelope.adapter_id, "interface_version": envelope.interface_version,
    }
    if envelope.template is not None:
        identity["template"] = envelope.template
    return identity


def publish_in(
    conn: sqlite3.Connection, job_id: str, attempt: int, *, limits: ResultLimits, now: float | None = None,
) -> str:
    """The publication step of D4, in the caller's transaction: set the bundle reference, mark the attempt's stage rows
    published. The caller's same transaction moves the job to its terminal state (``update_status(before_commit=...)``).

    Checks first, writes after: at least one staged result; the bundle's total size and asset count under the limits;
    every JSON value present and unchanged. A failure raises ``ResultRefused`` and writes nothing. Stage rows of other,
    unpublished attempts of the job are marked ``aborted``. Returns the bundle reference.
    """
    published_at = _now() if now is None else now
    if _erased(conn, _job_row(conn, job_id)):
        raise ResultRefused("erased", "the job's results were erased")
    if conn.execute("SELECT 1 FROM result_bundle WHERE job_id = ?", (job_id,)).fetchone() is not None:
        raise ResultRefused("published", "the job already published its results")
    rows = conn.execute(
        "SELECT name, ref, kind, count, bytes, sha256 FROM result_stage "
        "WHERE job_id = ? AND attempt = ? AND state = 'staged' ORDER BY name", (job_id, attempt),
    ).fetchall()
    if not rows:
        raise ResultRefused("no_results", "the attempt staged no results")
    total = sum(row[4] for row in rows)
    if total > limits.max_total_bytes:
        raise ResultRefused("total_too_large", f"a result is larger than {limits.max_total_bytes} bytes in total")
    if sum(row[3] for row in rows if row[2] == "assets") > limits.max_assets:
        raise ResultRefused("too_many_assets", f"a result holds more than {limits.max_assets} assets")
    for _name, ref, kind, _count, size, sha in rows:
        if kind != "json":
            continue
        value = conn.execute("SELECT value FROM result_json WHERE ref = ?", (ref,)).fetchone()
        if value is None or len(value[0]) != size or "sha256:" + hashlib.sha256(bytes(value[0])).hexdigest() != sha:
            raise ResultRefused("result_missing", "a staged JSON value is missing or changed")
    bundle_ref = f"bundle:{job_id}:{attempt}"
    summary = {
        "bundle_version": BUNDLE_VERSION, "bundle": bundle_ref, "envelope": _envelope_identity(conn, job_id),
        "results": [{"name": name, "kind": kind, "count": count, "size_bytes": size}
                    for name, _ref, kind, count, size, _sha in rows],
    }
    conn.execute(
        "INSERT INTO result_bundle (job_id, attempt, bundle_ref, summary_json, published_at, receipt_linked_at) "
        "VALUES (?, ?, ?, ?, ?, NULL)",
        (job_id, attempt, bundle_ref, json.dumps(summary, sort_keys=True, separators=(",", ":")), published_at),
    )
    from gateway.definition_store import set_bundle_in

    set_bundle_in(conn, job_id, bundle_ref)
    conn.execute(
        "UPDATE result_stage SET state = 'published' WHERE job_id = ? AND attempt = ? AND state = 'staged'",
        (job_id, attempt),
    )
    conn.execute(
        "UPDATE result_stage SET state = 'aborted' WHERE job_id = ? AND attempt != ? AND state = 'staged'",
        (job_id, attempt),
    )
    return bundle_ref


def abort(job_id: str, attempt: int, *, path: Path = media_jobs.DB_PATH) -> int:
    """Mark the attempt's staged rows ``aborted`` (a run that failed or was canceled). The startup scan and the sweep
    delete them. Returns how many rows changed."""
    with media_jobs.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE result_stage SET state = 'aborted' WHERE job_id = ? AND attempt = ? AND state = 'staged'",
            (job_id, attempt),
        )
        conn.commit()
        return cursor.rowcount


def complete_job(
    job_id: str,
    stage: Callable[[sqlite3.Connection, int], None],
    *,
    limits: ResultLimits,
    status: str = media_jobs.STATUS_SUCCEEDED,
    path: Path = media_jobs.DB_PATH,
    receipts_path: Path | None = None,
    **status_args: Any,
) -> dict[str, Any] | None:
    """Stage, publish and link, for a run that has its results in hand.

    ``stage(conn, attempt)`` writes the run's results with ``stage_json_in`` and ``stage_assets_in`` in their own
    transaction. Then one transaction publishes the bundle and moves the job to ``status`` (``status_args`` go to
    ``media_jobs.update_status``). If anything fails, the attempt's stage is aborted and the error surfaces: a refused
    result as ``ResultRefused``, a pending cancel as ``media_jobs.CancellationPending``. After the commit the completion
    receipt is linked; a failure there is left to the startup scan and never fails the job. Returns the updated job.
    """
    job = media_jobs.get(job_id, audience="internal", path=path)
    if job is None:
        return None
    attempt = attempt_of(job)
    try:
        with media_jobs.connection(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                stage(conn, attempt)
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        def publish(conn: sqlite3.Connection, _job: dict[str, Any]) -> None:
            publish_in(conn, job_id, attempt, limits=limits)

        updated = media_jobs.update_status(job_id, status, path=path, before_commit=publish, **status_args)
    except BaseException:
        abort(job_id, attempt, path=path)
        raise
    try:
        link_receipt(job_id, jobs_path=path, receipts_path=receipts_path)
    except Exception:  # noqa: BLE001 - the startup scan links it; the job is done
        pass
    return updated


def mirror_assets(
    job_id: str, assets: Iterable[Mapping[str, Any]], *, limits: ResultLimits, name: str = DEFAULT_NAME,
    stored: Mapping[str, Mapping[str, Any]] | None = None, **status_args: Any,
) -> dict[str, Any] | None:
    """``complete_job`` for a route that lists its files in the job's ``assets`` already: the bundle mirrors that list
    under one name. The job view does not change. ``stored`` gives the ``size_bytes`` and ``media_type`` of a file (by
    asset id) whose entry in the list does not carry them, so a route need not add a field to the job's list.
    ``status_args`` go to ``media_jobs.update_status``."""
    listed = [dict(asset) for asset in assets]
    known = stored or {}
    items = []
    for asset in listed:
        facts = known.get(str(asset.get("asset_id"))) or {}
        items.append({"asset_ref": asset.get("asset_id"), "media_type": asset.get("media_type", facts.get("media_type")),
                      "size_bytes": asset.get("size_bytes", facts.get("size_bytes"))})

    def stage(conn: sqlite3.Connection, attempt: int) -> None:
        stage_assets_in(conn, job_id, attempt, name, items, limits=limits)

    return complete_job(job_id, stage, limits=limits, add_assets=listed, **status_args)


# ----------------------------------------------------------------------------------------------------- receipts

def link_receipt(
    job_id: str, *, jobs_path: Path = media_jobs.DB_PATH, receipts_path: Path | None = None,
    now: float | None = None,
) -> bool:
    """Link the job's completion receipt to its published bundle, once per job. Idempotent: a bundle that is already
    linked is left alone, and writing the same summary twice is the same write.

    The receipt carries the envelope identity and each result's name, kind, count and size (``size_bytes``), never a
    value. Returns True when the bundle is linked. Returns False when the job has no published bundle, or its receipt
    is not stored yet (the next scan tries again)."""
    from gateway import receipt_store

    target = receipt_store.DB_PATH if receipts_path is None else receipts_path
    with media_jobs.connection(jobs_path) as conn:
        row = conn.execute(
            "SELECT summary_json, receipt_linked_at FROM result_bundle WHERE job_id = ?", (job_id,),
        ).fetchone()
    if row is None:
        return False
    if row[1] is not None:
        return True
    summary = json.loads(row[0])
    if receipt_store.merge_job_metadata(job_id, {"result_bundle": summary}, path=target) == 0:
        return False
    with media_jobs.connection(jobs_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE result_bundle SET receipt_linked_at = ? WHERE job_id = ? AND receipt_linked_at IS NULL",
            (_now() if now is None else now, job_id),
        )
        conn.commit()
    return True


def startup_scan(
    *, running: Iterable[tuple[str, int]] = (), jobs_path: Path = media_jobs.DB_PATH,
    receipts_path: Path | None = None, now: float | None = None,
) -> dict[str, int]:
    """The scan of D4, once at startup, before any job is resumed.

    It deletes the stage rows and JSON values of every attempt that is not published and not in ``running`` (a set of
    ``(job_id, attempt)``; a freshly started process has none). Deleting without a running set is safe only because one
    gateway process owns the jobs database: the process holds ``.owner.lock`` in its private directory for its whole
    life (gateway/private_tmp.py), so no other process can be mid-run while this one starts. Run it before this process
    starts any job. Then it links the receipt of every published bundle that
    has none. A bundle older than ``LINK_WINDOW_S`` with no receipt to link is given up, so the scan does not retry it
    forever. Returns the counts ``{"stage_rows", "linked", "gave_up"}``.
    """
    kept = set(running)
    current = _now() if now is None else now
    with media_jobs.connection(jobs_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        doomed = [(job, attempt, ref) for job, attempt, ref in conn.execute(
            "SELECT job_id, attempt, ref FROM result_stage WHERE state != 'published'")
            if (job, attempt) not in kept]
        for job, attempt, ref in doomed:
            conn.execute("DELETE FROM result_json WHERE ref = ?", (ref,))
            conn.execute("DELETE FROM result_stage WHERE job_id = ? AND attempt = ? AND ref = ? AND state != 'published'",
                         (job, attempt, ref))
        conn.commit()
        unlinked = [row[0] for row in conn.execute(
            "SELECT job_id FROM result_bundle WHERE receipt_linked_at IS NULL ORDER BY published_at")]
        stale = {row[0] for row in conn.execute(
            "SELECT job_id FROM result_bundle WHERE receipt_linked_at IS NULL AND published_at < ?",
            (current - LINK_WINDOW_S,))}
    linked = gave_up = 0
    for job_id in unlinked:
        if link_receipt(job_id, jobs_path=jobs_path, receipts_path=receipts_path, now=current):
            linked += 1
        elif job_id in stale:
            with media_jobs.connection(jobs_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE result_bundle SET receipt_linked_at = ? WHERE job_id = ?", (current, job_id))
                conn.commit()
            gave_up += 1
    return {"stage_rows": len(doomed), "linked": linked, "gave_up": gave_up}


# ---------------------------------------------------------------------------------------------- the JSON store

@dataclass(frozen=True)
class Reader:
    """Who is reading, from the authenticated request: the App's owner scope, and the person, credential or MCP
    connection the request ran as."""

    owner_scope: str
    person_id: str | None = None
    credential_id: str | None = None
    connection_id: str | None = None


@dataclass(frozen=True)
class JsonRead:
    body: bytes
    media_type: str = JSON_MEDIA_TYPE


def _may_read(reader: Reader, row: Sequence[Any]) -> bool:
    owner, person, credential, connection = row
    if not reader.owner_scope or reader.owner_scope != owner:
        return False
    # The maker stamp decides, in the order of the job's own authorization: the person, else the credential, else the
    # connection. A job with none of them has no maker and no reader.
    if person:
        return reader.person_id == person
    if credential:
        return reader.credential_id == credential
    if connection:
        return reader.connection_id == connection
    return False


def read_json(job_id: str, name: str, reader: Reader, *, path: Path = media_jobs.DB_PATH) -> JsonRead | None:
    """The published JSON result ``name`` of ``job_id`` for ``reader``, or None. None is the answer for every refusal
    (no such result, not published, expired, erased, another owner, another maker): a caller cannot tell them apart."""
    with media_jobs.connection(path) as conn:
        row = conn.execute(
            "SELECT j.owner_scope, j.maker_person_id, j.maker_credential_id, j.maker_connection_id, j.value "
            "FROM result_stage s JOIN result_json j ON j.ref = s.ref "
            "WHERE s.job_id = ? AND s.name = ? AND s.kind = 'json' AND s.state = 'published'",
            (job_id, name),
        ).fetchone()
    if row is None or not _may_read(reader, row[:4]):
        return None
    return JsonRead(body=bytes(row[4]))


def result_href(job_id: str, name: str) -> str:
    """Where the authenticated read of a JSON result is, on the gateway's job routes."""
    return f"/gateway/v1/media/jobs/{quote(job_id, safe='')}/results/{quote(name, safe='')}"


def results_view(job_ids: Iterable[str], *, path: Path = media_jobs.DB_PATH) -> dict[str, dict[str, dict[str, Any]]]:
    """The named results the client job view shows, by job: ``{name: {kind, count, href?}}``.

    Only published results are shown, so a job whose run failed, was canceled or is still running has none: a staged
    result is not readable and is not listed. A ``json`` result is listed with the ``href`` of its read route while its
    value is still stored (erasure and retention delete the value, and the result leaves the view with it). An
    ``assets`` result is listed with its count and no ``href``: its files are the job's ``assets``. The view holds no
    value, hash or size. A job with no published result is absent from the answer."""
    ids = sorted({job_id for job_id in job_ids if job_id})
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if not ids:
        return out
    with media_jobs.connection(path) as conn:
        if not _has_tables(conn):
            return out
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = conn.execute(
                "SELECT s.job_id, s.name, s.kind, s.count, j.ref IS NOT NULL FROM result_stage s "
                "LEFT JOIN result_json j ON j.ref = s.ref "
                f"WHERE s.state = 'published' AND s.job_id IN ({','.join('?' for _ in chunk)}) "  # noqa: S608
                "ORDER BY s.job_id, s.name", chunk,
            ).fetchall()
            for job_id, name, kind, count, stored in rows:
                if kind == "json" and not stored:
                    continue
                entry: dict[str, Any] = {"kind": kind, "count": count}
                if kind == "json":
                    entry["href"] = result_href(job_id, name)
                out.setdefault(job_id, {})[name] = entry
    return out


def bundle_summary(job_id: str, *, path: Path = media_jobs.DB_PATH) -> dict[str, Any] | None:
    """The published bundle's facts (name, kind, count, size), or None. Holds no value."""
    with media_jobs.connection(path) as conn:
        row = conn.execute("SELECT summary_json FROM result_bundle WHERE job_id = ?", (job_id,)).fetchone()
    return json.loads(row[0]) if row else None


# ------------------------------------------------------------------------------- release, erasure and retention

def _has_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_stage'").fetchone() is not None


def release_in(conn: sqlite3.Connection, job_id: str) -> None:
    """Delete the job's stage rows, bundle and JSON values. Idempotent. Every path that deletes a job row calls this in
    the same transaction (``media_jobs.delete_succeeded_in``). A database that predates the tables has nothing to
    release."""
    if not conn.in_transaction:
        raise RuntimeError("result release needs the caller's transaction (BEGIN IMMEDIATE)")
    if not _has_tables(conn):
        return
    conn.execute("DELETE FROM result_json WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM result_stage WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM result_bundle WHERE job_id = ?", (job_id,))


def erase_in(conn: sqlite3.Connection, owner_keys: set[str], *, before: int) -> int:
    """Erase the result values of these owners' jobs created up to ``before`` (a household kill passes ``{"*"}``).

    The JSON values go, and the unpublished stage rows of those jobs with them. The job rows stay, as erasure keeps
    them, and so do the published bundle's facts (name, kind, count, size): the fingerprint of an erased value does not
    stay, so the stage row's digest is cleared. Needs the caller's transaction. Returns how many values were deleted."""
    if not conn.in_transaction:
        raise RuntimeError("result erasure needs the caller's transaction (BEGIN IMMEDIATE)")
    if not _has_tables(conn) or not owner_keys:
        return 0
    everyone = "*" in owner_keys
    marks = ",".join("?" for _ in owner_keys)
    scope = "1=1" if everyone else f"j.owner_scope IN ({marks})"
    params: list[Any] = [] if everyone else sorted(owner_keys)
    jobs = [row[0] for row in conn.execute(
        f"SELECT DISTINCT j.job_id FROM result_json j JOIN media_jobs m ON m.job_id = j.job_id "  # noqa: S608
        f"WHERE {scope} AND m.created_at <= ?", [*params, before])]
    for job_id in jobs:
        conn.execute("DELETE FROM result_json WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM result_stage WHERE job_id = ? AND state != 'published'", (job_id,))
        conn.execute("UPDATE result_stage SET sha256 = NULL WHERE job_id = ? AND kind = 'json'", (job_id,))
    return len(jobs)


def expire_in(conn: sqlite3.Connection, *, cutoff: float) -> int:
    """Retention: delete the JSON values created before ``cutoff``, and the stage rows that no longer have a value to
    point to. The bundle's facts stay. Needs the caller's transaction. Returns how many values were deleted."""
    if not conn.in_transaction:
        raise RuntimeError("result expiry needs the caller's transaction (BEGIN IMMEDIATE)")
    if not _has_tables(conn):
        return 0
    refs = [row[0] for row in conn.execute("SELECT ref FROM result_json WHERE created < ?", (cutoff,))]
    for ref in refs:
        conn.execute("DELETE FROM result_json WHERE ref = ?", (ref,))
        conn.execute("UPDATE result_stage SET sha256 = NULL WHERE ref = ? AND kind = 'json'", (ref,))
        conn.execute("DELETE FROM result_stage WHERE ref = ? AND state != 'published'", (ref,))
    return len(refs)


def sweep(*, retention_s: float = JSON_RETENTION_S, path: Path = media_jobs.DB_PATH, now: float | None = None) -> int:
    """``expire_in`` in its own transaction, for the maintenance loop."""
    with media_jobs.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            deleted = expire_in(conn, cutoff=(_now() if now is None else now) - retention_s)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return deleted
