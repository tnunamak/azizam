"""A workflow's advertised inputs are inputs the route that carries the request takes."""

from __future__ import annotations

import pytest

import proxy
from gateway.workflow_call import effective_schema

ENTRIES = proxy._mcp_catalog()
RUN_PREFIX = "/gateway/v1/workflows/"


def _chosen_route(entry: dict) -> dict:
    execution = entry["execution"]
    return execution[execution["recommended"]]


def _path(route: dict) -> str:
    return (route.get("path") or route["submit"]["path"])


RUN_ROUTE_ENTRIES = [e for e in ENTRIES if _path(_chosen_route(e)).startswith(RUN_PREFIX)]


def test_the_bundled_workflows_include_run_route_entries() -> None:
    assert {"video.edit.video-mux"} <= {e["id"] for e in RUN_ROUTE_ENTRIES}


@pytest.mark.parametrize("entry", RUN_ROUTE_ENTRIES, ids=[e["id"] for e in RUN_ROUTE_ENTRIES])
def test_a_run_route_is_advertised_only_the_inputs_its_mapping_carries(entry: dict) -> None:
    route = _chosen_route(entry)
    assert set(effective_schema(entry)["properties"]) <= set(route["fields"])


def test_the_mux_advertises_exactly_its_two_inputs() -> None:
    (entry,) = [e for e in ENTRIES if e["id"] == "video.edit.video-mux"]
    assert set(effective_schema(entry)["properties"]) == {"source_video", "source_audio"}


def test_text_to_video_keeps_seconds_and_size_on_the_videos_route() -> None:
    (entry,) = [e for e in ENTRIES if e["id"] == "video.generate.ltx-2.5-t2v"]
    assert _path(_chosen_route(entry)) == "/v1/videos"
    assert {"seconds", "size"} <= set(effective_schema(entry)["properties"])
