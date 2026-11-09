"""Literal compatibility pins before workflow route words move to registry rows."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.workflow_openapi import (  # noqa: E402
    match_alias_target,
    request_mapping,
    workflow_catalog,
)


def record(intent, *, inputs=None, bindings=None, seconds=31):
    return SimpleNamespace(
        id=f"{intent}.demo",
        intent=intent,
        contract={
            "inputs": inputs if inputs is not None else {},
            "bindings": bindings if bindings is not None else {},
            "typical_seconds": seconds,
        },
    )


def catalog(intent, **kwargs):
    (row,) = workflow_catalog([record(intent, **kwargs)])
    assert row["short_id"] == "demo"
    return row


# All route words and expectations are literals, never production constants.
@pytest.mark.parametrize(
    "intent,sync,submit,recommended",
    [
        ("image.generate", ("/v1/images/generations", "application/json", "demo"),
         ("/gateway/v1/media/jobs", "application/json", "demo"), "async"),
        ("image.edit", ("/v1/images/edits", "multipart/form-data", "demo"), None, "sync"),
        ("video.generate", None, ("/v1/videos", "application/json", "video-demo"), "async"),
        ("video.edit", ("/gateway/v1/workflows/demo/run", "multipart/form-data", None),
         ("/v1/videos", "application/json", "video-demo"), "async"),
        ("audio.generate", ("/v1/audio/generate", "application/json", "demo"),
         ("/gateway/v1/media/jobs", "application/json", "demo"), "async"),
        ("mesh.generate", ("/v1/3d/generations/glb", "multipart/form-data", None),
         ("/v1/3d/generations/glb", "multipart/form-data", None), "async"),
        ("speech.synthesis", None, None, "sync"),
        ("future.unknown", None, None, "sync"),
    ],
)
def test_execution_route_words(intent, sync, submit, recommended):
    row = catalog(intent)
    assert row["typical_seconds"] == 31
    execution = row["execution"]
    assert execution["recommended"] == recommended
    for actual, expected in (
        (execution["sync"], sync),
        (execution["async"]["submit"] if execution["async"] else None, submit),
    ):
        if expected is None:
            assert actual is None
        else:
            path, content_type, model = expected
            assert actual["method"] == "POST"
            assert actual["path"] == path
            assert actual["content_type"] == content_type
            if model is None:
                assert "model" not in actual
            else:
                assert actual["model"] == model


def test_matching_protocol_frontend_rows_keep_the_same_literal_routes():
    registry = yaml.safe_load((ROOT / "registry/protocol_frontends.yml").read_text())
    rows = {row["id"]: row for row in registry["frontends"]}
    # A frontend's mode is its wire route, not the workflow's recommendation.
    for row_id, mode, shape, path in [
        ("images-generations", "sync", "openai.images.json", "/v1/images/generations"),
        ("images-edits", "sync", "openai.images.multipart", "/v1/images/edits"),
        ("audio-generate", "sync", "openai.audio.json", "/v1/audio/generate"),
        ("videos-create", "job", "openai.video.create", "/v1/videos"),
        ("mesh-glb", "sync", "multipart.glb", "/v1/3d/generations/glb"),
    ]:
        row = rows[row_id]
        assert (row["mode"], row["shape"]) == (mode, shape)
        assert [(route["methods"], route["canonical"]) for route in row["routes"]] == [
            (["POST"], path),
        ]
    # Preserve the current differences; do not substitute these for catalog routes.
    assert rows["image-jobs"]["routes"][0]["canonical"] == "/gateway/v1/media/jobs/image-generations"
    assert rows["videos-edit"]["routes"][0]["canonical"] == "/v1/videos/edits"
    gateway = next(row for row in registry["records"] if row["id"] == "gateway")
    assert "/gateway/v1/media/jobs" in gateway["public_discovery"]["endpoints"]
    openai = next(row for row in registry["records"] if row["id"] == "openai")
    speech = [route for route in openai["registered_routes"]
              if route.get("canonical") == "/v1/audio/speech"]
    assert len(speech) == 1
    assert speech[0]["methods"] == ["POST"]
    assert speech[0]["runtime_policy"]["intent"] == "speech.synthesis"
    # The registered speech route exists, but the catalog intentionally pins None above.


def test_image_generate_input_mapping():
    execution = catalog("image.generate", inputs={
        "prompt": {"type": "string"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "source_image": {"type": "asset", "media_type": "image/png"},
        "seed": {"type": "integer"},
    })["execution"]
    expected = {
        "prompt": {"in": "body", "field": "prompt"},
        "width": {"in": "body", "field": "size", "combine": ["width", "height"]},
        "height": {"in": "body", "field": "size", "combine": ["width", "height"]},
        "source_image": {"in": "none"},
        "seed": {"in": "extension", "field": "seed"},
    }
    assert execution["sync"]["fields"] == expected
    assert execution["async"]["fields"] == expected
    assert execution["async"]["submit"]["body"] == {
        "intent": "image.generate", "request": "<the sync request body>",
    }


def edit_record():
    # Source is not first; binding order also differs from manifest order.
    return record("image.edit", inputs={
        "reference_b": {"type": "asset", "media_type": "image/png"},
        "reference_a": {"type": "asset", "media_type": "image/png"},
        "source_image": {"type": "asset", "media_type": "image/png"},
        "mask": {"type": "asset", "media_type": "image/png"},
        "prompt": {"type": "string"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "extra_picture": {"type": "asset", "media_type": "image/png"},
        "source_mask": {"type": "asset", "media_type": "image/png"},
        "mask_asset": {"type": "asset", "media_type": "image/png"},
    }, bindings={"synthetic": {"route_bound_inputs": ["reference_a", "reference_b"]}})


def test_image_edit_input_mapping():
    fields = request_mapping(edit_record(), "sync")
    assert fields == {
        "source_image": {"in": "file", "field": "image[]"},
        "reference_b": {"in": "file", "field": "image[]"},
        "reference_a": {"in": "file", "field": "image[]"},
        "mask": {"in": "file", "field": "mask"},
        "prompt": {"in": "form", "field": "prompt"},
        "width": {"in": "form", "field": "size", "combine": ["width", "height"]},
        "height": {"in": "form", "field": "size", "combine": ["width", "height"]},
        "extra_picture": {"in": "file+extension", "field": "extra_picture"},
        "source_mask": {"in": "none"},
        "mask_asset": {"in": "none"},
    }


def test_image_part_order_is_an_independent_literal():
    fields = request_mapping(edit_record(), "sync")
    assert [name for name, where in fields.items() if where.get("field") == "image[]"] == [
        "source_image", "reference_b", "reference_a",
    ]


@pytest.mark.parametrize("intent", ["video.generate", "video.edit"])
def test_video_async_seconds_and_reference_mapping(intent):
    execution = catalog(intent, inputs={
        "length": {"type": "number"},
        "image": {"type": "asset", "media_type": "image/png"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
    })["execution"]
    assert execution["async"]["fields"] == {
        "length": {"in": "body", "field": "seconds"},
        "image": {"in": "body", "field": "input_reference"},
        "width": {"in": "body", "field": "size", "combine": ["width", "height"]},
        "height": {"in": "body", "field": "size", "combine": ["width", "height"]},
    }
    if intent == "video.edit":
        assert execution["sync"]["fields"] == {
            "length": {"in": "form", "field": "length"},
            "image": {"in": "file", "field": "image"},
            "width": {"in": "form", "field": "width"},
            "height": {"in": "form", "field": "height"},
        }


def test_audio_duration_mapping():
    execution = catalog("audio.generate", inputs={"duration": {"type": "number"}})["execution"]
    expected = {"duration": {"in": "body", "field": "duration"}}
    assert execution["sync"]["fields"] == expected
    assert execution["async"]["fields"] == expected
    assert execution["async"]["submit"]["body"] == {
        "intent": "audio.generate", "request": "<the sync request body>",
    }


def test_mesh_bound_fields_and_async_preference_header():
    execution = catalog("mesh.generate", inputs={
        "image": {"type": "asset", "media_type": "image/png"},
        "seed": {"type": "integer"},
        "extensions": {"type": "object"},
    }, bindings={"synthetic": {"map": {"image": "input_image", "seed": "random_seed"}}})["execution"]
    expected = {
        "image": {"in": "file", "field": "image"},
        "seed": {"in": "form", "field": "seed"},
        "extensions": {"in": "none"},
    }
    assert execution["sync"]["fields"] == expected
    assert execution["async"]["fields"] == expected
    assert execution["async"]["kind"] == "respond_async"
    assert execution["async"]["submit"]["headers"] == {"Prefer": "respond-async"}


@pytest.mark.parametrize("intent", ["speech.synthesis", "future.unknown"])
def test_unrouted_scalar_remains_an_extension(intent):
    assert request_mapping(record(intent, inputs={"text": {"type": "string"}}), "sync") == {
        "text": {"in": "extension", "field": "text"},
    }


@pytest.mark.parametrize(
    "intent,names,size_in",
    [
        ("image.generate", ["n", "size"], "body"),
        ("image.edit", ["n", "size"], "form"),
        ("video.generate", ["seconds", "size"], "body"),
        ("video.edit", ["seconds", "size"], "body"),
        ("audio.generate", [], None),
        ("mesh.generate", ["size"], "body"),
        ("speech.synthesis", ["size"], "body"),
        ("future.unknown", ["size"], "body"),
    ],
)
def test_default_route_inputs_without_dimensions(intent, names, size_in):
    inputs = catalog(intent)["route_inputs"]
    assert [item["name"] for item in inputs] == names
    if size_in is not None:
        assert next(item for item in inputs if item["name"] == "size") == {
            "name": "size", "type": "string",
            "description": "WIDTHxHEIGHT, e.g. 960x512.",
            "in": size_in, "field": "size",
        }
    for item in inputs:
        if item["name"] == "seconds":
            assert item == {
                "name": "seconds", "type": "number", "minimum": 1,
                "description": "Clip length in seconds.", "in": "body", "field": "seconds",
            }


def test_dimensions_and_length_remove_only_their_default_route_inputs():
    assert catalog("video.generate", inputs={
        "width": {"type": "integer"}, "height": {"type": "integer"},
        "length": {"type": "number"},
    })["route_inputs"] == []
    assert catalog("image.generate", inputs={
        "width": {"type": "integer"}, "height": {"type": "integer"},
    })["route_inputs"] == [{
        "name": "n", "type": "integer", "minimum": 1, "maximum": 10,
        "label": "number of results", "description": "Number of images.",
        "in": "body", "field": "n",
    }]


@pytest.mark.parametrize("intent", ["image.generate", "audio.generate", "mesh.generate"])
@pytest.mark.parametrize("seconds,recommended", [(30, "sync"), (31, "async")])
def test_runtime_recommendation_boundary(intent, seconds, recommended):
    assert catalog(intent, seconds=seconds)["execution"]["recommended"] == recommended


@pytest.mark.parametrize("intent", ["image.edit", "video.generate"])
def test_native_route_overrides_family_routes(intent):
    row = catalog(intent, inputs={
        "source_image": {"type": "asset", "media_type": "image/png"},
        "seed": {"type": "integer"},
    }, bindings={"gateway": {"route": "/synthetic/demo"}})
    execution = row["execution"]
    assert execution["recommended"] == "sync"
    assert execution["async"] is None
    sync = execution["sync"]
    assert sync["path"] == "/synthetic/demo"
    assert sync["method"] == "POST"
    assert sync["content_type"] == "multipart/form-data"
    assert "model" not in sync
    assert sync["fields"] == {
        "source_image": {"in": "file", "field": "image"},
        "seed": {"in": "form", "field": "seed"},
    }
    assert row["route_inputs"] == []
    if intent == "image.edit":
        assert sync["alternate"] == {
            "method": "POST", "path": "/v1/images/edits", "model": "demo",
            "note": "options go in extensions['com.azizam.workflow']",
        }


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_nested_assets_stay_unsettable(mode):
    workflow = record("video.edit", inputs={
        "pictures": {"type": "array", "items": {"type": "asset", "media_type": "image/png"}},
        "bundle": {"type": "object", "properties": {
            "picture": {"type": "asset", "media_type": "image/png"},
        }},
    })
    assert request_mapping(workflow, mode) == {
        "pictures": {"in": "none"}, "bundle": {"in": "none"},
    }


@pytest.mark.parametrize(
    "target,intent,shorts,expected",
    [
        ("video-demo", "video.generate", {"demo"}, "demo"),
        ("demo-edit", "image.edit", {"demo"}, "demo"),
        ("missing", "image.edit", {"demo"}, None),
        ("video-demo", "video.generate", {"demo", "video-demo"}, "video-demo"),
        ("demo-edit", "image.edit", {"demo", "demo-edit"}, "demo-edit"),
    ],
)
def test_alias_target_candidates_and_exact_precedence(target, intent, shorts, expected):
    assert match_alias_target(target, intent, shorts) == expected
