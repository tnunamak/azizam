from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Barrier

import pytest

import responses_store as rs


def save(path, response_id, previous=None, marker="hello",
         person="p1", created_at=10):
    rs.save(response_id, created_at=created_at, model=marker,
            previous_response_id=previous,
            request_json={"input": marker},
            response_json={"output_text": marker},
            api_key_name="fixture", person_id=person, app_id="app-fixture",
            owner_key_hash="synthetic-owner-marker", path=path)


def test_save_requires_stable_attribution_before_replacing_a_response(tmp_path):
    path = tmp_path / "responses.db"
    rs.init_db(path)
    save(path, "kept")
    original = rs.get("kept", path)
    for identity in ({}, {"person_id": "person-only"},
                     {"person_id": "", "app_id": " ", "owner_key_hash": ""}):
        with pytest.raises(ValueError, match="stable App or credential scope"):
            rs.save("kept", created_at=11, model="test", previous_response_id=None,
                    request_json={}, response_json={}, path=path, **identity)
        assert rs.get("kept", path) == original
    for identity in ({"app_id": "app"}, {"owner_key_hash": "sha256:fixture"}):
        rs.save("scoped", created_at=11, model="test", previous_response_id=None,
                request_json={}, response_json={}, path=path, **identity)
        assert rs.get("scoped", path)[next(iter(identity))] == next(iter(identity.values()))


# S1: kills missing cycle detection, reversed/missing reversal, wrong depth
# comparison, starting at parent instead of supplied ID, and dangling failure.
@pytest.mark.parametrize("edges,start,depth,expected", [
    ([("a", None), ("b", "a"), ("c", "b")], "c", 100, ["a", "b", "c"]),
    ([("a", None), ("b", "a"), ("c", "b")], "c", 2, ["b", "c"]),
    ([("a", None)], "a", 0, []),
    ([("a", "a")], "a", 100, ["a"]),
    ([("a", "b"), ("b", "a")], "a", 100, ["b", "a"]),
    ([("b", "missing"), ("c", "b")], "c", 100, ["b", "c"]),
    ([("a", None)], "missing", 100, []),
])
def test_walk_chain_is_bounded_oldest_first_and_tolerates_missing_parents(
        tmp_path, edges, start, depth, expected):
    path = tmp_path / "responses.db"
    rs.init_db(path)
    for response_id, previous in edges:
        save(path, response_id, previous=previous)
    rows = rs.walk_chain(start, max_depth=depth, path=path)
    assert [row["id"] for row in rows] == expected


# S2: kills non-idempotent INSERT, missing commit, incorrect field serialization,
# swallowing write errors, or delete-before-save implementations losing old data.
def test_save_replaces_whole_row_and_failures_preserve_last_committed_value(tmp_path):
    path = tmp_path / "responses.db"
    rs.init_db(path)
    response_id = "resp_'_literal"
    save(path, response_id, marker="سلام 🌍", person="p1", created_at=1)
    assert rs.get(response_id, path)["request_json"] == {"input": "سلام 🌍"}
    save(path, response_id, marker="new", person="p2", created_at=2)
    assert rs.count(path) == 1
    expected = rs.get(response_id, path)
    assert expected["request_json"] == {"input": "new"}
    assert expected["response_json"] == {"output_text": "new"}
    assert expected["person_id"] == "p2"
    assert expected["created_at"] == 2
    assert expected["owner_key_hash"] == "synthetic-owner-marker"

    with pytest.raises(TypeError):
        rs.save(response_id, created_at=3, model="broken",
                previous_response_id=None, request_json={"bad": object()},
                response_json={}, path=path)
    assert rs.get(response_id, path) == expected

    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TRIGGER reject_insert BEFORE INSERT ON responses
                        BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic write failure"):
        save(path, response_id, marker="should not commit", created_at=4)
    assert rs.get(response_id, path) == expected
    assert rs.count(path) == 1
    assert rs.delete(response_id, path) is True
    assert rs.delete(response_id, path) is False
    assert rs.get(response_id, path) is None


# S3: kills deleting too broadly, changing <= to <, disabling secure_delete,
# and losing WAL truncation where deleted bytes remain. No active-reader case
# is claimed here; this verifies the uncomplicated completed-erasure contract.
@pytest.mark.parametrize("operation", ["person", "person_before", "all_before", "all"])
def test_erasure_removes_target_bytes_and_preserves_out_of_scope_rows(
        tmp_path, operation):
    path = tmp_path / "responses.db"
    rs.init_db(path)
    sentinel = "ERASE_RESPONSE_SENTINEL_79a69b"
    erased = sentinel * 200
    save(path, "old", marker=erased, person="p1", created_at=9)
    save(path, "boundary", marker=erased, person="p1", created_at=10)
    save(path, "new", marker="KEEP_NEW", person="p1", created_at=11)
    save(path, "other", marker="KEEP_OTHER", person="p2", created_at=9)

    def database_bytes():
        candidates = [path, path.with_name(path.name + "-wal")]
        return b"".join(p.read_bytes() for p in candidates if p.exists())

    assert sentinel.encode() in database_bytes()
    if operation == "person":
        removed = rs.delete_person("p1", path)
        expected_count, kept = 3, {"other"}
    elif operation == "person_before":
        removed = rs.delete_person_before("p1", 10, path)
        expected_count, kept = 2, {"new", "other"}
    elif operation == "all_before":
        removed = rs.delete_all_before(10, path)
        expected_count, kept = 3, {"new"}
    else:
        removed = rs.delete_all(path)
        expected_count, kept = 4, set()
    assert removed == expected_count
    assert {response_id for response_id in ("old", "boundary", "new", "other")
            if rs.get(response_id, path) is not None} == kept
    assert rs.count(path) == len(kept)
    assert sentinel.encode() not in database_bytes()


# S4: kills multi-statement partial updates, per-field replacement or missing
# commits. This is whole-row contention, not a claim about erasure races.
def test_parallel_saves_commit_complete_rows(tmp_path):
    path = tmp_path / "responses.db"
    rs.init_db(path)
    barrier = Barrier(4)

    def worker(number):
        marker = "writer-" + str(number)
        barrier.wait(timeout=10)
        save(path, "shared", marker=marker, person=marker, created_at=number)
        save(path, "unique-" + str(number), marker=marker,
             person=marker, created_at=number)
        return marker

    with ThreadPoolExecutor(max_workers=4) as pool:
        markers = list(pool.map(worker, range(4)))
    assert rs.count(path) == 5
    shared = rs.get("shared", path)
    assert shared["model"] in markers
    assert shared["person_id"] == shared["model"]
    assert shared["request_json"]["input"] == shared["model"]
    assert shared["response_json"]["output_text"] == shared["model"]
    assert shared["created_at"] == int(shared["model"].rsplit("-", 1)[1])
    for number, marker in enumerate(markers):
        row = rs.get("unique-" + str(number), path)
        assert row["request_json"] == {"input": marker}
        assert row["response_json"] == {"output_text": marker}
