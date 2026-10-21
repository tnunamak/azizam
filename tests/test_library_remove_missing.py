"""Remove missing items: drop Library and job entries whose files are gone.

The files vanished before this was a feature (see docs/ops/DATA_RETENTION.md),
so the Library lists items it can never show again. One confirmed action clears
them. It touches only the caller's own entries, never a file that exists and
never a receipt.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, media_jobs, me_media, prompt_memory, receipt_store
from gateway.public_failure import PublicFailure
from tests import test_me_media as base

REMOVE = "/me/api/media/remove-missing"
CONFIRM_FOLDER = "/me/api/media/confirm-folder"
CHANGED = "Your Library changed since you opened this. Review it again."
VALID = {"x-azizam-user-action": "1", "x-azizam-csrf": "valid-csrf"}
UNSEEN = "Azizam can't see its media folder right now, so nothing was removed. Try again when the folder is back."
EMPTY = "Azizam's media folder is empty, which usually means it isn't mounted, so nothing was removed. Check the disk, then try again."


def asset(letter: str) -> str:
    return "img_" + letter * 64


class RemoveMissingTests(unittest.TestCase):
    def setUp(self) -> None:
        base.MeMediaRouterTests.setUp(self)
        self.client = self.make_client(self._asset_path)
        # Existing fixtures represent a known, mounted volume.
        prompt_memory.confirm_media_folder(self.cache, owner=True, path=self.jobs_db)

    def make_client(self, asset_path_resolver, storage_provider=None, session_provider=None) -> TestClient:
        # The route reads the media folder through this provider; without it, it refuses.
        app = FastAPI()
        app.include_router(me_media.create_me_media_router(
            session_provider=session_provider or self._session,
            mutation_validator=self._mutation_validator,
            signed_url_builder=lambda request, asset_id, mode: f"http://testserver/v1/assets/{asset_id}/content?md={mode}&sig=test",
            asset_path_resolver=asset_path_resolver,
            asset_media_type_resolver=lambda asset_id, path: "image/png",
            asset_deleter=self._delete_unshared,
            asset_catalog_provider=self._catalog,
            asset_metadata_reader=self._asset_metadata,
            asset_storage_provider=storage_provider or me_media.create_storage_snapshot_provider(self.cache),
            admin_db_path=self.admin_db,
            media_jobs_path=self.jobs_db,
            receipts_path=self.receipts_db,
            prompt_memory_path=self.prompt_db,
        ))
        return TestClient(app, raise_server_exceptions=False)

    tearDown = base.MeMediaRouterTests.tearDown
    _session, _mutation_validator = base.MeMediaRouterTests._session, base.MeMediaRouterTests._mutation_validator
    _asset_path, _meta_path = base.MeMediaRouterTests._asset_path, base.MeMediaRouterTests._meta_path
    _asset_metadata, _catalog = base.MeMediaRouterTests._asset_metadata, base.MeMediaRouterTests._catalog
    _delete_unshared, _write_asset = base.MeMediaRouterTests._delete_unshared, base.MeMediaRouterTests._write_asset
    _receipt, _job = base.MeMediaRouterTests._receipt, base.MeMediaRouterTests._job

    def remove_missing(self):
        storage = me_media.create_storage_snapshot_provider(self.cache)() or me_media.StorageSnapshot(frozenset())
        plan = me_media._viewer_removal(
            self.current_user["id"], storage, admin_path=self.admin_db, jobs_path=self.jobs_db,
            receipts_path=self.receipts_db,
        )
        return self.client.post(REMOVE, headers=VALID, json={"expected_missing": plan.missing_count()})

    def confirm_folder(self):
        storage = me_media.create_storage_snapshot_provider(self.cache)() or me_media.StorageSnapshot(frozenset())
        here, total, _ = me_media._folder_counts(
            storage, admin_path=self.admin_db, jobs_path=self.jobs_db, receipts_path=self.receipts_db,
        )
        return self.client.post(CONFIRM_FOLDER, headers=VALID, json={"items_here": here, "library_items": total})

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

    def empty_the_folder(self) -> None:
        """Every stored file is gone: the folder is readable and holds no media."""
        for letter in "BD":
            self._asset_path(asset(letter)).unlink()
            self._meta_path(asset(letter)).unlink()
        self.assertEqual(os.listdir(self.cache), [".azizam-media"])

    def library(self) -> dict[str, bool]:
        rows = self.client.get("/me/api/media").json()["assets"]
        return {row["asset_id"]: row["available"] for row in rows}

    def job_ids(self, request_id: str) -> list[str]:
        return [job["job_id"] for job in media_jobs.recent(500, path=self.jobs_db) if job["request_id"] == request_id]

    def test_viewer_plan_ignores_filters_and_equals_removal(self) -> None:
        self.seed()
        before_jobs = media_jobs.recent(500, path=self.jobs_db)
        before_marker = (self.cache / ".azizam-media").read_bytes()
        with patch("gateway.me_media._viewer_removal", wraps=me_media._viewer_removal) as selection:
            page = self.client.get("/me/api/media?folder_state=1&removal_plan=1&q=prompt+A&limit=1").json()
            self.assertEqual(page["removal_plan"], {"missing_items": 2})
            self.assertNotIn("assets", page)
            self.assertEqual(media_jobs.recent(500, path=self.jobs_db), before_jobs)
            self.assertEqual((self.cache / ".azizam-media").read_bytes(), before_marker)
            removed = self.client.post(REMOVE, headers=VALID, json={"expected_missing": page["removal_plan"]["missing_items"]})
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertEqual(removed.json()["removed_assets"], page["removal_plan"]["missing_items"])
            self.assertEqual(selection.call_count, 2, "plan and action use the same viewer selection")
        self.assertEqual(self.client.get("/me/api/media?folder_state=1&removal_plan=1").json()["removal_plan"], {"missing_items": 0})

    def test_removal_reselects_when_a_file_disappears_after_the_plan(self) -> None:
        self.seed()
        page = self.client.get("/me/api/media?folder_state=1&removal_plan=1").json()
        self.assertEqual(page["removal_plan"], {"missing_items": 2})
        self._asset_path(asset("B")).unlink()
        response = self.client.post(REMOVE, headers=VALID, json={"expected_missing": page["removal_plan"]["missing_items"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["message"], CHANGED)
        self.assert_nothing_changed()
        self.assertEqual(self.library(), {asset("A"): False, asset("B"): False, asset("C"): False, asset("D"): True})

    def test_removal_allows_a_smaller_fresh_selection(self) -> None:
        self.seed()
        page = self.client.get("/me/api/media?folder_state=1&removal_plan=1").json()
        self._write_asset(asset("A"), ["sha256:alice"])
        response = self.client.post(REMOVE, headers=VALID, json={"expected_missing": page["removal_plan"]["missing_items"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 1, "removed_jobs": 0})
        self.assertTrue(self._asset_path(asset("A")).exists())

    def test_shared_missing_asset_counts_hidden_jobs_independently_of_job_order(self) -> None:
        self._write_asset(asset("B"), ["sha256:alice"])
        for request_id in ("visible", "hidden"):
            self._receipt(self.app_a["id"], request_id)
            self._job(request_id, asset("A"))
        hidden_job = self.job_ids("hidden")[0]
        with sqlite3.connect(self.jobs_db) as conn:
            media_jobs.hide_assets_in(conn, self.alice["id"], [(asset("A"), hidden_job)])
        scoped_jobs = me_media._scoped_jobs
        selections = []
        for hidden_first in (False, True):
            with self.subTest(hidden_first=hidden_first):
                def ordered_jobs(*args, **kwargs):
                    return sorted(scoped_jobs(*args, **kwargs),
                                  key=lambda row: (row[0]["job_id"] == hidden_job) != hidden_first)
                with patch.object(me_media, "_scoped_jobs", side_effect=ordered_jobs):
                    page = self.client.get("/me/api/media?folder_state=1&removal_plan=1").json()
                    self.assertEqual(page["removal_plan"], {"missing_items": 2})
                    response = self.client.post(REMOVE, headers=VALID, json={"expected_missing": 1})
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json()["error"]["message"], CHANGED)
                    storage = me_media.create_storage_snapshot_provider(self.cache)()
                    plan = me_media._viewer_removal(
                        self.alice["id"], storage, admin_path=self.admin_db,
                        jobs_path=self.jobs_db, receipts_path=self.receipts_db,
                    )
                    selections.append((set(plan.job_ids), plan.missing_count()))
        self.assertEqual(selections[0], selections[1])
        self.assertEqual(selections[0][0], {hidden_job, self.job_ids("visible")[0]})
        response = self.client.post(REMOVE, headers=VALID, json={"expected_missing": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 1, "removed_jobs": 2})
        self.assertEqual(self.job_ids("visible") + self.job_ids("hidden"), [])

    def test_a_removal_that_would_delete_only_a_job_the_caller_was_not_shown_is_refused(self) -> None:
        """An already hidden asset leaves no Library item, but its job entry still goes: the plan says so."""
        self._write_asset(asset("B"), ["sha256:alice"], prompt="prompt B")
        prompt_memory.put(asset("A"), self.alice["id"], "prompt A", kept_until=None, path=self.prompt_db)
        self._receipt(self.app_a["id"], "req_a")
        self._job("req_a", asset("A"))
        with sqlite3.connect(self.jobs_db) as conn:
            media_jobs.hide_assets_in(conn, self.alice["id"], [(asset("A"), self.job_ids("req_a")[0])])
        response = self.client.post(REMOVE, headers=VALID, json={"expected_missing": 0})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["message"], CHANGED)
        self.assertEqual(len(self.job_ids("req_a")), 1)
        self.assertIn(asset("A"), set(prompt_memory.get_person_prompts(self.alice["id"], path=self.prompt_db)))
        self.assertEqual(
            self.client.get("/me/api/media?folder_state=1&removal_plan=1").json()["removal_plan"],
            {"missing_items": 1},
            "the job entry the removal deletes is part of what the caller sees",
        )
        removed = self.client.post(REMOVE, headers=VALID, json={"expected_missing": 1})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json(), {"removed_assets": 0, "removed_jobs": 1})
        self.assertEqual(self.job_ids("req_a"), [])

    def test_an_id_of_64_characters_that_is_not_hex_is_never_removed(self) -> None:
        """Only 64 hex characters are a digest, so a 64-character id that is not hex cannot be called gone."""
        self.seed()
        odd = asset("z")
        self.assertEqual(len(odd.split("_", 1)[1]), 64)
        prompt_memory.put(odd, self.alice["id"], "prompt odd", kept_until=None, path=self.prompt_db)
        self._receipt(self.app_a["id"], "req_odd")
        self._job("req_odd", odd)
        self.assertEqual(
            self.client.get("/me/api/media?folder_state=1&removal_plan=1").json()["removal_plan"],
            {"missing_items": 2},
            "an id that cannot be checked is neither counted nor removed",
        )
        response = self.remove_missing()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 2, "removed_jobs": 1})
        self.assertEqual(len(self.job_ids("req_odd")), 1)
        self.assertIn(odd, {a["asset_id"] for job in media_jobs.recent(500, path=self.jobs_db) for a in job["assets"]})
        self.assertIn(odd, set(prompt_memory.get_person_prompts(self.alice["id"], path=self.prompt_db)))

    def test_confirm_refuses_folder_swapped_after_the_read(self) -> None:
        self.seed()
        self.unconfirm_folder()
        self.current_user = self.owner
        evidence = self.client.get("/me/api/media?folder_state=1").json()["media_folder"]
        original = self.cache.with_name("original-media")
        self.cache.rename(original)
        self.cache.mkdir()
        self._write_asset(asset("B"), ["sha256:alice"])
        response = self.client.post(CONFIRM_FOLDER, headers=VALID, json={
            "items_here": evidence["items_here"], "library_items": evidence["library_items"],
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["message"], CHANGED)
        self.assert_not_recorded()
        self.assertEqual(admin_store.list_audit(action_prefix="media.folder.", limit=10, path=self.admin_db), [])

    def test_listing_never_reads_folder_evidence_or_removal_plan(self) -> None:
        self.seed()
        self.unconfirm_folder()
        self.current_user = self.owner
        with patch("gateway.me_media._folder_counts", side_effect=AssertionError("folder counts")), \
             patch("gateway.me_media._viewer_removal", side_effect=AssertionError("plan")):
            for query in ("", "?removal_plan=1", "?kind=image&q=prompt"):
                response = self.client.get("/me/api/media" + query)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("media_folder", response.json())
                self.assertNotIn("removal_plan", response.json())

    def test_confirm_refuses_changed_evidence_in_the_same_folder(self) -> None:
        for change in ("added", "removed"):
            with self.subTest(change=change):
                self.seed()
                self.unconfirm_folder()
                self.current_user = self.owner
                evidence = self.client.get("/me/api/media?folder_state=1").json()["media_folder"]
                if change == "added":
                    self._write_asset(asset("A"), ["sha256:alice"])
                else:
                    self._asset_path(asset("B")).unlink()
                response = self.client.post(CONFIRM_FOLDER, headers=VALID, json={
                    "items_here": evidence["items_here"], "library_items": evidence["library_items"],
                })
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["message"], CHANGED)
                self.assert_not_recorded()
                self.assertEqual(admin_store.list_audit(action_prefix="media.folder.", limit=10, path=self.admin_db), [])
                self._asset_path(asset("A")).unlink(missing_ok=True)

    def test_lightweight_folder_read_skips_listing_and_plan(self) -> None:
        self.seed()
        with patch("gateway.me_media._list_media", side_effect=AssertionError("listing")), \
             patch("gateway.me_media._viewer_removal", side_effect=AssertionError("plan")):
            response = self.client.get("/me/api/media?folder_state=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"media_folder": {"state": "confirmed"}})
        with patch("gateway.me_media._viewer_removal", side_effect=AssertionError("plan")):
            self.assertNotIn("removal_plan", self.client.get("/me/api/media").json())
        signed_out = self.make_client(self._asset_path, session_provider=lambda request: None)
        self.assertEqual(signed_out.get("/me/api/media?folder_state=1").status_code, 401)

    def test_owner_everyone_plan_only_counts_owners_items_and_cursor_has_no_plan(self) -> None:
        self.seed()
        owner_app = admin_store.create_app(
            "Owner", kind="personal", owner_user_id=self.owner["id"], access_plan="person", path=self.admin_db,
        )
        self._receipt(owner_app["id"], "owner_missing")
        self._job("owner_missing", asset("f"))
        self.current_user = self.owner
        plan = self.client.get("/me/api/media?folder_state=1&scope=everyone&removal_plan=1").json()
        self.assertEqual(plan["removal_plan"], {"missing_items": 1})
        page = self.client.get("/me/api/media?scope=everyone&limit=1").json()
        self.assertIsNotNone(page["next_cursor"])
        with patch("gateway.me_media._viewer_removal", side_effect=AssertionError("cursor plan")):
            response = self.client.get("/me/api/media?scope=everyone&removal_plan=1&cursor=" + page["next_cursor"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("removal_plan", response.json())

    def test_action_evidence_is_required_and_must_be_a_nonnegative_integer(self) -> None:
        self.seed()
        for value in (None, {}, {"expected_missing": True}, {"expected_missing": -1}, {"expected_missing": "2"}):
            self.assertEqual(self.client.post(REMOVE, headers=VALID, json=value).status_code, 400)
        self.current_user = self.owner
        for value in (None, {}, {"items_here": True, "library_items": 5}):
            self.assertEqual(self.client.post(CONFIRM_FOLDER, headers=VALID, json=value).status_code, 400)
        self.assert_nothing_changed()

    def test_owner_plan_includes_shared_asset_named_in_another_persons_deletion_audit(self) -> None:
        owner_app = admin_store.create_app(
            "Owner", kind="personal", owner_user_id=self.owner["id"], access_plan="person", path=self.admin_db,
        )
        shared = asset("a")
        self._write_asset(shared, ["sha256:alice", "sha256:owner"])
        self._write_asset(asset("b"), ["sha256:owner"])
        for app, request_id, output in ((self.app_a, "alice_shared", shared),
                                        (owner_app, "owner_shared", shared),
                                        (owner_app, "owner_kept", asset("b"))):
            self._receipt(app["id"], request_id)
            self._job(request_id, output)
        self.assertEqual(self.client.delete(f"/me/api/media/{shared}", headers=VALID).status_code, 200)
        self.assertTrue(self._asset_path(shared).exists())
        self.current_user = self.owner
        # Force the evidence path: the shared asset remains whole-Library evidence.
        (self.cache / ".azizam-media").unlink()
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("DELETE FROM media_volume_identity")
        present = self.client.get("/me/api/media?folder_state=1&removal_plan=1").json()
        self.assertEqual(present["media_folder"]["library_items"], 2)
        self.assertEqual(present["media_folder"]["items_here"], 2)
        self.assertEqual(present["removal_plan"], {"missing_items": 0})
        self._asset_path(shared).unlink()
        page = self.client.get("/me/api/media?folder_state=1&removal_plan=1").json()
        self.assertEqual(page["media_folder"]["library_items"], 2)
        self.assertEqual(page["media_folder"]["items_here"], 1)
        self.assertEqual(page["removal_plan"], {"missing_items": 1})
        self.assertEqual(self.confirm_folder().status_code, 200)
        self.assertEqual(self.remove_missing().json()["removed_assets"], 1)

    def test_an_unmounted_folder_with_a_new_file_removes_nothing(self) -> None:
        self.seed()
        # Simulate an absent volume followed by an ordinary write on the bare mount.
        shutil.rmtree(self.cache)
        self.cache.mkdir()
        self._write_asset(asset("N"), ["sha256:alice"])
        self.assertFalse((self.cache / ".azizam-media").exists())
        response = self.remove_missing()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "media_folder_unconfirmed")
        self.assert_nothing_changed(written=frozenset({asset("N")}))

    def test_a_different_volume_id_removes_nothing(self) -> None:
        self.seed()
        marker = self.cache / ".azizam-media"
        value = json.loads(marker.read_text())
        value["volume_id"] = "0" * 32
        marker.write_text(json.dumps(value))
        response = self.remove_missing()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "media_folder_unconfirmed")
        self.assertEqual(response.json()["error"]["details"]["layer"], "library")
        self.assert_nothing_changed()

    def test_an_existing_install_needs_owner_confirmation_before_removal(self) -> None:
        self.seed()
        (self.cache / ".azizam-media").unlink()
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("DELETE FROM media_volume_identity")
        self.assertEqual(self.remove_missing().status_code, 503)
        self.assertEqual(self.confirm_folder().status_code, 403)
        self.assertFalse((self.cache / ".azizam-media").exists())
        self.assert_nothing_changed()
        self.current_user = self.owner
        response = self.confirm_folder()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"confirmed": True})
        self.assert_nothing_changed()
        self.current_user = self.alice
        self.assertEqual(self.remove_missing().json(), {"removed_assets": 2, "removed_jobs": 1})

    def test_confirm_requires_owner_in_core_and_user_action_at_route(self) -> None:
        before = (self.cache / ".azizam-media").read_bytes()
        with self.assertRaises(PublicFailure) as raised:
            prompt_memory.confirm_media_folder(self.cache, owner=False, path=self.jobs_db)
        self.assertEqual(raised.exception.status_code, 403)
        self.current_user = self.owner
        self.assertEqual(self.client.post(CONFIRM_FOLDER).status_code, 403)
        self.assertEqual(self.client.post(CONFIRM_FOLDER, headers={**VALID, "x-azizam-csrf": "bad"}).status_code, 403)
        self.assertEqual((self.cache / ".azizam-media").read_bytes(), before)

    def test_a_person_with_no_entries_cannot_bootstrap_over_another_person(self) -> None:
        self.seed()
        (self.cache / ".azizam-media").unlink()
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("DELETE FROM media_volume_identity")
        self.current_user = self.owner
        # The owner has nothing missing, so there is nothing to remove and nothing to bootstrap.
        response = self.remove_missing()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 0, "removed_jobs": 0})
        self.assertFalse((self.cache / ".azizam-media").exists())
        self.assert_nothing_changed()

    def test_an_empty_index_does_not_create_a_marker_on_a_remove(self) -> None:
        (self.cache / ".azizam-media").unlink()
        response = self.remove_missing()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse((self.cache / ".azizam-media").exists())
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("DELETE FROM media_volume_identity")
        (self.cache / ("f" * 64 + ".png")).write_bytes(b"orphaned media")
        response = self.remove_missing()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse((self.cache / ".azizam-media").exists())

    def test_an_invalid_marker_is_refused_without_touching_entries(self) -> None:
        self.seed()
        marker = self.cache / ".azizam-media"
        for value in ('not json', '{}', '{"volume_id": 7, "created_at": 1}'):
            with self.subTest(value=value):
                marker.write_text(value)
                response = self.remove_missing()
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["error"]["code"], "media_folder_unconfirmed")
                self.assert_nothing_changed()

    def test_the_core_refuses_a_missing_marker_before_any_delete(self) -> None:
        self.seed()
        (self.cache / ".azizam-media").unlink()
        with self.assertRaises(PublicFailure) as raised:
            prompt_memory.remove_missing_media(
                self.alice["id"], [asset("A")], [(asset("C"), "req_cd")], self.job_ids("req_a"),
                path=self.jobs_db, media_folder=self.cache,
            )
        self.assertEqual(raised.exception.code, "media_folder_unconfirmed")
        self.assert_nothing_changed()

    def test_losing_the_marker_during_removal_rolls_back_every_change(self) -> None:
        self.seed()
        real = media_jobs.delete_succeeded_in

        def lose_marker(conn, job_ids):
            removed = real(conn, job_ids)
            (self.cache / ".azizam-media").unlink()
            return removed

        with patch("gateway.media_jobs.delete_succeeded_in", lose_marker):
            response = self.remove_missing()
        self.assertEqual(response.status_code, 503, response.text)
        self.assert_nothing_changed()
        with sqlite3.connect(self.jobs_db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_hidden_assets").fetchone()[0], 0)

    # Folder state, the one-time owner confirmation and first-read adoption.

    NO_MATCHES = (
        "None of the Library's items have their files in this folder, so Azizam can't confirm it. "
        "Check that the media disk is mounted, then try again."
    )

    def media_folder(self) -> dict:
        response = self.client.get("/me/api/media?folder_state=1")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["media_folder"]

    def unconfirm_folder(self) -> None:
        """An install that never confirmed: no marker on disk and no recorded identity."""
        (self.cache / ".azizam-media").unlink(missing_ok=True)
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("DELETE FROM media_volume_identity")

    def recorded_volume(self) -> str:
        with sqlite3.connect(self.jobs_db) as conn:
            return conn.execute("SELECT volume_id FROM media_volume_identity WHERE singleton=1").fetchone()[0]

    def refused_unconfirmed(self) -> None:
        response = self.remove_missing()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "media_folder_unconfirmed")
        self.assert_nothing_changed()

    def test_the_owner_sees_the_state_the_path_and_how_many_items_are_here(self) -> None:
        self.seed()
        self.unconfirm_folder()
        self.current_user = self.owner
        self.assertEqual(self.media_folder(), {
            "state": "unconfirmed", "path": str(self.cache), "library_items": 5, "items_here": 2,
            "uncheckable_items": 0,
        })

    def test_a_member_is_told_only_the_state(self) -> None:
        self.seed()
        self.unconfirm_folder()
        self.assertEqual(self.media_folder(), {"state": "unconfirmed"})

    def test_a_confirmed_folder_reports_confirmed_and_a_changed_volume_reports_mismatch(self) -> None:
        self.seed()
        self.assertEqual(self.media_folder(), {"state": "confirmed"})
        marker = self.cache / ".azizam-media"
        value = json.loads(marker.read_text())
        value["volume_id"] = "0" * 32
        marker.write_text(json.dumps(value))
        self.assertEqual(self.media_folder(), {"state": "mismatch"})
        self.refused_unconfirmed()

    def test_a_marker_without_a_recorded_identity_is_not_trusted(self) -> None:
        self.seed()
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("DELETE FROM media_volume_identity")
        self.assertEqual(self.media_folder(), {"state": "unconfirmed"})
        self.refused_unconfirmed()

    def test_confirm_refuses_an_empty_folder_while_the_library_has_entries(self) -> None:
        self.seed()
        self.unconfirm_folder()
        for letter in "BD":
            self._asset_path(asset(letter)).unlink()
            self._meta_path(asset(letter)).unlink()
        self.assertEqual(os.listdir(self.cache), [])
        self.current_user = self.owner
        response = self.confirm_folder()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "media_folder_no_matches")
        self.assertEqual(response.json()["error"]["message"], self.NO_MATCHES)
        self.assertFalse((self.cache / ".azizam-media").exists())
        self.assertEqual(admin_store.list_audit(action_prefix="media.folder.", limit=10, path=self.admin_db), [])

    def test_confirm_allows_a_folder_that_holds_part_of_the_library_and_audits_it(self) -> None:
        self.seed()
        self.unconfirm_folder()
        self.current_user = self.owner
        response = self.confirm_folder()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.media_folder(), {"state": "confirmed"})
        rows = admin_store.list_audit(action_prefix="media.folder.confirm", limit=10, path=self.admin_db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_id"], self.owner["id"])
        self.assertEqual(rows[0]["metadata"], {"library_items": 5, "items_here": 2})

    def test_confirm_allows_an_empty_folder_when_the_library_is_empty(self) -> None:
        self.unconfirm_folder()
        self.current_user = self.owner
        response = self.confirm_folder()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((self.cache / ".azizam-media").is_file())

    def test_an_id_that_is_not_a_digest_is_not_counted_as_here_or_in_the_library(self) -> None:
        """A legacy id cannot be checked against the folder, so it is neither here nor in the count."""
        self.seed()
        self.unconfirm_folder()
        self._receipt(self.app_a["id"], "req_legacy")
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_legacy", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": "img_legacy123", "media_type": "image/png"}]), path=self.jobs_db)
        self.current_user = self.owner
        self.assertEqual(self.media_folder(), {
            "state": "unconfirmed", "path": str(self.cache), "library_items": 5, "items_here": 2,
            "uncheckable_items": 1,
        })
        self.assertEqual(self.confirm_folder().status_code, 200)
        rows = admin_store.list_audit(action_prefix="media.folder.confirm", limit=10, path=self.admin_db)
        self.assertEqual(rows[0]["metadata"], {"library_items": 5, "items_here": 2})

    def test_a_library_whose_only_ids_are_not_digests_reads_as_unverifiable(self) -> None:
        self.unconfirm_folder()
        self.assertEqual(os.listdir(self.cache), [])
        self._receipt(self.app_a["id"], "req_legacy")
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_legacy", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": "img_legacy123", "media_type": "image/png"}]), path=self.jobs_db)
        self.current_user = self.owner
        self.assertEqual(self.media_folder(), {
            "state": "unconfirmed", "path": str(self.cache), "library_items": 0, "items_here": 0,
            "uncheckable_items": 1,
        })

    def test_a_member_is_refused_before_the_folder_is_looked_up(self) -> None:
        self.client = self.make_client(self._asset_path, storage_provider=lambda: None)
        self.assertEqual(self.confirm_folder().status_code, 403)

    def test_remove_with_nothing_missing_on_an_unconfirmed_folder_is_a_normal_success(self) -> None:
        self._write_asset(asset("B"), ["sha256:alice"], prompt="prompt B")
        self._receipt(self.app_a["id"], "req_b")
        self._job("req_b", asset("B"))
        self.unconfirm_folder()
        response = self.remove_missing()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 0, "removed_jobs": 0})
        self.assertFalse((self.cache / ".azizam-media").exists())

    def test_the_first_library_read_adopts_a_fresh_folder_and_ignores_its_non_media_files(self) -> None:
        self.unconfirm_folder()
        (self.cache / ".url_signing_secret").write_text("not a media file")
        (self.cache / ("a" * 64 + ".meta.json")).write_text(json.dumps({"asset_id": "img_" + "a" * 64}))
        (self.cache / ("a" * 64 + ".png.tmp")).write_bytes(b"half written")
        self.assertEqual(self.media_folder(), {"state": "confirmed"})
        self.assertTrue((self.cache / ".azizam-media").is_file())

    def test_a_folder_that_holds_media_is_not_adopted_on_read(self) -> None:
        self.unconfirm_folder()
        (self.cache / ("a" * 64 + ".png")).write_bytes(b"media")
        self.assertEqual(self.media_folder(), {"state": "unconfirmed"})
        self.assertFalse((self.cache / ".azizam-media").exists())

    def test_a_prompt_entry_or_a_job_blocks_adoption_even_when_no_file_is_here(self) -> None:
        self.unconfirm_folder()
        prompt_memory.put(asset("A"), self.alice["id"], "prompt A", kept_until=None, path=self.prompt_db)
        self.assertEqual(self.media_folder(), {"state": "unconfirmed"})
        self.assertFalse((self.cache / ".azizam-media").exists())
        with sqlite3.connect(self.prompt_db) as conn:
            conn.execute("DELETE FROM asset_prompts")
        self._receipt(self.app_a["id"], "req_a")
        self._job("req_a", asset("A"))
        self.assertEqual(self.media_folder(), {"state": "unconfirmed"})
        self.assertFalse((self.cache / ".azizam-media").exists())

    def test_a_recorded_identity_is_never_recreated_by_a_read(self) -> None:
        (self.cache / ".azizam-media").unlink()
        self.assertEqual(self.media_folder(), {"state": "mismatch"})
        self.assertFalse((self.cache / ".azizam-media").exists())

    # An unconfirmed folder is never trusted from what it holds: only the owner's explicit confirmation records it.

    def library_of(self, stored: int, total: int) -> list[str]:
        """Alice's Library of ``total`` checkable items; the first ``stored`` have their files in the folder."""
        ids = [f"img_{index:064x}" for index in range(1, total + 1)]
        for index, asset_id in enumerate(ids):
            if index < stored:
                self._write_asset(asset_id, ["sha256:alice"], prompt=f"prompt {index}")
            else:
                prompt_memory.put(asset_id, self.alice["id"], f"prompt {index}", kept_until=None, path=self.prompt_db)
            self._receipt(self.app_a["id"], f"req_{index}")
            self._job(f"req_{index}", asset_id)
        self.alice_jobs = media_jobs.recent(500, path=self.jobs_db)
        return ids

    def assert_not_recorded(self) -> None:
        self.assertFalse((self.cache / ".azizam-media").exists())
        with sqlite3.connect(self.jobs_db) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM media_volume_identity").fetchone())

    def test_a_partial_copy_of_the_library_is_never_adopted_so_removal_cannot_erase_the_rest(self) -> None:
        """A stale backup holding A-C of A-D: D still exists on the original disk, so its record must survive."""
        ids = self.library_of(stored=3, total=4)
        self.unconfirm_folder()
        for person in (self.alice, self.owner, self.alice):
            self.current_user = person
            self.assertEqual(self.media_folder()["state"], "unconfirmed")
            self.assert_not_recorded()
        self.current_user = self.alice
        self.refuse_removal()
        self.assertEqual(len(prompt_memory.get_person_prompts(self.alice["id"], path=self.prompt_db)), 4)
        self.assertIn(ids[3], {a["asset_id"] for job in media_jobs.recent(500, path=self.jobs_db) for a in job["assets"]})

    def refuse_removal(self) -> None:
        response = self.remove_missing()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "media_folder_unconfirmed")
        self.assertEqual(len(media_jobs.recent(500, path=self.jobs_db)), len(self.alice_jobs))

    def test_a_mismatched_folder_is_never_adopted_however_much_of_the_library_is_here(self) -> None:
        self.library_of(stored=3, total=4)
        recorded = self.recorded_volume()
        marker = self.cache / ".azizam-media"
        foreign = json.dumps({"volume_id": "0" * 32, "created_at": 5})
        marker.write_text(foreign)
        self.assertEqual(self.media_folder(), {"state": "mismatch"})
        self.assertEqual(marker.read_text(), foreign)
        self.assertEqual(self.recorded_volume(), recorded)
        marker.unlink()
        self.assertEqual(self.media_folder(), {"state": "mismatch"})
        self.assertFalse(marker.exists())
        self.assertEqual(self.recorded_volume(), recorded)
        self.refuse_removal()

    def test_a_confirmed_folder_and_a_member_read_are_not_counted(self) -> None:
        self.library_of(stored=3, total=4)
        with patch.object(me_media, "_folder_counts", wraps=me_media._folder_counts) as counted:
            self.assertEqual(self.media_folder(), {"state": "confirmed"})
            self.unconfirm_folder()
            self.assertEqual(self.media_folder(), {"state": "unconfirmed"})
        counted.assert_not_called()

    def test_an_unrelated_folder_is_not_adopted_and_removal_still_refuses(self) -> None:
        for with_a_file in (False, True):
            with self.subTest(with_a_file=with_a_file):
                self.seed()
                self.unconfirm_folder()
                self.cache.rename(self.root / "cache-elsewhere")
                self.cache.mkdir()
                if with_a_file:
                    (self.cache / ("f" * 64 + ".png")).write_bytes(b"someone else's picture")
                self.assertEqual(self.media_folder(), {"state": "unconfirmed"})
                self.assert_not_recorded()
                response = self.remove_missing()
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["error"]["code"],
                                 "media_folder_unconfirmed" if with_a_file else "media_folder_unavailable")
                self.assert_nothing_changed()
                shutil.rmtree(self.cache)
                (self.root / "cache-elsewhere").rename(self.cache)

    # The owner's confirmation writes the marker into the directory it checked, not into whatever the path names later.

    def swap_when_the_folder_is_opened(self, *, before: bool):
        """Replace the media folder with an empty one at the moment the marker writer opens it."""
        real_open = os.open
        swapped: list[bool] = []

        def swap() -> None:
            self.cache.rename(self.root / "cache-before-swap")
            self.cache.mkdir()
            swapped.append(True)

        def open_then_swap(path, flags, *args, **kwargs):
            is_the_folder = Path(path) == self.cache and flags & os.O_DIRECTORY and not swapped
            if is_the_folder and before:
                swap()
            descriptor = real_open(path, flags, *args, **kwargs)
            if is_the_folder and not before:
                swap()
            return descriptor

        return patch.object(os, "open", open_then_swap), swapped

    def test_the_marker_is_not_written_into_a_folder_swapped_in_after_it_was_opened(self) -> None:
        self.library_of(stored=2, total=2)
        self.unconfirm_folder()
        listed = os.stat(self.cache)
        opener, swapped = self.swap_when_the_folder_is_opened(before=False)
        with opener, self.assertRaises(PublicFailure):
            prompt_memory.confirm_media_folder(
                self.cache, owner=True, path=self.jobs_db, expected=(listed.st_dev, listed.st_ino),
            )
        self.assertEqual(swapped, [True])
        self.assertEqual(os.listdir(self.cache), [])  # the replacement never receives a marker
        with sqlite3.connect(self.jobs_db) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM media_volume_identity").fetchone())
        self.assertEqual(self.media_folder()["state"], "unconfirmed")

    def test_the_marker_is_not_written_into_a_folder_swapped_in_before_it_was_opened(self) -> None:
        self.library_of(stored=2, total=2)
        self.unconfirm_folder()
        listed = os.stat(self.cache)
        opener, swapped = self.swap_when_the_folder_is_opened(before=True)
        with opener, self.assertRaises(PublicFailure) as raised:
            prompt_memory.confirm_media_folder(
                self.cache, owner=True, path=self.jobs_db, expected=(listed.st_dev, listed.st_ino),
            )
        self.assertEqual(swapped, [True])
        self.assertEqual(raised.exception.code, "media_folder_unavailable")
        self.assertEqual(os.listdir(self.cache), [])
        self.assertEqual(os.listdir(self.root / "cache-before-swap").count(".azizam-media"), 0)
        self.assert_not_recorded_in_db()

    def assert_not_recorded_in_db(self) -> None:
        with sqlite3.connect(self.jobs_db) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM media_volume_identity").fetchone())

    def test_later_pages_do_not_recount_the_folder(self) -> None:
        self.seed()
        self.unconfirm_folder()
        # Alice owns the seeded entries, so her own Library has a second page.
        first = self.client.get("/me/api/media", params={"limit": 1}).json()
        self.assertNotIn("media_folder", first)
        self.assertIsNotNone(first["next_cursor"])
        second = self.client.get("/me/api/media", params={"limit": 1, "cursor": first["next_cursor"]})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotIn("media_folder", second.json())

    def test_an_unconfirmed_folder_that_holds_media_is_refused_before_the_library_is_read(self) -> None:
        self.unconfirm_folder()
        (self.cache / ("a" * 64 + ".png")).write_bytes(b"media")
        with patch.object(prompt_memory, "_has_library_entries", wraps=prompt_memory._has_library_entries) as read:
            self.assertEqual(self.media_folder(), {"state": "unconfirmed"})
        read.assert_not_called()

    def test_an_ordinary_media_write_never_creates_the_marker(self) -> None:
        self.unconfirm_folder()
        self._write_asset(asset("B"), ["sha256:alice"], prompt="prompt B")
        self.assertFalse((self.cache / ".azizam-media").exists())

    def test_adoption_works_on_a_filesystem_without_hard_links(self) -> None:
        self.unconfirm_folder()
        with patch.object(os, "link", side_effect=OSError(errno.EPERM, "Operation not permitted")):
            self.assertEqual(self.media_folder(), {"state": "confirmed"})
        self.assertTrue((self.cache / ".azizam-media").is_file())

    def test_owner_confirmation_works_on_a_filesystem_without_hard_links(self) -> None:
        self.seed()
        self.unconfirm_folder()
        self.current_user = self.owner
        with patch.object(os, "link", side_effect=OSError(errno.EPERM, "Operation not permitted")):
            response = self.confirm_folder()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.media_folder(), {"state": "confirmed"})

    def test_writing_a_marker_never_replaces_one_already_there(self) -> None:
        marker = self.cache / ".azizam-media"
        foreign = json.dumps({"volume_id": "1" * 32, "created_at": 5})
        marker.write_text(foreign)
        # The adoption path checks for a marker first; this is the write it must not clobber on a race.
        with self.assertRaises(FileExistsError):
            prompt_memory._write_media_marker(self.cache, replace=False)
        self.assertEqual(marker.read_text(), foreign)
        self.assertEqual(sorted(os.listdir(self.cache)), [".azizam-media"])

    def test_a_marker_symlink_is_never_followed(self) -> None:
        self.seed()
        marker = self.cache / ".azizam-media"
        target = self.root / "elsewhere.json"
        target.write_text(marker.read_text())
        marker.unlink()
        marker.symlink_to(target)
        self.assertEqual(self.media_folder(), {"state": "mismatch"})
        self.refused_unconfirmed()

    def test_a_marker_that_is_not_a_regular_file_is_refused_without_blocking(self) -> None:
        self.seed()
        marker = self.cache / ".azizam-media"
        for kind in ("directory", "fifo"):
            with self.subTest(kind=kind):
                marker.unlink()
                if kind == "directory":
                    marker.mkdir()
                else:
                    os.mkfifo(marker)
                try:
                    self.assertEqual(self.media_folder(), {"state": "mismatch"})
                    self.refused_unconfirmed()
                finally:
                    if kind == "directory":
                        marker.rmdir()
                    else:
                        marker.unlink()
                    marker.write_text(json.dumps({"volume_id": self.recorded_volume(), "created_at": 1}))

    def test_a_fifo_holding_a_valid_marker_is_not_a_marker(self) -> None:
        self.seed()
        marker = self.cache / ".azizam-media"
        marker.unlink()
        os.mkfifo(marker)
        # O_RDWR opens a FIFO without waiting for a reader on Linux, so the value is there to be read.
        writer = os.open(marker, os.O_RDWR)
        try:
            os.write(writer, json.dumps({"volume_id": self.recorded_volume(), "created_at": 1}).encode())
            self.assertEqual(self.media_folder(), {"state": "mismatch"})
            self.refused_unconfirmed()
        finally:
            os.close(writer)
            marker.unlink()

    def test_a_folder_replaced_after_it_was_listed_removes_nothing(self) -> None:
        self.seed()
        snapshot = me_media.create_storage_snapshot_provider(self.cache)()
        self.cache.rename(self.root / "cache-before-swap")
        shutil.copytree(self.root / "cache-before-swap", self.cache)
        self.client = self.make_client(self._asset_path, storage_provider=lambda: snapshot)
        self.refused()

    def test_a_folder_swapped_while_it_is_listed_is_not_read(self) -> None:
        self.seed()
        real_listdir = os.listdir
        swapped: list[Path] = []

        def swap_while_listing(path):
            names = real_listdir(path)
            if Path(path) == self.cache and not swapped:
                swapped.append(self.cache)
                self.cache.rename(self.root / "cache-moved")
                self.cache.mkdir()
            return names

        with patch("gateway.me_media.os.listdir", side_effect=swap_while_listing):
            self.assertIsNone(me_media.create_storage_snapshot_provider(self.cache)())
        self.assertEqual(len(swapped), 1)

    def test_the_removal_checks_the_marker_before_it_deletes_anything(self) -> None:
        self.seed()
        (self.cache / ".azizam-media").unlink()
        with patch.object(media_jobs, "hide_assets_in", wraps=media_jobs.hide_assets_in) as hidden, \
                patch.object(media_jobs, "delete_succeeded_in", wraps=media_jobs.delete_succeeded_in) as deleted:
            self.refused_unconfirmed()
        hidden.assert_not_called()
        deleted.assert_not_called()

    def test_library_lists_the_gone_items_before_the_removal(self) -> None:
        self.seed()
        self.assertEqual(self.library(), {asset("A"): False, asset("B"): True, asset("C"): False, asset("D"): True})

    def test_removes_only_the_callers_missing_entries_and_keeps_files_receipts_and_other_people(self) -> None:
        self.seed()
        response = self.remove_missing()
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
        self.remove_missing()
        again = self.remove_missing()
        self.assertEqual(again.json(), {"removed_assets": 0, "removed_jobs": 0})
        self.assertEqual(self.library(), {asset("B"): True, asset("D"): True})

    def test_the_owner_removes_only_their_own_entries(self) -> None:
        self.seed()
        self.current_user = self.owner
        self.assertEqual(self.remove_missing().json(), {"removed_assets": 0, "removed_jobs": 0})
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

    def assert_nothing_changed(self, written: frozenset[str] = frozenset()) -> None:
        # ``written`` names entries the test itself added after the seed; they stay.
        self.assertEqual(len(self.job_ids("req_a")), 1)
        self.assertEqual(len(self.job_ids("req_cd")), 1)
        self.assertEqual(len(self.job_ids("req_e")), 1)
        kept = prompt_memory.get_person_prompts(self.alice["id"], path=self.prompt_db)
        self.assertEqual(set(kept), {asset(letter) for letter in "ABCD"} | written)
        # The one-time owner confirmation is an audited change of its own, so it is not "nothing changed".
        audit = admin_store.list_audit(action_prefix="media.", limit=50, path=self.admin_db)
        self.assertEqual([row for row in audit if row["action"] != "media.folder.confirm"], [])

    def refused(self, message: str = UNSEEN) -> None:
        response = self.remove_missing()
        self.assertEqual(response.status_code, 503, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "media_folder_unavailable")
        self.assertEqual(error["message"], message)
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

    def test_an_empty_media_folder_with_entries_is_refused_and_removes_nothing(self) -> None:
        # The folder is readable but holds no media. An unmounted volume looks the same, and there every entry would read as gone.
        self.seed()
        self.empty_the_folder()
        self.refused(EMPTY)

    def test_an_empty_media_folder_removes_nothing_for_a_caller_with_no_entries(self) -> None:
        # The owner has nothing on record, so the empty folder is no reason to refuse. Nobody else's entries change.
        self.seed()
        self.empty_the_folder()
        self.current_user = self.owner
        response = self.remove_missing()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 0, "removed_jobs": 0})
        self.assert_nothing_changed()

    def test_an_empty_media_folder_and_an_empty_library_has_nothing_to_remove(self) -> None:
        # A fresh install is adopted on its first Library read, not by a removal.
        (self.cache / ".azizam-media").unlink()
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("DELETE FROM media_volume_identity")
        self.assertEqual(os.listdir(self.cache), [])
        self.assertEqual(self.media_folder(), {"state": "confirmed"})
        marker = json.loads((self.cache / ".azizam-media").read_text())
        response = self.remove_missing()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 0, "removed_jobs": 0})
        self.assertRegex(marker["volume_id"], r"^[0-9a-f]{32}$")
        self.assertGreater(marker["created_at"], 0)
        with sqlite3.connect(self.jobs_db) as conn:
            self.assertEqual(conn.execute("SELECT volume_id FROM media_volume_identity").fetchone()[0], marker["volume_id"])
        self.assertEqual(self.remove_missing().status_code, 200)
        self.assertEqual(json.loads((self.cache / ".azizam-media").read_text()), marker)

    def test_a_folder_with_only_sidecars_and_scratch_files_removes_nothing(self) -> None:
        self.seed()
        for letter in "BD":
            self._asset_path(asset(letter)).unlink()
        (self.cache / ("f" * 64 + ".png.tmp")).write_bytes(b"half a write")
        self.refused(EMPTY)

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
        response = self.remove_missing()
        self.assertEqual(response.json(), {"removed_assets": 2, "removed_jobs": 1})
        self.assertEqual(len(self.job_ids("req_odd0")), 1)
        self.assertEqual(len(self.job_ids("req_odd1")), 1)
        self.assertTrue((self.cache / ("1" * 64 + ".gif")).is_file())

    def test_a_file_stored_under_any_suffix_counts_as_stored(self) -> None:
        self.seed()
        (self.cache / ("A" * 64 + ".bin")).write_bytes(b"unknown suffix")
        self.assertEqual(self.remove_missing().json(), {"removed_assets": 1, "removed_jobs": 0})
        self.assertEqual(len(self.job_ids("req_a")), 1)

    def test_the_media_folder_is_listed_once_however_many_files_are_gone(self) -> None:
        """Each gone asset used to rescan the whole folder: 13 ms each at 66k names."""
        self.seed()
        for index in range(20):
            self._receipt(self.app_a["id"], f"req_many{index}")
            self._job(f"req_many{index}", "img_" + f"{index + 16:02x}" * 32)
        probes: list[str] = []

        def resolver(asset_id):
            probes.append(asset_id)
            return self._asset_path(asset_id)

        client = self.make_client(resolver)
        real_listdir = os.listdir
        listings: list[object] = []

        def counting_listdir(path="."):
            listings.append(path)
            return real_listdir(path)

        with patch("gateway.me_media.os.listdir", counting_listdir):
            response = client.post(REMOVE, headers=VALID, json={"expected_missing": 22})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"removed_assets": 22, "removed_jobs": 21})
        self.assertEqual(probes, [], "presence comes from the one listing, not a lookup per asset")
        self.assertEqual([str(path) for path in listings], [str(self.cache)])

    # --- a job goes only when every file it names is gone -------------------------------

    def test_a_job_with_a_hidden_but_present_file_is_kept(self) -> None:
        """The person deleted the first item from their Library while another owner still holds the file."""
        present, gone = "img_" + "1" * 64, "img_" + "2" * 64
        self._write_asset(present, ["sha256:alice", "sha256:bob"], prompt="shared")
        prompt_memory.put(gone, self.alice["id"], "gone", kept_until=None, path=self.prompt_db)
        self._receipt(self.app_a["id"], "req_pg")
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_pg", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": present, "media_type": "image/png"},
                    {"asset_id": gone, "media_type": "image/png"}]), path=self.jobs_db)
        deleted = self.client.delete(f"/me/api/media/{present}", headers=VALID)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.library(), {gone: False})
        self.assertEqual(self.remove_missing().json(), {"removed_assets": 1, "removed_jobs": 0})
        self.assertEqual(len(self.job_ids("req_pg")), 1, "the file is still on disk")
        self.assertEqual(self.library(), {})

    def test_an_output_named_by_two_jobs_follows_its_file(self) -> None:
        present, gone, other = ("img_" + digit * 64 for digit in "123")
        self._write_asset(present, ["sha256:alice"], prompt="present")
        for name, assets in (("one", [gone]), ("two", [gone]), ("pone", [present]), ("ptwo", [present, other])):
            self._receipt(self.app_a["id"], f"req_{name}")
            media_jobs.create(media_jobs.MediaJob(
                request_id=f"req_{name}", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100,
                assets=[{"asset_id": asset_id, "media_type": "image/png"} for asset_id in assets]), path=self.jobs_db)
        response = self.remove_missing()
        # The gone output is gone in both jobs that name it: both go. The present one keeps its jobs.
        self.assertEqual(response.json(), {"removed_assets": 2, "removed_jobs": 2})
        self.assertEqual((self.job_ids("req_one"), self.job_ids("req_two")), ([], []))
        self.assertEqual(len(self.job_ids("req_pone")), 1)
        self.assertEqual(len(self.job_ids("req_ptwo")), 1)
        self.assertEqual(self.library(), {present: True})

    def test_identical_output_made_again_is_listed_again(self) -> None:
        """A hide belongs to an asset in one job, not to the content hash for ever."""
        again, stored = "img_" + "1" * 64, "img_" + "2" * 64
        self._receipt(self.app_a["id"], "req_one")
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_one", intent="image.generate", status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": again, "media_type": "image/png"},
                    {"asset_id": stored, "media_type": "image/png"}]), path=self.jobs_db)
        self._write_asset(stored, ["sha256:alice"])
        self.assertEqual(self.remove_missing().json(), {"removed_assets": 1, "removed_jobs": 0})
        self.assertEqual(self.library(), {stored: True})
        # The same bytes are generated again: the file is back and a new job names it.
        self._write_asset(again, ["sha256:alice"], prompt="again")
        self._receipt(self.app_a["id"], "req_two")
        self._job("req_two", again)
        self.assertEqual(self.library(), {stored: True, again: True})

    # --- one transaction -----------------------------------------------------------------

    def test_a_failure_part_way_changes_nothing(self) -> None:
        self.seed()
        real = media_jobs.delete_succeeded_in

        def fail_after_deleting(conn, job_ids):
            real(conn, job_ids)
            raise sqlite3.OperationalError("disk I/O error")

        with patch("gateway.media_jobs.delete_succeeded_in", fail_after_deleting):
            response = self.remove_missing()
        self.assertGreaterEqual(response.status_code, 500)
        self.assert_nothing_changed()
        self.assertEqual(self.library(), {asset("A"): False, asset("B"): True, asset("C"): False, asset("D"): True})
        with sqlite3.connect(self.jobs_db) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            hidden = conn.execute("SELECT COUNT(*) FROM media_hidden_assets").fetchone()[0] if "media_hidden_assets" in tables else 0
        self.assertEqual(hidden, 0)
        # A retry after the fault completes the removal.
        self.assertEqual(self.remove_missing().json(), {"removed_assets": 2, "removed_jobs": 1})

    # --- erasing a person erases their hides too ------------------------------------------

    def hides(self) -> dict[str, int]:
        with sqlite3.connect(self.jobs_db) as conn:
            return dict(conn.execute("SELECT person_id, COUNT(*) FROM media_hidden_assets GROUP BY person_id"))

    def seed_hides(self) -> None:
        self.seed()
        self.assertEqual(self.remove_missing().json(), {"removed_assets": 2, "removed_jobs": 1})
        with media_jobs._conn(self.jobs_db) as conn:
            media_jobs.hide_assets_in(conn, self.bob["id"], [(asset("E"), "job_bob")])
            conn.commit()
        self.assertEqual(self.hides(), {self.alice["id"]: 1, self.bob["id"]: 1})

    def test_deleting_a_person_deletes_their_hide_rows_and_only_theirs(self) -> None:
        self.seed_hides()
        prompt_memory.delete_person(self.alice["id"], path=self.jobs_db)
        self.assertEqual(self.hides(), {self.bob["id"]: 1})
        self.assertEqual(prompt_memory.get_person_prompts(self.alice["id"], path=self.prompt_db), {})

    def test_deleting_a_person_on_a_database_without_the_hide_table_still_works(self) -> None:
        self.seed()
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("DROP TABLE IF EXISTS media_hidden_assets")
        self.assertGreater(prompt_memory.delete_person(self.alice["id"], path=self.jobs_db), 0)

    def test_a_failure_deleting_the_hides_keeps_the_prompts(self) -> None:
        self.seed_hides()
        with patch("gateway.media_jobs.delete_person_hides_in", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                prompt_memory.delete_person(self.alice["id"], path=self.jobs_db)
        self.assertEqual(self.hides(), {self.alice["id"]: 1, self.bob["id"]: 1})
        self.assertTrue(prompt_memory.get_person_prompts(self.alice["id"], path=self.prompt_db))

    # --- a failed read of the hides must not show hidden items ----------------------------

    @contextmanager
    def failing_hide_reads(self, *errors: Exception):
        """The hide query raises each error in turn, then reads for real."""
        pending = list(errors)
        calls = [0]
        real = media_jobs._conn

        @contextmanager
        def conn(path=media_jobs.DB_PATH):
            with real(path) as inner:
                class Wrapped:
                    def execute(self, sql, *args):
                        if "media_hidden_assets" in sql and sql.lstrip().startswith("SELECT") and pending:
                            calls[0] += 1
                            raise pending.pop(0)
                        return inner.execute(sql, *args)
                yield Wrapped()

        with patch("gateway.media_jobs._conn", conn), patch("gateway.media_jobs.time.sleep"):
            yield calls

    def hidden_by_alice(self) -> None:
        self.seed()
        self.assertEqual(self.remove_missing().json(), {"removed_assets": 2, "removed_jobs": 1})
        self.assertEqual(len(media_jobs.hidden_assets([self.alice["id"]], path=self.jobs_db)), 1)

    def test_a_lock_on_the_hide_read_is_retried_briefly(self) -> None:
        self.hidden_by_alice()
        locked = sqlite3.OperationalError("database is locked")
        with self.failing_hide_reads(locked, locked) as calls:
            self.assertEqual(self.library(), {asset("B"): True, asset("D"): True})
        self.assertEqual(calls[0], 2)

    def test_a_lock_that_does_not_clear_fails_the_request_instead_of_showing_hidden_items(self) -> None:
        self.hidden_by_alice()
        locked = sqlite3.OperationalError("database is locked")
        with self.failing_hide_reads(*[locked] * 20):
            response = self.client.get("/me/api/media")
        self.assertGreaterEqual(response.status_code, 500)
        self.assertNotIn(asset("C"), response.text)
        with self.assertRaises(sqlite3.OperationalError):
            with self.failing_hide_reads(*[locked] * 20):
                media_jobs.hidden_assets([self.alice["id"]], path=self.jobs_db)

    def test_another_database_error_on_the_hide_read_is_not_swallowed(self) -> None:
        self.hidden_by_alice()
        with self.failing_hide_reads(sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                media_jobs.hidden_assets([self.alice["id"]], path=self.jobs_db)

    def test_only_a_missing_hide_table_means_nothing_is_hidden(self) -> None:
        self.hidden_by_alice()
        with self.failing_hide_reads(sqlite3.OperationalError("no such table: media_hidden_assets")):
            self.assertEqual(media_jobs.hidden_assets([self.alice["id"]], path=self.jobs_db), set())
        with self.failing_hide_reads(sqlite3.OperationalError("no such table: media_jobs")):
            with self.assertRaises(sqlite3.OperationalError):
                media_jobs.hidden_assets([self.alice["id"]], path=self.jobs_db)

    # --- a database made before the hide table ---------------------------------------------

    def old_database(self, path):
        """A media_jobs.db as origin/main's schema made it: the current schema without the hide table."""
        start = media_jobs._SCHEMA.index("CREATE TABLE IF NOT EXISTS media_hidden_assets")
        end = media_jobs._SCHEMA.index("CREATE TABLE IF NOT EXISTS media_jobs_schema_version")
        old_schema = media_jobs._SCHEMA[:start] + media_jobs._SCHEMA[end:]
        self.assertNotIn("media_hidden_assets", old_schema)
        with sqlite3.connect(path) as conn:
            conn.executescript(old_schema)
            conn.execute("INSERT INTO media_jobs_schema_version(version) VALUES (?)", (media_jobs.SCHEMA_VERSION,))
        conn.close()

    def hide_table_columns(self, path) -> list[str]:
        with sqlite3.connect(path) as conn:
            return [row[1] for row in conn.execute("PRAGMA table_info(media_hidden_assets)")]

    def test_an_old_media_jobs_database_gains_the_hide_table_and_keeps_its_rows(self) -> None:
        old = self.root / "old_media_jobs.db"
        self.old_database(old)
        self.assertEqual(self.hide_table_columns(old), [])
        media_jobs.init_db(old)
        columns = ["person_id", "asset_id", "job_id", "hidden_at"]
        self.assertEqual(self.hide_table_columns(old), columns)
        with sqlite3.connect(old) as conn:
            conn.execute("INSERT INTO media_hidden_assets VALUES ('p','a','j',1)")
        media_jobs.init_db(old)  # idempotent: the second start changes nothing and keeps the row
        self.assertEqual(self.hide_table_columns(old), columns)
        self.assertEqual(media_jobs.hidden_assets(["p"], path=old), {("a", "j")})

    def test_an_old_database_lists_with_nothing_hidden_and_hides_on_first_use(self) -> None:
        old = self.root / "old_media_jobs.db"
        self.old_database(old)
        self.assertEqual(media_jobs.hidden_assets([self.alice["id"]], path=old), set())
        with media_jobs._conn(old) as conn:
            media_jobs.hide_assets_in(conn, self.alice["id"], [("a", "j")])
            conn.commit()
        self.assertEqual(media_jobs.hidden_assets([self.alice["id"]], path=old), {("a", "j")})
