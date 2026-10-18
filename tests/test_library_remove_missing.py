"""Remove missing items: drop Library and job entries whose files are gone.

The files vanished before this was a feature (see docs/ops/DATA_RETENTION.md),
so the Library lists items it can never show again. One confirmed action clears
them. It touches only the caller's own entries, never a file that exists and
never a receipt.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, media_jobs, me_media, prompt_memory, receipt_store
from tests import test_me_media as base

REMOVE = "/me/api/media/remove-missing"
VALID = {"x-azizam-user-action": "1", "x-azizam-csrf": "valid-csrf"}


def asset(letter: str) -> str:
    return "img_" + letter * 64


class RemoveMissingTests(unittest.TestCase):
    def setUp(self) -> None:
        base.MeMediaRouterTests.setUp(self)
        # The route reads the media folder through this provider; without it, it refuses.
        app = FastAPI()
        app.include_router(me_media.create_me_media_router(
            session_provider=self._session,
            mutation_validator=self._mutation_validator,
            signed_url_builder=lambda request, asset_id, mode: f"http://testserver/v1/assets/{asset_id}/content?md={mode}&sig=test",
            asset_path_resolver=self._asset_path,
            asset_media_type_resolver=lambda asset_id, path: "image/png",
            asset_deleter=self._delete_unshared,
            asset_catalog_provider=self._catalog,
            asset_metadata_reader=self._asset_metadata,
            asset_storage_provider=me_media.create_storage_snapshot_provider(self.cache),
            admin_db_path=self.admin_db,
            media_jobs_path=self.jobs_db,
            receipts_path=self.receipts_db,
            prompt_memory_path=self.prompt_db,
        ))
        self.client = TestClient(app, raise_server_exceptions=False)

    tearDown = base.MeMediaRouterTests.tearDown
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
        summary = admin_store.list_audit(actor_id=self.alice["id"], action_prefix="media.library.remove_missing",
                                         limit=5, path=self.admin_db)
        self.assertEqual([row["metadata"] for row in summary], [{"removed_assets": 2, "removed_jobs": 1}])

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

    def test_only_succeeded_jobs_are_deleted_with_their_events(self) -> None:
        done = media_jobs.create(media_jobs.MediaJob(job_id="done", intent="image.generate",
                                                     status=media_jobs.STATUS_SUCCEEDED), path=self.jobs_db)
        running = media_jobs.create(media_jobs.MediaJob(job_id="running", intent="image.generate",
                                                        status=media_jobs.STATUS_RUNNING), path=self.jobs_db)
        media_jobs.append_event(done.job_id, "queued", {"progress": 0}, path=self.jobs_db)
        deleted = media_jobs.delete_succeeded(["done", "running", "unknown", ""], path=self.jobs_db)
        self.assertEqual(deleted, 1)
        self.assertIsNone(media_jobs.get("done", path=self.jobs_db))
        self.assertIsNotNone(media_jobs.get(running.job_id, path=self.jobs_db))
        self.assertEqual(media_jobs.delete_succeeded([], path=self.jobs_db), 0)


    # --- the media folder must be seen before anything is called gone -------------------

    def assert_nothing_changed(self) -> None:
        self.assertEqual(len(self.job_ids("req_a")), 1)
        self.assertEqual(len(self.job_ids("req_cd")), 1)
        self.assertEqual(len(self.job_ids("req_e")), 1)
        kept = prompt_memory.get_person_prompts(self.alice["id"], path=self.prompt_db)
        self.assertEqual(set(kept), {asset(letter) for letter in "ABCD"})
        self.assertEqual(admin_store.list_audit(action_prefix="media.", limit=50, path=self.admin_db), [])

    def refused(self) -> None:
        response = self.client.post(REMOVE, headers=VALID)
        self.assertEqual(response.status_code, 503, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "media_folder_unavailable")
        self.assertEqual(error["message"], "Azizam can't see its media folder right now, so nothing was removed. Try again when the folder is back.")
        self.assert_nothing_changed()

    def test_a_missing_media_folder_removes_nothing(self) -> None:
        self.seed()
        shutil.rmtree(self.cache)
        self.refused()

    def test_a_media_folder_that_is_not_a_directory_removes_nothing(self) -> None:
        self.seed()
        shutil.rmtree(self.cache)
        self.cache.write_text("an unmounted volume left a file here")
        self.refused()

    def test_an_unreadable_media_folder_removes_nothing(self) -> None:
        self.seed()
        with patch("gateway.me_media.os.listdir", side_effect=PermissionError("denied")):
            self.refused()

    def test_an_empty_media_folder_is_healthy_and_every_entry_is_gone(self) -> None:
        # The folder exists and is readable. It holds nothing, so nothing is stored.
        self.seed()
        for letter in "BD":
            self._asset_path(asset(letter)).unlink()
            self._meta_path(asset(letter)).unlink()
        self.assertEqual(os.listdir(self.cache), [])
        response = self.client.post(REMOVE, headers=VALID)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 4, "removed_jobs": 3})
        self.assertEqual(self.library(), {})
        self.assertEqual(len(self.job_ids("req_e")), 1, "Bob's entries are not the caller's")

    def test_an_empty_media_folder_and_an_empty_library_has_nothing_to_remove(self) -> None:
        self.assertEqual(os.listdir(self.cache), [])
        response = self.client.post(REMOVE, headers=VALID)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 0, "removed_jobs": 0})

    def test_a_folder_with_only_sidecars_and_scratch_files_removes_nothing(self) -> None:
        self.seed()
        for letter in "BD":
            self._asset_path(asset(letter)).unlink()
        (self.cache / ("f" * 64 + ".png.tmp")).write_bytes(b"half a write")
        self.refused()

    # --- missing is decided by what is on disk, not by a guessed suffix ----------------

    def test_a_gif_or_webp_output_of_a_video_job_is_not_missing(self) -> None:
        """ComfyUI `gifs` and `images` outputs keep their own suffix, whatever the asset kind."""
        self.seed()
        gif, webp = "vid_" + "1" * 64, "aud_" + "2" * 64
        (self.cache / ("1" * 64 + ".gif")).write_bytes(b"GIF89a")
        (self.cache / ("2" * 64 + ".webp")).write_bytes(b"RIFF....WEBP")
        for index, (asset_id, media_type) in enumerate(((gif, "image/gif"), (webp, "image/webp"))):
            self._receipt(self.app_a["id"], f"req_odd{index}")
            media_jobs.create(media_jobs.MediaJob(
                request_id=f"req_odd{index}", intent="video.generate", status=media_jobs.STATUS_SUCCEEDED,
                progress=100, assets=[{"asset_id": asset_id, "media_type": media_type,
                                       "filename": f"out.{media_type.split('/')[1]}"}]), path=self.jobs_db)
        response = self.client.post(REMOVE, headers=VALID)
        self.assertEqual(response.json(), {"removed_assets": 2, "removed_jobs": 1})
        self.assertEqual(len(self.job_ids("req_odd0")), 1)
        self.assertEqual(len(self.job_ids("req_odd1")), 1)
        self.assertTrue((self.cache / ("1" * 64 + ".gif")).is_file())

    def test_a_file_stored_under_any_suffix_counts_as_stored(self) -> None:
        self.seed()
        (self.cache / ("A" * 64 + ".bin")).write_bytes(b"unknown suffix")
        self.assertEqual(self.client.post(REMOVE, headers=VALID).json(), {"removed_assets": 1, "removed_jobs": 0})
        self.assertEqual(len(self.job_ids("req_a")), 1)

    # --- a job goes only when every file it names is gone -------------------------------

    def test_a_job_with_a_hidden_but_present_file_is_kept(self) -> None:
        """The person deleted P from their Library while another owner still holds the file."""
        self._write_asset(asset("P"), ["sha256:alice", "sha256:bob"], prompt="shared")
        prompt_memory.put(asset("G"), self.alice["id"], "gone", kept_until=None, path=self.prompt_db)
        self._receipt(self.app_a["id"], "req_pg")
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_pg", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": asset("P"), "media_type": "image/png"},
                    {"asset_id": asset("G"), "media_type": "image/png"}]), path=self.jobs_db)
        deleted = self.client.delete(f"/me/api/media/{asset('P')}", headers=VALID)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.library(), {asset("G"): False})
        self.assertEqual(self.client.post(REMOVE, headers=VALID).json(), {"removed_assets": 1, "removed_jobs": 0})
        self.assertEqual(len(self.job_ids("req_pg")), 1, "the file P is still on disk")
        self.assertEqual(self.library(), {})

    def test_an_output_named_by_two_jobs_follows_its_file(self) -> None:
        self._write_asset(asset("P"), ["sha256:alice"], prompt="present")
        for name, assets in (("one", ["G"]), ("two", ["G"]), ("pone", ["P"]), ("ptwo", ["P", "H"])):
            self._receipt(self.app_a["id"], f"req_{name}")
            media_jobs.create(media_jobs.MediaJob(
                request_id=f"req_{name}", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100,
                assets=[{"asset_id": asset(letter), "media_type": "image/png"} for letter in assets]), path=self.jobs_db)
        response = self.client.post(REMOVE, headers=VALID)
        # G is gone in both jobs that name it: both go. P is stored, so both jobs naming it stay.
        self.assertEqual(response.json(), {"removed_assets": 2, "removed_jobs": 2})
        self.assertEqual((self.job_ids("req_one"), self.job_ids("req_two")), ([], []))
        self.assertEqual(len(self.job_ids("req_pone")), 1)
        self.assertEqual(len(self.job_ids("req_ptwo")), 1)
        self.assertEqual(self.library(), {asset("P"): True})

    def test_identical_output_made_again_is_listed_again(self) -> None:
        """A hide belongs to an asset in one job, not to the content hash for ever."""
        self._receipt(self.app_a["id"], "req_one")
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_one", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": asset("G"), "media_type": "image/png"},
                    {"asset_id": asset("S"), "media_type": "image/png"}]), path=self.jobs_db)
        self._write_asset(asset("S"), ["sha256:alice"])
        self.assertEqual(self.client.post(REMOVE, headers=VALID).json(), {"removed_assets": 1, "removed_jobs": 0})
        self.assertEqual(self.library(), {asset("S"): True})
        # The same bytes are generated again: the file is back and a new job names it.
        self._write_asset(asset("G"), ["sha256:alice"], prompt="again")
        self._receipt(self.app_a["id"], "req_two")
        self._job("req_two", asset("G"))
        self.assertEqual(self.library(), {asset("S"): True, asset("G"): True})

    # --- one transaction -----------------------------------------------------------------

    def test_a_failure_part_way_changes_nothing(self) -> None:
        self.seed()
        real = media_jobs.delete_succeeded_in

        def fail_after_deleting(conn, job_ids):
            real(conn, job_ids)
            raise sqlite3.OperationalError("disk I/O error")

        with patch("gateway.media_jobs.delete_succeeded_in", fail_after_deleting):
            response = self.client.post(REMOVE, headers=VALID)
        self.assertGreaterEqual(response.status_code, 500)
        self.assert_nothing_changed()
        self.assertEqual(self.library(), {asset("A"): False, asset("B"): True, asset("C"): False, asset("D"): True})
        with sqlite3.connect(self.jobs_db) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            hidden = conn.execute("SELECT COUNT(*) FROM media_hidden_assets").fetchone()[0] if "media_hidden_assets" in tables else 0
        self.assertEqual(hidden, 0)
        # A retry after the fault completes the removal.
        self.assertEqual(self.client.post(REMOVE, headers=VALID).json(), {"removed_assets": 2, "removed_jobs": 1})
