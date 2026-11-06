"""The snapshot lifecycle (gateway/transition_snapshots.py): publish is a verified pointer move, files are private and
bounded, deletion needs proven ownership, and a crash on either side of the pointer leaves one whole snapshot published."""
from __future__ import annotations

import os
import random
import sqlite3
from pathlib import Path

import pytest

from gateway.transition_snapshots import (
    PrivateSnapshotDir,
    SnapshotBudget,
    SnapshotRejected,
    SqliteSnapshotStore,
    compatible,
)

COMPAT = {"artifact": "a1", "tokenizer": "t1", "engine": "e1", "defaults": "d1", "placement": "gpu0"}
BODY = b"0123456789abcdef" * 4
D = "dom"


class Crash(BaseException):
    """A process death: nothing in the code under test may catch it."""


def make(tmp_path: Path, *, max_bytes: int = 10_000, max_count: int = 4, **kwargs):
    files = PrivateSnapshotDir(tmp_path / "snap")
    store = SqliteSnapshotStore(tmp_path / "transition.db", files, SnapshotBudget(max_bytes, max_count), **kwargs)
    return files, store


def put(files, handle: str, body: bytes = BODY, domain: str = D) -> Path:
    path = files.path_for(domain, handle)
    path.write_bytes(body)
    path.chmod(0o600)
    return path


def record(files, store, handle: str, *, body: bytes = BODY, reported=None, compat=COMPAT, op="op-1", domain: str = D):
    put(files, handle, body, domain)
    return store.pending(domain=domain, op_id=op, handle=handle, compatibility=compat,
                         bytes=len(body) if reported is None else reported)


def publish(files, store, handle: str, **kwargs):
    record(files, store, handle, **kwargs)
    return store.publish(handle)


# -- lifecycle ---------------------------------------------------------------


def test_publish_moves_the_pointer_and_supersedes_the_old_snapshot(tmp_path) -> None:
    files, store = make(tmp_path)
    first = publish(files, store, "g1.snap")
    assert first.state == "published" and store.pointer(D) == "g1.snap"
    second = publish(files, store, "g2.snap")
    assert second.state == "published" and store.pointer(D) == "g2.snap"
    assert store.get("g1.snap").state == "abandoned"
    assert [state for _, _, state in store.rows(D)] == ["abandoned", "published"]


def test_a_pending_snapshot_is_not_served_until_published(tmp_path) -> None:
    files, store = make(tmp_path)
    record(files, store, "g1.snap")
    assert store.latest_published(D, COMPAT) is None
    store.publish("g1.snap")
    assert store.latest_published(D, COMPAT).handle == "g1.snap"


def test_a_generation_is_never_reused_even_after_deletion(tmp_path) -> None:
    files, store = make(tmp_path)
    publish(files, store, "g1.snap")
    store.retire_published(D)
    store.collect(D)
    assert store.get("g1.snap") is None
    record(files, store, "g2.snap")
    record(files, store, "g3.snap")
    generations = [generation for generation, _, _ in store.rows(D)]
    assert generations == sorted(set(generations)) and len(generations) == 3
    assert generations[-1] > generations[0]


def test_an_abandoned_snapshot_is_never_published(tmp_path) -> None:
    files, store = make(tmp_path)
    record(files, store, "g1.snap")
    assert [s.handle for s in store.abandon_pending(D, "op-1")] == ["g1.snap"]
    with pytest.raises(SnapshotRejected, match="abandoned"):
        store.publish("g1.snap")
    assert store.pointer(D) is None


def test_a_published_snapshot_cannot_be_published_again_and_an_older_one_cannot_replace_a_newer(tmp_path) -> None:
    files, store = make(tmp_path)
    record(files, store, "old.snap")
    publish(files, store, "new.snap")
    with pytest.raises(SnapshotRejected, match="older"):
        store.publish("old.snap")
    assert store.pointer(D) == "new.snap"
    with pytest.raises(SnapshotRejected, match="published"):
        store.publish("new.snap")


def test_abandon_pending_is_scoped_to_the_operation_and_never_touches_published(tmp_path) -> None:
    files, store = make(tmp_path)
    publish(files, store, "keep.snap", op="op-0")
    record(files, store, "a.snap", op="op-1")
    record(files, store, "b.snap", op="op-2")
    store.abandon_pending(D, "op-1")
    assert (store.get("a.snap").state, store.get("b.snap").state, store.get("keep.snap").state) == (
        "abandoned", "pending", "published")


# -- what publishing refuses -------------------------------------------------


@pytest.mark.parametrize("case", ["half_written", "missing", "replaced", "changed_after_record", "over_count"])
def test_publish_refuses_a_snapshot_that_failed_verification(tmp_path, case) -> None:
    files, store = make(tmp_path, max_count=2)
    publish(files, store, "good.snap")
    path = put(files, "bad.snap", BODY if case != "half_written" else BODY[:10])
    store.pending(domain=D, op_id="op-2", handle="bad.snap", compatibility=COMPAT, bytes=len(BODY))
    if case == "missing":
        path.unlink()
    elif case == "replaced":  # another file of the same size and modification time, so only its identity differs
        stat = path.stat()
        other = path.with_name("other.tmp")
        other.write_bytes(BODY)
        other.chmod(0o600)
        os.utime(other, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(other, path)
    elif case == "changed_after_record":
        stat = path.stat()
        path.write_bytes(BODY[::-1])
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    elif case == "over_count":
        put(files, "extra.snap")
    with pytest.raises(SnapshotRejected):
        store.publish("bad.snap")
    assert store.pointer(D) == "good.snap", "the old snapshot stays published"
    assert store.get("bad.snap").state == "pending"


def test_publish_refuses_a_snapshot_over_the_byte_budget(tmp_path) -> None:
    files, store = make(tmp_path, max_bytes=100)
    record(files, store, "a.snap", body=b"x" * 80)
    put(files, "orphan.bin", b"y" * 80)  # an unowned file still uses the disk
    with pytest.raises(SnapshotRejected, match="budget"):
        store.publish("a.snap")


def test_pending_refuses_evidence_gaps(tmp_path) -> None:
    files, store = make(tmp_path)
    put(files, "a.snap")
    with pytest.raises(SnapshotRejected, match="byte count"):
        store.pending(domain=D, op_id="o", handle="a.snap", compatibility=COMPAT, bytes=None)
    with pytest.raises(SnapshotRejected, match="compatibility"):
        store.pending(domain=D, op_id="o", handle="a.snap", compatibility={}, bytes=len(BODY))
    with pytest.raises(SnapshotRejected, match="no file"):
        store.pending(domain=D, op_id="o", handle="absent.snap", compatibility=COMPAT, bytes=1)
    assert store.rows(D) == ()


def test_pending_refuses_a_second_live_snapshot_on_the_same_handle(tmp_path) -> None:
    files, store = make(tmp_path)
    record(files, store, "a.snap")
    with pytest.raises(SnapshotRejected, match="live snapshot"):
        store.pending(domain=D, op_id="o2", handle="a.snap", compatibility=COMPAT, bytes=len(BODY))


@pytest.mark.parametrize("handle", ["../escape", "a/b", "", ".hidden", "x" * 200, "a b"])
def test_a_handle_must_match_the_pattern(tmp_path, handle) -> None:
    files, store = make(tmp_path)
    with pytest.raises(SnapshotRejected, match="pattern"):
        files.path_for(D, handle)


def test_a_symlink_or_a_hard_link_is_not_a_private_snapshot_file(tmp_path) -> None:
    files, store = make(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(BODY)
    files.path_for(D, "link.snap").symlink_to(outside)
    with pytest.raises(SnapshotRejected, match="private regular file"):
        store.pending(domain=D, op_id="o", handle="link.snap", compatibility=COMPAT, bytes=len(BODY))
    os.link(outside, files.path_for(D, "hard.snap"))
    with pytest.raises(SnapshotRejected, match="private regular file"):
        store.pending(domain=D, op_id="o", handle="hard.snap", compatibility=COMPAT, bytes=len(BODY))
    assert outside.exists()


def test_the_snapshot_directory_must_be_private(tmp_path) -> None:
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    with pytest.raises(SnapshotRejected, match="private"):
        PrivateSnapshotDir(open_dir)


# -- crash on either side of the pointer ------------------------------------


@pytest.mark.parametrize("point,new_is_published", [("publish:verified", False), ("publish:in_transaction", False),
                                                    ("publish:committed", True)])
def test_a_crash_around_the_pointer_leaves_one_whole_snapshot_published(tmp_path, point, new_is_published) -> None:
    files, store = make(tmp_path)
    publish(files, store, "old.snap")
    record(files, store, "new.snap", op="op-2")

    def hook(name: str) -> None:
        if name == point:
            raise Crash(name)

    crashing = SqliteSnapshotStore(tmp_path / "transition.db", files, store.budget, crash_hook=hook)
    with pytest.raises(Crash):
        crashing.publish("new.snap")
    restarted = SqliteSnapshotStore(tmp_path / "transition.db", files, store.budget)
    expected = "new.snap" if new_is_published else "old.snap"
    assert restarted.pointer(D) == expected
    served = restarted.latest_published(D, COMPAT)
    assert served is not None and served.handle == expected
    states = {handle: state for _, handle, state in restarted.rows(D)}
    assert list(states.values()).count("published") == 1
    assert states["new.snap"] == ("published" if new_is_published else "pending")
    restarted.startup_reconcile()  # the restart reconciles whatever is still pending
    assert "pending" not in {state for _, _, state in restarted.rows(D)}
    assert restarted.pointer(D) == expected


# -- compatibility -----------------------------------------------------------


@pytest.mark.parametrize("key", ["artifact", "tokenizer", "engine", "defaults"])
def test_a_changed_identity_is_not_compatible(tmp_path, key) -> None:
    files, store = make(tmp_path)
    publish(files, store, "a.snap")
    assert store.latest_published(D, {**COMPAT, key: "changed"}) is None
    assert store.pointer(D) == "a.snap", "a mismatch leaves the snapshot published for another config"
    assert store.latest_published(D, COMPAT) is not None


def test_only_a_reviewed_placement_key_may_differ(tmp_path) -> None:
    files, store = make(tmp_path)
    publish(files, store, "a.snap")
    moved = {**COMPAT, "placement": "gpu1"}
    assert store.latest_published(D, moved) is None
    assert store.latest_published(D, moved, ignore=["placement"]).handle == "a.snap"
    assert store.latest_published(D, {**moved, "tokenizer": "t2"}, ignore=["placement"]) is None
    assert store.latest_published(D, {k: v for k, v in moved.items() if k != "engine"}, ignore=["placement"]) is None


def test_an_unknown_token_never_matches() -> None:
    assert not compatible({}, {}) and not compatible(COMPAT, {}) and not compatible({}, COMPAT)
    assert not compatible({"placement": "a"}, {"placement": "a"}, ignore=["placement"]), "nothing left to compare"


# -- a published file that is no longer the verified one --------------------


@pytest.mark.parametrize("damage", ["truncate", "rewrite", "replace", "delete"])
def test_a_damaged_published_file_is_never_offered_and_is_retired(tmp_path, damage) -> None:
    files, store = make(tmp_path)
    path = put(files, "a.snap")
    store.pending(domain=D, op_id="o", handle="a.snap", compatibility=COMPAT, bytes=len(BODY))
    store.publish("a.snap")
    before = path.stat()
    if damage == "truncate":
        path.write_bytes(BODY[:8])
    elif damage == "rewrite":
        path.write_bytes(BODY[::-1])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000))
    elif damage == "replace":  # same size and modification time: only the identity differs
        other = path.with_name("other.tmp")
        other.write_bytes(BODY)
        other.chmod(0o600)
        os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(other, path)
    else:
        path.unlink()
    assert store.latest_published(D, COMPAT) is None
    assert store.pointer(D) is None and store.get("a.snap").state == "abandoned"


# -- budget, pins, deletion --------------------------------------------------


def test_has_room_counts_every_file_on_disk_including_orphans(tmp_path) -> None:
    files, store = make(tmp_path, max_bytes=300, max_count=3)
    assert store.has_room(D)
    put(files, "o1.bin", b"x" * 100)
    put(files, "o2.bin", b"x" * 100)
    assert store.has_room(D) and not store.has_room(D, expect_bytes=150)
    put(files, "o3.bin", b"x" * 10)
    assert not store.has_room(D), "count limit"
    assert store.orphans(D) == ("o1.bin", "o2.bin", "o3.bin")


def test_collect_deletes_only_abandoned_unpinned_snapshots_it_owns(tmp_path) -> None:
    files, store = make(tmp_path)
    publish(files, store, "live.snap")
    record(files, store, "dead.snap", op="op-2")
    record(files, store, "pinned.snap", op="op-3")
    store.abandon_pending(D)
    assert store.pin("pinned.snap")
    foreign = put(files, "foreign.bin")
    deleted = store.collect(D)
    assert deleted == ("dead.snap",)
    assert files.facts(D, "live.snap") is not None and files.facts(D, "pinned.snap") is not None and foreign.exists()
    assert not files.path_for(D, "dead.snap").exists() and store.get("dead.snap") is None
    store.unpin("pinned.snap")
    assert store.collect(D) == ("pinned.snap",) and foreign.exists()


def test_collect_does_not_delete_a_file_that_is_no_longer_the_recorded_one(tmp_path) -> None:
    files, store = make(tmp_path)
    path = record(files, store, "a.snap") and files.path_for(D, "a.snap")
    store.abandon_pending(D)
    other = path.with_name("swap.tmp")
    other.write_bytes(b"someone else's file")
    other.chmod(0o600)
    os.replace(other, path)
    assert store.collect(D) == ()
    assert path.read_bytes() == b"someone else's file", "ownership was not proven, so the file stays"
    assert store.get("a.snap").state == "abandoned"


def test_collect_marks_a_snapshot_whose_file_is_already_gone_deleted(tmp_path) -> None:
    files, store = make(tmp_path)
    path = put(files, "a.snap")
    store.pending(domain=D, op_id="o", handle="a.snap", compatibility=COMPAT, bytes=len(BODY))
    store.abandon_pending(D)
    path.unlink()
    assert store.collect(D) == ("a.snap",) and store.get("a.snap") is None


def test_a_file_in_another_domain_is_not_reachable_by_this_domains_ledger(tmp_path) -> None:
    files, store = make(tmp_path)
    other = put(files, "same-name.snap", domain="other")
    record(files, store, "same-name.snap")
    store.abandon_pending(D)
    store.collect(D)
    assert other.exists() and not files.path_for(D, "same-name.snap").exists()


def test_startup_reconcile_clears_dead_pins_and_abandons_pending(tmp_path) -> None:
    files, store = make(tmp_path)
    publish(files, store, "live.snap")
    record(files, store, "half.snap", op="op-2")
    store.pin("live.snap")
    SqliteSnapshotStore(tmp_path / "transition.db", files, store.budget).startup_reconcile()
    assert store.get("half.snap") is None and not files.path_for(D, "half.snap").exists()
    assert store.pin("live.snap") and store.unpin("live.snap")
    store.retire_published(D)
    assert store.collect(D) == ("live.snap",), "a dead process's pin no longer protects a file"


# -- seeded state machine ----------------------------------------------------

RANK = {"pending": 0, "published": 1, "abandoned": 2, "deleted": 3}


@pytest.mark.parametrize("seed", [1, 7, 42, 2026, 90210])
def test_snapshot_lifecycle_state_machine(tmp_path, seed) -> None:
    """Random operations; after each one: a published snapshot passed integrity, an abandoned one is never
    published again, a delete touches only owned files, and the state of a row never moves backwards."""
    rng = random.Random(seed)
    files, store = make(tmp_path, max_bytes=100_000, max_count=40)
    domains = ["d1", "d2"]
    counter = iter(range(10_000))
    live: dict[str, dict] = {}  # handle -> {"domain", "tampered", "path"}
    foreign: dict[Path, bytes] = {}
    last_rank: dict[str, int] = {}
    ever_published_ok: set[str] = set()
    stats = {"published": 0, "rejected": 0, "deleted": 0}

    def new_pending(domain: str, mismatch: bool) -> None:
        handle = f"h{next(counter)}.snap"
        body = BODY + bytes([rng.randrange(256)])
        path = put(files, handle, body, domain)
        try:
            store.pending(domain=domain, op_id=f"op-{rng.randrange(3)}", handle=handle, compatibility=COMPAT,
                          bytes=len(body) - (5 if mismatch else 0))
        except SnapshotRejected:
            return
        live[handle] = {"domain": domain, "tampered": mismatch, "path": path}

    def tamper(handle: str) -> None:
        info = live[handle]
        path: Path = info["path"]
        if not path.exists():
            return
        how = rng.choice(["append", "rewrite", "replace"])
        stat = path.stat()
        if how == "append":
            with path.open("ab") as handle_file:
                handle_file.write(b"!")
        elif how == "rewrite":
            path.write_bytes(path.read_bytes()[::-1])
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 3_000_000))
        else:
            swap = path.with_name("swap.tmp")
            swap.write_bytes(path.read_bytes())
            swap.chmod(0o600)
            os.replace(swap, path)
        info["tampered"] = True

    for step in range(220):
        action = rng.choice(["pending"] * 4 + ["mismatch", "publish", "publish", "publish", "abandon", "retire", "collect",
                                          "collect", "tamper", "foreign", "restart", "pin", "delete_foreign"])
        pendings = [h for h in live if (row := store.get(h)) is not None and row.state == "pending"]
        if action == "pending":
            new_pending(rng.choice(domains), False)
        elif action == "mismatch":
            new_pending(rng.choice(domains), True)
        elif action == "publish" and pendings:
            handle = rng.choice(pendings)
            try:
                store.publish(handle)
            except SnapshotRejected:
                stats["rejected"] += 1
            else:
                assert not live[handle]["tampered"], "a snapshot that failed integrity was published"
                ever_published_ok.add(handle)
                stats["published"] += 1
        elif action == "abandon":
            store.abandon_pending(rng.choice(domains), rng.choice([None, "op-0", "op-1"]))
        elif action == "retire":
            store.retire_published(rng.choice(domains))
        elif action == "collect":
            stats["deleted"] += len(store.collect(rng.choice(domains)))
        elif action == "tamper" and live:
            tamper(rng.choice(sorted(live)))
        elif action == "foreign":
            path = put(files, f"foreign{next(counter)}.bin", b"mine", rng.choice(domains))
            foreign[path] = b"mine"
        elif action == "restart":
            store = SqliteSnapshotStore(tmp_path / "transition.db", files, store.budget)
            if rng.random() < 0.5:
                store.startup_reconcile()
        elif action == "pin" and live:
            handle = rng.choice(sorted(live))
            (store.pin if rng.random() < 0.5 else store.unpin)(handle)
        elif action == "delete_foreign" and foreign:
            path = rng.choice(sorted(foreign))
            domain = next(d for d in domains if path.parent == files.path_for(d, "x").parent)
            with pytest.raises(SnapshotRejected):
                files.remove(domain, path.name, "0:0")

        # -- invariants after every step
        for domain in domains:
            rows = store.rows(domain)
            assert [state for _, _, state in rows].count("published") <= 1
            pointer = store.pointer(domain)
            if pointer is not None:
                assert store.get(pointer).state == "published" and pointer in ever_published_ok
            elif rows:
                assert "published" not in {state for _, _, state in rows}
        for domain in domains:
            for generation, handle, state in store.rows(domain):
                key = f"{handle}#{generation}"
                assert RANK[state] >= last_rank.get(key, 0), f"{key} moved backwards to {state}"
                last_rank[key] = RANK[state]
                if state == "published":
                    assert handle in ever_published_ok
                if state == "deleted":
                    assert not files.path_for(domain, handle).exists()
        for path, body in foreign.items():
            assert path.exists() and path.read_bytes() == body, "a file the ledger does not own was deleted"
    generations = [g for d in domains for g, _, _ in store.rows(d)]
    assert len(generations) == len(set(generations)), "a generation was reused"
    assert stats["published"] >= 5 and stats["rejected"] >= 1 and stats["deleted"] >= 3, stats


# -- handles are per domain, and a domain never reuses one --------------------

# The ledger schema as the first T1.3 commit wrote it: one live row per handle across every domain.
FIRST_SCHEMA = """
CREATE TABLE transition_snapshot (
    generation    INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT NOT NULL,
    handle        TEXT NOT NULL,
    op_id         TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('pending', 'published', 'abandoned', 'deleted')),
    compatibility TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    file_size     INTEGER NOT NULL,
    file_mtime_ns INTEGER NOT NULL,
    file_id       TEXT NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0,
    created       REAL NOT NULL,
    updated       REAL NOT NULL
);
CREATE UNIQUE INDEX transition_snapshot_live_handle ON transition_snapshot(handle) WHERE state != 'deleted';
CREATE INDEX transition_snapshot_domain ON transition_snapshot(domain, state);
CREATE TABLE transition_snapshot_pointer (
    domain     TEXT PRIMARY KEY,
    generation INTEGER NOT NULL REFERENCES transition_snapshot(generation)
);
"""


def indexes(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        return {name: sql for name, sql in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'transition_snapshot' AND sql IS NOT NULL")}


def test_a_ledger_of_the_first_schema_is_migrated_to_a_per_domain_live_index(tmp_path) -> None:
    db = tmp_path / "transition.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(FIRST_SCHEMA)
        for generation, handle, state in ((1, "a.snap", "published"), (2, "old.snap", "deleted")):
            connection.execute("INSERT INTO transition_snapshot(generation, domain, handle, op_id, state, compatibility, bytes,"
                               " file_size, file_mtime_ns, file_id, created, updated) VALUES (?, ?, ?, 'op-0', ?, '{}', 1, 1, 1, '1:1', 1, 1)",
                               (generation, D, handle, state))
        connection.execute("INSERT INTO transition_snapshot_pointer(domain, generation) VALUES (?, 1)", (D,))
    before = indexes(db)
    assert "(handle)" in before["transition_snapshot_live_handle"].replace(" ", "") or "ON transition_snapshot(handle)" in before["transition_snapshot_live_handle"]
    files = PrivateSnapshotDir(tmp_path / "snap")
    store = SqliteSnapshotStore(db, files, SnapshotBudget(10_000, 4))
    after = indexes(db)
    assert "transition_snapshot_live_handle" not in after, "the global live-handle index is gone"
    per_domain = [sql for name, sql in after.items() if "live" in name]
    assert len(per_domain) == 1 and "(domain, handle)" in per_domain[0] and "state != 'deleted'" in per_domain[0]
    assert store.rows(D) == ((1, "a.snap", "published"), (2, "old.snap", "deleted")), "no row was lost"
    assert store.pointer(D) == "a.snap"
    # What the old index refused now works: the same handle live in another domain.
    record(files, store, "a.snap", domain="other")
    assert store.get("a.snap", "other").domain == "other"
    # Opening the migrated ledger again changes nothing.
    SqliteSnapshotStore(db, files, SnapshotBudget(10_000, 4))
    assert indexes(db) == after


def test_the_same_handle_may_be_live_in_two_domains_and_calls_name_the_domain(tmp_path) -> None:
    files, store = make(tmp_path)
    publish(files, store, "same.snap", domain="one", op="op-1")
    record(files, store, "same.snap", domain="two", op="op-2")
    with pytest.raises(SnapshotRejected, match="more than one domain"):
        store.publish("same.snap")
    assert store.publish("same.snap", "two").domain == "two"
    assert store.pointer("one") == "same.snap" and store.pointer("two") == "same.snap"
    assert store.pin("same.snap", "one") and store.get("same.snap", "two") is not None
    assert store.unpin("same.snap", "one")


@pytest.mark.parametrize("path", ["live", "abandoned", "deleted"])
def test_a_handle_an_earlier_row_of_the_domain_used_is_refused_deleted_rows_included(tmp_path, path) -> None:
    files, store = make(tmp_path)
    publish(files, store, "fixed.snap")
    if path != "live":
        store.retire_published(D)
    if path == "deleted":
        assert store.collect(D) == ("fixed.snap",)
        assert store.get("fixed.snap") is None
    put(files, "fixed.snap")
    with pytest.raises(SnapshotRejected, match="earlier snapshot of this domain|live snapshot") as caught:
        store.pending(domain=D, op_id="op-2", handle="fixed.snap", compatibility=COMPAT, bytes=len(BODY))
    if path == "deleted":
        assert "deleted rows count" in str(caught.value) and "new handle" in str(caught.value)
    assert [row[2] for row in store.rows(D)] == [{"live": "published", "abandoned": "abandoned", "deleted": "deleted"}[path]], "no row was added"
    record(files, store, "fresh.snap")  # another handle is fine
