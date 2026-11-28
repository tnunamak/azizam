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


def entry_video_length(entry: dict) -> dict:
    """The clip-length bounds the workflow's own manifest declares."""
    record = next(r for r in proxy.GATEWAY_WORKFLOW_REGISTRY.records() if r.id == entry["id"])
    return record.contract["video"]["length"]


def test_text_to_video_keeps_seconds_and_size_on_the_videos_route() -> None:
    (entry,) = [e for e in ENTRIES if e["id"] == "video.generate.ltx-2.5-t2v"]
    route = _chosen_route(entry)
    assert _path(route) == "/v1/videos"
    # The manifest lists width and height as inputs, so the route does not advertise its own `size` beside them; they
    # travel as the wire's `size`. The manifest's `length` counts frames, which the route does not take: it is not
    # advertised, and the route's own `seconds` (bounded by the manifest's video.length) is.
    schema = effective_schema(entry)["properties"]
    advertised = set(schema)
    assert {"width", "height", "seconds"} <= advertised and "length" not in advertised and "size" not in advertised
    assert {route["fields"][name]["field"] for name in ("width", "height")} == {"size"}
    assert route["fields"]["length"] == {"in": "none"}
    assert schema["seconds"]["minimum"] == entry_video_length(entry)["min_seconds"]
    assert schema["seconds"]["maximum"] == entry_video_length(entry)["max_seconds"]
