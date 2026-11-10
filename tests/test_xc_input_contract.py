"""Closed frontend mappings and neutral manifest roles."""
import pytest
import yaml

from gateway.extension_catalog import load_input_contract
from gateway.workflow_contract import check_contract
from tests.test_frontend_shapes import contract


@pytest.mark.parametrize("change", [
    {"module": "package.handler"}, {"extension_hints": []},
    {"extension_hints": {"picture": "package.handler"}},
    {"source_role_paths": []}, {"source_role_paths": [["package.handler"]]},
    {"source_role_paths": [["roles", "source"], ["roles", "source"]]},
])
def test_input_contract_refuses_invalid_fields(tmp_path, change):
    doc = {"extension_hints": {"picture": "input_reference"}, "source_role_paths": [["roles", "source"]]}
    doc.update(change)
    path = tmp_path / "frontends.yml"
    path.write_text(yaml.safe_dump({"input_contract": doc}))
    with pytest.raises(ValueError, match="input_contract"):
        load_input_contract(path)


@pytest.mark.parametrize("change", [
    {"roles": {"unknown": "source_image"}}, {"roles": {"source": "missing"}},
    {"roles": {"source": "prompt"}}, {"input_fields": {"missing": "input_reference"}},
    {"input_fields": {"source_image": "package.handler"}},
])
def test_neutral_manifest_mappings_refuse_invalid_fields(change):
    doc = contract("qwen-image-2.1-edit")
    doc.update(change)
    assert not check_contract(doc).ok


def test_neutral_roles_accept_a_declared_media_input():
    doc = contract("qwen-image-2.1-edit")
    doc.update(roles={"source": "source_image"}, input_fields={"source_image": "input_reference"})
    assert check_contract(doc).ok


def test_extensions_and_source_roles_ignore_workflow_spelling(monkeypatch):
    import asyncio
    from dataclasses import asdict

    from gateway.frontend_shapes import _base, build_shape, shapes
    from gateway.video_extensions import bind_video_workflow_extensions
    from gateway.video_source import VideoSource
    from gateway.workflow_runtime_types import RunRequest
    from tests.test_frontend_shapes import FakeSpool, json_request
    from tests.test_video_create_shape import ROWS, snap, video_contract

    class Hooks(shapes.VideoHooks):
        async def source(self, *args):
            return VideoSource(wire_id="video_mj_source", asset_id="asset-source", model="m",
                               resolved_model="m", fps=None, size=None, seconds=None, has_audio=False)
    monkeypatch.setattr(shapes, "VIDEO_HOOKS", Hooks())
    outcomes = []
    for picture, source, selected in (("image", "source_video", "first"), ("portrait", "motion", "renamed")):
        doc = video_contract("unfamiliar")
        doc["id"] = "custom.workflow"
        doc["inputs"] = {picture: {"type": "asset", "accepts": ["image/*"]},
                         source: {"type": "asset", "accepts": ["video/*"]},
                         "steps": {"type": "integer"}, "unmapped": {"type": "integer"}}
        doc["roles"] = {"source": source}
        doc["input_fields"] = {picture: "input_reference"}
        doc["bindings"] = {"decoy": {"map": {}}, selected: {
            "map": {picture: [{"path": "picture"}], source: [{"path": "motion"}], "steps": [{"path": "count"}]},
            "route_bound_inputs": [picture, source],
        }}
        result = bind_video_workflow_extensions({"inputs": {picture: "asset:override", "steps": 7, "unmapped": 3, "seed": 9}},
                                                doc, {"count": 1}, backend=selected)
        assert result.validation.rejected[picture] == (
            f"{picture} is managed by the standard input_reference request field; do not pass it as a workflow extension")
        assert "seed" in result.validation.rejected
        assert "standard seed request field" in result.validation.rejected["seed"]
        assert "unmapped" in result.validation.ignored
        assert result.binding.applied_inputs == ["steps"]
        assert result.request == {"count": 7}
        assert result.binding.backend == selected
        async def chosen(*args, **kwargs):
            return RunRequest(doc["id"], {"video": {"id": "video_mj_source"}})
        monkeypatch.setattr(_base.Shape, "to_run_request", chosen)
        row = ROWS["videos-edit"]
        attached = asyncio.run(build_shape(row).to_run_request(json_request({}), row, snap(doc), spool=FakeSpool()))
        assert isinstance(attached, RunRequest), getattr(attached, "body", attached)
        assert list(attached.media) == [source]
        outcomes.append((result.request, result.validation.accepted, result.validation.ignored,
                         [asdict(ref) for ref in attached.media[source]], dict(attached.facts)))
    assert outcomes[0] == outcomes[1]


def test_source_role_must_be_bound():
    doc = contract("qwen-image-2.1-edit")
    doc["inputs"]["unbound_source"] = {"type": "asset", "accepts": ["image/*"]}
    doc["roles"] = {"source": "unbound_source"}
    assert any(error.code == "role_input_unbound" for error in check_contract(doc).errors)
