"""Hermetic stdlib tests for durable opaque snapshots."""
import hashlib
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from gateway.category_store import (
    CategoryStore, CorruptSnapshotError, NoActiveSnapshotError,
    SnapshotNotFoundError, StaleBaseError,
)


class CategoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "policy.db"
        self.store = CategoryStore(self.path)

    def candidate(self, body=None, base=None):
        return self.store.create_candidate(
            {"enabled": True} if body is None else body, base, "owner", "edit"
        )

    def initial(self):
        candidate = self.candidate()
        return self.store.activate(candidate.revision, None)

    def sql(self, statement, parameters=()):
        with sqlite3.connect(str(self.path)) as conn:
            return conn.execute(statement, parameters).fetchall()

    def test_empty_store_explicitly_reports_no_active(self):
        with self.assertRaises(NoActiveSnapshotError):
            self.store.get_active()
        self.assertEqual(self.store.active_revision_counter, 0)
        self.assertEqual(self.store.history(), [])
        self.candidate()
        with self.assertRaises(NoActiveSnapshotError):
            self.store.get_active()

    def test_create_activate_canonical_json_and_reopen(self):
        body = {"z": [True, None], "a": "é"}
        candidate = self.candidate(body)
        body["z"].append(False)
        self.assertEqual(candidate.body_json, '{"a":"é","z":[true,null]}')
        self.assertEqual(candidate.body_sha256,
                         hashlib.sha256(candidate.body_json.encode()).hexdigest())
        active = self.store.activate(candidate.revision, None)
        self.assertEqual(active.active_counter, 1)
        reopened = CategoryStore(self.path)
        self.assertEqual(reopened.get_active(), active)
        self.assertEqual(reopened.active_revision_counter, 1)
        decoded = reopened.get_active().body
        decoded["a"] = "changed"
        self.assertEqual(reopened.get(candidate.revision).body["a"], "é")
        self.assertEqual(self.sql("PRAGMA journal_mode")[0][0], "wal")

    def test_stale_base_rejected_pointer_and_audit_unchanged(self):
        first = self.initial()
        winner = self.candidate({"n": 1}, first.revision)
        loser = self.candidate({"n": 2}, first.revision)
        active = self.store.activate(winner.revision, first.revision)
        before = self.store.history()
        with self.assertRaises(StaleBaseError):
            self.store.activate(loser.revision, first.revision)
        self.assertEqual(self.store.get_active(), active)
        self.assertEqual(self.store.history(), before)
        self.assertEqual(self.store.active_revision_counter, 2)

    def test_candidate_base_cannot_be_relabelled_at_activation(self):
        stale = self.candidate({"old": True})
        current = self.initial()
        with self.assertRaises(StaleBaseError):
            self.store.activate(stale.revision, current.revision)
        self.assertEqual(self.store.get_active(), current)

    def test_stale_candidate_creation_does_not_insert(self):
        current = self.initial()
        with self.assertRaises(StaleBaseError):
            self.candidate({"stale": True}, None)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM policy_snapshots")[0][0], 1)
        self.assertEqual(self.store.get_active(), current)

    def test_concurrent_activations_exactly_one_wins(self):
        first = self.initial()
        candidates = [self.candidate({"n": n}, first.revision) for n in range(2)]
        stores = [CategoryStore(self.path), CategoryStore(self.path)]
        barrier = threading.Barrier(2)

        def activate(index):
            barrier.wait(timeout=5)
            try:
                return stores[index].activate(candidates[index].revision, first.revision)
            except StaleBaseError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(activate, range(2)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.get_active(), winners[0])
        self.assertEqual(len(self.store.history()), 2)
        self.assertEqual(self.store.active_revision_counter, 2)

    def test_restore_creates_new_candidate_and_requires_activation(self):
        original = self.initial()
        edited = self.candidate({"enabled": False}, original.revision)
        current = self.store.activate(edited.revision, original.revision)
        restored = self.store.restore(original.revision, current.revision, "restorer")
        self.assertGreater(restored.revision, current.revision)
        self.assertEqual(restored.parent_revision, current.revision)
        self.assertEqual(restored.body_json, original.body_json)
        self.assertEqual(restored.body_sha256, original.body_sha256)
        self.assertEqual(self.store.get_active(), current)
        self.assertEqual(self.store.active_revision_counter, 2)
        audit = self.store.history()[-1]
        self.assertEqual(audit["action"], "restore")
        self.assertEqual(audit["source_revision"], original.revision)
        self.assertEqual(audit["author"], "restorer")
        active = self.store.activate(restored.revision, current.revision)
        self.assertEqual(active.active_counter, 3)
        self.assertEqual(CategoryStore(self.path).get_active(), active)

    def test_interrupted_activation_rolls_back_pointer_counter_and_audit(self):
        original = self.initial()
        candidate = self.candidate({"changed": True}, original.revision)
        before = self.store.history()
        append = self.store._append_audit

        def fail_after_audit(*args):
            append(*args)
            raise RuntimeError("simulated interruption")

        with patch.object(self.store, "_append_audit", side_effect=fail_after_audit):
            with self.assertRaises(RuntimeError):
                self.store.activate(candidate.revision, original.revision)
        reopened = CategoryStore(self.path)
        self.assertEqual(reopened.get_active(), original)
        self.assertEqual(reopened.active_revision_counter, 1)
        self.assertEqual(reopened.history(), before)
        self.assertEqual(reopened.get(candidate.revision), candidate)

    def test_interrupted_restore_rolls_back_candidate_and_audit(self):
        original = self.initial()
        before = self.store.history()
        count = self.sql("SELECT COUNT(*) FROM policy_snapshots")[0][0]
        append = self.store._append_audit

        def fail_after_audit(*args):
            append(*args)
            raise RuntimeError("simulated interruption")

        with patch.object(self.store, "_append_audit", side_effect=fail_after_audit):
            with self.assertRaises(RuntimeError):
                self.store.restore(original.revision, original.revision, "owner")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM policy_snapshots")[0][0], count)
        self.assertEqual(self.store.history(), before)
        self.assertEqual(self.store.get_active(), original)

    def test_tampered_active_hash_fails_read_and_startup(self):
        original = self.initial()
        # Simulate out-of-band corruption, deliberately bypassing the guard.
        self.sql("DROP TRIGGER policy_snapshots_no_update")
        self.sql("UPDATE policy_snapshots SET body_json='{}' WHERE revision=?",
                 (original.revision,))
        with self.assertRaises(CorruptSnapshotError):
            self.store.get(original.revision)
        with self.assertRaises(CorruptSnapshotError):
            self.store.get_active()
        with self.assertRaises(CorruptSnapshotError):
            CategoryStore(self.path)
        with self.assertRaises(CorruptSnapshotError):
            _ = self.store.active_revision_counter

    def test_tampered_candidate_cannot_activate(self):
        original = self.initial()
        candidate = self.candidate({"new": True}, original.revision)
        before = self.store.history()
        self.sql("DROP TRIGGER policy_snapshots_no_update")
        self.sql("UPDATE policy_snapshots SET body_sha256='wrong' WHERE revision=?",
                 (candidate.revision,))
        with self.assertRaises(CorruptSnapshotError):
            self.store.activate(candidate.revision, original.revision)
        self.assertEqual(self.store.get_active(), original)
        self.assertEqual(self.store.history(), before)
        with self.assertRaises(CorruptSnapshotError):
            self.store.restore(candidate.revision, original.revision, "owner")

    def test_audit_is_ordered_append_only_and_limited(self):
        first = self.initial()
        second = self.store.restore(first.revision, first.revision, "owner")
        self.store.activate(second.revision, first.revision)
        history = self.store.history()
        self.assertEqual([row["action"] for row in history],
                         ["activate", "restore", "activate"])
        self.assertEqual([row["sequence"] for row in history], [1, 2, 3])
        self.assertEqual([row["active_counter"] for row in history], [1, 1, 2])
        self.assertEqual(self.store.history(2), history[-2:])
        self.assertEqual(self.store.history(0), [])
        for statement in ("UPDATE policy_audit SET author='other'",
                          "DELETE FROM policy_audit"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.sql(statement)
        self.assertEqual(self.store.history(), history)

    def test_snapshot_rows_are_immutable(self):
        candidate = self.candidate()
        for statement in ("UPDATE policy_snapshots SET reason='other'",
                          "DELETE FROM policy_snapshots"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.sql(statement)
        self.assertEqual(self.store.get(candidate.revision), candidate)

    def test_missing_revision_and_invalid_input_leave_state_unchanged(self):
        for value in (float("nan"), float("inf"), {1: "coerced"}, (1, 2), {"x": object()}):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    self.candidate(value)
        with self.assertRaises(SnapshotNotFoundError):
            self.store.get(999)
        with self.assertRaises(SnapshotNotFoundError):
            self.store.activate(999, None)
        with self.assertRaises(SnapshotNotFoundError):
            self.store.restore(999, None, "owner")
        with self.assertRaises(ValueError):
            self.store.get(True)
        with self.assertRaises(ValueError):
            self.store.history(-1)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM policy_snapshots")[0][0], 0)
        self.assertEqual(self.store.active_revision_counter, 0)

    def test_missing_active_row_is_not_silently_reinitialized(self):
        self.initial()
        self.sql("DELETE FROM policy_active")
        with self.assertRaises(CorruptSnapshotError):
            CategoryStore(self.path)

    def test_newer_schema_is_rejected_without_reset(self):
        original = self.initial()
        self.sql("PRAGMA user_version=99")
        with self.assertRaises(sqlite3.DatabaseError):
            CategoryStore(self.path)
        self.assertEqual(self.sql("PRAGMA user_version")[0][0], 99)
        self.assertEqual(self.store.get(original.revision).body, original.body)


if __name__ == "__main__":
    unittest.main()


class RestoreReasonTests(unittest.TestCase):
    def test_restore_records_the_given_reason_and_defaults_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CategoryStore(Path(tmp) / "categories.db")
            first = store.create_candidate({"v": 1}, None, "owner", "first")
            store.activate(first.revision, None)
            second = store.create_candidate({"v": 2}, first.revision, "owner", "second")
            store.activate(second.revision, first.revision)
            named = store.restore(first.revision, second.revision, "owner", "undo second")
            self.assertEqual(named.reason, "undo second")
            default = store.restore(first.revision, second.revision, "owner")
            self.assertEqual(default.reason, f"restore revision {first.revision}")
            actions = [row["action"] for row in store.history()]
            self.assertEqual(actions.count("restore"), 2)

