"""The long-video cap and per-model segment limits are published, not just enforced."""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import proxy
from gateway.video_limits import limits_for_workflow, video_limits
from gateway.workflow_openapi import ROOT_EXTENSION

VIDEOS = {
    "max_total_seconds": 40,
    "chain_segment_seconds": 8,
    "workflows": {
        "video-a-t2v": {"chain_workflow": "video-a-long", "min_seconds": 1, "max_seconds": 10, "frame_rate": 24, "max_size": "1280x704"},
        "video-a-long": {"builder": "ltx25_chain", "max_seconds": 40, "default_size": "960x512"},
        "video-b-long": {"builder": "h3_chain", "chain_segment_seconds": 10},
        "video-plain": {"max_seconds": 5},
        "video-dangling": {"chain_workflow": "video-missing"},
    },
}


class VideoLimitsTests(unittest.TestCase):
    def test_chain_capable_workflows_publish_the_total_cap_and_their_segment_length(self) -> None:
        limits = video_limits(VIDEOS)
        self.assertEqual(limits["video-a-long"]["max_total_seconds"], 40)
        self.assertEqual(limits["video-a-long"]["segment_seconds"], 8)
        self.assertEqual(limits["video-b-long"]["segment_seconds"], 10)  # per-workflow override
        self.assertEqual(limits["video-a-t2v"]["chain_workflow"], "video-a-long")
        self.assertEqual(limits["video-a-t2v"]["max_total_seconds"], 40)
        self.assertEqual((limits["video-a-t2v"]["min_seconds"], limits["video-a-t2v"]["max_seconds"]), (1, 10))

    def test_a_chain_workflow_does_not_publish_its_template_second_range_as_native_length(self) -> None:
        limits = video_limits(VIDEOS)["video-a-long"]
        self.assertNotIn("max_seconds", limits)

    def test_plain_and_dangling_workflows_have_no_cap(self) -> None:
        limits = video_limits(VIDEOS)
        self.assertNotIn("max_total_seconds", limits["video-plain"])
        self.assertEqual(limits["video-plain"]["max_seconds"], 5)
        self.assertNotIn("max_total_seconds", limits["video-dangling"])

    def test_cap_defaults_to_the_enforced_default_and_follows_config(self) -> None:
        self.assertEqual(video_limits({"workflows": {"w": {"builder": "x"}}})["w"]["max_total_seconds"], 40)
        self.assertEqual(video_limits({"max_total_seconds": 25.5, "workflows": {"w": {"builder": "x"}}})["w"]["max_total_seconds"], 25.5)
        self.assertEqual(video_limits(None), {})

    def test_lookup_accepts_config_keys_and_registry_ids(self) -> None:
        published = video_limits(VIDEOS)
        self.assertIs(limits_for_workflow(published, "video-a-long"), published["video-a-long"])
        self.assertIs(limits_for_workflow(published, "video.generate.a-long"), published["video-a-long"])
        self.assertEqual(limits_for_workflow(published, "image.generate.x"), {})
        self.assertEqual(limits_for_workflow(published, None), {})

    def test_the_published_cap_is_the_enforced_cap(self) -> None:
        self.assertEqual(video_limits(proxy.config["videos"])["video-ltx-2.5-long"]["max_total_seconds"], proxy.VIDEO_MAX_TOTAL_SECONDS)


class VideoLimitsDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = copy.deepcopy(proxy.config)
        cfg["videos"]["max_total_seconds"] = 33
        for patcher in (
            patch.object(proxy, "config", cfg),
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy.app, "openapi_schema", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(proxy.app)

    def test_capabilities_carry_the_cap_and_segment_limits(self) -> None:
        rows = self.client.get("/gateway/v1/capabilities").json()["capabilities"]
        by_workflow = {r["workflow"]: r for r in rows if r.get("intent") == "video.generate" and r.get("workflow")}
        long = by_workflow["video-ltx-2.5-long"]["limits"]
        self.assertEqual(long["max_total_seconds"], 33)
        self.assertEqual(long["segment_seconds"], 8)
        self.assertEqual(by_workflow["video-minimax-h3-long"]["limits"]["segment_seconds"], 10)
        # The registry-derived rows (workflow = registry id) carry them too.
        self.assertEqual(by_workflow["video.generate.ltx-2.5-long"]["limits"]["max_total_seconds"], 33)
        # A model that cannot chain publishes no cap.
        self.assertNotIn("max_total_seconds", by_workflow["video-wan-2.2-ti2v-5b-q8"]["limits"])

    def test_workflow_catalog_carries_the_same_limits(self) -> None:
        catalog = {w["id"]: w for w in self.client.get("/openapi.json").json()[ROOT_EXTENSION]}
        self.assertEqual(catalog["video.generate.ltx-2.5-long"]["limits"]["max_total_seconds"], 33)
        self.assertEqual(catalog["video.generate.minimax-h3-long"]["limits"]["segment_seconds"], 10)
        self.assertEqual(catalog["video.generate.ltx-2.5-t2v"]["limits"]["chain_workflow"], "video-ltx-2.5-long")
        self.assertTrue(all("limits" not in w for w in catalog.values() if not w["intent"].startswith("video.")))


if __name__ == "__main__":
    unittest.main()
