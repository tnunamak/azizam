"""Frontend rows and wire shapes (CODE_TO_DATA_S4_S8 section 8, builder D): the bundled rows are valid, each shape turns
its wire into a ``RunRequest`` and renders the core's answers in its own vocabulary. The core is the frozen fake
(``tests.workflow_runtime_fakes``); the spool is a test double of ``RequestSpool``. The goldens that need the real core run
on the integration branch."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml
from starlette.requests import Request
from starlette.responses import Response

from gateway import frontend_profiles, frontend_shapes
from gateway.frontend_shapes import RowError, bundled_rows, parse_rows, profile_problems
from gateway.manifests import ContractErrors
from gateway.workflow_kernel import selector_matches
from gateway.workflow_runtime_types import (
    IDEMPOTENCY_CONFLICT,
    JobNotFound,
    MediaRef,
    ResultContent,
    ResultRef,
    RunFailure,
    RunRefused,
)
from tests.workflow_runtime_fakes import FakeJobService, FakeRunCore, FakeSnapshot, entry

MANIFESTS = Path(__file__).resolve().parent.parent / "manifests" / "workflows"
CALLER = "caller-1"


def contract(name: str) -> dict[str, Any]:
    return yaml.safe_load((MANIFESTS / f"{name}.yml").read_text(encoding="utf-8"))


def snapshot(*names: str) -> FakeSnapshot:
    entries = [entry(contract(name)) for name in names]
    return FakeSnapshot(1, MappingProxyType({e.id: e for e in entries}))


VIDEO_EDIT = {"id": "video.edit.demo", "version": "1", "intent": "video.edit", "schema_version": 2,
              "inputs": {"prompt": {"type": "string", "required": True},
                         "video": {"type": "asset", "accepts": ["video/mp4"], "required": True}},
              "outputs": {"video": {"media_type": "video/mp4"}}, "bindings": {"comfyui": {}}}
ROWS = {row.id: row for row in bundled_rows()}


@pytest.fixture(autouse=True)
def no_deployment_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The main module installs the deployment's default workflow when it loads; these tests read the wire alone."""
    monkeypatch.setattr(frontend_shapes.shapes, "DEFAULT_WORKFLOWS", {})


def make_request(body: bytes = b"", *, headers: dict[str, str] | None = None, content_type: str = "application/json") -> Request:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    raw = {"content-type": content_type, **(headers or {})}
    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": [
        (key.lower().encode(), value.encode()) for key, value in raw.items()]}
    return Request(scope, receive)


def json_request(payload: Any, **kw: Any) -> Request:
    return make_request(json.dumps(payload).encode() if not isinstance(payload, bytes) else payload, **kw)


def multipart_request(fields: dict[str, str], files: list[tuple[str, str, str, bytes]], **kw: Any) -> Request:
    boundary = "bnd"
    out = b""
    for name, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    for name, filename, declared, data in files:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {declared}\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return make_request(out, content_type=f"multipart/form-data; boundary={boundary}", **kw)


class FakeSpool:
    """A test double of ``RequestSpool``: keeps what it was given, refuses a type on request."""

    def __init__(self, refuse: RunRefused | None = None) -> None:
        self.staged: list[tuple[str, bytes, str | None, str | None]] = []
        self.refuse = refuse

    async def stage(self, input_name: str, chunks: Any, *, filename: str | None, wire_media_type: str | None) -> Any:
        if self.refuse is not None:
            return self.refuse
        data = b"".join([chunk async for chunk in chunks])
        self.staged.append((input_name, data, filename, wire_media_type))
        return MediaRef(digest=hashlib.sha256(data).hexdigest(), media_type="image/png", size=len(data), filename=filename,
                        wire_media_type=wire_media_type, spool_key=f"k{len(self.staged)}")

    def release(self) -> None:
        pass


def shape_of(row_id: str, **options: Any):
    return frontend_shapes.build_shape(ROWS[row_id], **options)


def run_request(row_id: str, request: Request, snap: FakeSnapshot, spool: FakeSpool | None = None, **options: Any):
    shape = shape_of(row_id, **options)
    return asyncio.run(shape.to_run_request(request, ROWS[row_id], snap, spool=spool or FakeSpool()))


def answer(row_id: str, outcome: Any, jobs: FakeJobService, **options: Any) -> Response:
    return asyncio.run(shape_of(row_id, **options).to_response(outcome, ROWS[row_id], jobs=jobs, caller=CALLER))


def finish(core: FakeRunCore, job_id: str, output: str, media_type: str, body: bytes, *, asset_id: str = "ast_1") -> None:
    view = core.jobs[job_id]
    core.contents[(job_id, output, 0)] = ResultContent(media_type, body=body)
    core.jobs[job_id] = replace(view, status="succeeded", results=(ResultRef(output, 0, media_type, size=len(body), asset_id=asset_id),))


def submit(core: FakeRunCore, req: Any, snap: FakeSnapshot) -> Any:
    return asyncio.run(core.submit_run(req, CALLER, snapshot=snap))


# ---------------------------------------------------------------------------------------------------- the rows


def test_the_bundled_rows_are_valid_and_each_selects_a_bundled_workflow() -> None:
    assert len(ROWS) == len(bundled_rows()) == 10
    every = {n: contract(n) for n in ("flux2-klein", "qwen-image-2.1-edit", "tangoflux", "video-ltx-2.5-t2v", "mesh.generate.trellis-2")}
    every["edit"] = VIDEO_EDIT
    for row in ROWS.values():
        assert any(selector_matches(row.selects, str(c["id"]), c) for c in every.values()), row.id


def test_a_row_with_an_intent_word_or_a_free_selector_is_refused() -> None:
    good = {"id": "r", "shape": "openai.audio.json", "mode": "sync", "routes": [{"methods": ["POST"], "canonical": "/v1/x"}],
            "selects": {"ids": ["a"]}}
    assert parse_rows({"frontends": [good]})[0].id == "r"                      # the valid neighbour passes
    for bad_selects in ({"intent": "audio.generate"}, {"match": {}}, {"ids": []}, {"ids": ["a"], "match": {"outputs": {"kind": "json"}}},
                        {"match": {"inputs": {"p": {"type": "string", "media_type": "a/b"}}}}):
        with pytest.raises(RowError):
            parse_rows({"frontends": [{**good, "selects": bad_selects}]})
    for bad in ({**good, "shape": "nope"}, {**good, "mode": "maybe"}, {**good, "routes": []}):
        with pytest.raises(RowError):
            parse_rows({"frontends": [bad]})
    with pytest.raises(RowError):
        parse_rows({"frontends": [good, good]})


def test_the_rows_that_list_as_models_are_the_ones_today_lists() -> None:
    assert {row.id for row in ROWS.values() if row.lists_as_model} >= {"images-generations", "audio-generate", "videos-create"}


# ---------------------------------------------------------------------------------------------------- requests


def test_images_json_fills_the_inputs_of_the_workflow_the_model_names() -> None:
    snap = snapshot("flux2-klein", "qwen-image-2.1-edit")
    run = run_request("images-generations", json_request({"prompt": "a fox", "model": "image.generate.flux2-klein", "size": "512x768",
                                                          "response_format": "url",
                                                          "extensions": {"com.azizam.workflow": {"seed": 3}}}),
                      snap)
    assert run.workflow_id == "image.generate.flux2-klein" and run.frontend_id == "images-generations"
    assert run.inputs == {"prompt": "a fox", "width": 512, "height": 768, "seed": 3}
    assert run.idempotency_key is None and not run.media


def test_the_json_routes_pass_the_idempotency_key_and_core_replays_it() -> None:
    snap = snapshot("flux2-klein")
    core = FakeRunCore()
    first = run_request("image-jobs", json_request({"prompt": "a"}, headers={"Idempotency-Key": "k1"}), snap)
    again = run_request("image-jobs", json_request({"prompt": "a"}, headers={"Idempotency-Key": "k1"}), snap)
    changed = run_request("image-jobs", json_request({"prompt": "b"}, headers={"Idempotency-Key": "k1"}), snap)
    assert first.idempotency_key == "k1"
    view = submit(core, first, snap)
    assert submit(core, again, snap).disposition == "replay" and len(core.submitted) == 1
    conflict = submit(core, changed, snap)
    assert isinstance(conflict, RunRefused) and conflict.code == IDEMPOTENCY_CONFLICT
    assert view.job_id == "job-1"


def test_a_request_without_a_model_reaches_the_one_workflow_the_row_can_serve() -> None:
    snap = snapshot("flux2-klein", "qwen-image-2.1-edit", "video-ltx-2.5-t2v")
    run = run_request("images-generations", json_request({"prompt": "a fox"}), snap)
    assert run.workflow_id == "image.generate.flux2-klein"          # the edit workflow needs a file this wire cannot carry


def test_two_workflows_and_no_model_is_a_400_that_asks_for_a_model() -> None:
    snap = snapshot("flux2-klein", "krea2-turbo")
    response = run_request("images-generations", json_request({"prompt": "a fox"}), snap)
    assert response.status_code == 400 and json.loads(response.body)["error"]["code"] == "workflow_ambiguous"


def test_a_workflow_is_addressed_by_its_id_or_by_its_id_without_the_intent() -> None:
    snap = snapshot("flux2-klein", "krea2-turbo", "qwen-image-2.1-edit")
    short = run_request("images-generations", json_request({"prompt": "a fox", "model": "krea2-turbo"}), snap)
    assert short.workflow_id == "image.generate.krea2-turbo"
    unknown = run_request("images-generations", json_request({"prompt": "a fox", "model": "no-such"}), snap)
    assert unknown.status_code == 404 and json.loads(unknown.body)["error"]["code"] == "model_not_found"
    wrong_row = run_request("images-generations", json_request({"prompt": "a fox", "model": "qwen-image-2.1-edit"}), snap)
    assert wrong_row.status_code == 404                 # the row's selection bounds what a short name can reach


def test_the_deployment_default_runs_when_no_model_is_named_and_a_named_model_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = snapshot("flux2-klein", "krea2-turbo")
    resolvers = {frontend_shapes.shapes.OpenAIImagesJson.shape_id: lambda: "krea2-turbo"}
    monkeypatch.setattr(frontend_shapes.shapes, "DEFAULT_WORKFLOWS", resolvers)
    assert run_request("images-generations", json_request({"prompt": "a fox"}), snap).workflow_id == "image.generate.krea2-turbo"
    named = run_request("images-generations", json_request({"prompt": "a fox", "model": "image.generate.flux2-klein"}), snap)
    assert named.workflow_id == "image.generate.flux2-klein"
    resolvers.clear()          # no default configured: the wire asks for a model, as before
    assert run_request("images-generations", json_request({"prompt": "a fox"}), snap).status_code == 400


def test_the_picture_encoding_fields_of_the_wire_are_not_workflow_inputs() -> None:
    snap = snapshot("flux2-klein")
    body = {"prompt": "a fox", "n": 3, "quality": "high", "background": "opaque", "output_format": "jpeg",
            "output_compression": 80, "size": "512x512", "response_format": "b64_json"}
    run = run_request("images-generations", json_request(body), snap)
    assert run.inputs == {"prompt": "a fox", "width": 512, "height": 512}


@pytest.mark.parametrize("payload,status,code", [
    (b"{", 400, None), (b"[]", 400, None),
    ({"prompt": "x", "model": "no.such.workflow"}, 404, "model_not_found"),
    ({"prompt": "x", "model": "image.edit.qwen-image-2.1-edit"}, 404, "model_not_found"),   # needs a file: not this wire's
])
def test_a_malformed_json_request_is_the_shapes_own_error(payload: Any, status: int, code: str | None) -> None:
    response = run_request("images-generations", json_request(payload), snapshot("flux2-klein", "qwen-image-2.1-edit"))
    assert response.status_code == status
    body = json.loads(response.body)["error"]
    assert body["type"] == "invalid_request_error" and (code is None or body["code"] == code)


def test_a_multipart_file_is_staged_with_the_declared_type_and_fills_the_required_asset_input() -> None:
    snap = snapshot("flux2-klein", "qwen-image-2.1-edit")
    spool = FakeSpool()
    run = run_request("images-edits", multipart_request({"prompt": "make it night", "extensions": '{"com.azizam.workflow": {"steps": 8}}'},
                                                        [("image", "photo.png", "image/webp", b"PIXELS")]), snap, spool)
    assert run.workflow_id == "image.edit.qwen-image-2.1-edit"
    assert spool.staged == [("source_image", b"PIXELS", "photo.png", "image/webp")]
    assert run.inputs == {"prompt": "make it night", "steps": 8}
    (ref,) = run.media["source_image"]
    assert ref.wire_media_type == "image/webp" and ref.spool_key == "k1"


def test_a_nested_file_part_is_refused_before_anything_is_staged() -> None:
    spool = FakeSpool()
    response = run_request("images-edits", multipart_request({"prompt": "p"}, [("image.file", "a.png", "image/png", b"x")]),
                           snapshot("qwen-image-2.1-edit"), spool)
    assert response.status_code == 422 and json.loads(response.body)["error"]["code"] == "media_path_unsupported"
    assert spool.staged == []


def test_a_spool_refusal_is_rendered_in_the_shapes_vocabulary() -> None:
    spool = FakeSpool(RunRefused(415, "unsupported_media_type", "That file type is not supported"))
    response = run_request("images-edits", multipart_request({"prompt": "p"}, [("image", "a.bin", "application/x-zzz", b"x")]),
                           snapshot("qwen-image-2.1-edit"), spool)
    assert response.status_code == 415 and json.loads(response.body)["error"]["code"] == "unsupported_media_type"


def test_an_unknown_file_field_is_a_400() -> None:
    response = run_request("images-edits", multipart_request({"prompt": "p"}, [("a", "a.png", "image/png", b"x"), ("b", "b.png", "image/png", b"y")]),
                           snapshot("qwen-image-2.1-edit"))
    assert response.status_code == 400 and json.loads(response.body)["error"]["param"] in ("a", "b")


def test_a_video_edit_names_its_source_video_and_fills_the_asset_input_by_reference(monkeypatch) -> None:
    from gateway.frontend_shapes import shapes
    from gateway.video_source import VideoSource

    class Hooks(shapes.VideoHooks):
        async def source(self, job_id, wire_id, caller):
            return VideoSource(wire_id, "ast_9", None, None, None, None, None, None)

    monkeypatch.setattr(shapes, "VIDEO_HOOKS", Hooks())
    snap = FakeSnapshot(1, MappingProxyType({"video.edit.demo": entry(VIDEO_EDIT)}))
    run = run_request("videos-edit", json_request({"prompt": "night", "video": {"id": "video_9"}}), snap)
    assert run.workflow_id == "video.edit.demo" and run.inputs == {"prompt": "night"}
    assert run.media["video"][0].asset_id == "ast_9"
    missing = run_request("videos-edit", json_request({"prompt": "night"}), snap)
    assert missing.status_code == 400 and json.loads(missing.body)["error"]["param"] == "video"


# ---------------------------------------------------------------------------------------------------- responses


def test_a_refusal_keeps_its_status_code_and_retry_after() -> None:
    jobs = FakeJobService(FakeRunCore())
    response = answer("images-generations", RunRefused(409, "idempotency_in_progress", "wait", retry_after_s=3), jobs)
    assert response.status_code == 409 and response.headers["retry-after"] == "3"
    assert json.loads(response.body)["error"] == {"message": "wait", "type": "invalid_request_error", "code": "idempotency_in_progress"}
    assert json.loads(answer("images-generations", RunRefused(503, "x", "down"), jobs).body)["error"]["type"] == "api_error"


def test_a_sync_image_row_waits_and_renders_one_item_per_result() -> None:
    snap, core = snapshot("flux2-klein"), FakeRunCore()
    view = submit(core, run_request("images-generations", json_request({"prompt": "a"}), snap), snap)
    finish(core, view.job_id, "image", "image/png", b"PNGDATA", asset_id="ast_7")
    response = answer("images-generations", view, FakeJobService(core), asset_url=lambda ref, caller: f"/v1/assets/{ref.asset_id}")
    data = json.loads(response.body)["data"]
    assert response.status_code == 200 and data == [{"asset_id": "ast_7", "media_type": "image/png", "url": "/v1/assets/ast_7"}]
    inline = json.loads(answer("images-generations", view, FakeJobService(core)).body)["data"][0]
    assert inline["b64_json"] == "UE5HREFUQQ=="


def test_a_job_row_answers_with_the_job_object_and_its_location() -> None:
    snap, core = snapshot("flux2-klein"), FakeRunCore()
    view = submit(core, run_request("image-jobs", json_request({"prompt": "a"}), snap), snap)
    queued = answer("image-jobs", view, FakeJobService(core))
    assert queued.status_code == 202 and queued.headers["location"] == "/gateway/v1/media/jobs/job-1"
    body = json.loads(queued.body)
    assert body["object"] == "media.job" and body["status"] == "queued" and body["poll_url"].endswith("/job-1")
    finish(core, view.job_id, "image", "image/png", b"x")
    assert answer("image-jobs", core.jobs["job-1"], FakeJobService(core)).status_code == 200


def test_a_failed_canceled_and_unfinished_job_each_get_their_own_answer() -> None:
    snap, core = snapshot("tangoflux"), FakeRunCore()
    jobs = FakeJobService(core)
    view = submit(core, run_request("audio-generate", json_request({"prompt": "a"}), snap), snap)
    assert answer("audio-generate", view, jobs).status_code == 504                      # still queued after the wait
    core.jobs[view.job_id] = replace(view, status="failed", failure=RunFailure("upstream_error", "upstream", "engine said no", upstream_status=500))
    failed = answer("audio-generate", core.jobs[view.job_id], jobs)
    assert failed.status_code == 500 and json.loads(failed.body)["error"]["code"] == "upstream_error"
    assert answer("audio-generate", replace(view, status="canceled"), jobs).status_code == 409
    gone = replace(view, job_id="job-404")
    assert answer("audio-generate", gone, jobs).status_code == 404


def test_the_audio_row_answers_with_the_bytes_and_their_type() -> None:
    snap, core = snapshot("tangoflux"), FakeRunCore()
    view = submit(core, run_request("audio-generate", json_request({"prompt": "a", "duration": 4}), snap), snap)
    finish(core, view.job_id, "audio", "audio/wav", b"RIFF")
    response = answer("audio-generate", view, FakeJobService(core))
    assert response.body == b"RIFF" and response.headers["content-type"] == "audio/wav"


@pytest.mark.parametrize("row_id,shape_output", [("mesh-generations", None), ("mesh-glb", None)])
def test_the_mesh_rows_take_one_picture_and_answer_with_the_model(row_id: str, shape_output: Any) -> None:
    snap, core = snapshot("mesh.generate.trellis-2"), FakeRunCore()
    spool = FakeSpool()
    run = run_request(row_id, multipart_request({"seed": "4"}, [("image", "p.png", "image/png", b"IMG")]), snap, spool)
    assert not isinstance(run, Response), run.body
    assert run.workflow_id == "mesh.generate.trellis-2" and run.inputs == {"seed": 4}
    assert spool.staged[0][0] == "image" and spool.staged[0][3] == "image/png"
    view = submit(core, run, snap)
    finish(core, view.job_id, "mesh", "model/gltf-binary", b"glTF")
    response = answer(row_id, view, FakeJobService(core))
    if row_id == "mesh-glb":
        assert response.body == b"glTF" and response.headers["content-type"] == "model/gltf-binary"
    else:
        item = json.loads(response.body)["data"][0]
        assert item["b64_json"] == "Z2xURg==" and item["media_type"] == "model/gltf-binary"


# ---------------------------------------------------------------------------------------------------- profiles


def test_the_mesh_profile_refuses_what_the_wire_cannot_carry_and_passes_the_bundled_workflow() -> None:
    good = contract("mesh.generate.trellis-2")
    assert profile_problems(good) == ()
    assert profile_problems(entry(good).contract) == ()                        # the snapshot's frozen form (tuples, not lists)
    two_pictures = json.loads(json.dumps(good))
    two_pictures["inputs"]["second"] = {**good["inputs"]["image"]}
    assert "exactly one required picture input" in profile_problems(two_pictures)
    with pytest.raises(ContractErrors) as caught:
        frontend_profiles.check(two_pictures)
    assert caught.value.primary.code == frontend_profiles.PROFILE_REFUSED
    frontend_profiles.check(good)


def test_a_workflow_no_single_purpose_row_selects_is_never_refused_at_save() -> None:
    for name in ("video-ltx-2.5-i2v", "qwen-image-2.1-edit", "flux2-klein", "tangoflux", "video-scail-2-replace", "face-likeness"):
        frontend_profiles.check(contract(name))
        assert profile_problems(contract(name)) == (), name


def test_a_shapes_profile_names_why_a_workflow_is_out_of_reach_of_its_wire() -> None:
    generate = shape_of("images-generations")
    assert generate.profile(contract("flux2-klein")) == ()
    assert generate.profile(contract("qwen-image-2.1-edit"))               # a JSON body cannot carry the file it needs
    assert generate.profile("not a mapping")                               # never an exception


# ---------------------------------------------------------------------------------------------------- interface gap


def test_to_response_can_honour_the_callers_choice_of_answer_form() -> None:
    parameters = inspect.signature(frontend_shapes.shapes.OpenAIImagesJson.to_response).parameters
    assert {"request", "wire", "context", "run_request"} & set(parameters)
