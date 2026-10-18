"""Remove missing items: drop Library and job entries whose files are gone.

The files vanished before this was a feature (see docs/ops/DATA_RETENTION.md),
so the Library lists items it can never show again. One confirmed action clears
them. It touches only the caller's own entries, never a file that exists and
never a receipt.
"""
from __future__ import annotations

import unittest

from gateway import admin_store, media_jobs, prompt_memory, receipt_store
from tests import test_me_media as base

REMOVE = "/me/api/media/remove-missing"
VALID = {"x-azizam-user-action": "1", "x-azizam-csrf": "valid-csrf"}


def asset(letter: str) -> str:
    return "img_" + letter * 64


class RemoveMissingTests(unittest.TestCase):
    setUp, tearDown = base.MeMediaRouterTests.setUp, base.MeMediaRouterTests.tearDown
    _session, _mutation_validator = base.MeMediaRouterTests._session, base.MeMediaRouterTests._mutation_validator
    _asset_path, _meta_path = base.MeMediaRouterTests._asset_path, base.MeMediaRouterTests._meta_path
    _asset_metadata, _catalog = base.MeMediaRouterTests._asset_metadata, base.MeMediaRouterTests._catalog
    _delete_unshared, _write_asset = base.MeMediaRouterTests._delete_unshared, base.MeMediaRouterTests._write_asset
    _receipt, _job = base.MeMediaRouterTests._receipt, base.MeMediaRouterTests._job

    def seed(self) -> None:
        """Alice: A gone alone, B stored, C gone beside D stored. Bob: E gone."""
        for letter in "BD":
            self._write_asset(asset(letter), ["sha256:alice"], prompt=f"prompt {letter}")
        for letter in "ACE":
            owner = self.alice if letter != "E" else self.bob
            prompt_memory.put(asset(letter), owner["id"], f"prompt {letter}", kept_until=None, path=self.prompt_db)
        self._receipt(self.app_a["id"], "req_a")
        self._receipt(self.app_a["id"], "req_b")
        self._receipt(self.app_a["id"], "req_cd")
        self._receipt(self.app_b["id"], "req_e")
        self._job("req_a", asset("A"))
        self._job("req_b", asset("B"))
        self._job("req_e", asset("E"))
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_cd", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": asset("C"), "media_type": "image/png"},
                    {"asset_id": asset("D"), "media_type": "image/png"}]), path=self.jobs_db)

    def library(self) -> dict[str, bool]:
        rows = self.client.get("/me/api/media").json()["assets"]
        return {row["asset_id"]: row["available"] for row in rows}

    def job_ids(self, request_id: str) -> list[str]:
        return [job["job_id"] for job in media_jobs.recent(500, path=self.jobs_db) if job["request_id"] == request_id]

    def test_library_lists_the_gone_items_before_the_removal(self) -> None:
        self.seed()
        self.assertEqual(self.library(), {asset("A"): False, asset("B"): True, asset("C"): False, asset("D"): True})

    def test_removes_only_the_callers_missing_entries_and_keeps_files_receipts_and_other_people(self) -> None:
        self.seed()
        response = self.client.post(REMOVE, headers=VALID)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 2, "removed_jobs": 1})
        # The Library shows only what is stored.
        self.assertEqual(self.library(), {asset("B"): True, asset("D"): True})
        # A job entry goes only when every file it made is gone.
        self.assertEqual(self.job_ids("req_a"), [])
        self.assertEqual(len(self.job_ids("req_b")), 1)
        self.assertEqual(len(self.job_ids("req_cd")), 1)
        # Stored files and every receipt are untouched.
        self.assertTrue(self._asset_path(asset("B")).is_file() and self._asset_path(asset("D")).is_file())
        for request_id in ("req_a", "req_b", "req_cd", "req_e"):
            self.assertIsNotNone(receipt_store.get(request_id, path=self.receipts_db), request_id)
        # The prompts of the removed items go with them; the others stay.
        kept = prompt_memory.get_person_prompts(self.alice["id"], path=self.prompt_db)
        self.assertEqual(set(kept), {asset("B"), asset("D")})
        # Bob's gone item is still Bob's.
        self.assertEqual(len(self.job_ids("req_e")), 1)
        self.current_user = self.bob
        self.assertEqual(self.library(), {asset("E"): False})
        audit = admin_store.list_audit(actor_id=self.alice["id"], action_prefix="media.asset.delete", limit=50, path=self.admin_db)
        self.assertEqual({row["target_id"] for row in audit}, {asset("A"), asset("C")})

    def test_a_second_removal_has_nothing_left_to_remove(self) -> None:
        self.seed()
        self.client.post(REMOVE, headers=VALID)
        again = self.client.post(REMOVE, headers=VALID)
        self.assertEqual(again.json(), {"removed_assets": 0, "removed_jobs": 0})
        self.assertEqual(self.library(), {asset("B"): True, asset("D"): True})

    def test_the_owner_removes_only_their_own_entries(self) -> None:
        self.seed()
        self.current_user = self.owner
        self.assertEqual(self.client.post(REMOVE, headers=VALID).json(), {"removed_assets": 0, "removed_jobs": 0})
        self.assertEqual(len(self.job_ids("req_a")), 1)

    def test_it_needs_the_user_action_and_csrf_headers(self) -> None:
        self.seed()
        self.assertEqual(self.client.post(REMOVE).status_code, 403)
        self.assertEqual(self.client.post(REMOVE, headers={**VALID, "x-azizam-csrf": "bad"}).status_code, 403)
        self.assertEqual(self.library()[asset("A")], False)
        self.assertEqual(len(self.job_ids("req_a")), 1)
