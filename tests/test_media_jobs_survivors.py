"""Cases that kill the media_jobs survivors left after mutation round 2 (lane MUT r3).

Each test names the mutant it kills in its docstring (ID prefix: gateway/media_jobs.py).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from gateway import media_jobs as mj
from gateway.ownership import principal_key


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.db"
    mj.init_db(path)
    return path


def _raw(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def test_upsert_returns_the_stored_record(db):
    """:496 return->None."""
    job = mj.MediaJob(intent="image.generate", job_id="job-up", request_id="r-up")
    out = mj.upsert(job, path=db)
    assert out is not None and out.job_id == "job-up"


def test_retained_job_json_of_a_released_request_id_is_sorted_and_not_ascii_escaped(db):
    """:591 True->False (sort_keys) and False->True (ensure_ascii)."""
    old = mj.create(mj.MediaJob(intent="image.generate", request_id="req-old", status="succeeded",
                                metadata={"zeta": "é", "alpha": 1}), path=db)
    (stored,) = _raw(db, "SELECT job_json FROM media_jobs WHERE job_id = ?", (old.job_id,))[0]
    shuffled = dict(reversed(list(json.loads(stored).items())))
    _raw(db, "UPDATE media_jobs SET updated_at = ?, job_json = ? WHERE job_id = ?",
         (1, json.dumps(shuffled, ensure_ascii=False), old.job_id))
    mj.create(mj.MediaJob(intent="image.generate", request_id="req-old"), path=db)
    (raw,) = _raw(db, "SELECT job_json FROM media_jobs WHERE job_id = ?", (old.job_id,))[0]
    retained = json.loads(raw)
    assert raw == json.dumps(retained, ensure_ascii=False, sort_keys=True)
    assert "é" in raw and "\\u00e9" not in raw


@pytest.mark.parametrize(
    "second",
    [
        {"intent": "video.generate"},
        {"metadata": {"other": 1}},
        {"app_id": "someone-else"},
    ],
    ids=["intent", "input", "owner"],
)
def test_a_replayed_request_id_conflicts_on_any_single_difference(db, second):
    """:597 or->and (every difference alone must conflict)."""
    first = dict(intent="image.generate", request_id="req-same", app_id="app-1", metadata={"a": 1})
    mj.create(mj.MediaJob(**first), path=db)
    with pytest.raises(mj.IdempotencyConflict):
        mj.create(mj.MediaJob(**{**first, **second}), path=db)


def _seed(db, job_id, **kw):
    kw.setdefault("intent", "image.generate")
    return mj.create(mj.MediaJob(job_id=job_id, request_id=f"req-{job_id}", **kw), path=db)


def test_erasure_skips_an_unparseable_row_and_still_erases_the_rows_after_it(db):
    """:741 continue->break."""
    _seed(db, "a-bad", app_id="app-1")
    _seed(db, "b-ok", app_id="app-1")
    _raw(db, "UPDATE media_jobs SET job_json = '[]' WHERE job_id = 'a-bad'")
    assert mj.erase_prompt_derivatives({principal_key("app", "app-1")}, before=2**40, path=db) == 1
    (raw,) = _raw(db, "SELECT job_json FROM media_jobs WHERE job_id = 'b-ok'")[0]
    assert json.loads(raw)["input_hash"] is None


def test_erasure_matches_the_owner_derived_from_the_apps_id(db):
    """:746 'app_id' key (a legacy row keeps its App but has no owner key hash)."""
    _seed(db, "job-app", app_id="app-9")
    (stored,) = _raw(db, "SELECT job_json FROM media_jobs WHERE job_id = 'job-app'")[0]
    legacy = {**json.loads(stored), "owner_key_hash": None}
    _raw(db, "UPDATE media_jobs SET job_json = ? WHERE job_id = 'job-app'", (json.dumps(legacy),))
    assert mj.erase_prompt_derivatives({principal_key("app", "app-9")}, before=2**40, path=db) == 1


def test_erasure_skips_a_row_deleted_between_the_scan_and_the_write_lock(db, monkeypatch):
    """:756 continue->break."""
    _seed(db, "a-gone", app_id="app-1")
    _seed(db, "b-kept", app_id="app-1")
    real = mj._safe_loads

    def hook(text):
        if '"b-kept"' in text:
            _raw(db, "DELETE FROM media_jobs WHERE job_id = 'a-gone'")
        return real(text)

    monkeypatch.setattr(mj, "_safe_loads", hook)
    assert mj.erase_prompt_derivatives({principal_key("app", "app-1")}, before=2**40, path=db) == 1


def test_recent_skips_an_unparseable_row_and_returns_the_older_valid_ones(db):
    """:903 continue->break."""
    _seed(db, "job-old")
    _seed(db, "job-new")
    _raw(db, "UPDATE media_jobs SET updated_at = 100 WHERE job_id = 'job-old'")
    _raw(db, "UPDATE media_jobs SET updated_at = 200, job_json = '[]' WHERE job_id = 'job-new'")
    assert [row["job_id"] for row in mj.recent(10, path=db)] == ["job-old"]


def test_update_status_names_add_assets_when_an_asset_has_a_forbidden_key(db):
    """:943 'add_assets[*]' label."""
    _seed(db, "job-asset")
    with pytest.raises(ValueError, match=r"^add_assets\[\*\] contains forbidden keys"):
        mj.update_status("job-asset", "running", add_assets=[{"prompt": "x"}], path=db)


def test_begin_attempt_on_a_job_without_an_attempt_number_returns_it_unchanged(db):
    """:1056 return->None."""
    _seed(db, "job-legacy", status="running")
    (raw,) = _raw(db, "SELECT job_json FROM media_jobs WHERE job_id = 'job-legacy'")[0]
    legacy = json.loads(raw)
    legacy["attempt_number"] = None
    _raw(db, "UPDATE media_jobs SET job_json = ? WHERE job_id = 'job-legacy'", (json.dumps(legacy),))
    out = mj.begin_attempt("job-legacy", path=db)
    assert out is not None and out["job_id"] == "job-legacy" and out["attempt_number"] is None


def test_a_client_event_that_claims_lifecycle_authority_is_refused_before_it_is_stored(db):
    """:1124 validate_client_event call removed."""
    _seed(db, "job-ev")
    with pytest.raises(ValueError, match="client notes cannot set phase or status"):
        mj.append_client_event("job-ev", "note", {"status": "succeeded"}, path=db)
    assert mj.events("job-ev", path=db) == []


def test_events_carry_their_job_id(db):
    """:1189 'job_id' key."""
    _seed(db, "job-id-ev")
    mj.append_event("job-id-ev", "note", {"step": 1}, path=db)
    assert [e["job_id"] for e in mj.events("job-id-ev", audience="client", path=db)] == ["job-id-ev"]


# --- mut4: entries that mut3 called equivalent and the independent review showed observable ---

def test_asking_for_no_events_does_not_touch_the_store(db):
    """:1156 ``limit <= 0`` -> ``limit < 0``: the mutant opens the store and repairs sequences before returning [].

    The repair is real (a plain read afterwards does it), so the NULL surviving a limit=0 read shows
    the early return.
    """
    _seed(db, "job-nil")
    mj.append_event("job-nil", "note", {"step": 1}, path=db)
    _raw(db, "UPDATE media_job_events SET sequence = NULL WHERE job_id = 'job-nil'")
    assert mj.events("job-nil", limit=0, path=db) == []
    assert mj.events("job-nil", limit=-1, path=db) == []
    assert _raw(db, "SELECT sequence FROM media_job_events WHERE job_id = 'job-nil'") == [(None,)]
    assert [e["sequence"] for e in mj.events("job-nil", path=db)] == [1]


def test_the_stored_input_hash_is_the_digest_of_the_documented_submission_facts(db):
    """:530 ``"input"`` key renamed.

    Replays are matched against the hash stored by an earlier release, so the hashed document is a
    persistence format: renaming a key makes every stored hash disagree and turns a retry after an
    upgrade into IdempotencyConflict. A same-process replay cannot see that; the stored value can.
    """
    import hashlib

    job = mj.create(mj.MediaJob(intent="image.generate", request_id="req-hash", workflow="wf@1",
                                backend="b", metadata={"k": "é"}), path=db, input_data={"prompt": "x"})
    (raw,) = _raw(db, "SELECT job_json FROM media_jobs WHERE job_id = ?", (job.job_id,))[0]
    stored = json.loads(raw)["input_hash"]
    document = {"intent": "image.generate", "workflow": "wf@1", "backend": "b",
                "metadata": json.loads(raw)["metadata"], "assets": [], "input": {"prompt": "x"}}
    assert stored == hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False,
                                               separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("job_id", ["A-first", "0-digit", "B-job"])
def test_erasure_reaches_a_job_whose_id_sorts_before_any_sentinel_text(db, job_id):
    """:729 ``cursor = ""`` -> ``"XXXX"``: the scan cursor is a SQL lower bound, so an id below it is never visited."""
    _seed(db, job_id, app_id="app-1")
    assert mj.erase_prompt_derivatives({principal_key("app", "app-1")}, before=2**40, path=db) == 1
    (raw,) = _raw(db, "SELECT job_json FROM media_jobs WHERE job_id = ?", (job_id,))[0]
    assert json.loads(raw)["input_hash"] is None


# Risk gaps: race ordering and atomic persisted outcomes, rather than only replies.
def test_cancel_after_dispatch_race_retains_running_reservation(db, monkeypatch):
    job = _seed(db, "cancel-dispatch-race", app_id="app-race")
    real_update = mj.update_status
    raced = False

    def dispatch_before_cancel(job_id, status, **kwargs):
        nonlocal raced
        if not raced and status == "canceled":
            raced = True
            real_update(job_id, "running", now=100, path=db)
        return real_update(job_id, status, **kwargs)

    monkeypatch.setattr(mj, "update_status", dispatch_before_cancel)
    monkeypatch.setattr(mj, "_now", lambda: 200)
    result = mj.request_cancel(job.job_id, path=db)
    assert raced
    assert result["status"] == "running"
    assert result["started_at"] == 100 and result["finished_at"] is None
    assert result["metadata"]["cancel_requested_at"] == 200
    assert mj.active_count(app_id="app-race", intents=["image.generate"], path=db) == 1
    assert mj.get(job.job_id, path=db) == result


def test_cancel_after_completion_race_cannot_reopen_or_rewrite_job(db, monkeypatch):
    job = _seed(db, "cancel-complete-race", status="running")
    real_update = mj.update_status
    completed = []

    def complete_before_cancel(job_id, status, **kwargs):
        if status == "canceled" and not completed:
            completed.append(real_update(job_id, "succeeded", now=123,
                                         add_assets=[{"asset_id": "img_done"}], path=db))
        return real_update(job_id, status, **kwargs)

    monkeypatch.setattr(mj, "update_status", complete_before_cancel)
    with pytest.raises(ValueError):
        mj.request_cancel(job.job_id, reason="too late", path=db)
    assert len(completed) == 1
    assert mj.get(job.job_id, path=db) == completed[0]


def test_every_terminal_state_rejects_cancel_and_other_transitions_atomically(db):
    for terminal in ("failed", "canceled", "succeeded"):
        job = _seed(db, "terminal-lock-" + terminal, status=terminal)
        before = mj.get(job.job_id, path=db)
        with pytest.raises(ValueError, match="already finished"):
            mj.request_cancel(job.job_id, reason="late cancel", path=db)
        assert mj.get(job.job_id, path=db) == before
        for target in ("queued", "running", "succeeded", "failed", "canceled"):
            if target == terminal:
                continue
            with pytest.raises(ValueError, match="illegal status transition"):
                mj.update_status(job.job_id, target, progress=41,
                                 error="must not persist", metadata_patch={"changed": True},
                                 add_assets=[{"asset_id": "img_late"}], now=999, path=db)
            assert mj.get(job.job_id, path=db) == before


def test_cancel_pending_success_rejects_all_output_writes_atomically(db, monkeypatch):
    job = _seed(db, "cancel-output-rollback", status="running",
                metadata={"keep": 1}, assets=[{"asset_id": "img_first"}])
    monkeypatch.setattr(mj, "_now", lambda: 300)
    mj.request_cancel(job.job_id, path=db)
    before = mj.get(job.job_id, path=db)
    with pytest.raises(mj.CancellationPending):
        mj.update_status(job.job_id, "succeeded", progress=100, error="late",
                         add_assets=[{"asset_id": "img_late"}],
                         metadata_patch={"keep": 2}, now=400, path=db)
    assert mj.get(job.job_id, path=db) == before
    assert mj.events(job.job_id, path=db) == []


def test_cancel_reservation_is_released_only_when_worker_reaches_terminal(db, monkeypatch):
    limit = mj.OutstandingLimit("app", 1, "risk-mode", "risk-app")

    def submit(request_id):
        return mj.create(mj.MediaJob(intent="image.generate", request_id=request_id,
                                    app_id="risk-app", metadata={"mode": "risk-mode"}),
                         outstanding_limits=[limit], path=db)

    job = submit("reserved-first")
    mj.update_status(job.job_id, "running", path=db)
    monkeypatch.setattr(mj, "_now", lambda: 100)
    mj.request_cancel(job.job_id, path=db)
    with pytest.raises(mj.OutstandingLimitReached):
        submit("reserved-second")
    assert mj.count(db) == 1
    assert [row["job_id"] for row in mj.unfinished(path=db)] == [job.job_id]
    mj.update_status(job.job_id, "canceled", now=200, path=db)
    next_job = submit("reserved-second")
    assert next_job.job_id != job.job_id
    assert mj.active_count(app_id="risk-app", intents=["image.generate"], path=db) == 1
    # An unstarted cancellation releases its slot immediately.
    mj.request_cancel(next_job.job_id, path=db)
    assert submit("reserved-third").status == "queued"


def test_receipt_link_and_prior_attempt_survive_retry_then_cancel(db, monkeypatch):
    job = _seed(db, "receipt-retry-cancel", creating_receipt_id="receipt-original",
                parent_request_id="parent-original")
    mj.update_status(job.job_id, "running", now=100,
                     add_assets=[{"asset_id": "img_partial"}], path=db)
    before = mj.get(job.job_id, path=db)
    retry = mj.begin_attempt(job.job_id, now=200, path=db)
    assert retry["creating_receipt_id"] == "receipt-original"
    assert retry["parent_request_id"] == "parent-original"
    assert retry["attempt_number"] == 2
    assert retry["previous_attempts"] == [
        {"attempt_number": 1, **{name: before[name] for name in mj.PHASE_FIELDS}}
    ]
    assert retry["queued_at"] is None and retry["first_output_at"] is None
    monkeypatch.setattr(mj, "_now", lambda: 250)
    mj.request_cancel(job.job_id, path=db)
    final = mj.update_status(job.job_id, "canceled", now=300, path=db)
    assert final["creating_receipt_id"] == "receipt-original"
    assert final["parent_request_id"] == "parent-original"
    assert final["started_at"] == 200 and final["finished_at"] == 300
    assert final["previous_attempts"] == retry["previous_attempts"]
    assert mj.get(job.job_id, path=db) == final

