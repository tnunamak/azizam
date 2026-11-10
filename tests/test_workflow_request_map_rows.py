"""The request-map rows: the loader refuses malformed rows, every bundled
workflow has a row in both modes, every row is used, and what
``gateway.workflow_openapi.request_mapping`` serves from the rows is frozen,
in order, in a golden file captured before the rows replaced the route code.
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway import workflow_request_maps  # noqa: E402
from gateway.discovery import WorkflowRecord, WorkflowRegistry  # noqa: E402
from gateway.manifests import ManifestIdentity  # noqa: E402
from gateway.workflow_openapi import request_mapping  # noqa: E402

MANIFESTS = ROOT / "manifests" / "workflows"
GOLDEN = ROOT / "tests" / "fixtures" / "workflow_request_maps" / "request_mapping_golden.json"
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


def test_refuses_two_inputs_keys_in_one_row(tmp_path: Path) -> None:
    path = tmp_path / "workflow_request_maps.yml"
    path.write_text(
        "schema_version: 1\n"
        "maps:\n"
        "  - select: {any: true}\n"
        "    inputs:\n"
        "      prompt: {in: body}\n"
        "    inputs:\n"
        "      seed: {in: extension}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as caught:
        workflow_request_maps.load(path)
    message = str(caught.value)
    assert str(path) in message
    assert "not valid YAML" in message
    assert "duplicate key \'inputs\'" in message


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


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_refuses_a_schema_version_that_only_compares_equal_to_1(tmp_path: Path, version: object) -> None:
    path = tmp_path / "workflow_request_maps.yml"
    path.write_text(yaml.safe_dump({"schema_version": version, "maps": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version must be 1"):
        workflow_request_maps.load(path)


# ---------------------------------------------------------------- equivalence


@lru_cache(maxsize=1)
def registry() -> WorkflowRegistry:
    return WorkflowRegistry.from_directory(MANIFESTS)


@lru_cache(maxsize=1)
def maps() -> workflow_request_maps.RequestMaps:
    return workflow_request_maps.load()


def test_registry_loads_clean() -> None:
    assert registry().errors == []


def test_all_eight_rows_are_present() -> None:
    selectors = [row.selector for row in maps().rows]
    assert selectors == [
        ("native", "sync"),
        ("intent", "mesh.generate", "sync"),
        ("intent", "video.edit", "sync"),
        ("intent", "image.generate", "any"),
        ("intent", "image.edit", "any"),
        ("intent", "audio.generate", "any"),
        ("intent_prefix", "video."),
        ("any",),
    ]


def test_every_bundled_record_has_a_row_in_both_modes() -> None:
    missing = [
        (record.id, mode)
        for record in registry().records()
        for mode in MODES
        if maps().select(record, mode) is None
    ]
    assert missing == []


def test_video_edit_sync_picks_its_exact_row_and_async_the_prefix_row() -> None:
    edit = next(record for record in registry().records() if record.intent == "video.edit")
    sync_row = maps().select(edit, "sync")
    async_row = maps().select(edit, "async")
    assert sync_row is not None
    assert sync_row.selector == ("intent", "video.edit", "sync")
    assert async_row is not None
    assert async_row.selector == ("intent_prefix", "video.")


def synthetic_record(manifest: dict) -> WorkflowRecord:
    return WorkflowRecord(
        manifest=manifest,
        identity=ManifestIdentity(
            id=manifest["id"], version=None, manifest_hash="", trusted=True, source="test"
        ),
        source_path=Path("synthetic.yml"),
    )


def test_a_second_route_image_keeps_the_repeated_image_part_order() -> None:
    record = synthetic_record(
        {
            "id": "image.edit.two-references",
            "intent": "image.edit",
            "inputs": {
                "source_image": {"type": "asset"},
                "reference_image": {"type": "asset"},
                "prompt": {"type": "string"},
                "steps": {"type": "integer"},
            },
            "bindings": {
                "comfy": {
                    "route_bound_inputs": ["source_image", "reference_image"],
                    "map": {"prompt": "prompt"},
                }
            },
        }
    )
    row = maps().select(record, "sync")
    assert row is not None
    assert row.selector == ("intent", "image.edit", "any")
    placed = list(maps().place(row, record, "sync").items())
    assert placed == list(request_mapping(record, "sync").items())
    assert [name for name, _ in placed] == ["source_image", "reference_image", "prompt", "steps"]
    assert placed[0][1] == {"in": "file", "field": "image[]"}
    assert placed[1][1] == {"in": "file", "field": "image[]"}


@pytest.mark.parametrize("name", ["prompt", "mask", "width", "height"])
def test_a_route_image_with_a_named_input_name_is_still_a_repeated_image_part(name: str) -> None:
    """The deleted route code placed every route picture at image[] before the named inputs (review 2478)."""
    record = synthetic_record(
        {
            "id": "image.edit.named-reference",
            "intent": "image.edit",
            "inputs": {
                "source_image": {"type": "asset"},
                name: {"type": "asset"},
                "steps": {"type": "integer"},
            },
            "bindings": {"comfy": {"route_bound_inputs": ["source_image", name], "map": {}}},
        }
    )
    for mode in MODES:
        placed = list(request_mapping(record, mode).items())
        assert placed == [
            ("source_image", {"in": "file", "field": "image[]"}),
            (name, {"in": "file", "field": "image[]"}),
            ("steps", {"in": "extension", "field": "steps"}),
        ], mode


def test_request_mapping_matches_the_frozen_golden_file() -> None:
    """The public mapping, container post-step included, is frozen for every bundled workflow and mode."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    live = {
        f"{record.id} {mode}": [
            [name, placement] for name, placement in request_mapping(record, mode).items()
        ]
        for record in registry().records()
        for mode in MODES
    }
    assert sorted(live) == sorted(golden)
    for key, expected in golden.items():
        assert live[key] == expected, f"{key}: request_mapping changed since the golden capture"


def test_every_row_is_chosen_by_a_bundled_record() -> None:
    chosen: dict[int, list[tuple[str, str]]] = {}
    for record in registry().records():
        for mode in MODES:
            row = maps().select(record, mode)
            if row is None:
                continue
            chosen.setdefault(row.index, []).append((record.id, mode))
    assert sorted(chosen) == list(range(1, len(maps().rows) + 1)), (
        f"every bundled row is chosen by a bundled record and mode; chosen: {chosen}"
    )
