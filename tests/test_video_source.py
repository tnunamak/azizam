"""``resolve_source`` reads what a stored clip is from the file, not from what its request asked for (review item 7)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway import video_source
from gateway.workflow_runtime_types import ResultRef, RunJobView


def _view(status: str = "succeeded", asset: str | None = "asset-1") -> RunJobView:
    results = (ResultRef(output="video", index=0, media_type="video/mp4", asset_id=asset),) if asset else ()
    return RunJobView(job_id="mj_a", status=status, disposition="replay", workflow_id="w", record_revision=1, binding_key="b",
                      created_at_s=1, updated_at_s=2, results=results)


class Jobs:
    def __init__(self, view: RunJobView | None, asked: dict | None = None) -> None:
        self.view, self.asked = view, asked or {}

    def get(self, job_id, caller): return self.view
    def request_facts(self, job_id, caller): return SimpleNamespace(facts=self.asked)
    def open_result(self, job_id, ref, caller): return SimpleNamespace(body=b"clip")


def _resolve(jobs: Jobs, probe=lambda body: {}):
    return asyncio.run(video_source.resolve_source("mj_a", "video_mj_a", "caller", jobs, probe=probe))


def test_the_properties_of_a_source_come_from_the_probe_not_from_the_request_facts() -> None:
    asked = {"model": "m", "resolved_model": "video-m", "frame_rate": 24, "size": "640x384", "actual_seconds": 5.0}
    found = _resolve(Jobs(_view(), asked), probe=lambda body: {"fps": 30.0, "width": 1280, "height": 704, "seconds": 4.2, "has_audio": True})
    assert (found.fps, found.size, found.seconds, found.has_audio) == (30.0, "1280x704", 4.2, True)
    assert (found.model, found.resolved_model, found.asset_id, found.wire_id) == ("m", "video-m", "asset-1", "video_mj_a")


def test_a_clip_that_cannot_be_probed_has_unknown_properties_never_the_requested_ones() -> None:
    found = _resolve(Jobs(_view(), {"frame_rate": 24, "size": "640x384"}))
    assert (found.fps, found.size, found.seconds, found.has_audio) == (None, None, None, None)


def test_an_unknown_unfinished_or_clipless_job_is_refused() -> None:
    assert _resolve(Jobs(None)).http_status == 404
    unfinished = _resolve(Jobs(_view("running")))
    assert (unfinished.http_status, unfinished.message, unfinished.code, unfinished.param) == (
        400, "Source video is not completed (status=in_progress)", "video_not_ready", "video")      # the old loader's words
    assert _resolve(Jobs(_view(asset=None))).http_status == 404
