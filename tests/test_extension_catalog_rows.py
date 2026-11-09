"""Literal route facts and unchanged extension output; never recapture goldens."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from gateway import extension_catalog as catalog
from gateway.audio_extensions import ROUTE_MANAGED_AUDIO_INPUTS
from gateway.video_extensions import ROUTE_MANAGED_VIDEO_INPUTS
from tests.support import c2d_parity


ROWS = Path(__file__).resolve().parents[1] / "registry" / "protocol_frontends.yml"
EXPECTED_ROUTES = {
    "com.azizam.prompts": [
        "/v1/images/generations", "/v1/images/edits", "/v1/videos",
        "/v1/videos/extensions", "/v1/videos/workflows/{workflow_id}",
        "/v1/audio/generate", "/v1/audio/speech", "/v1/responses",
        "/gateway/v1/media/jobs/image-generations",
    ],
    "com.azizam.workflow": [
        "/v1/images/generations", "/v1/images/edits",
        "/v1/videos", "/v1/audio/generate",
    ],
    "com.azizam.chat": ["/v1/chat/completions", "/v1/responses"],
    "com.azizam.routing": ["/v1/chat/completions", "/v1/responses"],
}

EXPECTED_WORKFLOWS = [
    ("/v1/images/generations", ("image.generate",), "image.generate",
     frozenset({"prompt", "source_image", "width", "height"})),
    ("/v1/images/edits", ("image.edit",), "image.edit",
     frozenset({"prompt", "source_image", "width", "height", "mask", "source_mask", "mask_asset"})),
    ("/v1/videos", ("video.",), "video.generate",
     frozenset({"prompt", "negative_prompt", "width", "height", "length",
                "frame_rate", "seed", "image", "image_strength"})),
    ("/v1/audio/generate", ("audio.",), "audio.generate",
     frozenset({"prompt", "tags", "lyrics", "duration", "seed"})),
]


def write_rows(tmp_path, rows):
    path = tmp_path / "rows.yml"
    path.write_text(yaml.safe_dump({"records": [{"registered_routes": rows}]}), encoding="utf-8")
    return path


def test_extension_routes_equal_literal_catalog():
    index = catalog.load_extension_routes(ROWS)
    assert {namespace: [row.route for row in rows] for namespace, rows in index.items()} == EXPECTED_ROUTES
    assert index == catalog.EXTENSION_ROUTES


def test_workflow_rows_equal_literal_facts():
    rows = catalog.load_extension_routes(ROWS)["com.azizam.workflow"]
    assert [(row.route, row.intent_prefixes, row.route_intent, row.route_inputs) for row in rows] == EXPECTED_WORKFLOWS


@pytest.mark.parametrize("route, expected", [
    ("/v1/audio/generate", ROUTE_MANAGED_AUDIO_INPUTS),
    ("/v1/videos", ROUTE_MANAGED_VIDEO_INPUTS),
])
def test_media_route_inputs_equal_binder_constants(route, expected):
    rows = catalog.load_extension_routes(ROWS)["com.azizam.workflow"]
    assert next(row.route_inputs for row in rows if row.route == route) == expected


@pytest.mark.parametrize("rows, message", [
    ([{"canonical": "/test", "extensions": ["com.example.unknown"], "extension_order": 1}],
     "unknown extension namespace"),
    ([{"canonical": "/test", "extensions": ["com.azizam.workflow"], "extension_order": 1,
       "route_inputs": ["prompt", 7],
       "workflow_targets": {"intent_prefixes": ["test."], "route_intent": "test.generate"}}],
     "route_inputs"),
    ([{"canonical": "/test", "extensions": ["com.azizam.workflow"], "extension_order": 1,
       "route_inputs": [], "workflow_targets": {"intent_prefixes": ["test."]}}],
     "route_intent"),
    ([{"canonical": "/one", "extensions": ["com.azizam.chat"], "extension_order": 1},
      {"canonical": "/two", "extensions": ["com.azizam.routing"], "extension_order": 1}],
     "duplicate extension_order"),
    ([{"canonical": "/test", "extensions": ["com.azizam.chat"], "extension_order": 50}],
     "duplicate extension_order"),
])
def test_invalid_extension_rows_refused(tmp_path, rows, message):
    with pytest.raises(ValueError, match=message):
        catalog.load_extension_routes(write_rows(tmp_path, rows))


@pytest.mark.parametrize("rows, namespace, route, row_number", [
    pytest.param(
        [{"canonical": "/v1/videos/workflows/{workflow_id}",
          "extensions": ["com.azizam.prompts"], "extension_order": 51}],
        "com.azizam.prompts", "/v1/videos/workflows/{workflow_id}", 1,
        id="workflow-residual",
    ),
    pytest.param(
        [{"canonical": "/gateway/v1/media/jobs/image-generations",
          "extensions": ["com.azizam.prompts"], "extension_order": 91}],
        "com.azizam.prompts", "/gateway/v1/media/jobs/image-generations", 1,
        id="media-jobs-residual",
    ),
    pytest.param(
        [{"canonical": "/same", "extensions": ["com.azizam.chat"], "extension_order": 1},
         {"canonical": "/same", "extensions": ["com.azizam.chat"], "extension_order": 2}],
        "com.azizam.chat", "/same", 2,
        id="registered-registered",
    ),
])
def test_duplicate_extension_route_refused(tmp_path, rows, namespace, route, row_number):
    with pytest.raises(ValueError) as error:
        catalog.load_extension_routes(write_rows(tmp_path, rows))
    assert str(error.value) == (
        f"duplicate extension route: namespace {namespace!r}, route {route!r}, "
        f"record 1, row {row_number}"
    )


def test_duplicate_extension_route_across_records_refused(tmp_path):
    path = tmp_path / "rows.yml"
    path.write_text(yaml.safe_dump({"records": [
        {"registered_routes": [
            {"canonical": "/same", "extensions": ["com.azizam.routing"], "extension_order": 1},
        ]},
        {"registered_routes": [
            {"canonical": "/same", "extensions": ["com.azizam.routing"], "extension_order": 2},
        ]},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        catalog.load_extension_routes(path)
    assert str(error.value) == (
        "duplicate extension route: namespace 'com.azizam.routing', route '/same', "
        "record 2, row 1"
    )


def test_same_route_in_distinct_extension_namespaces_allowed(tmp_path):
    index = catalog.load_extension_routes(write_rows(tmp_path, [
        {"canonical": "/shared", "extensions": ["com.azizam.chat"], "extension_order": 1},
        {"canonical": "/shared", "extensions": ["com.azizam.routing"], "extension_order": 2},
        {"canonical": "/multi", "extensions": ["com.azizam.chat", "com.azizam.routing"],
         "extension_order": 3},
    ]))
    for namespace in ("com.azizam.chat", "com.azizam.routing"):
        assert [row.route for row in index[namespace]] == ["/shared", "/multi"]


def test_extension_index_is_deeply_read_only():
    index = catalog.load_extension_routes(ROWS)
    with pytest.raises(TypeError):
        index["com.azizam.chat"] = ()
    rows = index["com.azizam.workflow"]
    assert isinstance(rows, tuple)
    with pytest.raises(FrozenInstanceError):
        rows[0].route = "/changed"
    assert isinstance(rows[0].route_inputs, frozenset)
    assert isinstance(rows[0].intent_prefixes, tuple)


def test_declared_order_not_yaml_order(tmp_path):
    path = write_rows(tmp_path, [
        {"canonical": "/second", "extensions": ["com.azizam.chat"], "extension_order": 2},
        {"canonical": "/first", "extensions": ["com.azizam.chat"], "extension_order": 1},
    ])
    assert [row.route for row in catalog.load_extension_routes(path)["com.azizam.chat"]] == [
        "/first", "/second",
    ]


def test_only_two_residual_routes_have_no_registered_row():
    document = yaml.safe_load(ROWS.read_text(encoding="utf-8"))
    registered = {row["canonical"] for record in document["records"]
                  for row in record.get("registered_routes", [])}
    residual = {
        "/v1/videos/workflows/{workflow_id}",
        "/gateway/v1/media/jobs/image-generations",
    }
    assert residual.isdisjoint(registered)
    assert set(EXPECTED_ROUTES["com.azizam.prompts"]) - registered == residual


def test_all_audience_summaries_match_unchanged_golden():
    golden = c2d_parity.load_golden("extension_catalog")
    registry = c2d_parity.shipped_registry()
    assert list(catalog.VALID_AUDIENCES) == golden["audiences"]
    for audience in catalog.VALID_AUDIENCES:
        assert catalog.summarize_extensions(registry, audience) == golden["summaries"][audience]


def test_summary_uses_loaded_row_facts(tmp_path, monkeypatch):
    index = catalog.load_extension_routes(write_rows(tmp_path, [
        {"canonical": "/row-owned", "extensions": ["com.azizam.chat"], "extension_order": 1},
    ]))
    monkeypatch.setattr(catalog, "EXTENSION_ROUTES", index)
    registry = c2d_parity.shipped_registry()
    summary = catalog.summarize_extensions(registry, next(iter(catalog.VALID_AUDIENCES)))
    chat = next(item for item in summary["extensions"] if item["namespace"] == "com.azizam.chat")
    assert [row["route"] for row in chat["routes"]] == ["/row-owned"]
