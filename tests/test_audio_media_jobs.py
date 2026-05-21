"""Tests for durable audio media-job shaping helpers."""

from __future__ import annotations

import base64
import hashlib
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import audio_media_jobs  # noqa: E402


class TestAudioMediaJobs(unittest.TestCase):
    def test_request_builds_safe_job_without_prompt_or_lyrics(self) -> None:
        job = audio_media_jobs.audio_job_from_request(
            {
                "model": "tangoflux",
                "prompt": "private prompt",
                "lyrics": "private lyrics",
                "type": "music",
                "duration": 8,
                "response_format": "mp3",
                "tags": ["cinematic", "private-ish"],
                "user": "customer-123",
            },
            request_id="req_audio_1",
            workflow="tangoflux",
            backend="comfyui.local",
        )

        self.assertEqual(job.intent, "music.generate")
        self.assertEqual(job.request_id, "req_audio_1")
        self.assertEqual(job.workflow, "tangoflux")
        self.assertEqual(job.metadata["model"], "tangoflux")
        self.assertEqual(job.metadata["duration"], 8)
        self.assertEqual(job.metadata["response_format"], "mp3")
        self.assertEqual(job.metadata["tag_count"], 2)
        self.assertNotIn("prompt", job.metadata)
        self.assertNotIn("lyrics", job.metadata)
        self.assertNotIn("user", job.metadata)
        self.assertNotIn("seed", job.metadata)

    def test_extracts_b64_audio_data_items(self) -> None:
        raw = b"RIFF....WAVE"
        result = {
            "data": [
                {
                    "b64_json": base64.b64encode(raw).decode("ascii"),
                    "media_type": "audio/wav",
                }
            ]
        }

        artifacts = audio_media_jobs.audio_artifacts_from_result(result)

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].content, raw)
        self.assertEqual(artifacts[0].media_type, "audio/wav")
        self.assertEqual(artifacts[0].suffix, ".wav")

    def test_stores_audio_artifacts_with_asset_contract(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_store_asset(
            content: bytes,
            *,
            kind: str,
            suffix: str,
            owner_key_hash: str | None = None,
        ) -> str:
            calls.append(
                {
                    "content": content,
                    "kind": kind,
                    "suffix": suffix,
                    "owner_key_hash": owner_key_hash,
                }
            )
            return f"{kind}_{hashlib.sha256(content).hexdigest()}"

        assets = audio_media_jobs.store_audio_artifacts(
            [
                audio_media_jobs.AudioArtifact(
                    content=b"audio bytes",
                    media_type="audio/mpeg",
                    suffix=".mp3",
                )
            ],
            store_asset=fake_store_asset,
            owner_key_hash="owner_1",
        )

        self.assertEqual(calls[0]["kind"], "aud")
        self.assertEqual(calls[0]["suffix"], ".mp3")
        self.assertEqual(calls[0]["owner_key_hash"], "owner_1")
        self.assertEqual(assets[0]["asset_id"], f"aud_{hashlib.sha256(b'audio bytes').hexdigest()}")
        self.assertEqual(assets[0]["kind"], "audio")
        self.assertEqual(assets[0]["media_type"], "audio/mpeg")
        self.assertEqual(assets[0]["size_bytes"], len(b"audio bytes"))

    def test_completion_event_payload_is_metadata_only(self) -> None:
        payload = audio_media_jobs.complete_audio_job_payload(
            [
                {
                    "asset_id": "aud_" + "a" * 64,
                    "media_type": "audio/wav",
                    "size_bytes": 12,
                },
                {
                    "asset_id": "aud_" + "b" * 64,
                    "media_type": "audio/mpeg",
                    "size_bytes": 10,
                },
            ]
        )

        self.assertEqual(payload["phase"], "completed")
        self.assertEqual(payload["asset_count"], 2)
        self.assertEqual(payload["media_types"], ["audio/mpeg", "audio/wav"])
        self.assertNotIn("content", payload)
        self.assertNotIn("bytes", payload)


if __name__ == "__main__":
    unittest.main()
