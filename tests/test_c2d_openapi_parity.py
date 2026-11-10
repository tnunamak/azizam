"""Parity goldens for gateway/workflow_openapi.py and gateway/workflows_catalog.py.

For every shipped workflow: the OpenAPI fragment that ``augment_openapi`` adds (its component schemas,
its ``x-azizam-workflows`` entry, the routes whose extension schema carries it, its run path) and the
catalog row and examples that ``build_workflows_catalog`` produces. Every other path operation of the
augmented spec (typed generation routes with the video ``x-azizam-async`` extension, media-jobs submit, poll and
content, remix bodies, the generic run-route template) is compared per path. The inputs (base URL, aliases, video
limits, capability rows, policy facts) are fixed in tests/support/c2d_snapshots.py.

Each golden item holds the SHA-256 of its canonical JSON and, when under 20 KB, the full JSON. The test
compares the hash first. On a mismatch it names the workflow and, when the full JSON is stored, the
first differing field. Only tests/fixtures/c2d_parity/capture.py writes the goldens.

List order is compared as returned (catalog order is sorted by workflow id by the code under test).
No helper is stubbed: both modules are pure and the manifests are the committed ones.
"""

from __future__ import annotations

import pytest

from tests.support import c2d_parity
from tests.support.c2d_snapshots import capture_openapi, capture_workflows_catalog

OPENAPI_GOLDEN = c2d_parity.load_golden("openapi")
CATALOG_GOLDEN = c2d_parity.load_golden("workflows_catalog")
OPENAPI = capture_openapi()
CATALOG = capture_workflows_catalog()


def _assert_item(actual: dict, golden: dict, label: str) -> None:
    if actual["sha256"] == golden["sha256"]:
        return
    if "full" in golden and "full" in actual:
        for field in sorted(set(actual["full"]) | set(golden["full"])):
            assert actual["full"].get(field) == golden["full"].get(field), f"{label}: field {field} differs"
    pytest.fail(f"{label}: canonical JSON differs (sha256 {actual['sha256'][:12]} != {golden['sha256'][:12]}); "
                f"{'no full copy stored' if 'full' not in golden else 'full copies equal field by field: ordering inside a list?'}")


def test_openapi_workflow_set_matches_golden():
    assert sorted(OPENAPI["workflows"]) == sorted(OPENAPI_GOLDEN["workflows"])
    assert OPENAPI["inputs"] == OPENAPI_GOLDEN["inputs"]


@pytest.mark.parametrize("workflow_id", sorted(OPENAPI_GOLDEN["workflows"]))
def test_openapi_fragment_matches_golden(workflow_id):
    _assert_item(OPENAPI["workflows"][workflow_id], OPENAPI_GOLDEN["workflows"][workflow_id], workflow_id)


def test_openapi_shared_parts_match_golden():
    _assert_item(OPENAPI["shared"], OPENAPI_GOLDEN["shared"], "shared (info, non-workflow components, catalog order)")


def test_openapi_path_set_matches_golden():
    assert sorted(OPENAPI["path_operations"]) == sorted(OPENAPI_GOLDEN["path_operations"])


@pytest.mark.parametrize("path", sorted(OPENAPI_GOLDEN["path_operations"]))
def test_openapi_path_operation_matches_golden(path):
    _assert_item(OPENAPI["path_operations"][path], OPENAPI_GOLDEN["path_operations"][path], f"path {path}")


def test_catalog_workflow_set_and_order_match_golden():
    assert CATALOG["workflow_ids_in_catalog_order"] == CATALOG_GOLDEN["workflow_ids_in_catalog_order"]
    assert CATALOG["inputs"] == CATALOG_GOLDEN["inputs"]
    assert CATALOG["envelope"] == CATALOG_GOLDEN["envelope"]


@pytest.mark.parametrize("workflow_id", sorted(CATALOG_GOLDEN["workflows"]))
def test_catalog_examples_match_golden(workflow_id):
    _assert_item(CATALOG["workflows"][workflow_id]["examples"], CATALOG_GOLDEN["workflows"][workflow_id]["examples"],
                 f"{workflow_id} examples")


@pytest.mark.parametrize("workflow_id", sorted(CATALOG_GOLDEN["workflows"]))
def test_catalog_row_matches_golden(workflow_id):
    _assert_item(CATALOG["workflows"][workflow_id]["row"], CATALOG_GOLDEN["workflows"][workflow_id]["row"],
                 f"{workflow_id} row")


def test_published_video_create_extension_contract_is_json_only():
    from gateway.openai_media_schemas import VIDEOS_CREATE_OPENAPI

    content = VIDEOS_CREATE_OPENAPI["requestBody"]["content"]
    request = content["application/json"]["schema"]
    assert "extensions" not in request.get("required", [])
    extensions = request["properties"]["extensions"]
    assert extensions == {
        "type": "object",
        "properties": {
            "com.azizam.video": {
                "type": "object",
                "properties": {"audio": {"type": "boolean", "default": True}},
                "additionalProperties": False,
            },
        },
        "additionalProperties": True,
    }
    assert type(extensions["properties"]["com.azizam.video"]["properties"]["audio"]["default"]) is bool
    multipart = content["multipart/form-data"]["schema"]
    assert "extensions" not in multipart["properties"]
    assert multipart["required"] == request["required"]
    assert multipart["properties"]["input_reference"]["format"] == "binary"


# These expectations are deliberately independent of execution.intents. A missing
# row must not silently remove its own coverage, and shared table reads must not
# let an incorrect prefix or native alternate pass the shadow comparison.
_SHIPPED_TEMPLATED_INTENTS = frozenset({
    "image.generate", "image.edit", "audio.generate",
    "mesh.generate", "video.generate", "video.edit",
})
_FROZEN_EXECUTION_METADATA = {
    "image.generate": ("", None),
    "image.edit": ("", {
        "method": "POST",
        "path": "/v1/images/edits",
        "model": {"from": "model"},
        "note": "options go in extensions['com.azizam.workflow']",
    }),
    "audio.generate": ("", None),
    "mesh.generate": ("", None),
    "video.generate": ("video-", None),
    "video.edit": ("video-", None),
    "image.analyze": ("", None),
}


@pytest.fixture
def shipped_execution_publication(monkeypatch):
    """Observe real capture inputs and output without replacing the publisher."""
    from gateway import workflow_openapi

    publications = []
    publish = workflow_openapi.workflow_catalog

    def observe(records, *args, **kwargs):
        records = tuple(records)
        catalog = publish(records, *args, **kwargs)
        publications.append((records, catalog))
        return catalog

    monkeypatch.setattr(workflow_openapi, "workflow_catalog", observe)
    capture_openapi()
    assert publications, "capture_openapi did not publish shipped workflow records"
    for records, catalog in publications:
        assert records, "shipped workflow registry is empty"
        assert {record.id for record in records} == {entry["id"] for entry in catalog}
    return publications


def test_shipped_templated_intents_have_execution_rows(shipped_execution_publication):
    from gateway import workflow_openapi, workflow_request_maps

    seen = set()
    for records, catalog in shipped_execution_publication:
        published = {entry["id"]: entry for entry in catalog}
        for record in records:
            seen.add(record.intent)
            execution = published[record.id]["execution"]
            # Also discover unexpected templated intents from publication, not
            # from table keys. Native-only routes need no templated primary row.
            native = workflow_openapi.gateway_route(record)
            templated = (
                not native and (execution["sync"] or execution["async"])
            ) or (execution["sync"] or {}).get("alternate")
            if templated:
                assert record.intent in _SHIPPED_TEMPLATED_INTENTS, record.id
            if record.intent in _SHIPPED_TEMPLATED_INTENTS or templated:
                assert workflow_request_maps.execution_row(record.intent) is not None, (
                    f"{record.id}: missing execution row for {record.intent}"
                )
    assert _SHIPPED_TEMPLATED_INTENTS <= seen, (
        f"shipped intents missing from capture: {_SHIPPED_TEMPLATED_INTENTS - seen}"
    )


def test_execution_row_metadata_matches_frozen_values():
    from gateway import workflow_request_maps

    execution = workflow_request_maps.load(workflow_request_maps.DEFAULT_PATH).execution
    assert set(execution.intents) == set(_FROZEN_EXECUTION_METADATA)
    for intent, (prefix, alternate) in _FROZEN_EXECUTION_METADATA.items():
        row = execution.row(intent)
        assert row is not None, f"{intent}: missing execution row"
        assert row.model_prefix == prefix, f"{intent}: model_prefix"
        assert row.native_alternate == alternate, f"{intent}: native_alternate"


def test_shipped_execution_rows_render_published_blocks(shipped_execution_publication):
    from gateway import workflow_openapi, workflow_request_maps

    execution = workflow_request_maps.load(workflow_request_maps.DEFAULT_PATH).execution
    for records, catalog in shipped_execution_publication:
        published = {entry["id"]: entry for entry in catalog}
        for record in records:
            entry = published[record.id]
            native = bool(workflow_openapi.gateway_route(record))
            sync = execution.render_sync(record, native=native)
            async_ = None if native else execution.render_async(record)
            typical = entry["typical_seconds"]
            if typical is not None and typical <= 30 and sync:
                recommended = "sync"
            elif async_:
                recommended = "async"
            else:
                recommended = "sync"
            assert {
                "recommended": recommended, "sync": sync, "async": async_,
            } == entry["execution"], f"{record.id}: rendered execution differs"


def test_data_only_video_intent_preserves_shipped_publications():
    import copy

    from gateway.discovery import BUNDLED_WORKFLOWS_DIR, WorkflowRegistry
    from gateway.openai_media_schemas import VIDEOS_CREATE_OPENAPI
    from gateway.workflow_openapi import (
        ROOT_EXTENSION, augment_openapi, extension_schema_name, schema_name, workflow_catalog,
    )

    registry = WorkflowRegistry.from_directory(BUNDLED_WORKFLOWS_DIR)
    assert not registry.errors
    shipped = registry.records()
    assert shipped

    # Clone manifest data, never an execution row or a publisher implementation.
    source = next(record for record in shipped if record.id == "video.generate.ltx-2.5-t2v")
    manifest = copy.deepcopy(source.manifest)
    manifest.update(id="video.interpolate.fixture", intent="video.interpolate")
    manifest.get("video", {}).pop("variants", None)
    manifest["inputs"]["fixture_detail"] = {"type": "number", "default": 0.5}
    manifest["bindings"]["comfyui"]["map"]["fixture_detail"] = copy.deepcopy(
        manifest["bindings"]["comfyui"]["map"]["prompt"]
    )

    video_paths = ("/v1/videos", "/openai/v1/videos")
    seed_spec = {"paths": {
        path: {"post": copy.deepcopy(VIDEOS_CREATE_OPENAPI)} for path in video_paths
    }}
    # Include the other shared publishers and concrete run paths in noninterference.
    for path in (
        "/v1/images/generations", "/openai/v1/images/generations",
        "/v1/audio/generate", "/openai/v1/audio/generate",
    ):
        seed_spec["paths"][path] = {"post": {"requestBody": {"content": {
            "application/json": {"schema": {"type": "object", "properties": {}}},
        }}}}
    seed_spec["paths"]["/gateway/v1/workflows/{workflow_id}/run"] = {"post": {}}
    seed_spec["paths"]["/gateway/v1/media/jobs"] = {"post": {}}

    baseline_catalog = workflow_catalog(shipped)
    baseline_spec = augment_openapi(copy.deepcopy(seed_spec), shipped)
    fixture = registry.add_manifest(manifest)
    records = registry.records()
    assert {record.id for record in records} == {record.id for record in shipped} | {fixture.id}
    published_catalog = workflow_catalog(records)
    published_spec = augment_openapi(copy.deepcopy(seed_spec), records)

    entry = next(row for row in published_catalog if row["id"] == fixture.id)
    execution = entry["execution"]
    assert execution["sync"] is None
    assert execution["recommended"] == "async"
    assert execution["async"] is not None
    assert execution["async"]["kind"] == "video"
    assert execution["async"]["submit"]["path"] == "/v1/videos"
    assert execution["async"]["submit"]["model"] == "video-fixture"
    assert next(row for row in published_spec[ROOT_EXTENSION] if row["id"] == fixture.id) == entry

    extension_name = extension_schema_name(fixture.id)
    full_name = schema_name(fixture.id)
    extension_ref = {"$ref": "#/components/schemas/" + extension_name}
    full_ref = {"$ref": "#/components/schemas/" + full_name}
    schemas = published_spec["components"]["schemas"]
    assert "prompt" in schemas[full_name]["properties"]
    extension = schemas[extension_name]
    assert extension["properties"]["fixture_detail"] == {"type": "number", "default": 0.5}
    assert extension["additionalProperties"] is False
    assert not ({"prompt", "width", "height", "length", "seed", "negative_prompt", "image"}
                & set(extension["properties"]))
    assert "prompt" not in extension.get("required", [])
    for path in video_paths:
        body = published_spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        refs = body["properties"]["extensions"]["properties"]["com.azizam.workflow"]["anyOf"]
        assert refs.count(extension_ref) == 1
        assert full_ref not in refs
        # The descriptor-owned video controls survive alongside the workflow schema.
        original = VIDEOS_CREATE_OPENAPI["requestBody"]["content"]["application/json"]["schema"]
        for namespace, control in original["properties"]["extensions"]["properties"].items():
            assert body["properties"]["extensions"]["properties"][namespace] == control

    assert [row for row in published_catalog if row["id"] != fixture.id] == baseline_catalog

    # Remove precisely the added record, its two components and references.
    # Everything else, including every shipped schema, catalog row, route union
    # and concrete run operation, must match the independently published baseline.
    without_fixture = copy.deepcopy(published_spec)
    without_fixture[ROOT_EXTENSION] = [
        row for row in without_fixture[ROOT_EXTENSION] if row["id"] != fixture.id
    ]
    del without_fixture["components"]["schemas"][extension_name]
    del without_fixture["components"]["schemas"][full_name]

    def remove_fixture_refs(value):
        if isinstance(value, list):
            return [remove_fixture_refs(item) for item in value if item != extension_ref]
        if isinstance(value, dict):
            return {key: remove_fixture_refs(item) for key, item in value.items()}
        return value

    assert remove_fixture_refs(without_fixture) == baseline_spec
