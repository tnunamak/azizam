from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway import media_ingestion, media_jobs


class SyncMediaIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "media_jobs.db"
        media_jobs.init_db(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sync_image_assets_are_indexed_as_succeeded_media_jobs(self) -> None:
        asset_id = "img_" + "a" * 64

        job = media_ingestion.index_sync_assets(
            request_id="req_image_sync",
            intent="image.generate",
            workflow="image.generate.local",
            backend="comfyui",
            app_profile="default-local",
            app_id="app_alice",
            owner_key_hash="sha256:alice",
            api_key_name="Alice key",
            assets=[{"asset_id": asset_id, "media_type": "image/png", "width": 512, "height": 768, "seed": 7}],
            metadata={"mode": "sync_media_route"},
            path=self.db,
        )

        self.assertIsNotNone(job)
        stored = media_jobs.get(job.job_id, audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["request_id"], "req_image_sync")
        self.assertEqual(stored["status"], media_jobs.STATUS_SUCCEEDED)
        self.assertEqual(stored["progress"], 100)
        self.assertEqual(stored["app_id"], "app_alice")
        self.assertEqual(stored["owner_key_hash"], "sha256:alice")
        self.assertEqual(stored["assets"][0]["asset_id"], asset_id)
        self.assertEqual(stored["assets"][0]["kind"], "image")
        self.assertEqual(stored["assets"][0]["width"], 512)

    def test_audio_speech_assets_use_audio_kind_and_idempotent_request(self) -> None:
        asset_id = "aud_" + "b" * 64

        first = media_ingestion.index_sync_assets(
            request_id="req_speech_sync",
            intent="speech.synthesis",
            assets=[{"asset_id": asset_id, "filename": "speech.mp3"}],
            path=self.db,
        )
        second = media_ingestion.index_sync_assets(
            request_id="req_speech_sync",
            intent="speech.synthesis",
            assets=[{"asset_id": "aud_" + "c" * 64, "filename": "other.wav"}],
            path=self.db,
        )

        assert first is not None and second is not None
        self.assertEqual(first.job_id, second.job_id)
        stored = media_jobs.get(first.job_id, audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["assets"][0]["asset_id"], asset_id)
        self.assertEqual(stored["assets"][0]["kind"], "audio")
        self.assertEqual(stored["assets"][0]["media_type"], "audio/mpeg")

    def test_invalid_or_empty_asset_lists_are_ignored(self) -> None:
        self.assertIsNone(
            media_ingestion.index_sync_assets(
                request_id="req_empty",
                intent="image.generate",
                assets=[{"asset_id": "not-valid"}],
                path=self.db,
            )
        )
        self.assertEqual(media_jobs.count(path=self.db, outer_only=False), 0)


if __name__ == "__main__":
    unittest.main()
