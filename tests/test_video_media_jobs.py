from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import media_jobs, video_media_jobs  # noqa: E402


class VideoMediaJobMetadataTests(unittest.TestCase):
    def test_builds_prompt_free_video_generate_job(self) -> None:
        spec = video_media_jobs.VideoJobSpec(
            model="sora-local",
            workflow="video-ltx-13b-q8",
            size="1280x720",
            seconds=5,
            actual_seconds=4.96,
            seed=123,
            request_hash="abc123",
            idempotency="seeded",
        )

        job = video_media_jobs.new_video_generate_job(
            spec,
            backend="comfyui",
            request_id="req_123",
            app_profile="video-agent",
            api_key_name="agent-key",
        )

        self.assertEqual(job.intent, "video.generate")
        self.assertEqual(job.status, media_jobs.STATUS_QUEUED)
        self.assertEqual(job.progress, 0)
        self.assertEqual(job.workflow, "video-ltx-13b-q8")
        self.assertEqual(job.backend, "comfyui")
        self.assertEqual(job.metadata["mode"], "async_video_generation")
        self.assertEqual(job.metadata["model"], "sora-local")
        self.assertEqual(job.metadata["actual_seconds"], 4.96)
        self.assertNotIn("prompt", job.metadata)
        self.assertNotIn("raw_body", job.metadata)

    def test_metadata_uses_media_job_safety_guard(self) -> None:
        spec = video_media_jobs.VideoJobSpec(
            model="sora-local",
            workflow="video-ltx-13b-q8",
            size="1280x720",
            seconds=5,
            request_hash="abc123",
        )
        metadata = video_media_jobs.video_job_metadata(spec)

        self.assertEqual(metadata["request_hash"], "abc123")
        with self.assertRaises(ValueError):
            media_jobs.MediaJob(
                intent="video.generate",
                workflow=spec.workflow,
                metadata={**metadata, "api_key": "secret"},
            )


class VideoAssetExtractionTests(unittest.TestCase):
    def test_extracts_and_normalizes_comfyui_video_outputs(self) -> None:
        history_job = {
            "outputs": {
                "10": {
                    "videos": [
                        {
                            "filename": "clip.mp4",
                            "subfolder": "run-a",
                            "type": "output",
                        },
                        {
                            "filename": "clip.mp4",
                            "subfolder": "run-a",
                            "type": "output",
                        },
                    ],
                    "images": [{"filename": "frame.png", "type": "output"}],
                },
                "11": {
                    "gifs": [{"filename": "preview.webm", "type": "temp"}],
                },
            }
        }

        assets = video_media_jobs.extract_comfyui_video_assets(history_job)

        self.assertEqual(len(assets), 2)
        self.assertEqual(assets[0]["kind"], "video")
        self.assertEqual(assets[0]["filename"], "clip.mp4")
        self.assertEqual(assets[0]["media_type"], "video/mp4")
        self.assertEqual(
            assets[0]["source"],
            {
                "node_id": "10",
                "output_key": "videos",
                "subfolder": "run-a",
                "type": "output",
            },
        )
        self.assertEqual(assets[1]["filename"], "preview.webm")
        self.assertEqual(assets[1]["media_type"], "video/webm")

    def test_normalized_asset_rejects_empty_filename(self) -> None:
        with self.assertRaises(ValueError):
            video_media_jobs.normalize_video_asset(filename="")


class VideoStatusShapeTests(unittest.TestCase):
    def test_status_event_uses_client_visible_progress_keys(self) -> None:
        event = video_media_jobs.video_status_event(
            status=media_jobs.STATUS_RUNNING,
            progress=42,
            step=12,
            total=30,
            phase="sampling",
            asset_count=0,
        )

        self.assertEqual(
            event,
            {
                "status": "running",
                "progress": 42,
                "step": 12,
                "total": 30,
                "phase": "sampling",
                "asset_count": 0,
            },
        )

    def test_video_generation_payload_matches_route_shape(self) -> None:
        job = media_jobs.MediaJob(
            intent="video.generate",
            workflow="video-ltx-13b-q8",
            status=media_jobs.STATUS_SUCCEEDED,
            progress=100,
            assets=[
                video_media_jobs.normalize_video_asset(
                    filename="clip.mp4",
                    asset_id="vid_abc",
                    content_url="/v1/videos/video_abc/content",
                )
            ],
            metadata={
                "model": "sora-local",
                "size": "1280x720",
                "seconds": 5.0,
                "actual_seconds": 4.96,
            },
        ).to_dict("internal")

        payload = video_media_jobs.video_generation_payload(
            job,
            public_id="video_abc",
        )

        self.assertEqual(payload["id"], "video_abc")
        self.assertEqual(payload["object"], "video.generation")
        self.assertEqual(payload["model"], "sora-local")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["progress"], 100)
        self.assertEqual(payload["content_url"], "/v1/videos/video_abc/content")
        self.assertNotIn("metadata", payload)

    def test_progress_validation_rejects_out_of_range_values(self) -> None:
        with self.assertRaises(ValueError):
            video_media_jobs.video_status_event(
                status=media_jobs.STATUS_RUNNING,
                progress=101,
            )


if __name__ == "__main__":
    unittest.main()
