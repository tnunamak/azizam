"""Persistence and ownership of /v1/videos job records.

Records used to live only in process memory. After a restart any API key could
read any video through ComfyUI's shared /history. These tests cover the SQLite
migration, the write-through store, and the 404 rules that depend on it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import media_jobs, video_job_records  # noqa: E402
from tests.test_video_list_delete_remix import (  # noqa: E402
    ALICE,
    BOB,
    VideoRegistryTestCase,
    _FakeVideoAsyncClient,
    _succeeded_history_entry,
)

# The schema media_jobs created before this change (version 1, no video table).
_V1_SCHEMA = """
CREATE TABLE media_jobs (
  job_id TEXT PRIMARY KEY, request_id TEXT, intent TEXT NOT NULL, workflow TEXT,
  backend TEXT, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, error TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, app_profile TEXT,
  api_key_name TEXT, parent_request_id TEXT, job_json TEXT NOT NULL
);
CREATE TABLE media_jobs_schema_version (version INTEGER NOT NULL);
INSERT INTO media_jobs_schema_version(version) VALUES (1);
INSERT INTO media_jobs VALUES ('mj_old', NULL, 'image.generate', NULL, NULL, 'succeeded', 100,
  NULL, 1, 1, NULL, NULL, NULL, '{}');
"""


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "media_jobs.db"

    def test_migrates_a_v1_database_without_touching_existing_rows(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.executescript(_V1_SCHEMA)
        conn.commit()
        conn.close()

        video_job_records.save("p1", {"created_at": 5, "owner_key_hash": "h"}, path=self.db)

        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT job_id FROM media_jobs").fetchall(), [("mj_old",)])
        # Rollback safety: the legacy schema version stays 1.
        self.assertEqual(conn.execute("SELECT version FROM media_jobs_schema_version").fetchone(), (1,))
        self.assertEqual(
            conn.execute("SELECT name FROM media_jobs_migrations").fetchall(),
            [(video_job_records.MIGRATION_NAME,)],
        )

    def test_migration_is_idempotent(self) -> None:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertTrue(video_job_records.migrate(conn))
        self.assertFalse(video_job_records.migrate(conn))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_jobs_migrations").fetchone(), (1,))

    def test_media_jobs_init_db_still_accepts_a_migrated_file(self) -> None:
        video_job_records.save("p1", {"created_at": 5}, path=self.db)
        media_jobs.init_db(self.db)  # must not raise a schema-version mismatch
        self.assertIn("p1", video_job_records.load_all(path=self.db))

    def test_save_load_remove_prune_roundtrip(self) -> None:
        now = int(time.time())
        video_job_records.save("new", {"created_at": now, "prompt": "pé", "deleted_at": None}, path=self.db)
        video_job_records.save("old", {"created_at": now - 1000}, path=self.db)
        loaded = video_job_records.load_all(path=self.db)
        self.assertNotIn("prompt", loaded["new"])
        self.assertNotIn("pé", self.db.read_bytes().decode("utf-8", errors="ignore"))
        self.assertEqual(video_job_records.prune(500, path=self.db), 1)
        self.assertEqual(set(video_job_records.load_all(path=self.db)), {"new"})
        video_job_records.remove("new", path=self.db)
        self.assertEqual(video_job_records.load_all(path=self.db), {})


class OwnershipAfterRestartTests(VideoRegistryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(media_jobs, "DB_PATH", Path(self.tmp.name) / "media_jobs.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.vid = proxy._video_id_from_prompt_id("p1")

    def _record(self, **fields):
        base = {
            "model": "ltx-2.5", "size": "960x512", "seconds": "5", "actual_seconds": 5.0,
            "created_at": int(time.time()), "completed_at": None, "deleted_at": None,
            "owner_key_hash": "sha256:alice", "prompt": "x",
        }
        base.update(fields)
        return base

    def _restart(self) -> int:
        """Drop the in-memory state, then reload from the store."""
        proxy._video_job_records.clear()
        proxy._video_progress.clear()
        return proxy._hydrate_video_job_records()

    async def _status(self, auth):
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            return await proxy.proxy_comfy_video_status(self.vid, auth)

    async def _content(self, auth):
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            return await proxy.proxy_comfy_video_content(self.vid, auth)

    def test_record_survives_restart_with_owner(self) -> None:
        proxy._video_job_records["p1"] = self._record(remixed_from_video_id="video_src")
        proxy._persist_video_record("p1")
        self.assertEqual(self._restart(), 1)
        rec = proxy._video_job_records["p1"]
        self.assertEqual(rec["owner_key_hash"], "sha256:alice")
        self.assertEqual(rec["remixed_from_video_id"], "video_src")

    async def test_other_key_gets_404_after_restart_and_owner_still_reads(self) -> None:
        proxy._video_job_records["p1"] = self._record()
        proxy._persist_video_record("p1")
        self._restart()
        for call in (self._status, self._content):
            self.assertEqual((await call(BOB)).status_code, 404, call.__name__)
        self.assertEqual((await self._status(ALICE)).status_code, 200)

    async def test_unknown_id_is_404_for_any_authenticated_key(self) -> None:
        """No record at all (never tracked, expired, or made before ownership
        existed) must not be readable through ComfyUI's shared history."""
        for call in (self._status, self._content):
            for who in (ALICE, BOB):
                self.assertEqual((await call(who)).status_code, 404, call.__name__)

    async def test_allow_untracked_ids_restores_the_old_fallback(self) -> None:
        with patch.object(proxy, "VIDEO_ALLOW_UNTRACKED_IDS", True):
            self.assertEqual((await self._status(BOB)).status_code, 200)
        self.assertFalse(proxy.VIDEO_ALLOW_UNTRACKED_IDS)  # default off

    async def test_unknown_id_still_works_when_auth_is_disabled(self) -> None:
        self.assertEqual((await self._status(proxy.NO_AUTH_CONTEXT)).status_code, 200)

    async def test_tombstone_survives_restart(self) -> None:
        proxy._video_job_records["p1"] = self._record()
        proxy._persist_video_record("p1")
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            resp = await proxy.proxy_comfy_video_delete(self.vid, ALICE)
        self.assertEqual(resp.status_code, 200)
        self._restart()
        self.assertIsNotNone(proxy._video_job_records["p1"]["deleted_at"])
        self.assertEqual((await self._status(ALICE)).status_code, 404)

    async def test_other_key_cannot_load_the_source_of_an_extension(self) -> None:
        proxy._video_job_records["p1"] = self._record()
        proxy._persist_video_record("p1")
        self._restart()
        with self.assertRaises(proxy._VideoAssetError) as ctx:
            await proxy._load_completed_video_bytes(self.vid, BOB)
        self.assertEqual(ctx.exception.status, 404)

    def test_hydrated_records_do_not_hold_up_the_drain(self) -> None:
        proxy._video_job_records["p1"] = self._record()
        proxy._persist_video_record("p1")
        self._restart()
        self.assertEqual(proxy._unfinished_openai_video_jobs(), 0)

    def test_gc_removes_expired_rows_from_the_store(self) -> None:
        old = int(time.time()) - proxy._VIDEO_JOB_RECORD_RETAIN - 10
        proxy._video_job_records["p1"] = self._record(created_at=old)
        proxy._persist_video_record("p1")
        proxy._gc_video_job_records_locked(time.time())
        self.assertNotIn("p1", proxy._video_job_records)
        self.assertEqual(video_job_records.load_all(path=media_jobs.DB_PATH), {})


if __name__ == "__main__":
    unittest.main()
