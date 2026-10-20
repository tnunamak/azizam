"""Exact validation messages, audience fields and event rules of gateway/media_jobs.py (mutation round 2)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import media_jobs as mj

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.db"
    mj.init_db(path)
    return path


def make(db, **kw):
    kw.setdefault("intent", "image.generate")
    kw.setdefault("request_id", "req-" + os.urandom(3).hex())
    return mj.create(mj.MediaJob(**kw), path=db)


def test_stored_constants_are_the_documented_values():
    assert mj.REPLAY_WINDOW_S == 14 * 24 * 3600 and mj.SCHEMA_VERSION == 3
    assert mj.ADMIN_ONLY_FIELDS == {"workflow", "backend", "app_profile", "app_id", "api_key_name"}
    assert mj.INTERNAL_ONLY_FIELDS == {"metadata", "owner_key_hash", "owner_scope", "input_hash", "creating_receipt_id"}
    assert mj.PHASE_FIELDS == ("accepted_at", "queued_at", "started_at", "first_output_at", "finished_at")


def test_the_database_location_is_read_from_the_documented_environment_variable(tmp_path):
    target = tmp_path / "elsewhere.db"
    code = "from gateway import media_jobs; print(media_jobs.DB_PATH)"
    out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); {code}"],
                         env={"AZIZAM_MEDIA_JOBS_DB": str(target), "PATH": os.environ.get("PATH", "")},
                         capture_output=True, text=True, cwd=str(tmp_path))
    assert out.stdout.strip() == str(target), out.stderr


def test_an_old_changed_job_stops_replaying_exactly_at_the_window_edge(monkeypatch):
    monkeypatch.setattr(mj, "_now", lambda: 10_000_000)
    edge = 10_000_000 - mj.REPLAY_WINDOW_S
    assert mj._replays("succeeded", edge) is True
    assert mj._replays("succeeded", edge - 1) is False
    assert mj._replays("running", 0) is True


@pytest.mark.parametrize("kwargs,message", [
    (dict(intent=""), "MediaJob.intent is required"),
    (dict(assets=["x"]), "MediaJob.assets entries must be mappings"),
    (dict(previous_attempts=["x"]), "previous_attempts must contain only phase facts"),
    (dict(previous_attempts=[{"started_at": 1, "bogus": 2}]), "previous_attempts must contain only phase facts"),
    (dict(previous_attempts=[{"started_at": -1}]), "previous_attempts phase facts must be non-negative integers or null"),
    (dict(previous_attempts=[{"started_at": True}]), "previous_attempts phase facts must be non-negative integers or null"),
    (dict(previous_attempts=[{"started_at": 1.5}]), "previous_attempts phase facts must be non-negative integers or null"),
])
def test_a_malformed_job_is_refused_with_its_message(kwargs, message):
    with pytest.raises(ValueError) as raised:
        mj.MediaJob(**{"intent": "image.generate", **kwargs})
    assert str(raised.value) == message


def test_phase_facts_of_zero_or_null_and_an_attempt_number_are_accepted():
    job = mj.MediaJob("image.generate", previous_attempts=[{"started_at": 0, "finished_at": None, "attempt_number": 1}])
    assert job.previous_attempts[0]["started_at"] == 0


@pytest.mark.parametrize("event_type,payload,message", [
    ("lifecycle", None, "client event_type must be progress or note; lifecycle events require a trusted writer"),
    ("note", {"phase": "x"}, "client notes cannot set phase or status"),
    ("note", {"status": "x"}, "client notes cannot set phase or status"),
    ("progress", {"progress": -1}, "progress must be a non-negative integer up to 100"),
    ("progress", {"progress": 101}, "progress must be a non-negative integer up to 100"),
    ("progress", {"progress": True}, "progress must be a non-negative integer up to 100"),
    ("progress", {"progress": 1.5}, "progress must be a non-negative integer up to 100"),
    ("progress", {"step": -1}, "step must be a non-negative integer"),
    ("progress", {"total": "3"}, "total must be a non-negative integer"),
])
def test_a_client_event_that_claims_authority_or_carries_a_bad_number_is_refused(event_type, payload, message):
    with pytest.raises(ValueError) as raised:
        mj.validate_client_event(event_type, payload)
    assert str(raised.value) == message


def test_a_client_event_with_bounded_numbers_passes():
    for payload in (None, {}, {"progress": 0}, {"progress": 100}, {"step": 0, "total": 0}, {"step": 7, "total": 9, "note": "x"}):
        mj.validate_client_event("progress", payload)
        mj.validate_client_event("note", payload)


def test_events_get_per_job_sequences_and_the_audience_decides_which_fields_show(db):
    job = make(db)
    other = make(db)
    with pytest.raises(ValueError, match=r"\Aevent_type\ must\ be\ a\ non\-empty\ string\Z"):
        mj.append_event(job.job_id, "", path=db)
    with pytest.raises(ValueError, match=r"\Aevent_type\ must\ be\ a\ non\-empty\ string\Z"):
        mj.append_event(job.job_id, 5, path=db)
    with pytest.raises(LookupError):
        mj.append_event("ghost", "x", path=db)
    assert mj.append_event(job.job_id, "queued", {"detail": "d", "progress": 5}, now=100, path=db) == 1
    assert mj.append_event(other.job_id, "queued", now=100, path=db) == 1
    assert mj.append_client_event(job.job_id, "progress", {"progress": 50, "step": 1, "total": 2}, path=db) == 2
    internal = mj.events(job.job_id, path=db)
    assert [(e["sequence"], e["event_type"], e["source"]) for e in internal] == [(1, "queued", "gateway"), (2, "progress", "client")]
    assert internal[0]["payload"] == {"detail": "d", "progress": 5} and internal[0]["created_at"] == 100
    assert set(internal[0]) == {"id", "sequence", "job_id", "created_at", "event_type", "source", "attempt_number", "payload"}
    admin = mj.events(job.job_id, audience="admin", path=db)
    assert set(admin[0]) == {"id", "sequence", "job_id", "created_at", "event_type", "source", "attempt_number"}
    client = mj.events(job.job_id, audience="client", path=db)
    assert set(client[0]) == {"sequence", "job_id", "created_at", "event_type", "source", "attempt_number", "payload"}
    assert client[0]["payload"] == {"progress": 5} and client[1]["payload"] == {"progress": 50, "step": 1, "total": 2}
    assert [e["sequence"] for e in mj.events(job.job_id, after_sequence=1, path=db)] == [2]
    assert [e["sequence"] for e in mj.events(job.job_id, after_id=internal[0]["id"], path=db)] == [2]
    assert [e["sequence"] for e in mj.events(job.job_id, limit=1, path=db)] == [1]
    assert mj.events(job.job_id, limit=0, path=db) == []
    assert mj.events(job.job_id, limit=-3, path=db) == []
    with pytest.raises(ValueError, match=r"\Ause\ either\ after_id\ or\ after_sequence,\ not\ both\Z"):
        mj.events(job.job_id, after_id=1, after_sequence=1, path=db)
    with pytest.raises(ValueError, match="audience must be one of"):
        mj.events(job.job_id, audience="nobody", path=db)


def test_a_client_event_without_a_progress_payload_has_no_payload_key(db):
    job = make(db)
    mj.append_event(job.job_id, "note", {"detail": "private"}, path=db)
    assert "payload" not in mj.events(job.job_id, audience="client", path=db)[0]


def test_update_status_validates_and_reports_each_refusal_exactly(db):
    job = make(db)
    with pytest.raises(ValueError, match="status must be one of"):
        mj.update_status(job.job_id, "weird", path=db)
    with pytest.raises(ValueError, match=r"\Aadd_assets\ entries\ must\ be\ mappings\Z"):
        mj.update_status(job.job_id, "running", add_assets=["x"], path=db)
    assert mj.update_status("ghost", "running", path=db) is None
    with pytest.raises(ValueError) as raised:
        mj.update_status(job.job_id, "running", expected_status="running", path=db)
    assert str(raised.value) == f"job {job.job_id!r} is 'queued', not 'running'"
    with pytest.raises(ValueError) as illegal:
        mj.update_status(job.job_id, "succeeded", path=db)
    assert str(illegal.value) == f"illegal status transition 'queued' -> 'succeeded' for job {job.job_id!r}"
    running = mj.update_status(job.job_id, "running", progress=30, add_assets=[{"kind": "image"}], metadata_patch={"k": "v"}, now=50, path=db)
    assert (running["status"], running["progress"], running["assets"], running["metadata"], running["updated_at"]) == (
        "running", 30, [{"kind": "image"}], {"k": "v"}, 50)
    done = mj.update_status(job.job_id, "succeeded", add_assets=[{"kind": "video"}], metadata_patch={"j": 1}, now=60, path=db)
    assert done["progress"] == 100 and [a["kind"] for a in done["assets"]] == ["image", "video"]
    assert done["metadata"] == {"k": "v", "j": 1}


def test_cancelling_queued_work_ends_it_and_running_work_keeps_its_reservation(db):
    queued = make(db)
    ended = mj.request_cancel(queued.job_id, path=db)
    assert (ended["status"], ended["error"]) == ("canceled", "Canceled by requester")
    with pytest.raises(ValueError) as finished:
        mj.request_cancel(queued.job_id, path=db)
    assert str(finished.value) == "This job has already finished"
    assert mj.request_cancel("ghost", path=db) is None
    running = make(db)
    mj.update_status(running.job_id, "running", path=db)
    pending = mj.request_cancel(running.job_id, reason="why", path=db)
    assert pending["status"] == "running" and "cancel_requested_at" in pending["metadata"]
    with pytest.raises(mj.CancellationPending) as late:
        mj.update_status(running.job_id, "succeeded", path=db)
    assert str(late.value) == "Cancel requested; waiting for resources to be released"
    why = make(db)
    assert mj.request_cancel(why.job_id, reason="because", path=db)["error"] == "because"


def test_only_a_running_job_may_begin_another_attempt(db):
    job = make(db)
    with pytest.raises(ValueError) as raised:
        mj.begin_attempt(job.job_id, path=db)
    assert str(raised.value) == "only a running job can begin another attempt"
    assert mj.begin_attempt("ghost", path=db) is None
    mj.update_status(job.job_id, "running", path=db)
    legacy = mj.begin_attempt(job.job_id, path=db)
    assert legacy is not None and legacy["job_id"] == job.job_id


def test_the_idempotency_conflict_and_limit_errors_carry_their_exact_words():
    conflict = mj.IdempotencyConflict()
    assert (str(conflict), conflict.status_code, conflict.code) == (
        "idempotency key conflicts with the owner, operation, or input", 409, "idempotency_conflict")
    limit = mj.OutstandingLimit("household", 3, "image", "app1")
    reached = mj.OutstandingLimitReached(limit)
    assert str(reached) == "household outstanding job limit reached" and reached.limit is limit


def test_the_input_hash_covers_the_submission_facts_and_the_extra_input():
    a = mj.MediaJob("image.generate", workflow="w", backend="b", metadata={"k": 1}, assets=[{"a": 1}])
    base = mj._input_hash(a)
    assert base == mj._input_hash(mj.MediaJob("image.generate", workflow="w", backend="b", metadata={"k": 1}, assets=[{"a": 1}]))
    assert mj._input_hash(a, {"prompt": "x"}) != base and mj._input_hash(a, {"prompt": "x"}) != mj._input_hash(a, {"prompt": "y"})
    assert mj._input_hash(mj.MediaJob("image.edit", workflow="w", backend="b", metadata={"k": 1}, assets=[{"a": 1}])) != base
    assert mj._input_hash(a, {}) != base


@pytest.mark.parametrize("data,expected", [
    ({"creating_receipt_id": "r1", "parent_request_id": "p", "request_id": "q"}, "r1"),
    ({"parent_request_id": "p", "request_id": "q"}, "p"),
    ({"request_id": "q"}, "q"),
    ({"creating_receipt_id": "imgjob_seeded_1", "parent_request_id": "p"}, "p"),
    ({"request_id": "imgjob_seeded_1"}, None),
    ({"creating_receipt_id": "", "request_id": 7}, None),
    ({}, None),
])
def test_the_receipt_link_candidate_prefers_explicit_links_and_skips_seeded_fingerprints(data, expected):
    assert mj._receipt_link_candidate(data) == expected


def test_clearing_prompt_derivatives_drops_only_the_hash_and_the_seeded_ids():
    job = mj.MediaJob("image.generate", request_id="imgjob_seeded_a", parent_request_id="imgjob_seeded_b",
                      metadata={"request_hash": "h", "keep": 1}, input_hash="ih")
    mj._clear_prompt_derivatives(job)
    assert (job.request_id, job.parent_request_id, job.input_hash, job.metadata) == ("", None, None, {"keep": 1})
    real = mj.MediaJob("image.generate", request_id="real", parent_request_id="parent", metadata={}, input_hash="ih")
    mj._clear_prompt_derivatives(real)
    assert (real.request_id, real.parent_request_id, real.input_hash) == ("real", "parent", None)
    none_parent = mj.MediaJob("image.generate", request_id="r")
    mj._clear_prompt_derivatives(none_parent)
    assert none_parent.parent_request_id is None


def test_erasing_prompt_derivatives_rewrites_matching_old_jobs_and_counts_them(db):
    mine = make(db, request_id="imgjob_seeded_mine", app_id="app-a", created_at=100, updated_at=100,
                metadata={"request_hash": "secret"})
    seeded_parent = make(db, request_id="q-parent", parent_request_id="imgjob_seeded_p", app_id="app-a", created_at=100, updated_at=100)
    other = make(db, request_id="imgjob_seeded_other", app_id="app-b", created_at=100, updated_at=100)
    newer = make(db, request_id="imgjob_seeded_new", app_id="app-a", created_at=900, updated_at=900)
    clean = make(db, request_id="plain", app_id="app-a", created_at=100, updated_at=100)
    owner = mj.stored_owner_key(None, app_id="app-a")
    assert mj.erase_prompt_derivatives(set(), before=500, path=db) == 0
    assert mj.erase_prompt_derivatives({owner}, before=500, path=db) >= 2
    got = mj.get(mine.job_id, path=db)
    assert got["metadata"].get("request_hash") is None and got["input_hash"] is None and got["request_id"] == ""
    assert mj.get(seeded_parent.job_id, path=db)["parent_request_id"] is None
    assert mj.get(other.job_id, path=db)["request_id"] == "imgjob_seeded_other"
    assert mj.get(newer.job_id, path=db)["request_id"] == "imgjob_seeded_new"
    assert mj.get(clean.job_id, path=db)["request_id"] == "plain"
    # the cutoff stays: a stale worker copy of an old job cannot bring the hash back
    stale = mj.MediaJob("image.generate", job_id=mine.job_id, request_id="", app_id="app-a", created_at=100,
                        metadata={"request_hash": "back"}, input_hash="back")
    with mj._conn(db) as conn:
        mj._write_job(conn, stale)
        conn.commit()
    again = mj.get(mine.job_id, path=db)
    assert again["metadata"].get("request_hash") is None and again["input_hash"] is None
    assert mj.erase_prompt_derivatives({"*"}, before=2000, path=db) >= 1
    assert mj.get(newer.job_id, path=db)["request_id"] == ""
    assert mj.erase_prompt_derivatives({"*"}, before=2000, path=db) == 0


def test_a_replayed_create_returns_the_same_job_and_a_different_input_conflicts(db):
    first = mj.create(mj.MediaJob("image.generate", request_id="r1", app_id="app-a", metadata={"k": 1}), input_data={"p": "x"}, path=db)
    again = mj.create(mj.MediaJob("image.generate", request_id="r1", app_id="app-a", metadata={"k": 1}), input_data={"p": "x"}, path=db)
    assert again.job_id == first.job_id and mj.count(db) == 1
    for changed in (dict(app_id="app-b"), dict(metadata={"k": 2}), dict(intent="image.edit")):
        base = dict(intent="image.generate", request_id="r1", app_id="app-a", metadata={"k": 1})
        base.update(changed)
        with pytest.raises(mj.IdempotencyConflict):
            mj.create(mj.MediaJob(**base), input_data={"p": "x"}, path=db)
    with pytest.raises(mj.IdempotencyConflict):
        mj.create(mj.MediaJob("image.generate", request_id="r1", app_id="app-a", metadata={"k": 1}), input_data={"p": "y"}, path=db)


def test_create_stamps_the_phase_facts_of_a_first_attempt_and_resets_what_the_caller_sent(db):
    queued = mj.create(mj.MediaJob("image.generate", request_id="a", accepted_at=1, started_at=2, previous_attempts=[{"started_at": 1}]), path=db)
    assert queued.attempt_number == 1 and queued.previous_attempts == []
    assert queued.accepted_at == queued.queued_at and queued.started_at is None and queued.finished_at is None
    assert queued.first_output_at is None and queued.owner_scope == "unscoped"
    running = mj.create(mj.MediaJob("image.generate", request_id="b", status="running", assets=[{"k": "v"}]), path=db)
    assert running.started_at == running.accepted_at == running.first_output_at and running.queued_at is None
    done = mj.create(mj.MediaJob("image.generate", request_id="c", status="succeeded"), path=db)
    assert done.finished_at == done.accepted_at and done.first_output_at is None


def test_a_full_outstanding_limit_stores_nothing_and_a_limit_for_another_app_does_not_count(db):
    mine = mj.OutstandingLimit("app", 1, "turbo", "app-a")
    mj.create(mj.MediaJob("image.generate", request_id="r1", app_id="app-a", metadata={"mode": "turbo"}), outstanding_limits=[mine], path=db)
    with pytest.raises(mj.OutstandingLimitReached) as raised:
        mj.create(mj.MediaJob("image.generate", request_id="r2", app_id="app-a", metadata={"mode": "turbo"}), outstanding_limits=[mine], path=db)
    assert raised.value.limit is mine and mj.count(db) == 1
    theirs = mj.OutstandingLimit("app", 1, "turbo", "app-b")
    mj.create(mj.MediaJob("image.generate", request_id="r3", app_id="app-b", metadata={"mode": "turbo"}), outstanding_limits=[theirs], path=db)
    household = mj.OutstandingLimit("household", 2, "turbo")
    assert mj.count(db) == 2
    with pytest.raises(mj.OutstandingLimitReached):
        mj.create(mj.MediaJob("image.generate", request_id="r4", app_id="app-c", metadata={"mode": "turbo"}), outstanding_limits=[household], path=db)
    mj.create(mj.MediaJob("image.generate", request_id="r5", app_id="app-c", metadata={"mode": "slow"}), outstanding_limits=[mj.OutstandingLimit("household", 1, "slow")], path=db)


def test_active_counts_and_unfinished_jobs_follow_status_app_and_intent(db):
    a = make(db, app_id="app-a")
    make(db, app_id="app-a", intent="video.generate")
    b = make(db, app_id="app-b")
    mj.update_status(b.job_id, "running", path=db)
    mj.update_status(a.job_id, "running", path=db)
    mj.update_status(a.job_id, "succeeded", path=db)
    assert mj.active_count(app_id="app-a", intents=["image.generate"], path=db) == 0
    assert mj.active_count(app_id="app-a", intents=["image.generate", "video.generate"], path=db) == 1
    assert mj.active_count(app_id="app-b", intents=["image.generate"], path=db) == 1
    assert sorted(j["app_id"] for j in mj.unfinished(path=db)) == ["app-a", "app-b"]


def _legacy(db, **kw):
    """A stored job with no input digest, as rows from before the digest existed."""
    job = mj.MediaJob("image.generate", created_at=100, updated_at=100, **kw)
    with mj._conn(db) as conn:
        mj._write_job(conn, job)
        conn.commit()
    return job.job_id


def test_erasure_rewrites_each_legacy_row_for_exactly_the_reason_it_holds_a_prompt_derivative(db):
    only_hash = _legacy(db, request_id="plain-1", app_id="app-a", metadata={"request_hash": "h"})
    only_request = _legacy(db, request_id="imgjob_seeded_r", app_id="app-a")
    only_parent = _legacy(db, request_id="plain-2", parent_request_id="imgjob_seeded_p", app_id="app-a")
    nothing = _legacy(db, request_id="plain-3", app_id="app-a", metadata={"keep": 1})
    foreign = _legacy(db, request_id="imgjob_seeded_f", app_id="app-b", metadata={"request_hash": "h"})
    owner = mj.stored_owner_key(None, app_id="app-a")
    assert mj.erase_prompt_derivatives({owner}, before=500, path=db) == 3
    assert mj.get(only_hash, path=db)["metadata"] == {}
    assert mj.get(only_request, path=db)["request_id"] == ""
    assert mj.get(only_parent, path=db)["parent_request_id"] is None
    assert mj.get(nothing, path=db)["metadata"] == {"keep": 1}
    assert mj.get(foreign, path=db)["request_id"] == "imgjob_seeded_f"
    assert mj.erase_prompt_derivatives({owner}, before=500, path=db) == 0


def test_erasure_pages_through_more_than_one_batch_of_rows(db):
    ids = [_legacy(db, request_id=f"imgjob_seeded_{i:03d}", app_id="app-a") for i in range(130)]
    owner = mj.stored_owner_key(None, app_id="app-a")
    assert mj.erase_prompt_derivatives({owner}, before=500, path=db) == 130
    assert all(mj.get(i, path=db)["request_id"] == "" for i in ids)


def test_add_assets_are_checked_for_forbidden_keys_and_recent_filters_by_age_and_app(db):
    job = make(db, app_id="app-a")
    with pytest.raises(ValueError) as raised:
        mj.update_status(job.job_id, "running", add_assets=[{"prompt": "secret text"}], path=db)
    assert "add_assets[*]" in str(raised.value)
    old = make(db, app_id="app-a")
    with mj._conn(db) as conn:
        conn.execute("UPDATE media_jobs SET updated_at = ? WHERE job_id = ?", (1, old.job_id))
        conn.commit()
    recent = mj.recent(path=db, since_s=3600)
    assert old.job_id not in [j["job_id"] for j in recent] and job.job_id in [j["job_id"] for j in recent]
    assert old.job_id in [j["job_id"] for j in mj.recent(path=db)]
    assert [j["job_id"] for j in mj.list_jobs(since_s=3600, path=db)] == [j["job_id"] for j in recent]


def test_an_event_projection_names_every_field_and_defaults_the_source_to_unavailable():
    event = {"id": 7, "sequence": 3, "job_id": "j", "created_at": 9, "event_type": "x", "attempt_number": 2,
             "payload": {"progress": 5, "secret": "s", "phase": "p", "status": "s2", "step": 1, "total": 9}}
    base = {"sequence": 3, "job_id": "j", "created_at": 9, "event_type": "x", "source": "unavailable", "attempt_number": 2}
    assert mj._filter_event_for_audience(event, "internal") == {"id": 7, **base, "payload": event["payload"]}
    assert mj._filter_event_for_audience(event, "admin") == {"id": 7, **base}
    assert mj._filter_event_for_audience(event, "client") == {
        **base, "payload": {"progress": 5, "phase": "p", "status": "s2", "step": 1, "total": 9}}
    assert mj._filter_event_for_audience({**event, "payload": ["x"]}, "client") == base
    assert mj._filter_event_for_audience({**event, "payload": {"secret": "s"}}, "client") == base
    assert mj._filter_event_for_audience({}, "internal") == {
        "id": None, "sequence": None, "job_id": None, "created_at": None, "event_type": None,
        "source": "unavailable", "attempt_number": None, "payload": {}}


def test_recent_returns_fifty_by_default_and_filters_by_age_status_and_ownership(db):
    for i in range(52):
        make(db, app_id="app-a" if i % 2 else "app-b", request_id=f"r{i}")
    assert len(mj.recent(path=db)) == 50 and len(mj.list_jobs(limit=3, path=db)) == 3
    assert mj.recent(limit=0, path=db) == [] and mj.recent(app_ids=[], path=db) == []
    assert {j["app_id"] for j in mj.recent(app_ids=["app-a"], limit=100, path=db)} == {"app-a"}
    assert mj.recent(status="running", path=db) == [] and len(mj.recent(status="queued", limit=100, path=db)) == 52
    with pytest.raises(ValueError, match=r"\Astatus must be one of \['canceled', 'failed', 'queued', 'running', 'succeeded'\]\Z"):
        mj.recent(status="weird", path=db)
    with pytest.raises(ValueError, match="audience must be one of"):
        mj.recent(audience="nobody", path=db)


def test_a_new_job_object_is_returned_by_create_and_by_a_replay(db):
    first = mj.create(mj.MediaJob("image.generate", request_id="same"), path=db)
    assert isinstance(first, mj.MediaJob) and first.request_id == "same"
    again = mj.create(mj.MediaJob("image.generate", request_id="same"), path=db)
    assert isinstance(again, mj.MediaJob) and again.job_id == first.job_id
    assert mj.MediaJob("image.generate").request_id == ""
