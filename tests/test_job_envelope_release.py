"""Every path that deletes a ``media_jobs`` row releases that job's envelope in the same transaction (S2.1b).

The paths are found in the code, not listed by hand. ``test_the_paths_are_found_in_the_code`` scans the gateway for
SQL that deletes from ``media_jobs`` and for every caller of the function that does. A new delete path makes it fail
until the path calls the release and a behavioural test below covers it.

Today one function deletes job rows (``media_jobs.delete_succeeded_in``). Retention, erasure and the kill switch keep
the job rows (``erase_prompt_derivatives`` rewrites them), so they have no envelope to release.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from gateway import media_jobs
from gateway.definition_store import DefinitionStore
from gateway.job_envelope import JobEnvelope, build_job_envelope

ROOT = Path(__file__).resolve().parent.parent
SCANNED = [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py")), *sorted((ROOT / "scripts").rglob("*.py"))]
DELETE_JOBS = re.compile(r"\bDELETE\s+FROM\s+media_jobs\b", re.IGNORECASE)
RELEASES = {"release_in", "_release_envelopes_in"}


def functions(path: Path) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """The function's own nodes, without those of functions nested inside it."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def text_of(node: ast.AST) -> str:
    return "".join(n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str))


def deleters() -> dict[str, Path]:
    """Function name -> file, for every function whose own code holds SQL that deletes from ``media_jobs``."""
    found: dict[str, Path] = {}
    for path in SCANNED:
        for function in functions(path):
            if any(isinstance(n, ast.Constant) and isinstance(n.value, str) and DELETE_JOBS.search(n.value)
                   or isinstance(n, ast.JoinedStr) and DELETE_JOBS.search(text_of(n))
                   for n in own_nodes(function)):
                found[function.name] = path
    return found


def callers_of(names: set[str]) -> dict[str, set[str]]:
    """For each name, the functions (file:name) that call it."""
    found: dict[str, set[str]] = {name: set() for name in names}
    for path in SCANNED:
        for function in functions(path):
            for node in own_nodes(function):
                if isinstance(node, ast.Call):
                    callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                    if callee in names:
                        found[callee].add(f"{path.relative_to(ROOT)}:{function.name}")
    return found


def test_the_paths_are_found_in_the_code():
    found = deleters()
    assert found, "the scan found no job delete at all: the pattern or the file list is wrong"
    # Each function that deletes job rows releases the envelope.
    for name, path in found.items():
        function = next(f for f in functions(path) if f.name == name)
        called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                  for n in own_nodes(function) if isinstance(n, ast.Call)}
        assert called & RELEASES, f"{path.relative_to(ROOT)}:{name} deletes media_jobs rows but never releases envelopes"
    # Each has a behavioural test here.
    assert set(found) == set(DELETE_PATHS), f"delete paths and their tests differ: {sorted(set(found) ^ set(DELETE_PATHS))}"
    # Each caller of a deleting function is a path too. A new caller needs its own test.
    reached = {name: callers for name, callers in callers_of(set(found) | {"delete_succeeded"}).items()}
    known = {
        "delete_succeeded_in": {"gateway/media_jobs.py:delete_succeeded", "gateway/prompt_memory.py:remove_missing_media"},
        "delete_succeeded": set(),
    }
    assert reached == known, f"a new caller deletes job rows: {reached}"


def test_no_job_table_is_dropped():
    """A dropped table would delete every job row at once without an envelope release."""
    drop = re.compile(r"\bDROP\s+TABLE\s+(IF\s+EXISTS\s+)?media_jobs\b", re.IGNORECASE)
    hits = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in SCANNED
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and drop.search(node.value)
    ]
    assert hits == []


def envelope_for(tag: str) -> JobEnvelope:
    return build_job_envelope(
        source_kind="workflow", source_id=f"wf-{tag}", contract={"contract": tag}, binding={"binding": tag}, template=None,
        protocol_id="proto-x", adapter_id="adapter-x", interface_version=1, service_id="svc-1",
        origin="http://engine.test:1",
    )


def count(path: Path, sql: str, *args) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchone()[0]


def two_finished_jobs(db: Path) -> tuple[str, str]:
    doomed = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", request_id="doomed", status=media_jobs.STATUS_SUCCEEDED, progress=100),
        envelope=envelope_for("a"), path=db,
    )
    kept = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", request_id="kept", status=media_jobs.STATUS_SUCCEEDED, progress=100),
        envelope=envelope_for("b"), path=db,
    )
    return doomed.job_id, kept.job_id


def delete_succeeded_in_one_transaction(db: Path, doomed: str) -> None:
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert media_jobs.delete_succeeded_in(conn, [doomed]) == 1
        assert count_in(conn, "job_envelope", doomed) == 0, "released inside the transaction, before the commit"
        conn.commit()


def count_in(conn: sqlite3.Connection, table: str, job_id: str) -> int:
    column = "job_id"
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (job_id,)).fetchone()[0]  # noqa: S608


def delete_succeeded_alone(db: Path, doomed: str) -> None:
    assert media_jobs.delete_succeeded([doomed], path=db) == 1


# One behavioural test per path the scan finds. The key is the function that holds the DELETE.
DELETE_PATHS: dict[str, Callable[[Path, str], None]] = {
    "delete_succeeded_in": delete_succeeded_in_one_transaction,
}


@pytest.mark.parametrize("name", sorted(DELETE_PATHS))
def test_each_delete_path_releases_the_jobs_references(tmp_path: Path, name: str):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    doomed, kept = two_finished_jobs(db)
    DELETE_PATHS[name](db, doomed)
    assert count(db, "SELECT COUNT(*) FROM media_jobs WHERE job_id = ?", doomed) == 0
    assert count(db, "SELECT COUNT(*) FROM job_envelope WHERE job_id = ?", doomed) == 0
    assert count(db, "SELECT COUNT(*) FROM definition_ref WHERE job_id = ?", doomed) == 0
    # The other job is whole, and no envelope or reference outlives its job.
    assert count(db, "SELECT COUNT(*) FROM job_envelope WHERE job_id = ?", kept) == 1
    assert count(db, "SELECT COUNT(*) FROM definition_ref WHERE job_id = ?", kept) == 2
    assert count(db, "SELECT COUNT(*) FROM job_envelope WHERE job_id NOT IN (SELECT job_id FROM media_jobs)") == 0
    assert count(db, "SELECT COUNT(*) FROM definition_ref WHERE holder_kind = 'job' "
                     "AND job_id NOT IN (SELECT job_id FROM media_jobs)") == 0


def test_the_standalone_wrapper_releases_too(tmp_path: Path):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    doomed, kept = two_finished_jobs(db)
    delete_succeeded_alone(db, doomed)
    assert count(db, "SELECT COUNT(*) FROM job_envelope") == 1
    assert count(db, "SELECT COUNT(*) FROM job_envelope WHERE job_id = ?", kept) == 1


def test_a_delete_that_rolls_back_keeps_the_envelope(tmp_path: Path):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    doomed, _ = two_finished_jobs(db)
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        media_jobs.delete_succeeded_in(conn, [doomed])
        conn.rollback()
    assert count(db, "SELECT COUNT(*) FROM media_jobs WHERE job_id = ?", doomed) == 1
    assert count(db, "SELECT COUNT(*) FROM job_envelope WHERE job_id = ?", doomed) == 1
    assert count(db, "SELECT COUNT(*) FROM definition_ref WHERE job_id = ?", doomed) == 2


def test_erasure_keeps_the_job_rows_and_so_their_envelopes(tmp_path: Path):
    """Erasure and the kill switch rewrite job rows and keep them. The envelope holds no prompt, so nothing to clear."""
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    doomed, _ = two_finished_jobs(db)
    media_jobs.erase_prompt_derivatives({"*"}, before=2**31, path=db)
    assert count(db, "SELECT COUNT(*) FROM media_jobs") == 2
    assert count(db, "SELECT COUNT(*) FROM job_envelope") == 2
    assert DefinitionStore(db).envelope(doomed) == envelope_for("a").envelope
