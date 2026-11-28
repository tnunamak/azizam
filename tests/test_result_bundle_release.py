"""Every path that deletes or erases a job gives up its results in the same transaction (S2.3).

The paths are found in the code, as ``tests/test_job_envelope_release.py`` finds the envelope ones. A new path makes
``test_the_paths_are_found_in_the_code`` fail until it releases the results and a behavioural test below covers it.

The paths today:
- job delete: ``media_jobs.delete_succeeded_in`` (through ``_release_envelopes_in``) releases stage rows, bundle and JSON
  values with the job row.
- erasure, forget and the kill switch: ``media_jobs.erase_prompt_derivatives`` keeps the job rows and erases the JSON
  values and unpublished stage rows (``result_bundle.erase_in``), inside its own transaction after it commits the cutoffs.
- retention: ``result_bundle.sweep`` (``expire_in``) from the maintenance loop.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

from gateway import media_jobs, result_bundle
from gateway.result_bundle import Reader, ResultLimits
from tests.test_job_envelope_release import SCANNED, ROOT, callers_of, functions, own_nodes, text_of

CANARY = "canary-value-erase-9d2e"
RESULT_DELETE = re.compile(r"\bDELETE\s+FROM\s+result_(stage|json|bundle)\b", re.IGNORECASE)


def called_names(function: ast.AST) -> set[str]:
    return {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in own_nodes(function) if isinstance(n, ast.Call)}


def called_on(function: ast.AST, owner: str) -> set[str]:
    """Attribute calls made on the module name ``owner`` (``result_bundle.release_in`` -> ``release_in``)."""
    return {n.func.attr for n in own_nodes(function)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == owner}


def function_named(path: Path, name: str) -> ast.FunctionDef:
    return next(f for f in functions(path) if f.name == name)


def test_the_paths_are_found_in_the_code():
    media = ROOT / "gateway" / "media_jobs.py"
    # 1. Job delete: the function that deletes job rows releases envelopes, and that release gives the results up too.
    assert "release_in" in called_on(function_named(media, "_release_envelopes_in"), "result_bundle")
    assert "_release_envelopes_in" in called_names(function_named(media, "delete_succeeded_in"))
    # 2. Erasure and the kill switch go through one function, which erases the results.
    assert "_erase_results" in called_names(function_named(media, "erase_prompt_derivatives"))
    assert "erase_in" in called_on(function_named(media, "_erase_results"), "result_bundle")
    erasers = callers_of({"erase_prompt_derivatives"})["erase_prompt_derivatives"]
    assert erasers == {
        "proxy.py:_erase_person_prompts_before", "proxy.py:_purge_prompt_memory_on_kill",
        "proxy.py:_maintain_household_prompt_memory", "proxy.py:_maintain_person_prompt_memory",
    }, f"a new erasure path needs a test here: {sorted(erasers)}"
    # 3. Retention: the sweep runs from the maintenance loop.
    assert callers_of({"sweep"})["sweep"] >= {"proxy.py:_collect_definition_garbage"}
    # 4. Nothing outside the result module deletes result rows: a second delete path would need its own release.
    outside = sorted(
        f"{path.relative_to(ROOT)}:{function.name}"
        for path in SCANNED if path.name != "result_bundle.py"
        for function in functions(path)
        if any(isinstance(n, ast.Constant) and isinstance(n.value, str) and RESULT_DELETE.search(n.value)
               or isinstance(n, ast.JoinedStr) and RESULT_DELETE.search(text_of(n)) for n in own_nodes(function)))
    assert outside == []


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    return path


def make_job(db: Path, tag: str, *, created_at: int | None = None, status: str = media_jobs.STATUS_SUCCEEDED) -> str:
    job = media_jobs.MediaJob(intent="thing.make", request_id=f"req-{tag}", status=status, app_id=f"app-{tag}",
                              owner_key_hash=f"key-{tag}", maker_person_id=f"person-{tag}")
    if created_at is not None:
        job.created_at = created_at
    return media_jobs.create(job, path=db, maker_from_request=False).job_id


def published(db: Path, tag: str, **kwargs) -> str:
    """A job that published one JSON result and one list of files."""
    job_id = make_job(db, tag, status=media_jobs.STATUS_RUNNING, **kwargs)
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, job_id, 1, "answer", json.dumps({"k": CANARY}).encode(), limits=ResultLimits())
        result_bundle.stage_assets_in(conn, job_id, 1, "files", [
            {"asset_ref": f"asset-{tag}", "media_type": "a/b", "size_bytes": 3}], limits=ResultLimits())
        conn.commit()
    media_jobs.update_status(
        job_id, media_jobs.STATUS_SUCCEEDED, path=db,
        before_commit=lambda conn, job: result_bundle.publish_in(conn, job_id, 1, limits=ResultLimits()))
    return job_id


def staged(db: Path, tag: str) -> str:
    """A running job with an unpublished JSON stage."""
    job_id = make_job(db, tag, status=media_jobs.STATUS_RUNNING)
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, job_id, 1, "answer", json.dumps({"k": CANARY}).encode(), limits=ResultLimits())
        conn.commit()
    return job_id


def count(db: Path, table: str, job_id: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE job_id = ?", (job_id,)).fetchone()[0]  # noqa: S608


def everything_for(db: Path, job_id: str) -> int:
    return sum(count(db, table, job_id) for table in ("result_stage", "result_json", "result_bundle"))


def reader(db: Path, job_id: str, tag: str) -> Reader:
    return Reader(media_jobs.get(job_id, path=db)["owner_scope"], person_id=f"person-{tag}")


# --- job delete -------------------------------------------------------------------------------------------------

def test_a_job_delete_releases_its_results_in_the_jobs_own_transaction(db: Path):
    doomed, kept = published(db, "a"), published(db, "b")
    assert everything_for(db, doomed) == 4 and everything_for(db, kept) == 4
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert media_jobs.delete_succeeded_in(conn, [doomed]) == 1
        # Released inside the transaction, before the commit, and visible to no other connection until then.
        assert conn.execute("SELECT COUNT(*) FROM result_stage WHERE job_id = ?", (doomed,)).fetchone()[0] == 0
        assert count(db, "result_stage", doomed) == 2
        conn.commit()
    assert everything_for(db, doomed) == 0
    assert everything_for(db, kept) == 4, "another job's results stay"
    assert result_bundle.read_json(kept, "answer", reader(db, kept, "b"), path=db) is not None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM result_stage WHERE job_id NOT IN (SELECT job_id FROM media_jobs)"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM result_json WHERE job_id NOT IN (SELECT job_id FROM media_jobs)"
                            ).fetchone()[0] == 0


def test_a_job_delete_that_rolls_back_keeps_its_results(db: Path):
    doomed = published(db, "a")
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        media_jobs.delete_succeeded_in(conn, [doomed])
        conn.rollback()
    assert count(db, "media_jobs", doomed) == 1 and everything_for(db, doomed) == 4


def test_the_standalone_delete_releases_too(db: Path):
    doomed = published(db, "a")
    assert media_jobs.delete_succeeded([doomed], path=db) == 1
    assert everything_for(db, doomed) == 0


def test_a_job_that_has_not_succeeded_keeps_its_stage(db: Path):
    running = staged(db, "r")
    assert media_jobs.delete_succeeded([running], path=db) == 0
    assert everything_for(db, running) == 2


# --- erasure, forget, the kill switch -------------------------------------------------------------------------

def test_person_erasure_erases_that_persons_values_and_nobody_elses(db: Path):
    mine, theirs = published(db, "a"), published(db, "b")
    in_flight = staged(db, "a2")
    owner = media_jobs.get(mine, path=db)["owner_scope"]
    media_jobs.erase_prompt_derivatives({owner}, before=2**31, path=db)
    assert count(db, "result_json", mine) == 0 and count(db, "media_jobs", mine) == 1, "the job row stays"
    assert result_bundle.read_json(mine, "answer", reader(db, mine, "a"), path=db) is None
    assert count(db, "result_json", theirs) == 1
    assert result_bundle.read_json(theirs, "answer", reader(db, theirs, "b"), path=db) is not None
    # Facts without a fingerprint stay: the bundle says what there was, not what it said.
    assert result_bundle.bundle_summary(mine, path=db)["results"][0]["name"] == "answer"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT sha256 FROM result_stage WHERE job_id = ? AND kind = 'json'", (mine,)).fetchone() == (None,)
        assert conn.execute("SELECT sha256 FROM result_stage WHERE job_id = ? AND kind = 'json'", (theirs,)
                            ).fetchone()[0].startswith("sha256:")
    # The unpublished stage of the same person's running job goes with it.
    assert count(db, "result_json", in_flight) == 1, "that job belongs to another owner scope"


def test_the_kill_switch_erases_every_value_and_unpublished_stage(db: Path):
    a, b, running = published(db, "a"), published(db, "b"), staged(db, "c")
    media_jobs.erase_prompt_derivatives({"*"}, before=2**31, path=db)
    for job_id in (a, b, running):
        assert count(db, "result_json", job_id) == 0
    assert count(db, "result_stage", running) == 0, "an unpublished stage is not kept"
    assert count(db, "result_stage", a) == 2, "published facts stay"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0] == 3


def test_erasure_leaves_jobs_made_after_its_cutoff(db: Path):
    old = published(db, "a", created_at=100)
    new = published(db, "b", created_at=10_000)
    media_jobs.erase_prompt_derivatives({"*"}, before=5_000, path=db)
    assert count(db, "result_json", old) == 0
    assert count(db, "result_json", new) == 1


def test_erasure_before_the_first_result_store_exists_is_a_no_op(tmp_path: Path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE media_jobs (job_id TEXT PRIMARY KEY, job_json TEXT, created_at INTEGER);")
    assert media_jobs.erase_prompt_derivatives({"*"}, before=2**31, path=path) == 0


# --- retention ------------------------------------------------------------------------------------------------

def test_retention_sweep_deletes_old_values_only(db: Path):
    old, new = published(db, "a"), published(db, "b")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE result_json SET created = 10.0 WHERE job_id = ?", (old,))
    assert result_bundle.sweep(retention_s=100.0, path=db, now=1000.0) == 1
    assert count(db, "result_json", old) == 0 and count(db, "result_json", new) == 1
    assert count(db, "result_stage", old) == 2, "the published facts stay"
