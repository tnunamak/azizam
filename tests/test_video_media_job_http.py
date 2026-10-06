from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import media_jobs, video_media_job_http, video_media_jobs  # noqa: E402


class VideoMediaJobHttpAdapterTests(unittest.TestCase):
    def test_request_body_builds_prompt_free_durable_job(self) -> None:
        body = {
            "model": "sora-local",
            "prompt": "private prompt",
            "size": "1024x576",
            "seconds": 6,
            "seed": 42,
        }

        job = video_media_job_http.job_from_openai_video_request(
            body,
            video_media_job_http.VideoRequestContext(
                workflow="video-ltx-2.3-distilled",
                backend="comfyui",
                request_id="req_video_1",
                idempotency_key="idem-1",
                app_profile="video-agent",
                api_key_name="agent-key",
            ),
        )

        self.assertEqual(job.intent, video_media_jobs.INTENT_VIDEO_GENERATE)
        self.assertEqual(job.request_id, "req_video_1")
        self.assertEqual(job.workflow, "video-ltx-2.3-distilled")
        self.assertEqual(job.backend, "comfyui")
        self.assertEqual(job.metadata["model"], "sora-local")
        self.assertEqual(job.metadata["size"], "1024x576")
        self.assertEqual(job.metadata["seconds"], 6.0)
        self.assertEqual(job.metadata["seed"], 42)
        self.assertEqual(job.metadata["idempotency"], "header")
        self.assertRegex(job.metadata["request_hash"], r"^[0-9a-f]{64}$")
        self.assertNotIn("prompt", job.metadata)
        self.assertNotIn("raw_body", job.metadata)

    def test_execution_result_builds_update_kwargs_and_event(self) -> None:
        asset = video_media_job_http.completed_asset_from_stored_video(
            filename="clip.mp4",
            asset_id="vid_abc",
            content_url="http://testserver/v1/assets/vid_abc/content?expires=1&sig=abc",
        )
        result = video_media_job_http.VideoExecutionResult(
            status=media_jobs.STATUS_SUCCEEDED,
            progress=100,
            assets=[asset],
            actual_seconds=4.95678,
            phase="complete",
        )

        update = video_media_job_http.update_from_execution_result(result)
        event = video_media_job_http.event_from_execution_result(result)

        self.assertEqual(update["status"], media_jobs.STATUS_SUCCEEDED)
        self.assertEqual(update["progress"], 100)
        self.assertEqual(update["add_assets"], [asset])
        self.assertEqual(update["metadata_patch"], {"actual_seconds": 4.9568})
        self.assertEqual(
            event,
            {
                "status": media_jobs.STATUS_SUCCEEDED,
                "progress": 100,
                "phase": "complete",
                "asset_count": 1,
            },
        )

    def test_generation_and_content_responses_from_completed_job(self) -> None:
        job = media_jobs.MediaJob(
            intent=video_media_jobs.INTENT_VIDEO_GENERATE,
            workflow="video-ltx-2.3-distilled",
            status=media_jobs.STATUS_SUCCEEDED,
            progress=100,
            assets=[
                video_media_job_http.completed_asset_from_stored_video(
                    filename="clip.mp4",
                    asset_id="vid_abc",
                    content_url="http://testserver/v1/assets/vid_abc/content?expires=1&sig=abc",
                )
            ],
            metadata={
                "model": "sora-local",
                "size": "1024x576",
                "seconds": 6.0,
            },
        ).to_dict("internal")

        generation = video_media_job_http.generation_response_from_job(
            job,
            public_id="video_abc",
        )
        content = video_media_job_http.content_response_from_job(job)

        self.assertEqual(generation["id"], "video_abc")
        self.assertEqual(generation["object"], "video")
        self.assertEqual(generation["azizam_object"], "video.generation")
        self.assertEqual(generation["status"], "completed")
        self.assertEqual(generation["azizam_status"], "succeeded")
        self.assertEqual(
            generation["content_url"],
            "http://testserver/v1/assets/vid_abc/content?expires=1&sig=abc",
        )
        self.assertNotIn("metadata", generation)
        self.assertEqual(
            content,
            {
                "id": job["job_id"],
                "object": "video.content",
                "media_type": "video/mp4",
                "asset_id": "vid_abc",
                "url": "http://testserver/v1/assets/vid_abc/content?expires=1&sig=abc",
                "filename": "clip.mp4",
            },
        )

    def test_completed_asset_from_stored_video_requires_vid_handle_and_url(self) -> None:
        asset = video_media_job_http.completed_asset_from_stored_video(
            filename="outputs/run/clip.webm",
            asset_id="vid_def",
            content_url="https://gateway.example/v1/assets/vid_def/content?expires=2&sig=def",
        )

        self.assertEqual(
            asset,
            {
                "kind": "video",
                "filename": "clip.webm",
                "media_type": "video/webm",
                "asset_id": "vid_def",
                "content_url": "https://gateway.example/v1/assets/vid_def/content?expires=2&sig=def",
            },
        )
        with self.assertRaises(ValueError):
            video_media_job_http.completed_asset_from_stored_video(
                filename="clip.mp4",
                asset_id="img_not_video",
                content_url="https://gateway.example/v1/assets/img_not_video/content?expires=2&sig=def",
            )
        with self.assertRaises(ValueError):
            video_media_job_http.completed_asset_from_stored_video(
                filename="clip.mp4",
                asset_id="vid_missing_url",
                content_url="",
            )

    def test_content_response_requires_succeeded_job_with_asset(self) -> None:
        queued = media_jobs.MediaJob(
            intent=video_media_jobs.INTENT_VIDEO_GENERATE,
        ).to_dict("internal")

        with self.assertRaises(ValueError):
            video_media_job_http.content_response_from_job(queued)

        missing_url = media_jobs.MediaJob(
            intent=video_media_jobs.INTENT_VIDEO_GENERATE,
            status=media_jobs.STATUS_SUCCEEDED,
            assets=[
                video_media_jobs.normalize_video_asset(
                    filename="clip.mp4",
                    asset_id="vid_missing_url",
                )
            ],
        ).to_dict("internal")

        with self.assertRaises(ValueError):
            video_media_job_http.content_response_from_job(missing_url)


if __name__ == "__main__":
    unittest.main()
