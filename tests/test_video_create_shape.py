"""The create path of the video wire as data (CODE_TO_DATA_S7_VIDEO_MOVE.md section 4, builder V5c).

Three layers, none of which starts an engine:

* the row data and the plumbing a video row needs (``unconfigured``, the 502 for a service that cannot be reached, the
  legacy handoff);
* the shape: a wire request read against a manifest's ``video`` block, in the order the old route checked it, with the old
  words for each refusal, and the facts it records for the job;
* the source of a remix or an edit (``video_source.resolve_source``).

The manifests are in-memory contracts that follow the frozen ``video`` block (2.2), so these tests do not wait for the
manifest data step."""

from __future__ import annotations

import asyncio
import base64
import json
from types import MappingProxyType
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response

from gateway import frontend_shapes
from gateway.frontend_shapes import RowError, bundled_rows, parse_rows
from gateway.frontend_shapes import _base, shapes
from gateway.workflow_run import BindingAvailability
from gateway.workflow_runtime_types import RunJobView, RunRefused, RunRequest
from tests.test_frontend_shapes import FakeSpool, json_request, make_request
from tests.test_workflow_run import Caller, make_core, request as core_request, run, snapshot as core_snapshot  # noqa: F401
from tests.test_workflow_run import CONTRACT, db  # noqa: F401
from tests.workflow_runtime_fakes import FakeSnapshot, entry

ROWS = {row.id: row for row in bundled_rows()}
CALLER = "caller-1"
SERVICE_UNREACHABLE = "service_unreachable"


def _inputs(*extra: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "prompt": {"type": "string", "required": True},
        "negative_prompt": {"type": "string"},
        "seed": {"type": "integer", "minimum": 0},
        "width": {"type": "integer", "required": True},
        "height": {"type": "integer", "required": True},
        "length": {"type": "integer", "required": True},
        "frame_rate": {"type": "integer"},
    }
    if "image" in extra:
        base["input_reference"] = {"type": "asset", "accepts": ["image/*"]}
        base["image_strength"] = {"type": "number", "minimum": 0, "maximum": 1, "default": 0.7}
    return base


def video_contract(short: str = "ltx-2.5-t2v", *, grid: dict | None = None, variants: dict | None = None, requires: list | None = None,
                   chain: bool = False, image: bool = False, size: dict | None = None, length: dict | None = None) -> dict[str, Any]:
    roles = {"width": "width", "height": "height", "length": "length", "frame_rate": "frame_rate"}
    if image:
        roles |= {"image": "input_reference", "image_strength": "image_strength"}
    return {
        "id": f"video.generate.{short}", "version": "1", "schema_version": 2, "intent": "video.generate", "capability": "video",
        "inputs": _inputs("image") if image else _inputs(),
        "outputs": {"video": {"media_type": "video/mp4"}},
        "video": {
            "roles": roles,
            "size": size or {"default": "960x512", "max": "1280x704", "multiple": 64},
            "length": length or {"min_seconds": 1, "max_seconds": 10},
            "grid": grid or {"fps": 24, "step": 8, "offset": 1},
            "variants": variants or {}, "requires": requires or [], "chain": chain},
        "bindings": {"comfyui": {}},
    }


def snap(*contracts: dict[str, Any]) -> FakeSnapshot:
    return FakeSnapshot(1, MappingProxyType({c["id"]: entry(c) for c in contracts}))


T2V = video_contract(variants={"image": "video.generate.ltx-2.5-i2v", "long": "video.generate.ltx-2.5-long"})
I2V = video_contract("ltx-2.5-i2v", image=True, requires=["image"])
LONG = video_contract("ltx-2.5-long", chain=True)
SNAP = snap(T2V, I2V, LONG)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 80


@pytest.fixture(autouse=True)
def no_deployment_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shapes, "DEFAULT_WORKFLOWS", {})
    monkeypatch.setattr(shapes, "VIDEO_HOOKS", None, raising=False)


def read(body: Any, snapshot: FakeSnapshot = SNAP, *, row: str = "videos-create", spool: FakeSpool | None = None, **headers: str):
    shape = frontend_shapes.build_shape(ROWS[row])
    request = json_request(body, headers=headers) if not isinstance(body, Request) else body
    return asyncio.run(shape.to_run_request(request, ROWS[row], snapshot, spool=spool or FakeSpool()))


def error(response: Response) -> dict[str, Any]:
    assert isinstance(response, Response), response
    return json.loads(response.body)["error"]


# ------------------------------------------------------------------------------------------------ the rows


def test_the_create_row_carries_the_old_words_and_rules_as_data() -> None:
    row = ROWS["videos-create"]
    assert row.refusal_bodies["service_disabled"] == {"message": "Video generation not enabled", "type": "service_unavailable"}
    assert (row.job_retries, row.job_failure_text) == ("none", "adapter")
    assert dict(row.result_headers) == {"duration_adjusted": "X-Duration-Adjusted"}      # the only header the old create sent


def test_a_remix_row_exists_and_both_source_rows_say_what_an_unconfigured_route_answers() -> None:
    remix, edit = ROWS["videos-remix"], ROWS["videos-edit"]
    assert remix.shape == "openai.video.remix" and remix.mode == "job"
    assert [r.canonical for r in remix.routes] == ["/v1/videos/{video_id}/remix"]
    assert remix.routes[0].runtime_policy["intent"] == "video.remix"
    for row in (remix, edit):
        assert row.unconfigured and row.unconfigured["status"] == 501 and row.unconfigured["code"] == "video_remix_not_configured"
        assert row.unconfigured["message"]


def test_a_row_parser_reads_unconfigured_and_refuses_a_malformed_one() -> None:
    raw = {"id": "r", "shape": "openai.video.create", "mode": "job", "selects": {"ids": ["x"]},
           "routes": [{"methods": ["POST"], "canonical": "/v1/x"}],
           "unconfigured": {"status": 501, "code": "c", "message": "m"}}
    assert parse_rows({"frontends": [raw]})[0].unconfigured == {"status": 501, "code": "c", "message": "m"}
    with pytest.raises(RowError, match="unconfigured"):
        parse_rows({"frontends": [{**raw, "unconfigured": {"status": 501}}]})


# ------------------------------------------------------------------ the core: a service that cannot be reached


def test_an_unreachable_service_is_a_502_naming_the_engine_and_no_job_exists(db, tmp_path) -> None:
    core, port, _ = make_core(db, tmp_path, core_snapshot())
    port.available = BindingAvailability(lambda binding: SERVICE_UNREACHABLE, None, lambda binding: "Engine Z unavailable at http://z:1")
    gate = core.target_gate("demo.run", Caller(), snapshot=core._snapshots.current())
    assert isinstance(gate, RunRefused) and (gate.http_status, gate.code) == (502, "service_unreachable")
    assert gate.message == "Engine Z unavailable at http://z:1"
    refused = run(core.submit_run(core_request(), Caller(), snapshot=core._snapshots.current()))
    assert isinstance(refused, RunRefused) and refused.http_status == 502
    from gateway import media_jobs
    with media_jobs.connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0] == 0
    assert port.engine_calls == 0


# ------------------------------------------------------------------------------------ the shape: the happy path


def test_a_plain_create_fills_size_frames_rate_and_seed_from_the_manifest_grid() -> None:
    run_request = read({"prompt": "waves", "model": "ltx-2.5-t2v", "size": "1280x720", "seconds": 3, "seed": 7})
    assert run_request.workflow_id == "video.generate.ltx-2.5-t2v"
    assert run_request.inputs["prompt"] == "waves" and run_request.inputs["seed"] == 7
    assert (run_request.inputs["width"], run_request.inputs["height"]) == (1280, 704)      # cut to the maximum, floored to 64
    assert run_request.inputs["length"] == 73 and run_request.inputs["frame_rate"] == 24      # the documented 3 s at 24 fps
    assert run_request.options == {"size": "1280x704", "seconds_frames": 73, "frame_rate": 24}
    facts = run_request.facts
    assert (facts["model"], facts["resolved_model"], facts["size"], facts["seconds"]) == ("ltx-2.5-t2v", "video-ltx-2.5-t2v", "1280x704", "3")
    assert (facts["length_frames"], facts["frame_rate"], facts["seed"]) == (73, 24, 7)
    assert facts["actual_seconds"] == round(73 / 24, 4)


def test_a_request_without_size_seconds_or_seed_uses_the_manifest_defaults_and_draws_a_seed() -> None:
    run_request = read({"prompt": "x", "model": "ltx-2.5-t2v"})
    assert (run_request.inputs["width"], run_request.inputs["height"]) == (960, 512)
    assert run_request.facts["seconds"] == "5" and run_request.inputs["length"] == 121
    assert "seed" not in run_request.inputs and "seed" not in run_request.facts          # the core draws it, once, after the claim


def test_an_unreadable_size_is_cut_to_the_maximum_and_not_refused() -> None:
    run_request = read({"prompt": "x", "model": "ltx-2.5-t2v", "size": "bad"})
    assert run_request.facts["size"] == "1024x704"


def _with_rate(fps: int | None, seconds: float = 3) -> Any:
    body: dict[str, Any] = {"prompt": "x", "model": "ltx-2.5-t2v", "seconds": seconds}
    if fps is not None:
        body["extensions"] = {"com.azizam.workflow": {"frame_rate": fps}}
    return read(body)


@pytest.mark.parametrize("fps,frames", [(12, 33), (24, 73), (30, 89)])
def test_a_frame_rate_extension_sets_the_frame_count_the_duration_the_inputs_the_facts_and_the_options(fps: int, frames: int) -> None:
    run_request = _with_rate(fps)
    assert run_request.inputs["length"] == frames and run_request.inputs["frame_rate"] == fps
    assert run_request.facts["length_frames"] == frames and run_request.facts["frame_rate"] == fps
    assert run_request.facts["actual_seconds"] == round(frames / fps, 4)
    assert run_request.options == {"size": "960x512", "seconds_frames": frames, "frame_rate": fps}


def test_a_frame_rate_that_is_not_a_positive_number_is_refused() -> None:
    for bad in (0, -5, "fast", float("nan")):
        refused = error(_with_rate(bad))
        assert refused["param"] == "frame_rate"


def test_size_auto_reads_as_the_old_route_read_it_and_the_clamp_comes_before_the_floor() -> None:
    """Pin of main's ``parse_size`` + ``_parse_size_clamped``: "auto" (any text that is not WxH) is 1024x1024, not the
    manifest default; the side is cut to the maximum first and floored to the multiple after, so an unaligned maximum is
    floored too."""
    assert read({"prompt": "x", "model": "ltx-2.5-t2v", "size": "auto"}).facts["size"] == "1024x704"
    unaligned = snap(video_contract("odd", size={"default": "960x512", "max": "1280x700", "multiple": 64}))
    assert read({"prompt": "x", "model": "odd", "size": "1920x1080"}, unaligned).facts["size"] == "1280x640"


def test_the_seconds_string_is_the_old_one_and_the_frame_count_follows_the_grid_bounds() -> None:
    capped = snap(video_contract("cap", grid={"fps": 24, "step": 4, "offset": 1, "max_frames": 81}))
    run_request = read({"prompt": "x", "model": "cap", "seconds": 8}, capped)
    assert run_request.inputs["length"] == 81 and run_request.facts["seconds"] == "8"
    assert read({"prompt": "x", "model": "ltx-2.5-t2v", "seconds": "3.5"}).facts["seconds"] == "3.5"


def test_the_answer_to_a_create_is_the_old_video_object_with_the_names_the_caller_used() -> None:
    run_request = read({"prompt": "waves", "model": "ltx-2.5-t2v", "seconds": 3, "size": "640x384"})
    shape = frontend_shapes.build_shape(ROWS["videos-create"])
    view = RunJobView(job_id="mj_a", status="queued", disposition="new", workflow_id=run_request.workflow_id, record_revision=1,
                      binding_key="comfyui", created_at_s=1000, updated_at_s=1000)
    request = make_request()
    request.state.video_facts = dict(run_request.facts)
    answer = asyncio.run(shape.to_response(view, ROWS["videos-create"], jobs=_NoFacts(), caller=CALLER, request=request))
    body = json.loads(answer.body)
    assert answer.status_code == 200 and "location" not in answer.headers
    assert (body["id"], body["object"], body["status"], body["progress"]) == ("video_mj_a", "video", "queued", 0)
    assert (body["model"], body["azizam_resolved_model"], body["size"], body["seconds"]) == (
        "ltx-2.5-t2v", "video-ltx-2.5-t2v", "640x384", "3")
    assert (body["created_at"], body["expires_at"], body["completed_at"], body["error"]) == (1000, 1000 + 86400, None, None)
    assert "azizam_status" not in body and "updated_at" not in body                  # the create answer never had them


class _NoFacts:
    """A job service that holds no recorded facts: the answer falls back to the facts the shape read."""

    def request_facts(self, job_id, caller):
        return None


def test_an_adjusted_duration_is_told_in_the_header_and_an_exact_one_is_not() -> None:
    run_request = read({"prompt": "x", "model": "ltx-2.5-t2v", "seconds": 3.2})      # 77 frames snap to 81: 0.175 s off
    shape = frontend_shapes.build_shape(ROWS["videos-create"])
    view = RunJobView(job_id="mj_a", status="queued", disposition="new", workflow_id=run_request.workflow_id, record_revision=1,
                      binding_key="comfyui", created_at_s=1, updated_at_s=1)
    request = make_request()
    request.state.video_facts = dict(run_request.facts)
    answer = asyncio.run(shape.to_response(view, ROWS["videos-create"], jobs=_NoFacts(), caller=CALLER, request=request))
    assert answer.headers["X-Duration-Adjusted"].startswith("requested=3.2 actual=3.3750 reason=")
    close = read({"prompt": "x", "model": "ltx-2.5-t2v", "seconds": 3})       # 3.0417 s is within the old 0.05 s tolerance
    request = make_request()
    request.state.video_facts = dict(close.facts)
    assert "X-Duration-Adjusted" not in asyncio.run(shape.to_response(view, ROWS["videos-create"], jobs=_NoFacts(), caller=CALLER, request=request)).headers
    exact = read({"prompt": "x", "model": "ltx-2.5-t2v", "seconds": 5, "size": "960x512"})      # 5 s is 121 frames: exact
    request = make_request()
    request.state.video_facts = dict(exact.facts)
    quiet = asyncio.run(shape.to_response(view, ROWS["videos-create"], jobs=_NoFacts(), caller=CALLER, request=request))
    assert "X-Duration-Adjusted" not in quiet.headers


# ------------------------------------------------------------------------- the shape: refusals, in the old order


def test_a_body_that_is_not_json_is_the_old_400() -> None:
    refused = read(make_request(b"{nope"))
    assert refused.status_code == 400 and error(refused) == {"message": "Invalid JSON body", "type": "invalid_request_error"}


def test_an_unknown_field_is_refused_with_its_name_and_the_unknown_parameter_code() -> None:
    refused = read({"prompt": "x", "model": "ltx-2.5-t2v", "colour": "red"})
    body = error(refused)
    assert refused.status_code == 400 and body["param"] == "colour" and body["code"] == "unknown_parameter"


def test_a_malformed_video_control_is_refused_before_the_prompt_is_read() -> None:
    refused = read({"prompt": 5, "extensions": {"com.azizam.video": {"audio": "no"}}})
    body = error(refused)
    assert refused.status_code == 400 and body["code"] == "invalid_video_control" and "audio" in body["param"]


@pytest.mark.parametrize(("body", "message", "param"), [
    ({"prompt": ["a"]}, "prompt must be a string", "prompt"),
    ({"prompt": "   "}, "prompt is required", None),
    ({}, "prompt is required", None)])
def test_the_prompt_is_checked_with_the_old_words(body: dict, message: str, param: str | None) -> None:
    refused = read({"model": "ltx-2.5-t2v", **body})
    found = error(refused)
    assert refused.status_code == 400 and found["message"] == message and found.get("param") == param
    assert found["type"] == "invalid_request_error" and "code" not in found


def test_an_unknown_model_is_a_404_with_the_old_text_and_code() -> None:
    refused = read({"prompt": "x", "model": "nope"})
    body = error(refused)
    assert refused.status_code == 404 and body["message"] == "Model 'nope' is not available for video generation."
    assert (body["code"], body["param"]) == ("model_not_found", "model")


@pytest.mark.parametrize(("seconds", "message"), [
    (0, "seconds must be between 1.0 and 10.0 for model video-ltx-2.5-t2v"),
    (-1, "seconds must be between 1.0 and 10.0 for model video-ltx-2.5-t2v"),
    ("bad", "seconds must be a number")])
def test_seconds_out_of_range_or_not_a_number_is_refused_with_the_old_words(seconds: Any, message: str) -> None:
    refused = read({"prompt": "x", "model": "ltx-2.5-t2v", "seconds": seconds})
    assert refused.status_code == 400 and error(refused)["message"] == message


def test_an_image_to_video_model_without_a_reference_is_refused_and_a_reference_selects_it() -> None:
    refused = read({"prompt": "x", "model": "ltx-2.5-i2v"})
    body = error(refused)
    assert refused.status_code == 400 and body["param"] == "input_reference"
    assert body["message"] == "Model video-ltx-2.5-i2v is image-to-video and requires input_reference"
    data = "data:image/png;base64," + base64.b64encode(PNG).decode()
    spool = FakeSpool()
    run_request = read({"prompt": "x", "model": "ltx-2.5-t2v", "input_reference": data, "image_strength": 5}, spool=spool)
    assert run_request.workflow_id == "video.generate.ltx-2.5-i2v"
    assert run_request.inputs["image_strength"] == 1.0                    # clamped to 0..1
    assert [(name, blob) for name, blob, *_ in spool.staged] == [("input_reference", PNG)]
    assert set(run_request.media) == {"input_reference"}
    assert run_request.facts["resolved_model"] == "video-ltx-2.5-i2v"


def test_a_silent_clip_is_recorded_as_a_fact_and_an_option() -> None:
    run_request = read({"prompt": "x", "model": "ltx-2.5-t2v", "extensions": {"com.azizam.video": {"audio": False}}})
    assert run_request.facts["audio"] is False and run_request.options["audio"] is False
    assert "audio" not in read({"prompt": "x", "model": "ltx-2.5-t2v"}).facts


def test_the_facts_hold_no_prompt_no_person_and_only_the_keys_the_design_names() -> None:
    run_request = read({"prompt": "a private secret sentence", "model": "ltx-2.5-t2v"})
    assert "private secret sentence" not in json.dumps(run_request.facts, default=str)
    assert set(run_request.facts) <= {"model", "resolved_model", "size", "seconds", "actual_seconds", "length_frames", "frame_rate",
                                      "seed", "audio", "metadata_mode", "provenance", "remixed_from_video_id",
                                      "extended_from_video_id", "source_asset_id", "total_seconds", "warnings"}
    assert run_request.facts["provenance"]["kind"] == "video"
    assert "private secret sentence" not in json.dumps(run_request.facts["provenance"], default=str)


def test_a_request_the_shape_does_not_move_is_handed_to_the_legacy_handler_with_its_parsed_body() -> None:
    for body in ({"model": "ltx-2.5-t2v", "shots": [{"prompt": "a"}]},                   # a storyboard
                 {"prompt": "x", "model": "ltx-2.5-t2v", "seconds": 20},                   # past the native length: the long variant
                 {"prompt": "x", "model": "ltx-2.5-long"},                                 # a chain manifest
                 {"prompt": "x", "model": "ltx-2.5-t2v", "inputs": {"a": 1}},
                 {"prompt": "x", "model": "ltx-2.5-t2v", "input_reference": "img_abc"}):    # an asset id the shape cannot resolve
        handoff = read(body)
        assert isinstance(handoff, _base.LegacyRoute), body
        assert handoff.name == "videos.create" and handoff.body.get("prompt") == body.get("prompt")


def test_a_manifest_without_a_video_block_is_left_to_the_legacy_handler() -> None:
    plain = video_contract("bare")
    del plain["video"]
    assert isinstance(read({"prompt": "x", "model": "bare"}, snap(plain)), _base.LegacyRoute)


def test_a_model_with_no_long_variant_refuses_shots_by_code_and_a_long_clip_by_its_range() -> None:
    """The old route named ``long_video_not_supported`` only for shots, or for a clip past the length of a model that names a
    chain it cannot reach; a model that names none answers a clip past its length with the plain range text."""
    lone = snap(video_contract("lone"))
    refused = read({"prompt": "x", "model": "lone", "seconds": 20}, lone)
    body = error(refused)
    assert refused.status_code == 400 and "code" not in body and body["message"].startswith("seconds must be between")
    shots = read({"model": "lone", "shots": [{"prompt": "a"}]}, lone)
    assert shots.status_code == 400 and error(shots)["param"] == "shots" and error(shots)["code"] == "long_video_not_supported"
    assert "10 s" in error(shots)["message"]


def test_the_prompt_is_kept_in_prompt_memory_after_a_new_job_and_not_for_a_replay() -> None:
    calls: list[tuple[str, str]] = []

    class Hooks(shapes.VideoHooks):
        def keep_prompt(self, caller, key, prompt):
            calls.append((key, prompt))
            return True

    shapes.VIDEO_HOOKS = Hooks()
    run_request = read({"prompt": "waves", "model": "ltx-2.5-t2v"})
    shape = frontend_shapes.build_shape(ROWS["videos-create"])
    new = RunJobView(job_id="mj_a", status="queued", disposition="new", workflow_id="w", record_revision=1, binding_key="b",
                     created_at_s=1, updated_at_s=1)
    request = make_request()
    request.state.video_prompt = "waves"
    asyncio.run(shape.after_submit(request, new, CALLER))
    asyncio.run(shape.after_submit(request, RunJobView(**{**new.__dict__, "disposition": "replay"}), CALLER))
    assert calls == [("video_mj_a", "waves")]
    assert run_request.facts["model"] == "ltx-2.5-t2v"


def test_the_deployment_hooks_resolve_the_model_name_and_can_refuse_the_body() -> None:
    class Hooks(shapes.VideoHooks):
        def resolve_model(self, name):
            return "ltx-2.5-t2v" if name == "sora-2" else name

        def begin_delivery(self, caller, body):
            return Response("no", status_code=400) if body.get("metadata_bad") else None

    shapes.VIDEO_HOOKS = Hooks()
    run_request = read({"prompt": "x", "model": "sora-2"})
    assert run_request.workflow_id == "video.generate.ltx-2.5-t2v" and run_request.facts["model"] == "sora-2"
    refused = shapes.VIDEO_HOOKS.begin_delivery(CALLER, {"metadata_bad": 1})
    assert refused.status_code == 400


# --------------------------------------------------------- the engine must take the job before the queued answer (review 1)


def _count(db, table: str) -> int:
    from gateway import media_jobs
    with media_jobs.connection(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _drive(db, tmp_path, adapter, **request_fields):
    """Submit one waiting run whose scheduled job really runs through ``adapter`` on the same loop."""
    snap = core_snapshot()
    core, port, _ = make_core(db, tmp_path, snap)
    port.run_adapter = adapter
    tasks: list[asyncio.Task] = []
    port.schedule = lambda job_id: tasks.append(asyncio.get_running_loop().create_task(core.execute_run(job_id)))

    async def go():
        import dataclasses
        outcome = await core.submit_run(dataclasses.replace(core_request(), submit_wait_s=5.0, **request_fields), Caller(),
                                        snapshot=core._snapshots.current())
        await asyncio.gather(*tasks)
        return outcome

    return asyncio.run(go()), port


def test_a_submit_the_engine_refuses_is_a_502_and_no_job_is_kept_though_the_health_probe_passed(db, tmp_path) -> None:
    class Refused(Exception):
        code, category, retryable = "upstream_refused", "upstream", False

    async def refusing(ctx):
        raise Refused("the engine refused the prompt")

    outcome, port = _drive(db, tmp_path, refusing)
    assert port.problem is None                                   # the health preflight said the service is fine
    assert isinstance(outcome, RunRefused) and outcome.http_status == 502 and outcome.code == "upstream_refused"
    assert _count(db, "media_jobs") == 0 and _count(db, "media_job_events") == 0


def test_a_submit_the_engine_took_answers_queued_without_waiting_for_the_render(db, tmp_path) -> None:
    from gateway import media_jobs

    async def taking(ctx):
        media_jobs.update_status(ctx.job_id, "running", metadata_patch={"protocol_job": "up-1"}, path=db)
        await asyncio.sleep(30)                                   # the render goes on; the answer must not wait for it

    outcome, _ = _drive_background(db, tmp_path, taking)
    assert not isinstance(outcome, RunRefused) and outcome.disposition == "new" and _count(db, "media_jobs") == 1


def _drive_background(db, tmp_path, adapter, **request_fields):
    snap = core_snapshot()
    core, port, _ = make_core(db, tmp_path, snap)
    port.run_adapter = adapter

    async def go():
        import dataclasses
        tasks: list[asyncio.Task] = []
        port.schedule = lambda job_id: tasks.append(asyncio.get_running_loop().create_task(core.execute_run(job_id)))
        outcome = await core.submit_run(dataclasses.replace(core_request(), submit_wait_s=5.0, **request_fields), Caller(),
                                        snapshot=core._snapshots.current())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return outcome

    return asyncio.run(go()), port


# ------------------------------------------- the seed is drawn once by the core, and the facts learn it by a typed ref (review 4, 8)


def test_two_creates_without_a_seed_are_one_request_for_the_idempotency_key() -> None:
    from gateway.workflow_kernel import run_identity
    first, second = (read({"prompt": "x", "model": "ltx-2.5-t2v"}) for _ in range(2))
    assert run_identity("scope", "maker", first) == run_identity("scope", "maker", second)
    seeded = read({"prompt": "x", "model": "ltx-2.5-t2v", "seed": 3})
    assert run_identity("scope", "maker", seeded) != run_identity("scope", "maker", first)       # an explicit seed is an input


def test_the_facts_wait_for_the_wire_id_and_the_drawn_seed_by_named_refs_that_never_enter_identity() -> None:
    from gateway.workflow_kernel import run_identity
    from gateway.workflow_run import resolve_fact_refs
    run_request = read({"prompt": "x", "model": "ltx-2.5-t2v"})
    assert run_request.facts["provenance"]["request_id"] == "{wire_id}"                  # the stamp stage fills the real id
    assert run_request.facts["provenance"].get("seed") is None
    assert {(ref.path, ref.source) for ref in run_request.fact_refs} == {
        (("seed",), "generated:seed"), (("provenance", "seed"), "generated:seed")}
    resolved = resolve_fact_refs(run_request.facts, run_request.fact_refs, job_id="mj_a", generated={"seed": 11})
    assert resolved["seed"] == 11 and resolved["provenance"]["seed"] == 11 and resolved["provenance"]["request_id"] == "{wire_id}"
    assert run_request.facts["provenance"].get("seed") is None                          # the request itself is not changed
    import dataclasses
    assert run_identity("s", "m", dataclasses.replace(run_request, fact_refs=())) == run_identity("s", "m", run_request)
    explicit = read({"prompt": "x", "model": "ltx-2.5-t2v", "seed": 5})
    assert explicit.facts["seed"] == 5 and explicit.facts["provenance"]["seed"] == 5
    assert all(ref.source != "generated:seed" for ref in explicit.fact_refs)
    assert resolve_fact_refs(run_request.facts, run_request.fact_refs, job_id="mj_a", generated={})["provenance"].get("seed") is None


# ----------------------------------------- a chain is the old handler's alone: no row policy, no row receipt (review 13)


def _row_app(calls: dict[str, list], snapshot=SNAP):
    from fastapi import FastAPI
    from gateway import row_routes

    async def legacy(request, caller, body):
        calls["legacy"].append((caller, dict(body)))
        return Response("old", status_code=200)

    class _Owner:
        def report(self, **_): calls["report"].append(1)
        def handoff(self): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    deps = row_routes.RowRouteDeps(
        authenticate=lambda request, path: calls["auth"].append(path) or "caller-1",
        enforce_policy=lambda caller, path: calls["policy"].append(path),
        core=lambda: object(), snapshot=lambda: snapshot, jobs=lambda: None, new_spool=FakeSpool,
        scope=lambda request, spool: __import__("contextlib").nullcontext(), refused=lambda r: Response("refused", status_code=r.http_status),
        new_request_id=lambda: "req-1", open_receipt=lambda *a: calls["receipt"].append(a) or _Owner(),
        name_receipt=lambda *a: None, pinned=lambda job_id: None, legacy={_base_legacy(): legacy})
    app = FastAPI()
    row_routes.mount_rows(app, deps, ("videos-create",))
    return app


def _base_legacy() -> str:
    from gateway.frontend_shapes import video_create
    return video_create.LEGACY_CREATE


def test_a_chain_create_reaches_the_old_handler_with_no_row_policy_and_no_row_receipt() -> None:
    from starlette.testclient import TestClient
    calls: dict[str, list] = {key: [] for key in ("auth", "policy", "receipt", "report", "legacy")}
    client = TestClient(_row_app(calls))
    answer = client.post("/v1/videos", json={"prompt": "x", "model": "ltx-2.5-long"})
    assert answer.status_code == 200 and answer.text == "old"
    assert calls["auth"] == ["/v1/videos"]                    # authenticated once, by the row
    assert calls["policy"] == [] and calls["receipt"] == [] and calls["report"] == []     # the old handler owns both
    assert [body["prompt"] for _, body in calls["legacy"]] == ["x"]
    shots = client.post("/openai/v1/videos", json={"model": "ltx-2.5-t2v", "shots": [{"prompt": "a"}]})
    assert shots.text == "old" and len(calls["legacy"]) == 2 and calls["receipt"] == []


def test_a_plain_create_is_never_handed_to_the_old_handler_and_opens_the_row_receipt_after_the_policy() -> None:
    from starlette.testclient import TestClient
    calls: dict[str, list] = {key: [] for key in ("auth", "policy", "receipt", "report", "legacy")}
    client = TestClient(_row_app(calls))
    answer = client.post("/v1/videos", json={"model": "nope-model", "prompt": "x"})
    assert answer.status_code == 404 and answer.json()["error"]["code"] == "model_not_found"
    assert calls["legacy"] == [] and len(calls["policy"]) == 1 and len(calls["receipt"]) == 1


def test_keyframes_create_sends_frames_and_is_accepted() -> None:
    """An input the manifest declares is never refused as an extension, even when it fills a role: ``frames`` is the keyframes
    length role and callers send it today. It wins over ``seconds``; a name the manifest does not declare is left to the old pipeline, which refuses it."""
    key = video_contract("ltx-2.5-keyframes", requires=["extensions"], image=False)
    key["inputs"]["frames"] = {"type": "integer", "minimum": 9, "maximum": 241}
    key["video"]["roles"]["length"] = "frames"
    del key["inputs"]["length"]
    snapshot = snap(key)
    block = {"com.azizam.workflow": {"frames": 49}}
    run_request = read({"model": "ltx-2.5-keyframes", "prompt": "p", "seconds": 5, "extensions": block}, snapshot)
    assert isinstance(run_request, RunRequest), run_request
    assert run_request.inputs["frames"] == 49
    # A name the manifest does not declare in the workflow block was refused (400 invalid_workflow_extension) by the old
    # pipeline's manifest check (tests/test_video_keyframes.py pins it): the shape hands it back and does not run it.
    undeclared = read({"model": "ltx-2.5-keyframes", "prompt": "p", "extensions": {"com.azizam.workflow": {"frames": 49, "bogus": 1}}}, snapshot)
    assert isinstance(undeclared, _base.LegacyRoute) and undeclared.name == "videos.create"


def _submit_app(calls: dict[str, list], *, disposition: str = "new"):
    """The row app with a core that accepts every run as a queued job; the receipt owner records what the route does to it."""
    from fastapi import FastAPI
    from gateway import row_routes

    class Core:
        async def submit_run(self, run_request, caller, *, snapshot):
            return RunJobView(job_id="mj_a", status="queued", disposition=disposition, workflow_id=run_request.workflow_id,
                              record_revision=1, binding_key="comfyui", created_at_s=1, updated_at_s=1)

    class Owner:
        receipt = type("Receipt", (), {"job_id": None, "metadata": {}})()      # a submit receipt names the job it queued
        def report(self, **outcome): calls["report"].append(outcome)
        def handoff(self): calls["handoff"].append(1)
        def dispatching(self, backend): calls["backend"].append(backend)
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    deps = row_routes.RowRouteDeps(
        authenticate=lambda request, path: "caller-1", enforce_policy=lambda caller, path: None,
        core=lambda: Core(), snapshot=lambda: SNAP, jobs=lambda: None, new_spool=FakeSpool,
        scope=lambda request, spool: __import__("contextlib").nullcontext(), refused=lambda r: Response("refused", status_code=r.http_status),
        new_request_id=lambda: "req-1", open_receipt=lambda *a: Owner(),
        name_receipt=lambda *a: calls["named"].append(a), pinned=lambda job_id: None, legacy={},
        record_inputs=lambda owner, notes: calls["inputs"].append(notes), record_facts=lambda owner, facts: calls["facts"].append(facts))
    app = FastAPI()
    row_routes.mount_rows(app, deps, ("videos-create",))
    return app


def _keys() -> dict[str, list]:
    return {key: [] for key in ("report", "handoff", "named", "inputs", "facts", "backend")}


def test_a_queued_create_closes_its_receipt_as_succeeded_and_names_nothing() -> None:
    """Item 2: the old create receipt was a submit receipt. It closes when the queued answer goes out and names no workflow,
    manifest, resolved model or accepted input; the job never takes it over, so a later render failure cannot fail it."""
    from starlette.testclient import TestClient
    calls = _keys()
    answer = TestClient(_submit_app(calls)).post("/v1/videos", json={"prompt": "x", "model": "ltx-2.5-t2v"})
    assert answer.status_code == 200 and answer.json()["status"] == "queued"
    assert calls["report"] == [{"status": "succeeded", "reason": "http_200"}]
    assert calls["handoff"] == [] and calls["named"] == [] and calls["inputs"] == []


def test_a_create_names_its_service_on_the_receipt_only_once_the_run_is_submitted() -> None:
    """The old route set receipt.backend after model resolution: a refusal before that left it unset."""
    from starlette.testclient import TestClient
    calls = _keys()
    TestClient(_submit_app(calls)).post("/v1/videos", json={"prompt": "x", "model": "ltx-2.5-t2v"})
    assert len(calls["backend"]) == 1
    refused = _keys()
    TestClient(_submit_app(refused)).post("/v1/videos", json={"prompt": "x", "model": "no-such-model"})
    assert refused["backend"] == []


def test_a_replay_of_a_create_also_names_nothing_on_its_receipt() -> None:
    from starlette.testclient import TestClient
    calls = _keys()
    TestClient(_submit_app(calls, disposition="replay")).post("/v1/videos", json={"prompt": "x", "model": "ltx-2.5-t2v"})
    assert calls["named"] == [] and calls["handoff"] == []


def test_a_create_adds_no_run_facts_to_the_history_entry() -> None:
    """Item 3: the old video summary has no run block and is titled from the body model."""
    from fastapi import FastAPI  # noqa: F401
    from starlette.testclient import TestClient
    from gateway import request_history_capture
    seen: list[Any] = []
    app = _submit_app(_keys())

    @app.middleware("http")
    async def look(request, call_next):
        response = await call_next(request)
        seen.append(getattr(request.state, request_history_capture.RUN_STATE_KEY, None))
        return response

    TestClient(app).post("/v1/videos", json={"prompt": "x", "model": "ltx-2.5-t2v"})
    assert seen == [None]


# ------------------------------------------------ the refusal order (review 5): replay, then body, then service state


def test_a_finished_key_still_replays_after_the_service_is_disabled_and_a_new_one_is_refused(db, tmp_path) -> None:
    snap = core_snapshot()
    core, port, _ = make_core(db, tmp_path, snap)

    async def go():
        first = await core.submit_run(core_request(idempotency_key="k1"), Caller(), snapshot=snap)
        port.problem = "adapter_missing"                          # the backend is disabled from now on
        replay = await core.submit_run(core_request(idempotency_key="k1"), Caller(), snapshot=snap)
        fresh = await core.submit_run(core_request(idempotency_key="k2"), Caller(), snapshot=snap)
        return first, replay, fresh

    first, replay, fresh = run(go())
    assert replay.disposition == "replay" and replay.job_id == first.job_id
    assert isinstance(fresh, RunRefused) and fresh.http_status == 503


def test_an_invalid_body_is_refused_as_a_body_before_the_service_is_asked() -> None:
    from starlette.testclient import TestClient
    calls = _keys()
    client = TestClient(_submit_app(calls))
    answer = client.post("/v1/videos", json={"prompt": "x", "model": "ltx-2.5-t2v", "seconds": "soon"})
    assert answer.status_code == 400 and answer.json()["error"]["message"] == "seconds must be a number"
    assert calls["report"] == [{"status": "failed", "reason": "http_400"}]


# ----------------------------------------------- what is stored (review 11): read-only storage assertions and a storage fault


def _everything_stored(db) -> str:
    from gateway import media_jobs
    with media_jobs.connection(db) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return "\n".join(repr(row) for table in tables for row in conn.execute(f'SELECT * FROM "{table}"'))


def test_a_single_prompt_create_stores_no_raw_prompt_and_no_legacy_job_record(db, tmp_path, monkeypatch) -> None:
    from gateway import media_jobs, video_job_records
    legacy_writes: list[Any] = []
    monkeypatch.setattr(video_job_records, "save", lambda *a, **k: legacy_writes.append(a))

    async def taking(ctx):
        media_jobs.update_status(ctx.job_id, "running", metadata_patch={"protocol_job": "up-1"}, path=db)
        await asyncio.sleep(30)

    secret = "a-private-sentence-nobody-may-store"
    outcome, _ = _drive_background(db, tmp_path, taking, inputs={"prompt": secret}, facts={"model": "m"})
    assert outcome.disposition == "new" and _count(db, "media_jobs") == 1
    assert secret not in _everything_stored(db)                  # the durable store holds the job, not the raw prompt
    assert legacy_writes == []                                   # no new legacy video record for a single-prompt job


def test_a_storage_fault_while_the_job_is_made_is_an_error_and_keeps_no_job(db, tmp_path, monkeypatch) -> None:
    from gateway import media_jobs

    def failing(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(media_jobs, "create", failing)

    async def never(ctx):
        raise AssertionError("no job was made, so nothing runs")

    try:
        outcome, _ = _drive_background(db, tmp_path, never)
    except OSError:
        outcome = None                                             # the fault reaches the route's error handler, not a queued answer
    assert not isinstance(outcome, RunJobView)
    assert _count(db, "media_jobs") == 0


def test_a_remix_or_edit_takes_the_frame_rate_and_length_from_the_stored_file_not_from_the_request_facts() -> None:
    """Review item 7. The source job asked for 24 fps; its stored file is 30 fps and 4.2 s. The run keeps 30 fps, and a request for
    more seconds than the file holds is refused with the old words."""
    from gateway.video_source import SourceRefused, VideoSource

    class Hooks(shapes.VideoHooks):
        async def source(self, job_id, wire_id, caller):
            if job_id == "mj_open":
                return SourceRefused(400, "Source video is not completed (status=queued)", code="video_not_ready")
            return VideoSource(wire_id=wire_id, asset_id="asset-1", model="m", resolved_model="video-m", fps=30.0, size="1280x704",
                               seconds=4.2, has_audio=True)

    edit = video_contract("ltx-2.5-edit", image=False)
    edit["inputs"]["video"] = {"type": "asset", "accepts": ["video/*"], "required": True}
    edit["inputs"]["seconds"] = {"type": "number"}
    edit["intent"] = "video.edit"
    snapshot = snap(edit)
    row = "videos-edit"
    shapes.VIDEO_HOOKS = Hooks()

    def edit_request(extra: dict) -> Any:
        return read({"model": "video.generate.ltx-2.5-edit", "prompt": "p", "video": {"id": "video_mj_done"}, **extra}, snapshot, row=row)

    try:
        made = edit_request({})
        assert isinstance(made, RunRequest), getattr(made, "body", made)
        assert made.inputs["frame_rate"] == 30 and tuple(made.media) == ("video",)
        too_long = edit_request({"seconds": 6})
        assert too_long.status_code == 400
        assert json.loads(too_long.body)["error"]["message"] == "seconds (6) is longer than the source video (4.20 s)"
        open_job = read({"model": "video.generate.ltx-2.5-edit", "prompt": "p", "video": {"id": "video_mj_open"}}, snapshot, row=row)
        body = json.loads(open_job.body)["error"]
        assert (open_job.status_code, body["message"], body["code"], body["param"]) == (
            400, "Source video is not completed (status=queued)", "video_not_ready", "video")
    finally:
        shapes.VIDEO_HOOKS = None


def test_a_legacy_hand_off_enforces_the_row_routes_policy_before_the_old_pipeline_runs(monkeypatch) -> None:
    """The create route leaves the registered routes, so the route policy for its path is the row's own data. The hand-off
    must still ask for it, or a chain create would skip the plan check the row path makes."""
    import proxy
    seen: dict[str, Any] = {}

    def deny(caller, path, *, policy=None):
        seen.update(path=path, policy=policy)
        return Response(status_code=403)

    async def never(*args, **kwargs):
        raise AssertionError("the old pipeline ran after a denial")

    monkeypatch.setattr(proxy, "_enforce_runtime_route_policy", deny)
    monkeypatch.setattr(proxy, "proxy_graph_video_create", never)
    response = asyncio.run(proxy._legacy_video_create(None, object(), {"prompt": "x"}))
    assert response.status_code == 403 and seen["path"] == "/v1/videos"
    assert seen["policy"] and seen["policy"]["intent"] == "video.generate" and seen["policy"]["protocol"] == "openai.video"
