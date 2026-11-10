"""The request-map rows: the loader refuses malformed rows, and where a row
claims a record it reproduces ``gateway.workflow_openapi._route_mapping`` exactly.

Jobs B and C add the image, audio and video rows. This file asserts that every
row the file holds is chosen and correct; it does not assert that every record
has a row yet.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway import workflow_request_maps  # noqa: E402
from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.workflow_openapi import _route_mapping  # noqa: E402

MANIFESTS = ROOT / "manifests" / "workflows"
MODES = ("sync", "async")


def write_maps(tmp_path: Path, maps: list) -> Path:
    """Write a candidate registry file; only ``tmp_path`` ever holds a bad one."""
    path = tmp_path / "workflow_request_maps.yml"
    path.write_text(
        yaml.safe_dump({"schema_version": 1, "maps": maps}, sort_keys=False),
        encoding="utf-8",
    )
    return path


def refusal(tmp_path: Path, maps: list) -> str:
    """Load a bad file and return the refusal, which must name the file."""
    path = write_maps(tmp_path, maps)
    with pytest.raises(ValueError) as caught:
        workflow_request_maps.load(path)
    message = str(caught.value)
    assert str(path) in message
    return message


# ------------------------------------------------------------------- refusals


def test_refuses_a_row_key_it_does_not_know(tmp_path: Path) -> None:
    message = refusal(
        tmp_path,
        [{"select": {"any": True}, "styles": {"in": "body"}}],
    )
    assert "row 1" in message
    assert "styles" in message


def test_refuses_an_in_word_it_does_not_know(tmp_path: Path) -> None:
    message = refusal(
        tmp_path,
        [{"select": {"any": True}, "default": {"in": "query"}}],
    )
    assert "row 1" in message
    assert "query" in message


def test_refuses_a_first_set_word_it_does_not_know(tmp_path: Path) -> None:
    message = refusal(
        tmp_path,
        [{"select": {"any": True}, "first": "image_generate_owned", "default": {"in": "extension"}}],
    )
    assert "row 1" in message
    assert "image_generate_owned" in message


def test_refuses_a_sets_word_it_does_not_know(tmp_path: Path) -> None:
    message = refusal(
        tmp_path,
        [
            {
                "select": {"any": True},
                "sets": [{"word": "route_videos", "placement": {"in": "none"}}],
                "default": {"in": "extension"},
            }
        ],
    )
    assert "row 1" in message
    assert "route_videos" in message


def test_refuses_two_rows_with_the_same_selector(tmp_path: Path) -> None:
    row = {
        "select": {"intent": "image.generate", "mode": "sync"},
        "default": {"in": "extension"},
    }
    message = refusal(tmp_path, [row, dict(row)])
    assert "row 2" in message


def test_refuses_a_row_with_no_select(tmp_path: Path) -> None:
    message = refusal(tmp_path, [{"default": {"in": "extension"}}])
    assert "row 1" in message
    assert "select" in message


# ---------------------------------------------------------------- equivalence


@lru_cache(maxsize=1)
def registry() -> WorkflowRegistry:
    return WorkflowRegistry.from_directory(MANIFESTS)


@lru_cache(maxsize=1)
def maps() -> workflow_request_maps.RequestMaps:
    return workflow_request_maps.load()


def test_registry_loads_clean() -> None:
    assert registry().errors == []


def test_job_a_writes_the_four_rows() -> None:
    selectors = {row.selector for row in maps().rows}
    assert ("native", "sync") in selectors
    assert ("intent", "mesh.generate", "sync") in selectors
    assert ("intent", "video.edit", "sync") in selectors
    assert ("any",) in selectors


def test_where_a_row_is_selected_it_matches_the_route_code() -> None:
    chosen: dict[int, list[tuple[str, str]]] = {}
    for record in registry().records():
        for mode in MODES:
            row = maps().select(record, mode)
            if row is None:
                continue
            chosen.setdefault(row.index, []).append((record.id, mode))
            assert maps().place(row, record, mode) == _route_mapping(record, mode), (
                f"{record.id} ({mode}): request map row {row.index} disagrees with _route_mapping"
            )
    assert sorted(chosen) == list(range(1, len(maps().rows) + 1)), (
        f"every bundled row is chosen by a bundled record and mode; chosen: {chosen}"
    )
