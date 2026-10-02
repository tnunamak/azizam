"""LTX-2.5 and MiniMax H3 video workflows: sizing, frame rules, I2V routing.

These tests drive POST /v1/videos against a fake ComfyUI and inspect the graph
the gateway submits. They use the real ``proxy_config.yml`` and workflow JSON,
so they also pin the shipped defaults.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class SizeAndLengthRuleTests(unittest.TestCase):
    def test_default_size_rule_is_unchanged(self) -> None:
        self.assertEqual(proxy._parse_size_clamped("1280x720", "1280x720"), (1280, 704))
        self.assertEqual(proxy._parse_size_clamped("1000x600", "1280x720"), (992, 576))

    def test_size_multiple_64_floors_and_clamps_to_max(self) -> None:
        self.assertEqual(proxy._parse_size_clamped("1280x720", "1280x704", 64), (1280, 704))
        self.assertEqual(proxy._parse_size_clamped("1000x600", "1280x704", 64), (960, 576))
        self.assertEqual(proxy._parse_size_clamped("1920x1080", "1280x704", 64), (1280, 704))
        self.assertEqual(proxy._parse_size_clamped("960x544", "1280x704", 64), (960, 512))
        self.assertEqual(proxy._parse_size_clamped("10x10", "1280x704", 64), (64, 64))

    def test_length_rule_ltx_is_8k_plus_1(self) -> None:
        self.assertEqual(proxy._seconds_to_length(5, 24), 121)

    def test_length_rule_h3_is_17k_plus_5(self) -> None:
        self.assertEqual(proxy._seconds_to_length(5, 24, step=17, offset=5), 124)
        for seconds in (2, 3, 4, 6, 8, 10):
            length = proxy._seconds_to_length(seconds, 24, step=17, offset=5)
            self.assertEqual((length - 5) % 17, 0, (seconds, length))


class ModelDiscoveryTests(unittest.TestCase):
    def test_new_media_models_are_listed_with_honest_descriptions(self) -> None:
        by_id = {e["id"]: e for e in proxy._build_workflow_model_entries()}
        for model_id in (
            "video-ltx-2.5-t2v", "video-ltx-2.5-i2v", "video-minimax-h3-t2v",
            "video-minimax-h3-i2v", "yue2-song", "sa3-small-sfx",
            "video-ltx-2.5-long", "tangoflux",
        ):
            self.assertIn(model_id, by_id)
        self.assertIn("US, EU, UK", by_id["video-minimax-h3-t2v"]["description"])
        self.assertIn("US, EU, UK", by_id["video-minimax-h3-i2v"]["description"])
        self.assertIn("CC BY-NC", by_id["yue2-song"]["description"])
        self.assertIn("MAXIMUM", by_id["yue2-song"]["description"])
        self.assertEqual(by_id["yue2-song"]["vivid_fish_intent"], "audio.generate")
        self.assertEqual(by_id["video-ltx-2.5-t2v"]["vivid_fish_intent"], "video.generate")

    def test_every_listed_media_id_resolves_on_its_route(self) -> None:
        for entry in proxy._build_workflow_model_entries():
            if entry["vivid_fish_intent"] == "video.generate":
                self.assertIsNotNone(proxy._resolve_video_workflow(entry["id"])[1], entry["id"])
            elif entry["vivid_fish_intent"] == "audio.generate":
                self.assertIsNotNone(proxy._resolve_audio_workflow(entry["id"])[1], entry["id"])


class _FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class _JsonRequest:
    headers = {"content-type": "application/json"}

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


class VideoCreateGraphTests(unittest.IsolatedAsyncioTestCase):
    async def _create(self, body: dict) -> tuple[int, dict, dict | None]:
        submitted: list[dict] = []

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, **kwargs):
                if url.endswith("/upload/image"):
                    return _FakeResponse(200, {"name": "uploaded-ref.png"})
                if url.endswith("/prompt"):
                    submitted.append(kwargs["json"]["prompt"])
                    return _FakeResponse(200, {"prompt_id": "11111111-2222-3333-4444-555555555555"})
                raise AssertionError(f"unexpected POST {url}")

        with patch.object(proxy.httpx, "AsyncClient", FakeClient), patch.object(
            proxy, "_service_availability_response", return_value=None
        ):
            response = await proxy.proxy_comfy_video_create(
                _JsonRequest(body), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
            )
        payload = json.loads(response.body)
        return response.status_code, payload, (submitted[0] if submitted else None)

    def test_ltx25_is_the_default_video_model(self) -> None:
        self.assertEqual(proxy._resolve_video_workflow("")[0], "video-ltx-2.5-t2v")
        self.assertEqual(proxy._resolve_video_workflow("default")[0], "video-ltx-2.5-t2v")
        self.assertEqual(proxy._resolve_video_workflow("sora-2")[0], "video-ltx-2.5-t2v")
        self.assertEqual(proxy._resolve_video_workflow("ltx-2.5")[0], "video-ltx-2.5-t2v")
        self.assertEqual(proxy._resolve_video_workflow("ltx-2.5-i2v")[0], "video-ltx-2.5-i2v")
        self.assertEqual(proxy._resolve_video_workflow("minimax-h3")[0], "video-minimax-h3-t2v")
        self.assertEqual(proxy._resolve_video_workflow("minimax-h3-i2v")[0], "video-minimax-h3-i2v")

    def test_retired_ltx23_names_resolve_to_ltx25(self) -> None:
        # LTX-2.3 was retired; every old name must keep working by pointing at
        # the LTX-2.5 workflow for its role. (name, expected workflow)
        expected = {
            "ltx-2.3": "video-ltx-2.5-t2v",
            "ltx-2.3-distilled": "video-ltx-2.5-t2v",
            "ltx-2.3-fast": "video-ltx-2.5-t2v",
            "ltx-2.3-distilled-fast": "video-ltx-2.5-t2v",
            "ltx-2.3-pure-distilled": "video-ltx-2.5-t2v",
            "ltx-2.3-22b": "video-ltx-2.5-t2v",
            "ltx-2.3-detail": "video-ltx-2.5-t2v",
            "video-ltx-2.3-distilled": "video-ltx-2.5-t2v",
            "video-ltx-2.3-pure-distilled": "video-ltx-2.5-t2v",
            "video-ltx-2.3-detail": "video-ltx-2.5-t2v",
            "ltx-2.3-long": "video-ltx-2.5-long",
            "ltx-2.3-long-form": "video-ltx-2.5-long",
            "video-ltx-2.3-long": "video-ltx-2.5-long",
            "ltx-2.5-long": "video-ltx-2.5-long",
            "video-fast": "video-ltx-2.5-t2v",
            "video-long": "video-ltx-2.5-long",
        }
        for name, workflow in expected.items():
            self.assertEqual(proxy._resolve_video_workflow(name)[0], workflow, name)

    def test_no_ltx23_workflow_remains_in_config(self) -> None:
        workflows = proxy.config["videos"]["workflows"]
        self.assertEqual([w for w in workflows if "ltx-2.3" in w], [])

    def test_long_workflow_is_built_in_code(self) -> None:
        cfg = proxy.config["videos"]["workflows"]["video-ltx-2.5-long"]
        self.assertEqual(cfg["builder"], "ltx25_chain")
        self.assertNotIn("file", cfg)

    def test_manifest_reference_graph_matches_the_builder(self) -> None:
        """workflows/videos/video-ltx-2.5-long.json is the one-segment graph the
        manifest binds. It must not drift from gateway/video_extend.py."""
        from gateway import video_extend
        stored = json.loads((ROOT / "workflows/videos/video-ltx-2.5-long.json").read_text())
        stored.pop("_meta")
        spec = video_extend.ChainSpec(
            [video_extend.Segment("A cinematic shot of a serene scene with natural motion.", 193, 0)],
            960, 512, 24.0, 42)
        self.assertEqual(stored, video_extend.build_chain_graph(spec).graph)

    async def test_default_model_t2v_sets_64_multiple_size_and_frames(self) -> None:
        status, payload, graph = await self._create(
            {"prompt": "a red kite", "size": "1280x720", "seconds": 5, "seed": 7}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], "video-ltx-2.5-t2v")
        self.assertEqual(payload["size"], "1280x704")
        self.assertEqual(graph["5"]["inputs"]["text"], "a red kite")
        self.assertEqual(graph["20"]["inputs"]["value"], 1280)
        self.assertEqual(graph["21"]["inputs"]["value"], 704)
        self.assertEqual(graph["22"]["inputs"]["value"], 121)
        self.assertEqual(graph["30"]["inputs"]["noise_seed"], 7)

    async def test_odd_size_floors_to_64_multiple(self) -> None:
        status, payload, graph = await self._create(
            {"prompt": "x", "size": "1000x600", "seconds": 2}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["size"], "960x576")
        self.assertEqual(graph["20"]["inputs"]["value"] % 64, 0)
        self.assertEqual(graph["21"]["inputs"]["value"] % 64, 0)

    async def test_omitted_size_uses_workflow_default(self) -> None:
        status, payload, _graph = await self._create({"prompt": "x", "seconds": 2})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["size"], "960x512")

    async def test_input_reference_switches_default_to_i2v_workflow(self) -> None:
        status, payload, graph = await self._create(
            {"prompt": "x", "seconds": 2, "input_reference": "data:image/png;base64,"
             + __import__("base64").b64encode(PNG).decode()}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], "video-ltx-2.5-t2v")
        self.assertEqual(payload["vivid_fish_resolved_model"], "video-ltx-2.5-i2v")
        self.assertEqual(graph["70"]["inputs"]["image"], "uploaded-ref.png")
        self.assertEqual(graph["79"]["inputs"]["value"], 0.7)

    async def test_explicit_image_strength_is_honored(self) -> None:
        status, payload, graph = await self._create(
            {"prompt": "x", "seconds": 2, "image_strength": 1.0, "input_reference":
             "data:image/png;base64," + __import__("base64").b64encode(PNG).decode()}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(graph["79"]["inputs"]["value"], 1.0)

    async def test_i2v_model_without_input_reference_is_rejected(self) -> None:
        for model in ("video-ltx-2.5-i2v", "video-minimax-h3-i2v"):
            status, payload, graph = await self._create({"model": model, "prompt": "x", "seconds": 4})
            self.assertEqual(status, 400, (model, payload))
            self.assertIn("input_reference", payload["error"]["message"])
            self.assertIsNone(graph)

    async def test_h3_t2v_uses_prompt_field_and_17k_plus_5_frames(self) -> None:
        status, payload, graph = await self._create(
            {"model": "minimax-h3", "prompt": "a cat walks\n\nAudio: footsteps", "seconds": 5, "seed": 3}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], "minimax-h3")
        self.assertEqual(payload["vivid_fish_resolved_model"], "video-minimax-h3-t2v")
        self.assertEqual(payload["size"], "960x544")
        self.assertEqual(graph["9"]["inputs"]["prompt"], "a cat walks\n\nAudio: footsteps")
        self.assertNotIn("text", graph["9"]["inputs"])
        self.assertEqual(graph["8"]["inputs"]["value"], 124)
        self.assertEqual(graph["6"]["inputs"]["value"], 960)
        self.assertEqual(graph["7"]["inputs"]["value"], 544)
        self.assertEqual(graph["10"]["inputs"]["noise_seed"], 3)

    async def test_h3_input_reference_switches_to_i2v_without_strength_node(self) -> None:
        status, payload, graph = await self._create(
            {"model": "minimax-h3", "prompt": "x", "seconds": 4, "input_reference":
             "data:image/png;base64," + __import__("base64").b64encode(PNG).decode()}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], "minimax-h3")
        self.assertEqual(payload["vivid_fish_resolved_model"], "video-minimax-h3-i2v")
        self.assertEqual(graph["20"]["inputs"]["image"], "uploaded-ref.png")
        self.assertEqual(graph["9"]["inputs"]["prompt"], "x")

    async def test_retired_ltx23_name_with_input_reference_uses_ltx25_i2v(self) -> None:
        status, payload, graph = await self._create(
            {"model": "ltx-2.3", "prompt": "x", "seconds": 2, "input_reference":
             "data:image/png;base64," + __import__("base64").b64encode(PNG).decode()}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], "ltx-2.3")
        self.assertEqual(payload["vivid_fish_resolved_model"], "video-ltx-2.5-i2v")
        self.assertEqual(graph["70"]["inputs"]["image"], "uploaded-ref.png")

    async def test_long_workflow_builds_a_segment_chain(self) -> None:
        status, payload, graph = await self._create(
            {"model": "video-long", "prompt": "a long walk", "seconds": 32, "seed": 5}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], "video-long")
        self.assertEqual(payload["vivid_fish_resolved_model"], "video-ltx-2.5-long")
        self.assertEqual(graph["s0_text"]["inputs"]["text"], "a long walk")
        self.assertEqual(graph["s0_noise"]["inputs"]["noise_seed"], 5)
        self.assertEqual(graph["s1_noise"]["inputs"]["noise_seed"], 6)
        # 32 s = a first 8 s segment and three added segments.
        self.assertIn("s3_sample", graph)
        self.assertNotIn("s4_sample", graph)
        self.assertEqual(payload["segments"], 4)
        self.assertAlmostEqual(payload["total_seconds"], 32, delta=0.4)
        self.assertNotIn("s0_i2v", graph)

    async def test_long_workflow_i2v_pins_first_frame(self) -> None:
        status, payload, graph = await self._create(
            {"model": "ltx-2.3-long", "prompt": "x", "seconds": 12, "image_strength": 1.0,
             "input_reference": "data:image/png;base64," + __import__("base64").b64encode(PNG).decode()}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], "ltx-2.3-long")
        self.assertEqual(payload["vivid_fish_resolved_model"], "video-ltx-2.5-long")
        self.assertEqual(graph["s0_image"]["inputs"]["image"], "uploaded-ref.png")
        self.assertEqual(graph["s0_i2v"]["inputs"]["strength"], 1.0)


if __name__ == "__main__":
    unittest.main()
