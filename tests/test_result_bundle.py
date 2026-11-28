"""The result bundle: staging, publication, the private JSON store, the completion receipt, the startup scan (S2.3).

The module under test names no workflow, engine or modality, and so do these tests: the result names are made up, and
nothing here is an audio, image or video fixture. The audio route's use of the bundle is in
``tests/test_result_bundle_audio.py``; the release and erasure paths are in ``tests/test_result_bundle_release.py``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from gateway import media_jobs, receipt_store, result_bundle
from gateway.definition_store import DefinitionStore
from gateway.job_envelope import JobEnvelope, build_job_envelope
from gateway.receipts import RouteReceipt
from gateway.result_bundle import LimitedAssetStore, Reader, ResultLimits, ResultRefused

CANARY = "canary-value-7c1f"
LIMITS = ResultLimits()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    return path


@pytest.fixture
def receipts(tmp_path: Path) -> Path:
    path = tmp_path / "receipts.db"
    receipt_store.init_db(path)
    return path


def make_job(db: Path, tag: str = "a", *, status: str = media_jobs.STATUS_RUNNING, maker: str | None = "person-1",
             app_id: str = "app-1", envelope: JobEnvelope | None = None) -> str:
    job = media_jobs.create(
        media_jobs.MediaJob(intent="thing.make", request_id=f"req-{tag}", status=status, app_id=app_id,
                            owner_key_hash=f"key-{tag}", maker_person_id=maker),
        path=db, maker_from_request=False, envelope=envelope,
    )
    return job.job_id


def make_envelope(tag: str = "a") -> JobEnvelope:
    return build_job_envelope(
        source_kind="workflow", source_id=f"wf-{tag}", contract={"contract": tag}, binding={"binding": tag},
        template=None, protocol_id="proto-x", adapter_id="adapter-x", interface_version=1, service_id="svc-1",
        origin="http://engine.test:1",
    )


def receipt_for(receipts: Path, job_id: str, request_id: str = "rcpt-1") -> None:
    receipt_store.begin(RouteReceipt(protocol="p", intent="thing.make", request_id=request_id, job_id=job_id),
                        path=receipts)


def stored_receipt(receipts: Path, request_id: str = "rcpt-1") -> dict:
    found = receipt_store.get(request_id, path=receipts)
    assert found is not None
    return found


def stage_json(db: Path, job_id: str, name: str, value: object, attempt: int = 1) -> str:
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ref = result_bundle.stage_json_in(conn, job_id, attempt, name, json.dumps(value).encode(), limits=LIMITS)
        conn.commit()
    return ref


def stage_assets(db: Path, job_id: str, name: str, sizes: list[int], attempt: int = 1) -> str:
    items = [{"asset_ref": f"asset-{i}", "media_type": "application/octet-stream", "size_bytes": size}
             for i, size in enumerate(sizes)]
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ref = result_bundle.stage_assets_in(conn, job_id, attempt, name, items, limits=LIMITS)
        conn.commit()
    return ref


def publish(db: Path, job_id: str, limits: ResultLimits = LIMITS) -> dict:
    """The publication transaction, without the receipt link."""
    def hook(conn: sqlite3.Connection, job: dict) -> None:
        result_bundle.publish_in(conn, job_id, result_bundle.attempt_of(job), limits=limits)

    updated = media_jobs.update_status(job_id, media_jobs.STATUS_SUCCEEDED, path=db, before_commit=hook)
    assert updated is not None
    return updated


def rows(db: Path, sql: str, *args) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, args).fetchall()


# ------------------------------------------------------------------------------------------------- limits (D6)

@pytest.mark.parametrize("raw, code", [
    (b"{" * 33 + b"}" * 33, "json_too_deep"),
    (b"[" * 40 + b"]" * 40, "json_too_deep"),
    (b'{"a": NaN}', "json_non_finite"),
    (b"[Infinity]", "json_non_finite"),
    (b"[-Infinity]", "json_non_finite"),
    (b"[1e999]", "json_non_finite"),
    (b'{"a": 1, "a": 2}', "json_duplicate_key"),
    (b'{"a": {"b": 1, "b": 1}}', "json_duplicate_key"),
    (b'{"a": ', "json_malformed"),
    (b"\xff\xfe", "json_malformed"),
    (b"", "json_malformed"),
    (b"1 2", "json_malformed"),
])
def test_json_that_breaks_a_rule_is_refused(raw: bytes, code: str):
    with pytest.raises(ResultRefused) as refused:
        result_bundle.check_json(raw, LIMITS)
    assert refused.value.code == code


def test_json_over_the_size_item_and_string_limits_is_refused():
    small = ResultLimits(json_max_bytes=20, json_max_items=3, json_max_string_length=5)
    for raw, code in [(b'["' + b"x" * 30 + b'"]', "json_too_large"), (b"[1,2,3,4]", "json_too_many_items"),
                      (b'["abcdef"]', "json_string_too_long"), (b'{"abcdef": 1}', "json_string_too_long")]:
        with pytest.raises(ResultRefused) as refused:
            result_bundle.check_json(raw, small)
        assert refused.value.code == code, raw


def test_nesting_is_counted_outside_strings_only():
    """Brackets inside a string, escaped quotes included, are not nesting."""
    raw = json.dumps({"a": "[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[\\\"[[[["}).encode()
    assert result_bundle.check_json(raw, LIMITS)["a"].startswith("[[[")


@pytest.mark.parametrize("value", [None, 0, -1.5, "text", [], {}, [1, [2, {"a": [3]}]], {"k": None}])
def test_any_one_json_value_passes(value: object):
    assert result_bundle.check_json(json.dumps(value).encode(), LIMITS) == value


def test_the_limits_come_from_trusted_config_and_a_bad_value_keeps_the_default():
    config = {"workflow_execution": {"result_limits": {"max_assets": 2, "json_max_depth": "deep", "max_total_bytes": 0,
                                                       "made_up": 9, "json_max_items": True}}}
    limits = result_bundle.limits_from_config(config)
    assert limits.max_assets == 2
    assert (limits.json_max_depth, limits.max_total_bytes, limits.json_max_items) == (
        LIMITS.json_max_depth, LIMITS.max_total_bytes, LIMITS.json_max_items)
    assert result_bundle.limits_from_config({}) == LIMITS
    assert result_bundle.limits_from_config({"workflow_execution": {"result_limits": []}}) == LIMITS


@pytest.mark.parametrize("sizes, code", [([1] * 5, "too_many_assets"), ([101], "asset_too_large"),
                                          ([100, 100, 100], "total_too_large")])
def test_assets_over_a_limit_are_refused(sizes: list[int], code: str):
    small = ResultLimits(max_assets=4, max_asset_bytes=100, max_total_bytes=250)
    with pytest.raises(ResultRefused) as refused:
        result_bundle.check_assets(small, sizes)
    assert refused.value.code == code
    result_bundle.check_assets(small, [100, 100, 50])


class RecordingStore:
    """An asset store that records how much of each stream it read before it stopped."""

    def __init__(self) -> None:
        self.saved: list[int] = []
        self.read_before_stop: list[int] = []

    async def read(self, ref, *, max_bytes):  # pragma: no cover - not used here
        raise NotImplementedError

    async def save(self, chunks, *, content_type, max_bytes, name=None):
        size = 0
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise RuntimeError("the inner store's own cap")
        finally:
            self.read_before_stop.append(size)
        self.saved.append(size)
        return f"ref-{len(self.saved)}"


async def chunks(*sizes: int):
    for size in sizes:
        yield b"x" * size


def test_the_asset_stream_is_limited_while_it_is_read():
    store = RecordingStore()
    limited = LimitedAssetStore(store, ResultLimits(max_assets=2, max_asset_bytes=10, max_total_bytes=15))

    async def run() -> None:
        assert await limited.save(chunks(4, 4), content_type="application/octet-stream", max_bytes=100) == "ref-1"
        # A stream over the per-asset limit stops at the chunk that crosses it: the rest is never read.
        with pytest.raises(ResultRefused) as refused:
            await limited.save(chunks(6, 6, 6, 6), content_type="application/octet-stream", max_bytes=100)
        assert refused.value.code == "asset_too_large"
        assert store.read_before_stop[-1] == 6 and store.saved == [8], "stopped on the chunk that crossed the limit"
        # 8 bytes are used; 8 more would pass the total of 15.
        with pytest.raises(ResultRefused) as refused:
            await limited.save(chunks(4, 4), content_type="application/octet-stream", max_bytes=100)
        assert refused.value.code == "total_too_large"
        # The asset count: a third asset is refused before it is read at all.
        assert await limited.save(chunks(1), content_type="application/octet-stream", max_bytes=100) == "ref-2"
        with pytest.raises(ResultRefused) as refused:
            await limited.save(chunks(1), content_type="application/octet-stream", max_bytes=100)
        assert refused.value.code == "too_many_assets"

    asyncio.run(run())


def test_the_callers_own_cap_still_applies_when_it_is_lower():
    store = RecordingStore()
    limited = LimitedAssetStore(store, LIMITS)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="inner store's own cap"):
            await limited.save(chunks(5, 5), content_type="application/octet-stream", max_bytes=6)

    asyncio.run(run())


# ----------------------------------------------------------------------------- staging and publication (D4)

def test_a_staged_result_is_not_readable_until_publication(db: Path):
    job_id = make_job(db)
    stage_json(db, job_id, "answer", {"k": CANARY})
    reader = Reader(owner_scope=media_jobs.get(job_id, path=db)["owner_scope"], person_id="person-1")
    assert result_bundle.read_json(job_id, "answer", reader, path=db) is None
    publish(db, job_id)
    read = result_bundle.read_json(job_id, "answer", reader, path=db)
    assert read is not None and read.media_type == "application/json" and json.loads(read.body) == {"k": CANARY}


def test_publication_sets_the_bundle_marks_the_rows_and_ends_the_job_in_one_transaction(db: Path):
    job_id = make_job(db, envelope=make_envelope())
    stage_json(db, job_id, "one", [1, 2])
    stage_assets(db, job_id, "two", [10, 20])
    before = media_jobs.get(job_id, path=db)
    assert before["status"] == media_jobs.STATUS_RUNNING
    seen: list = []

    def hook(conn: sqlite3.Connection, job: dict) -> None:
        result_bundle.publish_in(conn, job_id, result_bundle.attempt_of(job), limits=LIMITS)
        # Inside the transaction all three changes are visible to this connection and to no other one.
        seen.append(conn.execute("SELECT state FROM result_stage ORDER BY name").fetchall())
        seen.append(rows(db, "SELECT COUNT(*) FROM result_bundle"))
        seen.append(media_jobs.get(job_id, path=db)["status"])

    media_jobs.update_status(job_id, media_jobs.STATUS_SUCCEEDED, path=db, before_commit=hook)
    assert seen == [[("published",), ("published",)], [(0,)], media_jobs.STATUS_RUNNING]
    assert media_jobs.get(job_id, path=db)["status"] == media_jobs.STATUS_SUCCEEDED
    assert rows(db, "SELECT state FROM result_stage ORDER BY name") == [("published",), ("published",)]
    ref = rows(db, "SELECT bundle_ref FROM result_bundle WHERE job_id = ?", job_id)[0][0]
    assert DefinitionStore(db).envelope(job_id).bundle == ref


def test_a_failed_publication_commits_nothing(db: Path):
    """The hook raises: the job stays running, no row is published and no bundle is recorded."""
    job_id = make_job(db)
    stage_json(db, job_id, "one", 1)

    def hook(conn: sqlite3.Connection, job: dict) -> None:
        result_bundle.publish_in(conn, job_id, 1, limits=LIMITS)
        raise RuntimeError("the last step failed")

    with pytest.raises(RuntimeError):
        media_jobs.update_status(job_id, media_jobs.STATUS_SUCCEEDED, path=db, before_commit=hook)
    assert media_jobs.get(job_id, path=db)["status"] == media_jobs.STATUS_RUNNING
    assert rows(db, "SELECT state FROM result_stage") == [("staged",)]
    assert rows(db, "SELECT COUNT(*) FROM result_bundle") == [(0,)]


def test_a_pending_cancel_holds_publication_back(db: Path):
    """D4: publication waits until a cancel has settled. The hook never runs, so nothing is published."""
    job_id = make_job(db)
    stage_json(db, job_id, "one", 1)
    media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, metadata_patch={"cancel_requested_at": 5}, path=db)
    ran: list[bool] = []
    with pytest.raises(media_jobs.CancellationPending):
        media_jobs.update_status(job_id, media_jobs.STATUS_SUCCEEDED, path=db, before_commit=lambda c, j: ran.append(True))
    assert ran == []
    assert rows(db, "SELECT state FROM result_stage") == [("staged",)]
    assert rows(db, "SELECT COUNT(*) FROM result_bundle") == [(0,)]


def test_publication_checks_the_bundle_before_it_writes(db: Path):
    job_id = make_job(db)
    with pytest.raises(ResultRefused) as refused:
        with media_jobs.connection(db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            result_bundle.publish_in(conn, job_id, 1, limits=LIMITS)
    assert refused.value.code == "no_results"
    # The sum of the results is a bundle limit, whatever each result's own size.
    stage_assets(db, job_id, "a", [60])
    stage_assets(db, job_id, "b", [60])
    tight = ResultLimits(max_total_bytes=100)
    with pytest.raises(ResultRefused) as refused:
        with media_jobs.connection(db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            result_bundle.publish_in(conn, job_id, 1, limits=tight)
    assert refused.value.code == "total_too_large"
    assert rows(db, "SELECT COUNT(*) FROM result_bundle") == [(0,)]


def test_a_json_value_that_changed_after_staging_is_not_published(db: Path):
    job_id = make_job(db)
    ref = stage_json(db, job_id, "one", {"a": 1})
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE result_json SET value = ? WHERE ref = ?", (b'{"a":2}', ref))
    with pytest.raises(ResultRefused) as refused:
        media_jobs.update_status(
            job_id, media_jobs.STATUS_SUCCEEDED, path=db,
            before_commit=lambda conn, job: result_bundle.publish_in(conn, job_id, 1, limits=LIMITS))
    assert refused.value.code == "result_missing"
    assert media_jobs.get(job_id, path=db)["status"] == media_jobs.STATUS_RUNNING


def test_a_job_publishes_one_bundle(db: Path):
    job_id = make_job(db)
    stage_json(db, job_id, "one", 1)
    publish(db, job_id)
    with pytest.raises(ResultRefused) as refused:
        with media_jobs.connection(db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            result_bundle.publish_in(conn, job_id, 1, limits=LIMITS)
    assert refused.value.code == "published"
    with pytest.raises(ResultRefused):
        stage_json(db, job_id, "one", 2)


def test_only_the_current_attempt_publishes_and_older_stages_are_aborted(db: Path):
    job_id = make_job(db)
    stage_json(db, job_id, "old", "from a dead attempt", attempt=1)
    stage_json(db, job_id, "new", "from this attempt", attempt=2)
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.publish_in(conn, job_id, 2, limits=LIMITS)
        conn.commit()
    assert rows(db, "SELECT name, state FROM result_stage ORDER BY name") == [("new", "published"), ("old", "aborted")]


def test_a_stale_worker_cannot_stage_a_value_after_erasure(db: Path):
    job_id = make_job(db)
    media_jobs.erase_prompt_derivatives({"*"}, before=2**31, path=db)
    with pytest.raises(ResultRefused) as refused:
        stage_json(db, job_id, "one", CANARY)
    assert refused.value.code == "erased"
    assert rows(db, "SELECT COUNT(*) FROM result_json") == [(0,)]


def test_a_stale_worker_cannot_stage_files_after_erasure(db: Path):
    job_id = make_job(db)
    media_jobs.erase_prompt_derivatives({"*"}, before=2**31, path=db)
    with pytest.raises(ResultRefused) as refused:
        stage_assets(db, job_id, "files", [10])
    assert refused.value.code == "erased"
    assert rows(db, "SELECT COUNT(*) FROM result_stage") == [(0,)]


def test_a_stale_worker_cannot_publish_after_erasure(db: Path):
    job_id = make_job(db)
    stage_assets(db, job_id, "files", [10])
    media_jobs.erase_prompt_derivatives({"*"}, before=2**31, path=db)
    with pytest.raises(ResultRefused) as refused:
        with media_jobs.connection(db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            result_bundle.publish_in(conn, job_id, 1, limits=LIMITS)
    assert refused.value.code == "erased"
    assert rows(db, "SELECT COUNT(*) FROM result_bundle") == [(0,)]


def test_complete_job_stages_publishes_and_links(db: Path, receipts: Path):
    job_id = make_job(db, envelope=make_envelope())
    receipt_for(receipts, job_id)

    def stage(conn: sqlite3.Connection, attempt: int) -> None:
        result_bundle.stage_json_in(conn, job_id, attempt, "answer", json.dumps({"k": CANARY}).encode(), limits=LIMITS)
        result_bundle.stage_assets_in(conn, job_id, attempt, "files", [
            {"asset_ref": "a-1", "media_type": "x/y", "size_bytes": 7}], limits=LIMITS)

    updated = result_bundle.complete_job(job_id, stage, limits=LIMITS, path=db, receipts_path=receipts, progress=100)
    assert updated["status"] == media_jobs.STATUS_SUCCEEDED and updated["progress"] == 100
    summary = stored_receipt(receipts)["metadata"]["result_bundle"]
    assert summary["results"] == [{"name": "answer", "kind": "json", "count": 1,
                                   "size_bytes": len(json.dumps({"k": CANARY}))},
                                  {"name": "files", "kind": "assets", "count": 1, "size_bytes": 7}]
    assert summary["envelope"]["source_id"] == "wf-a" and summary["envelope"]["adapter_id"] == "adapter-x"
    assert summary["envelope"]["contract"].startswith("sha256:")
    assert CANARY not in json.dumps(stored_receipt(receipts))


def test_a_refused_stage_aborts_the_attempt_and_leaves_the_job_running(db: Path):
    job_id = make_job(db)

    def stage(conn: sqlite3.Connection, attempt: int) -> None:
        result_bundle.stage_json_in(conn, job_id, attempt, "ok", b"1", limits=LIMITS)
        result_bundle.stage_json_in(conn, job_id, attempt, "bad", b'{"a":1,"a":2}', limits=LIMITS)

    with pytest.raises(ResultRefused):
        result_bundle.complete_job(job_id, stage, limits=LIMITS, path=db)
    assert media_jobs.get(job_id, path=db)["status"] == media_jobs.STATUS_RUNNING
    assert rows(db, "SELECT COUNT(*) FROM result_stage") == [(0,)], "the stage transaction rolled back whole"
    assert rows(db, "SELECT COUNT(*) FROM result_json") == [(0,)]


def test_a_refused_publication_aborts_the_staged_rows(db: Path):
    job_id = make_job(db)

    def stage(conn: sqlite3.Connection, attempt: int) -> None:
        result_bundle.stage_assets_in(conn, job_id, attempt, "a", [
            {"asset_ref": "x", "media_type": "t/u", "size_bytes": 60}], limits=LIMITS)
        result_bundle.stage_assets_in(conn, job_id, attempt, "b", [
            {"asset_ref": "y", "media_type": "t/u", "size_bytes": 60}], limits=LIMITS)

    with pytest.raises(ResultRefused):
        result_bundle.complete_job(job_id, stage, limits=ResultLimits(max_total_bytes=100), path=db)
    assert rows(db, "SELECT state FROM result_stage ORDER BY name") == [("aborted",), ("aborted",)]
    assert media_jobs.get(job_id, path=db)["status"] == media_jobs.STATUS_RUNNING


# ------------------------------------------------------------------------------------------------- receipts (D4)

def published_job(db: Path, *, envelope: JobEnvelope | None = None) -> str:
    job_id = make_job(db, envelope=envelope)
    stage_json(db, job_id, "answer", {"k": CANARY})
    publish(db, job_id)
    return job_id


def test_the_receipt_is_linked_once_and_idempotently(db: Path, receipts: Path):
    job_id = published_job(db, envelope=make_envelope())
    receipt_for(receipts, job_id)
    assert result_bundle.link_receipt(job_id, jobs_path=db, receipts_path=receipts) is True
    first = stored_receipt(receipts)
    assert result_bundle.link_receipt(job_id, jobs_path=db, receipts_path=receipts) is True
    assert stored_receipt(receipts) == first
    assert [r for r in rows(receipts, "SELECT request_id FROM receipts")] == [("rcpt-1",)]
    assert rows(db, "SELECT receipt_linked_at IS NOT NULL FROM result_bundle") == [(1,)]
    # Writing the same summary again, with the flag lost, is the same write.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE result_bundle SET receipt_linked_at = NULL")
    assert result_bundle.link_receipt(job_id, jobs_path=db, receipts_path=receipts) is True
    assert stored_receipt(receipts) == first


def test_the_receipt_holds_names_kinds_counts_and_sizes_never_values(db: Path, receipts: Path):
    job_id = published_job(db, envelope=make_envelope())
    receipt_for(receipts, job_id)
    result_bundle.link_receipt(job_id, jobs_path=db, receipts_path=receipts)
    stored = stored_receipt(receipts)
    assert stored["metadata"]["result_bundle"]["results"] == [
        {"name": "answer", "kind": "json", "count": 1, "size_bytes": len(json.dumps({"k": CANARY}))}]
    for path in (receipts, db):
        for table in ("receipts", "media_jobs", "media_job_events", "job_envelope", "result_bundle", "result_stage"):
            try:
                found = rows(path, f"SELECT * FROM {table}")  # noqa: S608
            except sqlite3.OperationalError:
                continue
            assert CANARY not in repr(found), f"a value is in {table}"
    assert CANARY in repr(rows(db, "SELECT value FROM result_json")), "the value is only in the private store"


def test_no_receipt_yet_means_not_linked(db: Path, receipts: Path):
    job_id = published_job(db)
    assert result_bundle.link_receipt(job_id, jobs_path=db, receipts_path=receipts) is False
    assert rows(db, "SELECT receipt_linked_at FROM result_bundle") == [(None,)]
    receipt_for(receipts, job_id)
    assert result_bundle.link_receipt(job_id, jobs_path=db, receipts_path=receipts) is True


def test_a_job_with_no_bundle_links_nothing(db: Path, receipts: Path):
    job_id = make_job(db)
    receipt_for(receipts, job_id)
    assert result_bundle.link_receipt(job_id, jobs_path=db, receipts_path=receipts) is False
    assert "result_bundle" not in stored_receipt(receipts)["metadata"]


# --------------------------------------------------------------------------------------------- startup scan (D4)

def test_the_scan_deletes_what_is_neither_running_nor_published(db: Path, receipts: Path):
    done = published_job(db)
    dead = make_job(db, "dead")
    stage_json(db, dead, "gone", CANARY)
    stage_assets(db, dead, "also-gone", [5])
    aborted = make_job(db, "abort")
    stage_json(db, aborted, "gone-too", CANARY)
    result_bundle.abort(aborted, 1, path=db)
    live = make_job(db, "live")
    stage_json(db, live, "kept", CANARY)
    counts = result_bundle.startup_scan(running=[(live, 1)], jobs_path=db, receipts_path=receipts)
    assert counts["stage_rows"] == 3
    assert rows(db, "SELECT job_id, name, state FROM result_stage ORDER BY name") == [
        (done, "answer", "published"), (live, "kept", "staged")]
    assert sorted(r[0] for r in rows(db, "SELECT job_id FROM result_json")) == sorted([done, live])


def test_the_scan_links_a_published_bundle_that_has_no_receipt(db: Path, receipts: Path):
    """A crash between publication and the link leaves the bundle published with the flag unset."""
    job_id = published_job(db, envelope=make_envelope())
    receipt_for(receipts, job_id)
    assert "result_bundle" not in stored_receipt(receipts)["metadata"]
    assert result_bundle.startup_scan(jobs_path=db, receipts_path=receipts)["linked"] == 1
    assert stored_receipt(receipts)["metadata"]["result_bundle"]["envelope"]["source_id"] == "wf-a"
    assert result_bundle.startup_scan(jobs_path=db, receipts_path=receipts)["linked"] == 0, "linked once"


def test_the_scan_gives_up_on_an_old_bundle_whose_receipt_is_gone(db: Path, receipts: Path):
    job_id = published_job(db)
    now = 1000.0 + result_bundle.LINK_WINDOW_S + 1
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE result_bundle SET published_at = 1000.0")
    counts = result_bundle.startup_scan(jobs_path=db, receipts_path=receipts, now=now)
    assert counts == {"stage_rows": 0, "linked": 0, "gave_up": 1}
    assert result_bundle.startup_scan(jobs_path=db, receipts_path=receipts, now=now)["gave_up"] == 0
    assert rows(db, "SELECT job_id FROM result_bundle") == [(job_id,)], "the bundle stays; only the link is given up"


def test_a_recent_bundle_with_no_receipt_is_kept_for_the_next_scan(db: Path, receipts: Path):
    published_job(db)
    counts = result_bundle.startup_scan(jobs_path=db, receipts_path=receipts)
    assert counts == {"stage_rows": 0, "linked": 0, "gave_up": 0}
    assert rows(db, "SELECT receipt_linked_at FROM result_bundle") == [(None,)]


# ------------------------------------------------------------------------------------------- JSON store (D4)

def owner_of(db: Path, job_id: str) -> str:
    return media_jobs.get(job_id, path=db)["owner_scope"]


def test_only_the_maker_under_the_owner_scope_reads_a_value(db: Path):
    job_id = published_job(db)
    owner = owner_of(db, job_id)
    assert result_bundle.read_json(job_id, "answer", Reader(owner, person_id="person-1"), path=db) is not None
    for reader in (Reader(owner, person_id="person-2"), Reader(owner), Reader("other-owner", person_id="person-1"),
                   Reader("", person_id="person-1"), Reader(owner, credential_id="person-1")):
        assert result_bundle.read_json(job_id, "answer", reader, path=db) is None, reader
    assert result_bundle.read_json(job_id, "no-such-name", Reader(owner, person_id="person-1"), path=db) is None
    assert result_bundle.read_json("no-such-job", "answer", Reader(owner, person_id="person-1"), path=db) is None


def test_the_maker_stamp_decides_in_the_order_person_credential_connection(db: Path):
    by_credential = media_jobs.create(
        media_jobs.MediaJob(intent="thing.make", request_id="c", status=media_jobs.STATUS_RUNNING, app_id="app-1",
                            owner_key_hash="key-c", maker_credential_id="cred-1"),
        path=db, maker_from_request=False).job_id
    by_connection = media_jobs.create(
        media_jobs.MediaJob(intent="thing.make", request_id="n", status=media_jobs.STATUS_RUNNING, app_id="app-1",
                            owner_key_hash="key-n", maker_connection_id="conn-1"),
        path=db, maker_from_request=False).job_id
    nobody = make_job(db, "nobody", maker=None)
    for job_id in (by_credential, by_connection, nobody):
        stage_json(db, job_id, "answer", 1)
        publish(db, job_id)
    assert result_bundle.read_json(by_credential, "answer", Reader(owner_of(db, by_credential), credential_id="cred-1"), path=db)
    assert not result_bundle.read_json(by_credential, "answer", Reader(owner_of(db, by_credential), connection_id="cred-1"), path=db)
    assert result_bundle.read_json(by_connection, "answer", Reader(owner_of(db, by_connection), connection_id="conn-1"), path=db)
    assert not result_bundle.read_json(nobody, "answer", Reader(owner_of(db, nobody), person_id="person-1"), path=db), \
        "a job with no maker has no reader"


def test_an_asset_result_is_not_a_json_read(db: Path):
    job_id = make_job(db)
    stage_assets(db, job_id, "files", [3])
    publish(db, job_id)
    assert result_bundle.read_json(job_id, "files", Reader(owner_of(db, job_id), person_id="person-1"), path=db) is None


def test_retention_deletes_old_values_and_keeps_the_bundle_facts(db: Path):
    old, new = published_job(db), make_job(db, "new")
    stage_json(db, new, "answer", {"k": CANARY})
    publish(db, new)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE result_json SET created = 100.0 WHERE job_id = ?", (old,))
    assert result_bundle.sweep(retention_s=1000, path=db, now=5000.0) == 1
    assert result_bundle.read_json(old, "answer", Reader(owner_of(db, old), person_id="person-1"), path=db) is None
    assert result_bundle.read_json(new, "answer", Reader(owner_of(db, new), person_id="person-1"), path=db) is not None
    assert result_bundle.bundle_summary(old, path=db)["results"][0]["name"] == "answer"
    assert rows(db, "SELECT sha256 FROM result_stage WHERE job_id = ?", old) == [(None,)]


def test_the_envelope_records_the_bundle_it_published(db: Path):
    job_id = published_job(db, envelope=make_envelope())
    envelope = DefinitionStore(db).envelope(job_id)
    assert envelope.bundle == f"bundle:{job_id}:1"
    other = make_job(db, "other")
    stage_json(db, other, "x", 1)
    publish(db, other)
    assert DefinitionStore(db).envelope(other) is None, "a job with no pin still publishes"
    assert result_bundle.bundle_summary(other, path=db)["envelope"] is None
