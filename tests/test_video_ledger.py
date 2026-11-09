"""The merged reader of a wire's jobs: the job store beside older tracking records, in one total order (video move 3.4, 3.5).

The job store is the in-memory fake of the frozen interface; the older store is a small port double, so these tests pin the
merge and the cursor, and nothing of how either store is made."""
from __future__ import annotations

from dataclasses import replace

import pytest

from gateway.video_ledger import CursorNotFound, JobHit, LegacyHit, VideoLedger
from gateway.workflow_runtime_types import JobDeleteConflict, JobNotFound, RunJobView
from tests.workflow_runtime_fakes import FakeJobService, FakeRunCore


def view(job_id, created_at, status="succeeded") -> RunJobView:
    return RunJobView(job_id=job_id, status=status, disposition="replay", workflow_id="w", record_revision=1, binding_key="b",
                      created_at_s=created_at, updated_at_s=created_at + 1)


class Older:
    def __init__(self, records):
        self.records = dict(records)

    def find(self, raw_id, caller):
        return self.records.get(raw_id)

    def visible(self, caller):
        return list(self.records.items())


def job_id_of(wire):
    return wire[len("video_"):] if wire.startswith("video_") and len(wire) > 6 else None


def ledger(jobs=(), older=None):
    core = FakeRunCore()
    for item in jobs:
        core.jobs[item.job_id] = item
    service = FakeJobService(core)
    return VideoLedger(service, job_id_of, ("x", "y"), None if older is None else Older(older)), service


def ids(page):
    return [hit.view.job_id if isinstance(hit, JobHit) else hit.raw_id for hit in page.items]


def all_pages(book, order, limit):
    seen, after = [], None
    while True:
        page = book.page(None, limit, after, order)
        seen += ids(page)
        if not page.has_more:
            return seen
        last = page.items[-1]
        after = "video_" + (last.view.job_id if isinstance(last, JobHit) else last.raw_id)


MIXED_JOBS = [view("mj_1", 100), view("mj_3", 300), view("mj_5", 500)]
MIXED_OLDER = {"p2": {"created_at": 200}, "p4": {"created_at": 400}, "p6": {"created_at": 600}}


@pytest.mark.parametrize("order", ["desc", "asc"])
@pytest.mark.parametrize("limit", [1, 2, 4, 6, 10])
def test_pages_across_both_stores_never_repeat_or_skip(order, limit):
    book, _ = ledger(MIXED_JOBS, MIXED_OLDER)
    expected = ["mj_1", "p2", "mj_3", "p4", "mj_5", "p6"]
    assert all_pages(book, order, limit) == (expected if order == "asc" else expected[::-1])


def test_a_page_says_whether_an_item_remained():
    book, _ = ledger(MIXED_JOBS, MIXED_OLDER)
    assert book.page(None, 6, None, "asc").has_more is False
    assert book.page(None, 5, None, "asc").has_more is True


def test_equal_times_break_by_the_raw_id_in_both_stores():
    book, _ = ledger([view("mj_b", 100), view("mj_a", 100)], {"p_a": {"created_at": 100}})
    assert ids(book.page(None, 10, None, "asc")) == ["mj_a", "mj_b", "p_a"]      # "mj_" sorts before "p_"
    assert ids(book.page(None, 10, None, "desc")) == ["p_a", "mj_b", "mj_a"]


def test_a_cursor_that_is_an_older_record_resumes_among_jobs():
    book, _ = ledger(MIXED_JOBS, MIXED_OLDER)
    assert ids(book.page(None, 10, "video_p4", "asc")) == ["mj_5", "p6"]
    assert ids(book.page(None, 10, "video_p4", "desc")) == ["mj_3", "p2", "mj_1"]


def test_a_cursor_that_is_a_job_resumes_among_older_records():
    book, _ = ledger(MIXED_JOBS, MIXED_OLDER)
    assert ids(book.page(None, 10, "video_mj_3", "asc")) == ["p4", "mj_5", "p6"]


def test_with_no_older_records_the_ledger_is_the_job_listing():
    book, _ = ledger(MIXED_JOBS, {})
    assert ids(book.page(None, 2, None, "desc")) == ["mj_5", "mj_3"]
    bare, _ = ledger(MIXED_JOBS)
    assert ids(bare.page(None, 10, None, "asc")) == ["mj_1", "mj_3", "mj_5"]


def test_a_cursor_that_names_nothing_is_the_evicted_cursor_error():
    book, service = ledger(MIXED_JOBS, MIXED_OLDER)
    for gone in ("video_mj_gone", "video_p_gone"):
        with pytest.raises(CursorNotFound):
            book.page(None, 10, gone, "desc")
    service.deleted.add("mj_3")                       # a deleted boundary is gone in the job store too
    with pytest.raises(CursorNotFound):
        book.page(None, 10, "video_mj_3", "desc")


def test_lookup_finds_a_job_before_an_older_record_and_none_for_a_malformed_id():
    book, _ = ledger(MIXED_JOBS, MIXED_OLDER)
    assert isinstance(book.lookup("video_mj_1", None), JobHit)
    older = book.lookup("video_p2", None)
    assert isinstance(older, LegacyHit) and older.raw_id == "p2"
    assert book.lookup("video_nope", None) is None and book.lookup("bad", None) is None


def test_delete_removes_a_job_and_leaves_an_older_record_to_its_own_deleter():
    book, service = ledger(MIXED_JOBS, MIXED_OLDER)
    assert isinstance(book.delete("video_mj_1", None), JobHit) and "mj_1" in service.deleted
    assert isinstance(book.delete("video_p2", None), LegacyHit) and "p2" not in service.deleted
    assert book.delete("video_nope", None) is None
    with pytest.raises(JobNotFound):
        service.delete("mj_1", None)


def test_a_cancel_that_does_not_take_keeps_the_job_and_raises_the_conflict():
    book, service = ledger([view("mj_r", 100, "running")])
    service.cancel_fails.add("mj_r")
    with pytest.raises(JobDeleteConflict):
        book.delete("video_mj_r", None)
    assert isinstance(book.lookup("video_mj_r", None), JobHit)
