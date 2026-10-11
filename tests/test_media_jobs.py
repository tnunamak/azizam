"""Tests for gateway.media_jobs — generic media job model and store.

Uses a temporary SQLite file per test.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import media_jobs  # noqa: E402
from gateway.media_jobs import (  # noqa: E402
    ADMIN_ONLY_FIELDS,
    ALL_STATUSES,
    CLIENT_VISIBLE_FIELDS,
    INTERNAL_ONLY_FIELDS,
    STATUS_CANCELED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    MediaJob,
)


def _make_job(**overrides: Any) -> MediaJob:
    defaults: dict[str, Any] = dict(
        intent="video.generate",
        workflow="ltx.video.basic",
        backend="comfyui.local",
        request_id="req_abc123",
        app_profile="video-agent",
        api_key_name="agent-key-1",
        metadata={"prompt_id": "c_001", "queue_pos": 2},
    )
    defaults.update(overrides)
    return MediaJob(**defaults)


class TestMediaJobModel(unittest.TestCase):
    def test_defaults_are_filled(self) -> None:
        job = _make_job()
        self.assertTrue(job.job_id.startswith("mj_"))
        self.assertEqual(job.status, STATUS_QUEUED)
        self.assertEqual(job.progress, 0)
        self.assertIsInstance(job.created_at, int)
        self.assertEqual(job.created_at, job.updated_at)

    def test_missing_intent_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MediaJob(intent="")

    def test_invalid_status_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_job(status="bogus")

    def test_progress_must_be_in_range(self) -> None:
        with self.assertRaises(ValueError):
            _make_job(progress=-1)
        with self.assertRaises(ValueError):
            _make_job(progress=101)

    def test_progress_accepts_str_int(self) -> None:
        job = _make_job(progress="42")
        self.assertEqual(job.progress, 42)

    def test_metadata_forbidden_keys_rejected(self) -> None:
        for key in ("prompt", "image_b64", "raw_body", "api_key", "video_bytes"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    _make_job(metadata={key: "..."})

    def test_metadata_nested_forbidden_keys_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_job(metadata={"trace": {"prompt": "hello"}})

    def test_assets_forbidden_keys_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_job(assets=[{"id": "a1", "image_b64": "...."}])

    def test_assets_safe_shape_accepted(self) -> None:
        job = _make_job(assets=[{"id": "a1", "media_type": "video/mp4", "url": "/v1/assets/a1"}])
        self.assertEqual(len(job.assets), 1)


class TestAudienceFiltering(unittest.TestCase):
    def test_client_view_omits_admin_and_internal_fields(self) -> None:
        job = _make_job()
        view = job.to_dict("client")
        for name in ADMIN_ONLY_FIELDS | INTERNAL_ONLY_FIELDS:
            self.assertNotIn(name, view, f"client view leaked {name}")
        for name in ("job_id", "status", "progress", "intent"):
            self.assertIn(name, view)

    def test_admin_view_includes_admin_fields_excludes_metadata(self) -> None:
        job = _make_job()
        view = job.to_dict("admin")
        for name in ADMIN_ONLY_FIELDS:
            self.assertIn(name, view)
        for name in INTERNAL_ONLY_FIELDS:
            self.assertNotIn(name, view)

    def test_internal_view_includes_everything(self) -> None:
        job = _make_job()
        view = job.to_dict("internal")
        for name in CLIENT_VISIBLE_FIELDS | ADMIN_ONLY_FIELDS | INTERNAL_ONLY_FIELDS:
            self.assertIn(name, view)
        self.assertEqual(view["metadata"], {"prompt_id": "c_001", "queue_pos": 2})

    def test_invalid_audience_raises(self) -> None:
        with self.assertRaises(ValueError):
            _make_job().to_dict("public")


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Path(self.tmp.name)
        self.db.unlink(missing_ok=True)
        media_jobs.init_db(self.db)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db) + suffix)
            try:
                p.unlink()
            except FileNotFoundError:
                pass


class TestStoreLifecycle(_StoreCase):
    def test_init_records_schema_version(self) -> None:
        self.assertEqual(media_jobs.schema_version(self.db), media_jobs.SCHEMA_VERSION)

    def test_create_and_get_round_trip(self) -> None:
        job = _make_job()
        created = media_jobs.create(job, path=self.db)
        self.assertEqual(created.job_id, job.job_id)
        loaded = media_jobs.get(job.job_id, path=self.db)
        self.assertEqual(loaded["job_id"], job.job_id)
        self.assertEqual(loaded["intent"], "video.generate")
        self.assertEqual(loaded["status"], STATUS_QUEUED)

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(media_jobs.get("mj_does_not_exist", path=self.db))

    def test_create_is_idempotent_on_request_id(self) -> None:
        first = media_jobs.create(_make_job(request_id="req_dup"), path=self.db)
        second = media_jobs.create(
            _make_job(request_id="req_dup"),
            path=self.db,
        )
        self.assertEqual(first.job_id, second.job_id)
        # The second submission must not overwrite — first wins.
        loaded = media_jobs.get(first.job_id, path=self.db)
        self.assertEqual(loaded["workflow"], "ltx.video.basic")

    def _age(self, job_id: str, seconds: int) -> None:
        with media_jobs._conn(self.db) as conn:
            conn.execute("UPDATE media_jobs SET updated_at = updated_at - ? WHERE job_id = ?",
                         (seconds, job_id))
            conn.commit()

    def test_old_finished_request_id_starts_new_work_and_keeps_history(self) -> None:
        # Kept jobs must not turn a 14-day replay window into a permanent one.
        for status in (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELED):
            request_id = f"req_old_{status}"
            first = media_jobs.create(_make_job(request_id=request_id, status=status), path=self.db)
            media_jobs.append_event(first.job_id, "x", {"progress": 1}, path=self.db)
            self._age(first.job_id, media_jobs.REPLAY_WINDOW_S + 1)
            self.assertIsNone(media_jobs.get_by_request_id(request_id, path=self.db))
            second = media_jobs.create(_make_job(request_id=request_id), path=self.db)
            self.assertNotEqual(first.job_id, second.job_id)
            self.assertEqual(media_jobs.get_by_request_id(request_id, path=self.db)["job_id"], second.job_id)
            kept = media_jobs.get(first.job_id, audience="internal", path=self.db)
            self.assertEqual(kept["status"], status)
            self.assertEqual(kept["request_id"], "")
            self.assertEqual(len(media_jobs.events(first.job_id, path=self.db)), 1)

    def test_recent_or_active_request_id_still_replays(self) -> None:
        recent = media_jobs.create(_make_job(request_id="req_recent", status=STATUS_FAILED), path=self.db)
        self._age(recent.job_id, media_jobs.REPLAY_WINDOW_S - 60)
        self.assertEqual(media_jobs.create(_make_job(request_id="req_recent"), path=self.db).job_id, recent.job_id)
        active = media_jobs.create(_make_job(request_id="req_active", status=STATUS_RUNNING), path=self.db)
        self._age(active.job_id, media_jobs.REPLAY_WINDOW_S + 1)
        self.assertEqual(media_jobs.get_by_request_id("req_active", path=self.db)["job_id"], active.job_id)
        self.assertEqual(media_jobs.create(_make_job(request_id="req_active"), path=self.db).job_id, active.job_id)

    def test_create_without_request_id_makes_separate_jobs(self) -> None:
        a = media_jobs.create(_make_job(request_id=""), path=self.db)
        b = media_jobs.create(_make_job(request_id=""), path=self.db)
        self.assertNotEqual(a.job_id, b.job_id)
        self.assertEqual(media_jobs.count(self.db), 2)

    def test_upsert_replaces_existing(self) -> None:
        job = _make_job()
        media_jobs.upsert(job, path=self.db)
        job.metadata["queue_pos"] = 5
        # Mutate-then-upsert ought to leave only one row.
        media_jobs.upsert(job, path=self.db)
        self.assertEqual(media_jobs.count(self.db), 1)
        loaded = media_jobs.get(job.job_id, path=self.db)
        self.assertEqual(loaded["metadata"]["queue_pos"], 5)

    def test_get_by_request_id(self) -> None:
        job = media_jobs.create(_make_job(request_id="req_lookup"), path=self.db)
        found = media_jobs.get_by_request_id("req_lookup", path=self.db)
        self.assertEqual(found["job_id"], job.job_id)
        self.assertIsNone(media_jobs.get_by_request_id("nope", path=self.db))


class TestStatusTransitions(_StoreCase):
    def test_queued_to_running_to_succeeded(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        running = media_jobs.update_status(job.job_id, STATUS_RUNNING, progress=10, path=self.db)
        self.assertEqual(running["status"], STATUS_RUNNING)
        self.assertEqual(running["progress"], 10)
        done = media_jobs.update_status(
            job.job_id,
            STATUS_SUCCEEDED,
            add_assets=[{"id": "asset_001", "media_type": "video/mp4"}],
            path=self.db,
        )
        self.assertEqual(done["status"], STATUS_SUCCEEDED)
        self.assertEqual(done["progress"], 100)
        self.assertEqual(done["assets"], [{"id": "asset_001", "media_type": "video/mp4"}])

    def test_queued_to_failed_with_error(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        failed = media_jobs.update_status(
            job.job_id,
            STATUS_FAILED,
            error="comfy node 42 raised KSampler OOM",
            path=self.db,
        )
        self.assertEqual(failed["status"], STATUS_FAILED)
        self.assertIn("OOM", failed["error"])

    def test_running_to_canceled(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        media_jobs.update_status(job.job_id, STATUS_RUNNING, path=self.db)
        canceled = media_jobs.update_status(job.job_id, STATUS_CANCELED, path=self.db)
        self.assertEqual(canceled["status"], STATUS_CANCELED)

    def test_illegal_transition_rejected(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        # queued -> succeeded skips the running phase and is not allowed.
        with self.assertRaises(ValueError):
            media_jobs.update_status(job.job_id, STATUS_SUCCEEDED, path=self.db)
        # Drive it through running -> succeeded so we can test terminal locks.
        media_jobs.update_status(job.job_id, STATUS_RUNNING, path=self.db)
        media_jobs.update_status(job.job_id, STATUS_SUCCEEDED, path=self.db)
        with self.assertRaises(ValueError):
            media_jobs.update_status(job.job_id, STATUS_RUNNING, path=self.db)
        with self.assertRaises(ValueError):
            media_jobs.update_status(job.job_id, STATUS_FAILED, path=self.db)

    def test_same_state_progress_update_allowed(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        media_jobs.update_status(job.job_id, STATUS_RUNNING, progress=10, path=self.db)
        again = media_jobs.update_status(job.job_id, STATUS_RUNNING, progress=42, path=self.db)
        self.assertEqual(again["progress"], 42)

    def test_update_status_on_missing_returns_none(self) -> None:
        self.assertIsNone(
            media_jobs.update_status("mj_missing", STATUS_RUNNING, path=self.db)
        )

    def test_metadata_patch_merges(self) -> None:
        job = media_jobs.create(_make_job(metadata={"a": 1}), path=self.db)
        media_jobs.update_status(
            job.job_id,
            STATUS_RUNNING,
            metadata_patch={"b": 2},
            path=self.db,
        )
        loaded = media_jobs.get(job.job_id, path=self.db)
        self.assertEqual(loaded["metadata"], {"a": 1, "b": 2})

    def test_metadata_patch_rejects_forbidden_keys(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        with self.assertRaises(ValueError):
            media_jobs.update_status(
                job.job_id,
                STATUS_RUNNING,
                metadata_patch={"prompt": "shh"},
                path=self.db,
            )

    def test_add_assets_rejects_forbidden_keys(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        with self.assertRaises(ValueError):
            media_jobs.update_status(
                job.job_id,
                STATUS_RUNNING,
                add_assets=[{"id": "a1", "video_bytes": "...."}],
                path=self.db,
            )


class TestEvents(_StoreCase):
    def test_append_and_read_events(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        media_jobs.append_event(
            job.job_id,
            "comfy.executing",
            {"node": "31", "phase": "sampling", "step": 1, "total": 30, "progress": 3},
            path=self.db,
        )
        media_jobs.append_event(
            job.job_id,
            "comfy.executing",
            {"node": "31", "phase": "sampling", "step": 15, "total": 30, "progress": 50},
            path=self.db,
        )
        evs = media_jobs.events(job.job_id, path=self.db)
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["event_type"], "comfy.executing")
        self.assertEqual(evs[0]["payload"]["step"], 1)
        self.assertLess(evs[0]["id"], evs[1]["id"])

    def test_events_after_sequence_pagination(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        first_sequence = media_jobs.append_event(job.job_id, "a", {"progress": 1}, path=self.db)
        media_jobs.append_event(job.job_id, "b", {"progress": 2}, path=self.db)
        media_jobs.append_event(job.job_id, "c", {"progress": 3}, path=self.db)
        tail = media_jobs.events(job.job_id, after_sequence=first_sequence, path=self.db)
        self.assertEqual([e["event_type"] for e in tail], ["b", "c"])

    def test_events_client_audience_filters_payload(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        media_jobs.append_event(
            job.job_id,
            "comfy.executing",
            {
                "node": "31",
                "class_type": "KSampler",
                "phase": "sampling",
                "step": 5,
                "total": 30,
                "progress": 20,
                "backend_endpoint": "http://internal/comfy",
            },
            path=self.db,
        )
        client_view = media_jobs.events(job.job_id, audience="client", path=self.db)
        self.assertEqual(len(client_view), 1)
        payload = client_view[0].get("payload", {})
        self.assertIn("progress", payload)
        self.assertIn("step", payload)
        self.assertIn("phase", payload)
        self.assertNotIn("node", payload)
        self.assertNotIn("backend_endpoint", payload)
        self.assertNotIn("class_type", payload)

    def test_events_admin_audience_omits_payload(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        media_jobs.append_event(
            job.job_id,
            "comfy.executing",
            {"node": "31", "progress": 5},
            path=self.db,
        )
        admin_view = media_jobs.events(job.job_id, audience="admin", path=self.db)
        self.assertEqual(len(admin_view), 1)
        self.assertNotIn("payload", admin_view[0])
        self.assertEqual(admin_view[0]["event_type"], "comfy.executing")

    def test_event_forbidden_payload_keys_rejected(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        for key in ("prompt", "image_b64", "raw_body", "authorization", "video_bytes", "api_key"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    media_jobs.append_event(
                        job.job_id,
                        "x",
                        {key: "..."},
                        path=self.db,
                    )

    def test_event_unknown_job_raises(self) -> None:
        with self.assertRaises(LookupError):
            media_jobs.append_event("mj_missing", "x", {"progress": 1}, path=self.db)

    def test_event_type_must_be_nonempty(self) -> None:
        job = media_jobs.create(_make_job(), path=self.db)
        with self.assertRaises(ValueError):
            media_jobs.append_event(job.job_id, "", {"progress": 1}, path=self.db)


class TestRecentAndPrune(_StoreCase):
    def test_recent_orders_by_updated_at(self) -> None:
        base = int(time.time())
        a = media_jobs.create(
            _make_job(request_id="req_a", created_at=base, updated_at=base),
            path=self.db,
        )
        b = media_jobs.create(
            _make_job(request_id="req_b", created_at=base + 1, updated_at=base + 1),
            path=self.db,
        )
        # bump A so it becomes newer than B by explicit timestamp
        media_jobs.update_status(a.job_id, STATUS_RUNNING, now=base + 5, path=self.db)
        listed = media_jobs.recent(path=self.db)
        self.assertEqual([j["job_id"] for j in listed[:2]], [a.job_id, b.job_id])

    def test_recent_filters_by_profile_and_status(self) -> None:
        media_jobs.create(_make_job(request_id="r1", app_profile="alpha"), path=self.db)
        media_jobs.create(_make_job(request_id="r2", app_profile="beta"), path=self.db)
        alpha_jobs = media_jobs.recent(app_profile="alpha", path=self.db)
        self.assertEqual({j["app_profile"] for j in alpha_jobs}, {"alpha"})

        # Now flip one to running and filter by status.
        first = alpha_jobs[0]
        media_jobs.update_status(first["job_id"], STATUS_RUNNING, path=self.db)
        running = media_jobs.recent(status=STATUS_RUNNING, path=self.db)
        self.assertEqual([j["job_id"] for j in running], [first["job_id"]])

    def test_recent_applies_client_audience(self) -> None:
        media_jobs.create(_make_job(request_id="rc"), path=self.db)
        listed = media_jobs.recent(audience="client", path=self.db)
        self.assertEqual(len(listed), 1)
        for name in ADMIN_ONLY_FIELDS | INTERNAL_ONLY_FIELDS:
            self.assertNotIn(name, listed[0])

    def test_recent_zero_limit(self) -> None:
        media_jobs.create(_make_job(request_id="rz"), path=self.db)
        self.assertEqual(media_jobs.recent(limit=0, path=self.db), [])

    def test_restart_keeps_old_jobs_and_events_in_every_state(self) -> None:
        for status in ALL_STATUSES:
            job = media_jobs.create(_make_job(request_id=status, status=status), path=self.db)
            media_jobs.append_event(job.job_id, "x", {"progress": 1}, path=self.db)
        with media_jobs._conn(self.db) as conn:
            conn.execute("UPDATE media_jobs SET updated_at = 1, created_at = 1")
            conn.commit()
        media_jobs.init_db(self.db)
        self.assertEqual(media_jobs.count(self.db), len(ALL_STATUSES))
        for job in media_jobs.recent(path=self.db, audience="internal"):
            self.assertEqual(len(media_jobs.events(job["job_id"], path=self.db)), 1)


class TestStoreSafety(_StoreCase):
    def test_invalid_audience_in_get_raises(self) -> None:
        media_jobs.create(_make_job(request_id="bad_audience"), path=self.db)
        with self.assertRaises(ValueError):
            media_jobs.get("any", audience="public", path=self.db)

    def test_invalid_audience_in_events_raises(self) -> None:
        with self.assertRaises(ValueError):
            media_jobs.events("any", audience="public", path=self.db)

    def test_invalid_status_filter_raises(self) -> None:
        with self.assertRaises(ValueError):
            media_jobs.recent(status="nope", path=self.db)

    def test_status_set_is_complete(self) -> None:
        self.assertEqual(
            ALL_STATUSES,
            frozenset({
                STATUS_QUEUED,
                STATUS_RUNNING,
                STATUS_SUCCEEDED,
                STATUS_FAILED,
                STATUS_CANCELED,
            }),
        )


if __name__ == "__main__":
    unittest.main()
