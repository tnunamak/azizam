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
