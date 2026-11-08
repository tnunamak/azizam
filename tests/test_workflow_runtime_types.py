"""The S4-S8 interface file and its fakes load and behave as the design says (docs/design/CODE_TO_DATA_S4_S8.md)."""

import asyncio
import unittest

from gateway.workflow_runtime_types import Actor, RegistryConflict, RunRequest, WORKFLOW_EXISTS
from tests.workflow_runtime_fakes import FakeRunCore, FakeStore, entry

DOC = {"schema_version": 2, "id": "poster.generate.demo", "version": "1", "intent": "poster.generate",
       "capability": "image", "inputs": {}, "outputs": {"image": {"type": "asset"}}, "bindings": {"fake": {}}}
ME = Actor("owner", "o1")


class FakeStoreFollowsTheStoreRules(unittest.TestCase):
    def test_compare_and_swap_refuses_a_stale_revision_and_create_refuses_an_existing_id(self):
        store = FakeStore({})
        first = store.put_workflow(DOC, if_match=None, actor=ME)
        self.assertEqual(first.revision, 2)
        with self.assertRaises(RegistryConflict) as exists:
            store.put_workflow(DOC, if_match=None, actor=ME)
        self.assertEqual(exists.exception.code, WORKFLOW_EXISTS)
        with self.assertRaises(RegistryConflict) as stale:
            store.set_state(DOC["id"], "disabled", if_match=1, actor=ME)
        self.assertEqual(stale.exception.current_revision, 2)

    def test_a_save_is_unpublished_and_a_disabled_entry_is_not_runnable(self):
        store = FakeStore({})
        store.put_workflow(DOC, if_match=None, actor=ME)
        self.assertFalse(store.snapshot().get(DOC["id"]).published)
        store.set_state(DOC["id"], "disabled", if_match=2, actor=ME)
        self.assertIsNone(store.snapshot().runnable(DOC["id"]))

    def test_restore_puts_an_old_version_back_as_a_new_revision(self):
        store = FakeStore({})
        store.put_workflow(DOC, if_match=None, actor=ME)
        old = store.snapshot().get(DOC["id"]).contract_digest
        store.put_workflow({**DOC, "version": "2"}, if_match=2, actor=ME)
        commit = store.restore(DOC["id"], old, if_match=3, actor=ME)
        self.assertEqual((commit.revision, commit.contract_digest), (4, old))

    def test_the_snapshot_digest_changes_with_any_entry_and_listeners_see_both_snapshots(self):
        store = FakeStore({DOC["id"]: entry(DOC)})
        seen = []
        store.subscribe(lambda old, new: seen.append((old.digest, new.digest)))
        store.set_published(DOC["id"], False, if_match=1, actor=ME)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(*seen[0])

    def test_the_fake_run_core_accepts_and_finishes(self):
        store = FakeStore({DOC["id"]: entry(DOC)})
        core = FakeRunCore()
        accepted = asyncio.run(core.submit_run(RunRequest(DOC["id"], {}), None, snapshot=store.snapshot()))
        asyncio.run(core.execute_run(accepted.job_id))
        self.assertEqual((accepted.status, core.status[accepted.job_id]), ("queued", "succeeded"))


if __name__ == "__main__":
    unittest.main()
