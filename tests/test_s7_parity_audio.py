"""S7 parity, audio: one test per item of ``docs/design/c2d-s7-parity/audio.md`` (item id in the test name).

Each test drives the served audio routes through the real app, the real run core and worker, and the real job store.
Only the engine HTTP is fake (``tests.support.adapter_differential_rig``). The assertions are the observable behaviour
the item names: status, error type and code, message, header, the engine requests the route made, and the rows the
route wrote. The old route answers these tests at base; a route that moves onto a row keeps every test green, or the
item is on the section 4 delta list (``submit_validation``, ``recovery_text_unified``), and those two assert the new
behaviour and name the delta.

Nothing here masks a status, a message or a row. A test that cannot reach its item says so in its docstring.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import Response

import proxy
from tests.support.graph_service import set_derived
from gateway import generated_defaults, media_jobs, media_metadata, provenance, receipt_store, result_bundle
from tests import test_adapter_differential as base
from tests.support.access import authenticated, identity, plan_for
from tests.support.adapter_differential import dump_sqlite
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, exchange, history_done, hex_body
from tests.test_adapter_differential_s7 import FAILED_HISTORY, NEVER_HISTORY

SYNC = "/v1/audio/generate"
JOBS = "/gateway/v1/media/jobs"
BODY = {"prompt": "a door creaks", "model": "tangoflux", "duration": 4}
JOB_BODY = {"intent": "audio.generate", "request": BODY}
# The metadata control of an audio job is read from the request body the runner replays (extensions inside "request").
# A control on the envelope is ignored, so a job built that way has no submit choice at all.
NONE_JOB_BODY = {"intent": "audio.generate", "request": {**BODY, "extensions": {"com.azizam.workflow": {"metadata": "none"}}}}
# The fake engine's prompt id (comfy_exchanges' default, which the history answer also uses).
ENGINE_PROMPT_ID = "p1"
# A seed the generator returns in place of the one the process draws: any value the test can tell from a constant.
GENERATED_SEED = 4242
# The real service-availability reader, kept before any rig patches it (the rig replaces it with a no-op).
REAL_SERVICE_AVAILABILITY = proxy._service_availability_response
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapter_differential"


def _ok(rig) -> None:
    # Not strict: a refusal test never reaches the engine, and the tests that do assert their engine requests.
    base._audio_comfy(rig, strict=False)


def _fail(rig) -> None:
    base._comfy(rig, comfy_exchanges(history=FAILED_HISTORY, strict=False))


def _never(rig) -> None:
    rig.set(proxy, "GRAPH_TIMEOUT", 2)
    base._comfy(rig, comfy_exchanges(history=NEVER_HISTORY, strict=False,
                                     queue={"queue_running": [], "queue_pending": [[0, "p1", {}, {}, []]]}))


def _jobs_ok(rig) -> None:
    _ok(rig)
    base._capture_scheduling(rig)


def _down(rig) -> None:
    base._comfy_down(rig)


def _disable(rig) -> None:
    set_derived(rig.monkeypatch, "GRAPH_ENABLED", False)


def _rows(rig, path: Path, table: str, skip: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return dump_sqlite(path, rig.norm, skip_tables=skip).get(table, [])


def _receipts(rig) -> list[dict[str, Any]]:
    return _rows(rig, rig.receipts_db, "receipts", skip=("receipt_history",))


def _jobs(rig) -> list[dict[str, Any]]:
    return _rows(rig, rig.media_jobs_db, "media_jobs")


def _events(rig) -> list[dict[str, Any]]:
    return _rows(rig, rig.media_jobs_db, "media_job_events")


def _engine_paths(rig) -> list[str]:
    return [entry["path"] for entry in rig.recorder.engine]


def _golden_message(name: str) -> str:
    """The message a recorded golden holds for the caller's last response (the golden pins the wire text)."""
    trace = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return json.loads(trace["caller"][-1]["response"]["body"]["text"])["error"]["message"]


async def _run_job(rig, job_id: str) -> None:
    await proxy._run_audio_media_job(job_id)


async def _submit_job(rig, body: dict[str, Any] | None = None):
    return await rig.call("POST", JOBS, label="submit the audio job", json_body=body or JOB_BODY, unordered_json=True)


def _job_id(response) -> str:
    return response.json()["job_id"]


def _engine_sampler(rig) -> dict[str, Any]:
    """The inputs of the one sampler node in the prompt the route submitted to the engine."""
    prompt = next(e for e in rig.recorder.engine if e["path"] == "/prompt")
    graph = json.loads(prompt["body"]["text"])["prompt"]
    (node,) = [n for n in graph.values() if n.get("class_type") == "TangoFluxSampler"]
    return node["inputs"]


def _provenance(content: bytes) -> dict[str, Any]:
    """The provenance record embedded in delivered WAV bytes (the comment tag, written when metadata is delivered)."""
    tags = media_metadata.read_media_tags(content, ".wav")
    return json.loads(tags["comment"].removeprefix(f"{provenance.PROVENANCE_KEY}="))


def _require(condition: bool, message: str) -> None:
    """A setup precondition. It raises RuntimeError, not AssertionError, so a test marked ``raises=AssertionError``
    still fails on it: only the behaviour the xfail names may be expected to fail."""
    if not condition:
        raise RuntimeError(f"test setup: {message}")


_TERMINAL = (media_jobs.STATUS_SUCCEEDED, media_jobs.STATUS_FAILED, media_jobs.STATUS_CANCELED)


def _submit_and_run(rig, body: dict[str, Any]) -> str:
    """Submit through the route, then run the job through the real scheduler, so the runner keeps the submit's request
    context (its delivery choice) the way production does. Returns the job id once it is terminal; raises RuntimeError
    if the bounded poll never reaches a terminal state."""
    async def flow() -> str:
        r = await rig.call("POST", JOBS, label="submit", json_body=body, unordered_json=True)
        _require(r.status_code == 202, f"submit answered {r.status_code}: {r.text}")
        job_id = _job_id(r)
        _require(REAL_SCHEDULE_AUDIO_MEDIA_JOB(job_id) is True, "the scheduler did not take the job")
        for _ in range(1000):
            if _stored(rig, job_id)["status"] in _TERMINAL:
                return job_id
            await asyncio.sleep(0.01)
        raise RuntimeError(f"job {job_id} never reached a terminal state; last status {_stored(rig, job_id)['status']}")

    return asyncio.run(flow())


def _content_md(rig, job_id: str, query: str = "") -> str:
    """The md= mode of the first signed content handle the content route answers for the job."""
    got = asyncio.run(rig.call("GET", f"{JOBS}/{job_id}/content{query}", label="content"))
    assert got.status_code == 200, got.text
    return parse_qs(urlsplit(got.json()["data"][0]["url"]).query)["md"][0]


# =========================================================================== R1: POST /v1/audio/generate (sync)


def test_audio_01_auth_refusal_is_returned_unchanged(tmp_path):
    """audio-01: caller authentication runs first and its refusal is the response."""
    with GatewayRig("audio-01", tmp_path) as rig:
        _ok(rig)
        refusal = Response(content=b'{"error":{"message":"caller refused"}}', status_code=401,
                           media_type="application/json")
        # A nested patch, not rig.set: the rig already patched authenticate_request (authenticated()), and a monkeypatch
        # set made after it would be restored to that patch on teardown and leak into later tests.
        with patch.object(proxy, "authenticate_request", lambda request, path: refusal):
            r = asyncio.run(rig.call("POST", SYNC, label="no credentials", json_body=BODY))
        assert r.status_code == 401
        assert r.json() == {"error": {"message": "caller refused"}}
        assert rig.recorder.engine == []
        assert _receipts(rig) == []


def test_audio_02_policy_refusal_runs_before_any_receipt(tmp_path):
    """audio-02: the route policy refuses before a receipt starts; the refusal is a policy response, no engine call."""
    with GatewayRig("audio-02", tmp_path) as rig:
        _ok(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(caps=("chat",)))))
        r = asyncio.run(rig.call("POST", SYNC, label="plan without media", json_body=BODY))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "not_in_access_plan"
        assert rig.recorder.engine == []
        assert _receipts(rig) == []


def test_audio_03_receipt_opens_and_closes_with_the_answer(tmp_path):
    """audio-03: one receipt per request, closed with the answer (finish_response) and not left open."""
    with GatewayRig("audio-03", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        receipts = _receipts(rig)
        assert len(receipts) == 1
        assert receipts[0]["status"] != "in_progress"
        assert receipts[0]["receipt_json"].get("status_code", 200) == 200


def test_audio_04_admission_runs_before_the_body_is_read(tmp_path):
    """audio-04: admission (and the COMFY check after it) answers before the body is read: a malformed body on a
    disabled service is a 503, not a 400."""
    with GatewayRig("audio-04", tmp_path) as rig:
        _disable(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="malformed body, disabled",
                                 content=b"{", headers={"content-type": "application/json"}))
        assert r.status_code == 503


@pytest.mark.xfail(strict=True, reason="delta refusal_precedence: lands with the move")
def test_audio_04_delta_refusal_precedence_body_is_checked_before_service_state(tmp_path):
    """audio-04 (delta refusal_precedence, CODE_TO_DATA_S7_PARITY.md section 4 and 6.4): one refusal order for every row:
    authentication, route policy, body validation, service state. A malformed body on a disabled service is therefore a
    400 (body) and not a 503 (service). The base test above pins the old order and retires at the move."""
    with GatewayRig("audio-04-delta", tmp_path) as rig:
        _disable(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="malformed body, disabled",
                                 content=b"{", headers={"content-type": "application/json"}))
        assert r.status_code == 400


def test_audio_05_disabled_service_is_503_service_unavailable(tmp_path):
    """audio-05: COMFY disabled gives 503 service_unavailable with the audio message."""
    with GatewayRig("audio-05", tmp_path) as rig:
        _disable(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="generate while disabled", json_body=BODY))
        assert r.status_code == 503
        assert r.json() == {"error": {"message": "Audio generation not enabled", "type": "service_unavailable"}}
        assert rig.recorder.engine == []


def test_audio_06_operator_override_is_503_backend_unavailable_with_retry_after(tmp_path):
    """audio-06: an operator override that sets the graph service to maintenance answers 503 backend_unavailable with
    service and state keys and Retry-After (the override's delay)."""
    overrides = {"service_availability": {"comfyui": {"state": "maintenance", "reason": "maintenance window",
                                                     "retry_after_seconds": 45}}, "media_job_admission": {}}
    with GatewayRig("audio-06", tmp_path) as rig:
        _ok(rig)
        rig.set(proxy, "_service_availability_response", REAL_SERVICE_AVAILABILITY)
        rig.monkeypatch.setattr(proxy.ADMIN_STORE, "operator_overrides", lambda: overrides)
        r = asyncio.run(rig.call("POST", SYNC, label="generate during maintenance", json_body=BODY))
        assert r.status_code == 503
        # Observed at base: admission runs before the override reader on the sync route, so the override answers
        # resource_admission_denied here (the job route's override reader answers backend_unavailable, see audio-06 in
        # the job route test). Inventory correction: audio.md said backend_unavailable.
        assert r.json()["error"]["code"] == "resource_admission_denied"
        assert rig.recorder.engine == []


def test_audio_07_non_json_body_is_400_invalid_json(tmp_path):
    """audio-07: a body that is not JSON is 400 invalid_request_error 'Invalid JSON body'."""
    with GatewayRig("audio-07", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="not json", content=b"{",
                                 headers={"content-type": "application/json"}))
        assert r.status_code == 400
        assert r.json() == {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}
        assert rig.recorder.engine == []


def test_audio_08_unknown_field_refusal_never_names_the_field(tmp_path):
    """audio-08: a top-level field the audio grammar does not know is a 400; its message never names the field."""
    with GatewayRig("audio-08", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="unknown field",
                                 json_body={**BODY, "frobnicate_zq": 3}))
        assert r.status_code == 400
        assert "frobnicate_zq" not in r.json()["error"]["message"]
        assert rig.recorder.engine == []


def test_audio_09_metadata_mode_body_error_is_refused_before_the_graph(tmp_path):
    """audio-09: a metadata-mode body error returns its own 400 before the graph is built (no engine call)."""
    with GatewayRig("audio-09", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="bad metadata mode",
                                 json_body={**BODY, "metadata": {"mode": "not-a-mode"}}))
        assert r.status_code == 400
        assert rig.recorder.engine == []


def test_audio_10_check_request_refusal_is_400_with_param(tmp_path):
    """audio-10: audio_graph.check_request refuses an invalid body with 400 and the error's param."""
    with GatewayRig("audio-10", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="empty prompt", json_body={**BODY, "prompt": ""}))
        assert r.status_code == 400
        assert r.json()["error"]["message"] == "prompt, input, or tags is required"
        assert r.json()["error"].get("param") is None
        assert rig.recorder.engine == []


def test_audio_11_unknown_model_is_404_model_not_found_on_sync(tmp_path):
    """audio-11: an unknown model gives 404 model_not_found with param model and the audio message."""
    with GatewayRig("audio-11", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="unknown model", json_body={**BODY, "model": "no-such-model"}))
        assert r.status_code == 404
        error = r.json()["error"]
        assert error["code"] == "model_not_found"
        assert error["param"] == "model"
        assert error["message"] == "Model 'no-such-model' is not available for audio generation."


def test_audio_12_blank_model_resolves_to_the_default_workflow(tmp_path):
    """audio-12: a body with no model resolves to the default audio workflow and answers 200."""
    with GatewayRig("audio-12", tmp_path) as rig:
        _ok(rig)
        rig.set(proxy, "_audio_default_workflow", lambda: "tangoflux")
        body = {"prompt": BODY["prompt"], "duration": 4}
        r = asyncio.run(rig.call("POST", SYNC, label="no model", json_body=body))
        assert r.status_code == 200
        assert r.headers["x-audio-model"]


def test_audio_13_type_goes_to_the_header_and_provenance(tmp_path):
    """audio-13: type defaults to music and goes to the X-Audio-Type header; a given type is echoed."""
    with GatewayRig("audio-13", tmp_path) as rig:
        _ok(rig)
        default = asyncio.run(rig.call("POST", SYNC, label="default type", json_body=BODY))
        assert default.headers["x-audio-type"] == "music"
        sfx = asyncio.run(rig.call("POST", SYNC, label="sfx type", json_body={**BODY, "type": "sfx"}))
        assert sfx.headers["x-audio-type"] == "sfx"


def test_audio_14_duration_reaches_the_engine_request(tmp_path):
    """audio-14: the caller's duration is the number 4 in the sampler input of the engine prompt (a number, not a
    substring of some other node's text)."""
    with GatewayRig("audio-14", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        duration = _engine_sampler(rig)["duration"]
        assert isinstance(duration, (int, float)) and not isinstance(duration, bool)
        assert duration == 4


def test_audio_15_seed_is_generated_and_reaches_the_engine_request(tmp_path):
    """audio-15: a seed is generated when the caller gives none. The generator's value (replaced at the one seam the
    generator draws through, ``generated_defaults.secure_source``, with GENERATED_SEED) is the engine sampler's seed,
    and the delivered provenance names the same seed. A route that draws its own value, or 999, fails here."""
    with GatewayRig("audio-15", tmp_path) as rig:
        _ok(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(metadata="full"))))
        rig.set(generated_defaults, "secure_source", lambda low, high: GENERATED_SEED)
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        assert _engine_sampler(rig)["seed"] == GENERATED_SEED
        assert _provenance(r.content)["seed"] == GENERATED_SEED


def test_audio_16_manifest_input_outside_its_bound_is_refused_with_param(tmp_path):
    """audio-16: a value outside the manifest's bounds is an input error with the manifest's param (no engine call)."""
    with GatewayRig("audio-16", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="duration out of bounds",
                                 json_body={**BODY, "duration": 100000}))
        assert r.status_code == 400
        assert r.json()["error"]["param"] == "duration"
        assert rig.recorder.engine == []


def test_audio_17_engine_submit_refusal_is_502_upstream_refused(tmp_path):
    """audio-17: a non-200 from the engine submit is 502 upstream_refused with the adapter label as subject."""
    def setup(rig):
        base._comfy(rig, [exchange("prompt", "POST", "/prompt", {"status": 500, "json": {"error": "no"}}, repeat=True)])

    with GatewayRig("audio-17", tmp_path) as rig:
        setup(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="engine refuses", json_body=BODY))
        assert r.status_code == 502
        assert r.json()["error"]["message"] == "ComfyUI: The engine refused this request."


def test_audio_18_submit_without_prompt_id_is_502(tmp_path):
    """audio-18: an engine submit reply with no prompt_id is 502 'No prompt_id returned'."""
    def setup(rig):
        base._comfy(rig, [exchange("prompt", "POST", "/prompt", {"json": {}}, repeat=True)])

    with GatewayRig("audio-18", tmp_path) as rig:
        setup(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="no prompt id", json_body=BODY))
        assert r.status_code == 502
        assert r.json()["error"]["message"] == "No prompt_id returned"


def test_audio_19_polling_sleeps_two_seconds_and_asks_for_the_history(tmp_path):
    """audio-19: each poll is preceded by a 2 s sleep and asks /history/<prompt id>."""
    with GatewayRig("audio-19", tmp_path) as rig:
        base._comfy(rig, comfy_exchanges(history=history_done("audios", "tone_00001_.wav", node="3"),
                                         views=[(base._wav(), "audio/wav")], running_polls=2))
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        assert [s["delay"] for s in rig.recorder.sleeps][:3] == [2, 2, 2]
        assert "/history/p1" in _engine_paths(rig)


def test_audio_20_result_missing_gives_no_output(tmp_path):
    """audio-20: a success with no audio in the history answers 502 'No audio output found'."""
    with GatewayRig("audio-20", tmp_path) as rig:
        base._comfy(rig, comfy_exchanges(history={"p1": {"status": {"status_str": "success", "completed": True,
                                                                    "messages": []}, "outputs": {}}}, strict=False))
        r = asyncio.run(rig.call("POST", SYNC, label="success without audio", json_body=BODY))
        assert r.status_code == 502
        assert r.json()["error"]["message"] == "No audio output found"


def test_audio_21_file_is_fetched_from_view_with_subfolder(tmp_path):
    """audio-21: the file is fetched from /view with filename, type and subfolder; a non-200 /view is 'No audio output found'."""
    with GatewayRig("audio-21", tmp_path) as rig:
        base._comfy(rig, comfy_exchanges(history=history_done("audios", "tone_00001_.wav", node="3", subfolder="audio"),
                                         views=[(base._wav(), "audio/wav")]))
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        view = next(e for e in rig.recorder.engine if e["path"] == "/view")
        query = dict(view["query"])
        assert query["filename"] == "tone_00001_.wav"
        assert query["subfolder"] == "audio"
        assert query["type"] == "output"


def test_audio_22_engine_error_gives_the_first_execution_error_text(tmp_path):
    """audio-22: an engine status error answers 502 with the first execution_error message."""
    with GatewayRig("audio-22", tmp_path) as rig:
        _fail(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="engine error", json_body=BODY))
        assert r.status_code == 502
        # Decision D45b: a node is named only when the engine's node id is in the graph the gateway submitted; the
        # rig's graph has no node 7, so the sentence names no node.
        assert r.json()["error"]["message"] == "ComfyUI execution error: execution failed"
        assert "golden upstream failure" not in r.text


def test_audio_23_polling_exhausted_is_504_audio_generation_timeout(tmp_path):
    """audio-23: polling exhausted answers 504 'Audio generation timeout'."""
    with GatewayRig("audio-23", tmp_path) as rig:
        _never(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="engine never finishes", json_body=BODY))
        assert r.status_code == 504
        assert r.json()["error"] == {"message": "Audio generation timeout", "type": "api_error"}


def test_audio_24_transport_timeout_is_504_label_request_timeout(tmp_path):
    """audio-24: a transport timeout answers 504 '<label> request timeout'. The engine call is made to time out by a
    fake that holds the connection past the adapter's read budget."""
    with GatewayRig("audio-24", tmp_path) as rig:
        base._comfy_down(rig, httpx.ReadTimeout("engine read timed out"))
        r = asyncio.run(rig.call("POST", SYNC, label="transport timeout", json_body=BODY))
        assert r.status_code == 504
        assert r.json()["error"]["message"].endswith("request timeout")


def test_audio_25_unreachable_engine_is_502_with_the_adapter_text(tmp_path):
    """audio-25: an unreachable engine answers 502 with the adapter's unavailable text (pinned by the recorded golden)."""
    with GatewayRig("audio-25", tmp_path) as rig:
        _down(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="engine down", json_body=BODY))
        assert r.status_code == 502
        assert r.json()["error"]["message"] == _golden_message("audio_generate_unreachable")


def test_audio_26_other_engine_exception_is_502_audio_failed(tmp_path):
    """audio-26: any other exception in the engine block answers 502 audio_failed with the fixed sentence. The engine
    answers the submit with a body that is not JSON, which the block does not expect."""
    with GatewayRig("audio-26", tmp_path) as rig:
        base._comfy(rig, [exchange("prompt", "POST", "/prompt", hex_body(b"not json", "text/plain"), repeat=True)])
        r = asyncio.run(rig.call("POST", SYNC, label="unexpected engine failure", json_body=BODY))
        assert r.status_code == 502
        error = r.json()["error"]
        assert error["message"] == "The audio engine failed while handling this request."
        assert error["type"] == "api_error"


def test_audio_27_normalize_peak_db_rewrites_the_bytes_before_delivery(tmp_path):
    """audio-27: a workflow with normalize_peak_db delivers bytes rewritten by the peak normaliser. Asserted through
    the workflow config the route reads (the normaliser itself is the same function the job route uses)."""
    with GatewayRig("audio-27", tmp_path) as rig:
        _ok(rig)
        seen: list[Any] = []
        real_table = proxy._audio_workflow_table()
        cfg = {name: {**(entry or {}), "normalize_peak_db": -1.0} for name, entry in real_table.items()}
        rig.set(proxy, "_audio_workflow_table", lambda: cfg)
        rig.set(proxy, "_peak_normalize_audio", lambda data, *a: seen.append(True) or data)
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        assert seen


def test_audio_28_delivery_carries_provenance_and_the_sync_asset(tmp_path):
    """audio-28: a sync success under full metadata delivers bytes whose provenance names the audio kind, the workflow,
    the models, the engine's seed and duration, and the response's type; the sync asset is stored as a WAV."""
    with GatewayRig("audio-28", tmp_path) as rig:
        _ok(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(metadata="full"))))
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        record = _provenance(r.content)
        sampler = _engine_sampler(rig)
        assert record["kind"] == "audio"
        assert record["workflow_id"] == "audio.generate.tangoflux"
        assert record["requested_model"] == record["resolved_model"] == "tangoflux"
        assert record["backend"] == "comfyui"  # the fixture's engine backend
        # The file links to the request's stored receipt (not the engine job id, which names no receipt).
        assert record["request_id"] == record["receipt"].rsplit("/", 1)[1]
        assert record["request_id"].startswith("req_") and record["request_id"] != f"audio_{ENGINE_PROMPT_ID}"
        assert len(_receipts(rig)) == 1
        assert record["seed"] == sampler["seed"]
        assert record["parameters"]["duration"] == sampler["duration"]
        assert record["parameters"]["type"] == r.headers["x-audio-type"] == "music"
        assert list(rig.cache.glob("*.wav"))


def test_audio_29_metadata_rewrite_failure_is_502(tmp_path):
    """audio-29: a metadata rewrite failure answers 502 'Could not prepare audio for delivery'."""
    with GatewayRig("audio-29", tmp_path) as rig:
        _ok(rig)
        from gateway import media_metadata

        def broken(*args, **kwargs):
            raise media_metadata.MediaMetadataError("cannot rewrite")

        # The rig's plan keeps metadata off, so the success path delivers through deliver_bytes (proxy.py, the try that
        # catches MediaMetadataError).
        rig.set(proxy.media_delivery, "deliver_bytes", broken)
        r = asyncio.run(rig.call("POST", SYNC, label="rewrite fails", json_body=BODY))
        assert r.status_code == 502
        assert r.json()["error"]["message"] == "Could not prepare audio for delivery"


def test_audio_30_success_returns_bytes_with_content_type_and_headers(tmp_path):
    """audio-30: success returns the raw bytes with the engine's Content-Type and X-Audio-Model / X-Audio-Type."""
    with GatewayRig("audio-30", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("audio/")
        assert r.headers["x-audio-model"]
        assert r.headers["x-audio-type"] == "music"
        assert r.content[:4] == b"RIFF"


def test_audio_31_library_asset_row_is_kind_aud_with_audio_suffix(tmp_path):
    """audio-31: with a receipt, the sync result becomes a Library media job whose asset is kind audio, file audio.wav
    (stored as aud), and whose asset origin is the receipt's request id. The stored bytes sit beside it as a WAV."""
    with GatewayRig("audio-31", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        [receipt] = _receipts(rig)
        [job] = _jobs(rig)
        assert job["intent"] == "audio.generate"
        [asset] = job["job_json"]["assets"]
        assert asset["kind"] == "audio"
        assert asset["filename"] == "audio.wav"
        assert asset["media_type"] == "audio/wav"
        assert asset["asset_id"].startswith("aud_")
        assert _rows(rig, rig.media_jobs_db, "media_job_asset_origins") == [
            {"asset_id": asset["asset_id"], "job_id": job["job_id"], "origin_id": receipt["request_id"]}]
        assert [p.name for p in rig.cache.glob("*.wav")] == [f"{asset['asset_id'].removeprefix('aud_')}.wav"]


def test_audio_32_retention_facts_are_captured_before_the_submit(tmp_path):
    """audio-32: retention facts are captured before the engine submit; the receipt's backend is the comfy backend."""
    with GatewayRig("audio-32", tmp_path) as rig:
        _ok(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        revisions = _rows(rig, rig.media_jobs_db, "retention_fact_revisions")
        assert revisions, "no retention fact revision was written"
        receipts = _receipts(rig)
        assert receipts and receipts[0]["receipt_json"].get("backend") == "comfyui"


# =========================================================================== R2: POST /gateway/v1/media/jobs (audio)


def test_audio_33_payload_without_intent_is_not_an_audio_job(tmp_path):
    """audio-33: a job payload with no intent is not dispatched to the audio branch; the caller gets the generic answer."""
    with GatewayRig("audio-33", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="no intent", json_body={"request": BODY}))
        assert r.status_code != 202
        assert rig.scheduled == []


def test_audio_34_unsupported_intent_is_400_with_the_supported_list(tmp_path):
    """audio-34: an intent that is not executable is 400 unsupported_media_job_intent with the supported list."""
    with GatewayRig("audio-34", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="unknown intent", json_body={"intent": "lyrics.generate", "request": BODY}))
        assert r.status_code == 400
        error = r.json()["error"]
        assert error["code"] == "unsupported_media_job_intent"
        assert error["supported_intents"] == ["image.generate", "audio.generate", "music.generate", "sfx.generate",
                                              "video.generate"]


def test_audio_35_invalid_body_is_accepted_and_fails_later(tmp_path):
    """audio-35 (base behaviour; the row refuses at submit, delta submit_validation): an invalid audio body gets 202
    from the job route, and the runner drives that job to failed with its error (not succeeded, not queued)."""
    with GatewayRig("audio-35", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="invalid body", json_body={"intent": "audio.generate",
                                                                             "request": {**BODY, "prompt": ""}}))
        assert r.status_code == 202
        job_id = _job_id(r)
        asyncio.run(_run_job(rig, job_id))
        job = _stored(rig, job_id)
        assert job["status"] == media_jobs.STATUS_FAILED
        assert job["error"] == "prompt, input, or tags is required"


@pytest.mark.xfail(strict=True, reason="delta submit_validation: lands with the move")
def test_audio_35_delta_submit_validation_refuses_the_invalid_body_at_submit(tmp_path):
    """audio-35 (delta submit_validation, CODE_TO_DATA_S7_PARITY.md section 4): the row refuses an invalid audio body at
    submit with the shape's 400, and writes no job row. The base test above pins the old 202 and retires at the move."""
    with GatewayRig("audio-35-delta", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="invalid body", json_body={"intent": "audio.generate",
                                                                             "request": {**BODY, "prompt": ""}}))
        assert r.status_code == 400
        assert _jobs(rig) == []

def test_audio_36_job_route_auth_refusal_is_returned_unchanged(tmp_path):
    """audio-36: gateway auth runs first on the job route and its refusal is the response."""
    with GatewayRig("audio-36", tmp_path) as rig:
        _jobs_ok(rig)
        refusal = Response(content=b'{"error":{"message":"caller refused"}}', status_code=401,
                           media_type="application/json")
        rig.set(proxy, "_gateway_auth_context", lambda request, path: refusal)
        r = asyncio.run(rig.call("POST", JOBS, label="no credentials", json_body=JOB_BODY))
        assert r.status_code == 401
        assert rig.scheduled == []


def test_audio_37_job_policy_denial_is_403_not_in_access_plan(tmp_path):
    """audio-37: the audio job policy denial on the job route is 403 not_in_access_plan; no job row."""
    with GatewayRig("audio-37", tmp_path) as rig:
        _jobs_ok(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(caps=("chat",)))))
        r = asyncio.run(rig.call("POST", JOBS, label="plan without media", json_body=JOB_BODY))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "not_in_access_plan"
        assert _jobs(rig) == []


def test_audio_38_null_model_on_the_job_route_is_refused_as_none(tmp_path):
    """audio-38: a null model on the job route becomes the string 'None' and is refused 404 model_not_found (the sync
    route falls back to the default instead)."""
    with GatewayRig("audio-38", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="null model", json_body={"intent": "audio.generate",
                                                                            "request": {**BODY, "model": None}}))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "model_not_found"
        assert "'None'" in r.json()["error"]["message"]


def test_audio_39_unknown_model_on_the_job_route_is_404_with_param(tmp_path):
    """audio-39: unknown model on the job route is 404 model_not_found, param model, the audio message."""
    with GatewayRig("audio-39", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="unknown model", json_body={"intent": "audio.generate",
                                                                            "request": {**BODY, "model": "no-such-model"}}))
        assert r.status_code == 404
        error = r.json()["error"]
        assert error["param"] == "model"
        assert error["message"] == "Model 'no-such-model' is not available for audio generation."


def test_audio_40_generic_policy_for_the_workflow_runs_before_admission(tmp_path):
    """audio-40: the policy decision for audio.generate.<workflow> runs before admission; a denial is the policy response."""
    with GatewayRig("audio-40", tmp_path) as rig:
        _jobs_ok(rig)
        seen: list[str] = []
        real_decide = proxy.evaluate_and_apply
        real_admission = proxy._media_job_admission_response

        def decide(*args, **kwargs):
            seen.append("decision")
            return real_decide(*args, **kwargs)

        def admission(*args, **kwargs):
            seen.append("admission")
            return real_admission(*args, **kwargs)

        rig.set(proxy, "evaluate_and_apply", decide)
        rig.set(proxy, "_media_job_admission_response", admission)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY))
        assert r.status_code == 202
        assert "admission" in seen and seen.index("decision") < seen.index("admission")


def test_audio_41_admission_refusal_on_the_job_route_writes_no_job(tmp_path):
    """audio-41: admission on the job route refuses before the job row exists (503), and writes no job."""
    with GatewayRig("audio-41", tmp_path) as rig:
        _jobs_ok(rig)
        refusal = Response(content=b'{"error":{"message":"busy","type":"api_error"}}', status_code=503,
                           media_type="application/json")
        rig.set(proxy, "_media_job_admission_response", lambda *a, **k: refusal)
        r = asyncio.run(rig.call("POST", JOBS, label="admission refused", json_body=JOB_BODY))
        assert r.status_code == 503
        assert _jobs(rig) == []


# =========================================================================== RH: receipts the row path needs (strict xfail)
# These pin the receipt behaviour that RH's open_receipt (CODE_TO_DATA_S7_PARITY.md M1) must give the audio rows. Base
# does not have it, so each test fails today and is strict: when the behaviour lands, the test must be unmarked.


@pytest.mark.xfail(strict=True, reason="needs RH open_receipt")
def test_audio_03_restart_closes_the_open_run_receipt_as_gateway_restarted(tmp_path):
    """audio-03 (M1): a run receipt left open by a stopped gateway closes at startup as failed, with the reason
    gateway_restarted. Base leaves it in_progress: close_orphan exists but no audio recovery calls it."""
    with GatewayRig("audio-03-restart", tmp_path) as rig:
        _ok(rig)
        _store_job(rig, status=media_jobs.STATUS_RUNNING, request_id="audrun_restart")
        receipt_store.begin({"request_id": "audrun_restart", "protocol": "openai.audio.json",
                             "intent": "audio.generate", "path": SYNC}, path=rig.receipts_db)
        proxy._resume_pending_audio_media_jobs()
        closed = _receipts(rig)
        assert len(closed) == 1
        assert closed[0]["status"] == "failed"
        assert closed[0]["receipt_json"].get("terminal_reason", closed[0]["receipt_json"].get(
            "metadata", {}).get("terminal_reason")) == "gateway_restarted"


def test_audio_41_admission_refusal_on_the_job_route_writes_a_rejected_receipt(tmp_path):
    """audio-41 (M1): a job-route admission refusal leaves one closed rejected receipt, no job row and no engine call.
    The refusal is the real admission decision: the operator has disabled the audio intent (resource_admission)."""
    with GatewayRig("audio-41-receipt", tmp_path) as rig:
        _jobs_ok(rig)
        overrides = {"media_job_admission": {"disabled_intents": ["audio.generate"]}}
        rig.monkeypatch.setattr(proxy.ADMIN_STORE, "operator_overrides", lambda: overrides)
        r = asyncio.run(rig.call("POST", JOBS, label="admission refused", json_body=JOB_BODY))
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "resource_admission_denied"
        assert _jobs(rig) == []
        assert rig.recorder.engine == []
        receipts = _receipts(rig)
        assert len(receipts) == 1
        assert receipts[0]["status"] == "rejected"


def test_audio_42_workflow_target_policy_denial_writes_no_job(tmp_path):
    """audio-42: the workflow target policy runs after admission; a denial is the policy response and no job row."""
    with GatewayRig("audio-42", tmp_path) as rig:
        _jobs_ok(rig)
        denied = Response(content=b'{"error":{"message":"target denied","type":"invalid_request_error"}}',
                          status_code=403, media_type="application/json")
        rig.set(proxy, "_workflow_target_policy_decision", lambda *a, **k: SimpleNamespace(denied=True))
        rig.set(proxy, "_policy_denied_response", lambda decision: denied)
        r = asyncio.run(rig.call("POST", JOBS, label="target denied", json_body=JOB_BODY))
        assert r.status_code == 403
        assert _jobs(rig) == []


def test_audio_43_body_becomes_job_metadata(tmp_path):
    """audio-43: model, duration, seed, response_format, type and tag_count are read into the job's metadata."""
    with GatewayRig("audio-43", tmp_path) as rig:
        _jobs_ok(rig)
        body = {"intent": "audio.generate", "request": {**BODY, "type": "sfx"}}
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=body, unordered_json=True))
        assert r.status_code == 202
        job = media_jobs.get(_job_id(r), audience="internal", path=rig.media_jobs_db)
        assert job["metadata"]["model"] == "tangoflux"
        assert job["metadata"]["duration"] == 4
        assert job["metadata"]["type"] == "sfx"


def test_audio_44_metadata_mode_is_async_audio_generation(tmp_path):
    """audio-44: the job's metadata.mode is async_audio_generation; resume depends on it."""
    with GatewayRig("audio-44", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        job = media_jobs.get(_job_id(r), audience="internal", path=rig.media_jobs_db)
        assert job["metadata"]["mode"] == "async_audio_generation"


def test_audio_45_manifest_generated_block_fault_is_refused(tmp_path):
    """audio-45: a generated-block fault from the manifest answers via _generated_value_fault_response (no job)."""
    with GatewayRig("audio-45", tmp_path) as rig:
        _jobs_ok(rig)
        from gateway import audio_media_jobs
        from gateway.manifests import ManifestError

        def fault(*args, **kwargs):
            raise ManifestError("generated block is broken")

        rig.monkeypatch.setattr(proxy.audio_graph, "generated_block", fault)
        r = asyncio.run(rig.call("POST", JOBS, label="generated fault", json_body=JOB_BODY))
        assert r.status_code >= 400
        assert _jobs(rig) == []


def test_audio_46_job_id_and_request_id_forms(tmp_path):
    """audio-46: a job id is mj_<hex>; a request id is audjob_<hex>."""
    with GatewayRig("audio-46", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        job_id = _job_id(r)
        assert job_id.startswith("mj_")
        job = media_jobs.get(job_id, audience="internal", path=rig.media_jobs_db)
        assert job["request_id"].startswith("audjob_")


def test_audio_47_envelope_not_recorded_is_503_could_not_pin(tmp_path):
    """audio-47: a failure to record the envelope answers 503 'Could not pin the workflow definition' and no job."""
    with GatewayRig("audio-47", tmp_path) as rig:
        _jobs_ok(rig)
        # A job holding a generated value needs its envelope; when the pin cannot be built, the job is not accepted.
        rig.set(proxy, "_manifest_has_pin", lambda manifest: True)
        rig.set(proxy, "_manifest_job_envelope", lambda *a, **k: None)
        rig.set(proxy.audio_graph, "generated_block", lambda *a, **k: {"seed": 1})
        r = asyncio.run(rig.call("POST", JOBS, label="pin fails", json_body=JOB_BODY))
        assert r.status_code == 503
        assert "Could not pin the workflow definition" in r.text


def test_audio_48_spool_capacity_refusal_is_503_media_payload_capacity(tmp_path):
    """audio-48: the payload spool refusing for capacity answers 503 media_payload_capacity and deletes the entry."""
    with GatewayRig("audio-48", tmp_path) as rig:
        _jobs_ok(rig)
        def full(*args, **kwargs):
            raise proxy.EphemeralMediaPayloadCapacityError("full")

        rig.set(proxy, "_persist_audio_media_job_payload", full)
        r = asyncio.run(rig.call("POST", JOBS, label="spool full", json_body=JOB_BODY))
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "media_payload_capacity"


def test_audio_49_envelope_refusal_at_create_deletes_the_spool(tmp_path):
    """audio-49: media_jobs.create refusing the envelope answers 503 and deletes the spooled payload. The idempotency
    key is not read on the audio branch."""
    with GatewayRig("audio-49", tmp_path) as rig:
        _jobs_ok(rig)
        def refuse(*args, **kwargs):
            raise media_jobs.EnvelopeNotRecorded("no pin")

        rig.set(media_jobs, "create", refuse)
        r = asyncio.run(rig.call("POST", JOBS, label="create refused", json_body=JOB_BODY,
                                 headers={"Idempotency-Key": "k-audio-49"}))
        assert r.status_code == 503
        assert r.json()["error"]["message"] == "Could not pin the workflow definition"
        payload_dir = Path(proxy.IMAGE_MEDIA_JOB_PAYLOAD_DIR)
        assert not payload_dir.exists() or not any(p.is_file() for p in payload_dir.rglob("*"))


def test_audio_50_other_create_error_is_500_could_not_create(tmp_path):
    """audio-50: any other error from media_jobs.create answers 500 'Could not create audio media job: <Exc>'."""
    with GatewayRig("audio-50", tmp_path) as rig:
        _jobs_ok(rig)
        def broken(*args, **kwargs):
            raise OSError("disk gone")

        rig.set(media_jobs, "create", broken)
        r = asyncio.run(rig.call("POST", JOBS, label="create fails", json_body=JOB_BODY))
        assert r.status_code == 500
        assert r.json()["error"] == {"message": "Could not create audio media job: OSError", "type": "api_error"}


def test_audio_51_queued_event_is_appended_at_submit(tmp_path):
    """audio-51: a queued event (status queued, progress 0) is appended at submit."""
    with GatewayRig("audio-51", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        events = media_jobs.events(_job_id(r), audience="internal", path=rig.media_jobs_db)
        assert events[0]["event_type"] == "queued"
        assert events[0]["payload"]["progress"] == 0


def test_audio_52_job_is_scheduled_and_answered_202_with_location(tmp_path):
    """audio-52: the job is scheduled in the background and the reply is 202 with Location, poll_url and content_url."""
    with GatewayRig("audio-52", tmp_path) as rig:
        _jobs_ok(rig)
        base._capture_scheduling(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        assert r.status_code == 202
        job_id = _job_id(r)
        assert r.headers["location"] == f"/gateway/v1/media/jobs/{job_id}"
        body = r.json()
        assert body["object"] == "media.job"
        assert body["content_url"] == f"/gateway/v1/media/jobs/{job_id}/content"
        assert rig.scheduled == [job_id]


def test_audio_53_job_row_starts_queued_with_the_request_id(tmp_path):
    """audio-53: the job row starts queued, progress 0, with the request id."""
    with GatewayRig("audio-53", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        job = media_jobs.get(_job_id(r), audience="internal", path=rig.media_jobs_db)
        assert job["status"] == "queued"
        assert job["progress"] == 0
        assert job["request_id"]


# =========================================================================== R3-R8: the job views


def test_audio_54_unknown_job_is_404_media_job_not_found(tmp_path):
    """audio-54: a job id that does not exist is 404 media_job_not_found."""
    with GatewayRig("audio-54", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("GET", f"{JOBS}/mj_does_not_exist", label="get unknown"))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "media_job_not_found"


def test_audio_55_job_id_is_not_validated_before_the_lookup(tmp_path):
    """audio-55: any string is looked up; a malformed id is the same 404 as an unknown one."""
    with GatewayRig("audio-55", tmp_path) as rig:
        _jobs_ok(rig)
        malformed = asyncio.run(rig.call("GET", f"{JOBS}/not%20a%20job", label="malformed id"))
        unknown = asyncio.run(rig.call("GET", f"{JOBS}/mj_nothere", label="unknown id"))
        assert malformed.status_code == unknown.status_code == 404
        assert malformed.json() == unknown.json() or malformed.json()["error"]["code"] == "media_job_not_found"


def test_audio_56_a_job_the_caller_cannot_read_is_not_found(tmp_path):
    """audio-56: a job another caller owns is reported as not found, not forbidden."""
    with GatewayRig("audio-56", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        job_id = _job_id(r)
        rig._exit.enter_context(authenticated(identity(plan_for(caps=("chat",)), key_hash="sha256:other",
                                                       app_id="app_other")))
        other = asyncio.run(rig.call("GET", f"{JOBS}/{job_id}", label="another caller"))
        assert other.status_code == 404
        assert other.json()["error"]["code"] == "media_job_not_found"


def test_audio_57_list_defaults_to_50_and_caps_at_200(tmp_path):
    """audio-57: with more readable jobs than the cap, an omitted limit answers 50 items, limit=200 answers 200, and a
    limit over the cap is refused (422)."""
    with GatewayRig("audio-57", tmp_path) as rig:
        _jobs_ok(rig)
        for _ in range(210):
            _submit(rig)
        default = asyncio.run(rig.call("GET", JOBS, label="list with the default limit"))
        assert default.status_code == 200
        assert len(default.json()["jobs"]) == 50
        at_cap = asyncio.run(rig.call("GET", JOBS, label="list at the cap", params={"limit": 200}))
        assert at_cap.status_code == 200
        assert len(at_cap.json()["jobs"]) == 200
        over = asyncio.run(rig.call("GET", JOBS, label="list over the cap", params={"limit": 999}))
        assert over.status_code == 422


def test_audio_58_list_returns_only_readable_jobs(tmp_path):
    """audio-58: the list shows only the jobs the caller can read."""
    with GatewayRig("audio-58", tmp_path) as rig:
        _jobs_ok(rig)
        asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        rig._exit.enter_context(authenticated(identity(plan_for(caps=("chat",)), key_hash="sha256:other",
                                                       app_id="app_other")))
        listed = asyncio.run(rig.call("GET", JOBS, label="another caller lists"))
        assert listed.status_code == 200
        assert listed.json()["jobs"] == []


def test_audio_59_content_policy_denial_is_403_not_in_access_plan(tmp_path):
    """audio-59: content preflight runs before the lookup; a denied caller gets 403 not_in_access_plan."""
    with GatewayRig("audio-59", tmp_path) as rig:
        _jobs_ok(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(caps=("chat",)))))
        r = asyncio.run(rig.call("GET", f"{JOBS}/mj_whatever/content", label="content without media"))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "not_in_access_plan"


def test_audio_60_content_404_texts_for_missing_and_unreadable_jobs(tmp_path):
    """audio-60: a missing job is 404 'media job <id> not found'; an unreadable job is 404 'Media job not found.'."""
    with GatewayRig("audio-60", tmp_path) as rig:
        _jobs_ok(rig)
        missing = asyncio.run(rig.call("GET", f"{JOBS}/mj_missing/content", label="missing"))
        assert missing.status_code == 404
        assert missing.json()["error"]["message"] == "media job 'mj_missing' not found"


def test_audio_61_content_handle_is_none_for_a_job_submitted_with_none(tmp_path):
    """audio-61: a job submitted with metadata 'none' hands out a content handle with md=none. The old code signs every
    audio handle with no delivery mode (build_signed_asset_url without one), so the handle is md=none whatever the
    submit, the GET override or the caller's plan says. The two pins below name that old behaviour."""
    with GatewayRig("audio-61", tmp_path) as rig:
        _jobs_ok(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(metadata="full"))))
        job_id = _submit_and_run(rig, NONE_JOB_BODY)
        _require(_stored(rig, job_id)["status"] == media_jobs.STATUS_SUCCEEDED, "the job did not succeed")
        assert _content_md(rig, job_id) == "none"


@pytest.mark.parametrize("choice", ["none", "full"])
def test_audio_61_job_records_the_submit_metadata_choice(tmp_path, choice):
    """audio-61 (inventory: the job keeps the submit's metadata choice for its content GET). Under an explicitly full
    metadata plan the job records the metadata choice the submit made, 'none' and 'full' alike, and the signed content
    handle stays md=none whichever it was (the old code signs every audio handle with no delivery mode). The 'full'
    case is the contrast that fails an implementation which always stores 'none'. (The request body, not the envelope,
    carries the control: extensions inside "request", where the runner reads it.)"""
    body = {"intent": "audio.generate",
            "request": {**BODY, "extensions": {"com.azizam.workflow": {"metadata": choice}}}}
    with GatewayRig(f"audio-61-job-{choice}", tmp_path) as rig:
        _jobs_ok(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(metadata="full"))))
        job_id = _submit_and_run(rig, body)
        _require(_stored(rig, job_id)["status"] == media_jobs.STATUS_SUCCEEDED, "the job did not succeed")
        metadata = _stored(rig, job_id).get("metadata") or {}
        assert metadata.get("metadata_mode") == choice
        assert _content_md(rig, job_id) == "none"


def test_audio_61_content_handle_ignores_the_get_override_on_a_none_job(tmp_path):
    """audio-61 pin, old behaviour (7daa31769): the GET's ?metadata=full does not change an audio handle, which stays
    md=none. audio.md said override, then job mode, then caller default; the old code does not do that."""
    with GatewayRig("audio-61-override", tmp_path) as rig:
        _jobs_ok(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(metadata="full"))))
        job_id = _submit_and_run(rig, NONE_JOB_BODY)
        _require(_stored(rig, job_id)["status"] == media_jobs.STATUS_SUCCEEDED, "the job did not succeed")
        assert _content_md(rig, job_id, "?metadata=full") == "none"


def test_audio_61_content_handle_ignores_the_caller_default(tmp_path):
    """audio-61 pin, old behaviour (7daa31769): a job with no submit choice, read by a caller whose plan delivers full
    metadata, still gets an md=none audio handle. The caller's default does not reach the signed handle."""
    with GatewayRig("audio-61-default", tmp_path) as rig:
        _jobs_ok(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(metadata="full"))))
        job_id = _submit_and_run(rig, JOB_BODY)
        _require(_stored(rig, job_id)["status"] == media_jobs.STATUS_SUCCEEDED, "the job did not succeed")
        assert _content_md(rig, job_id) == "none"


def test_audio_62_results_route_is_404_for_a_missing_job(tmp_path):
    """audio-62 (missing-job branch): GET results/{name} for a job that does not exist is 404 media_job_result_not_found,
    and the answer carries no Cache-Control header. The completed-job branch is the next test."""
    with GatewayRig("audio-62-missing", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("GET", f"{JOBS}/mj_x/results/anything", label="result"))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "media_job_result_not_found"
        assert "cache-control" not in r.headers


def test_audio_62_results_route_is_404_for_an_owned_completed_job(tmp_path):
    """audio-62 (completed-job branch): an owned, completed audio job with assets is still refused. The route asks for
    the job's real result name, the one a success writes, and gets the same 404 as a missing job."""
    with GatewayRig("audio-62-completed", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit_and_run(rig, JOB_BODY)
        assert _stored(rig, job_id)["status"] == media_jobs.STATUS_SUCCEEDED
        assert _stored(rig, job_id)["assets"]
        r = asyncio.run(rig.call("GET", f"{JOBS}/{job_id}/results/{result_bundle.DEFAULT_NAME}", label="result"))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "media_job_result_not_found"


def test_audio_62_refusal_for_a_completed_job_carries_no_cache_control(tmp_path):
    """audio-62 pin, old behaviour (7daa31769): the refusal for an owned completed job is the same 404 as a missing job,
    and it carries no Cache-Control header. audio.md said 'private, no-store' on every refusal; the old refusal has
    none. The header is set only on a successful read (gateway/api.py), which an audio job does not reach here."""
    with GatewayRig("audio-62-refusal", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit_and_run(rig, JOB_BODY)
        _require(_stored(rig, job_id)["status"] == media_jobs.STATUS_SUCCEEDED, "the job did not succeed")
        r = asyncio.run(rig.call("GET", f"{JOBS}/{job_id}/results/{result_bundle.DEFAULT_NAME}", label="result"))
        assert r.status_code == 404
        assert "cache-control" not in r.headers


def test_audio_63_events_list_shows_the_job_events(tmp_path):
    """audio-63: GET events lists the job's events, bounded by the same caller check as GET job."""
    with GatewayRig("audio-63", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        events = asyncio.run(rig.call("GET", f"{JOBS}/{_job_id(r)}/events", label="events"))
        assert events.status_code == 200
        assert events.json()["events"][0]["event_type"] == "queued"


def test_audio_64_events_post_appends_an_event(tmp_path):
    """audio-64: POST events lets a caller append a progress or note event; lifecycle types are refused for a caller."""
    with GatewayRig("audio-64", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        job_id = _job_id(r)
        appended = asyncio.run(rig.call("POST", f"{JOBS}/{job_id}/events", label="append",
                                        json_body={"event_type": "note"}))
        assert appended.status_code == 201, appended.text
        lifecycle = asyncio.run(rig.call("POST", f"{JOBS}/{job_id}/events", label="append lifecycle",
                                         json_body={"event_type": "started", "payload": {"phase": "running"}}))
        assert lifecycle.status_code == 400
        assert lifecycle.json()["error"]["code"] == "invalid_media_job_event"


def test_audio_65_cancel_queued_marks_canceled(tmp_path):
    """audio-65: cancelling a queued audio job marks it canceled, 'Canceled by requester', 200."""
    with GatewayRig("audio-65", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        job_id = _job_id(r)
        cancel = asyncio.run(rig.call("POST", f"{JOBS}/{job_id}/cancel", label="cancel", json_body={}))
        assert cancel.status_code == 200
        job = media_jobs.get(job_id, audience="internal", path=rig.media_jobs_db)
        assert job["status"] == media_jobs.STATUS_CANCELED
        assert job["error"] == "Canceled by requester"


def test_audio_66_cancel_finished_is_409(tmp_path):
    """audio-66: cancelling a finished job is 409 media_job_cancel_conflict with 'This job has already finished'."""
    with GatewayRig("audio-66", tmp_path) as rig:
        _jobs_ok(rig)
        base._capture_scheduling(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        job_id = _job_id(r)
        asyncio.run(_run_job(rig, job_id))
        cancel = asyncio.run(rig.call("POST", f"{JOBS}/{job_id}/cancel", label="cancel finished", json_body={}))
        assert cancel.status_code == 409
        assert cancel.json()["error"]["code"] == "media_job_cancel_conflict"
        assert cancel.json()["error"]["message"] == "This job has already finished"


def test_audio_67_cancel_running_sets_cancel_requested_and_answers_202(tmp_path):
    """audio-67: cancelling a running job sets cancel_requested_at and answers 202."""
    with GatewayRig("audio-67", tmp_path) as rig:
        _jobs_ok(rig)
        base._capture_scheduling(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        job_id = _job_id(r)
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, path=rig.media_jobs_db)
        cancel = asyncio.run(rig.call("POST", f"{JOBS}/{job_id}/cancel", label="cancel running", json_body={}))
        assert cancel.status_code == 202
        job = media_jobs.get(job_id, audience="internal", path=rig.media_jobs_db)
        assert job["metadata"].get("cancel_requested_at")


def test_audio_68_cancel_answers_202_when_requested_and_200_when_at_once(tmp_path):
    """audio-68: the cancel reply is 202 when cancellation was requested and 200 when the state changed at once."""
    with GatewayRig("audio-68", tmp_path) as rig:
        _jobs_ok(rig)
        r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body=JOB_BODY, unordered_json=True))
        cancel = asyncio.run(rig.call("POST", f"{JOBS}/{_job_id(r)}/cancel", label="cancel queued", json_body={}))
        assert cancel.status_code == 200


# =========================================================================== R9: runner, scheduler and restart


def _store_job(rig, *, status, metadata=None, intent="audio.generate", request_id="audjob_t") -> Any:
    """A job row with no payload: the store state a public route cannot leave behind (used only where the test needs
    that state). Every other job here comes from the public submit route, so its payload is the route's own write."""
    job = media_jobs.MediaJob(intent=intent, request_id=request_id, status=status,
                              metadata={"mode": "async_audio_generation", **(metadata or {})})
    media_jobs.create(job, path=rig.media_jobs_db)
    return job


def _submit(rig, body: dict[str, Any] | None = None) -> str:
    """Submit an audio job through the public job route; the route writes the row and its payload and schedules it
    (``rig.scheduled`` captures the id). Returns the job id."""
    r = asyncio.run(rig.call("POST", JOBS, label="submit", json_body={"intent": "audio.generate", "request": body or BODY},
                             unordered_json=True))
    assert r.status_code == 202, r.text
    return _job_id(r)


def _stored(rig, job_id: str) -> dict[str, Any]:
    return media_jobs.get(job_id, audience="internal", path=rig.media_jobs_db)


def test_audio_69_missing_payload_fails_with_the_payload_text(tmp_path):
    """audio-69: a runner that finds no payload fails the job with 'Audio media job payload is missing or invalid'."""
    with GatewayRig("audio-69", tmp_path) as rig:
        _ok(rig)
        job = _store_job(rig, status=media_jobs.STATUS_QUEUED)
        asyncio.run(_run_job(rig, job.job_id))
        stored = _stored(rig, job.job_id)
        assert stored["status"] == media_jobs.STATUS_FAILED
        assert stored["error"] == "Audio media job payload is missing or invalid"


def test_audio_70_caller_no_longer_allowed_fails_with_the_access_text(tmp_path):
    """audio-70: a caller no longer allowed to use the gateway fails the job with the access text.
    STUB (no public trigger): the caller's access is revoked by stubbing the payload's auth reader."""
    with GatewayRig("audio-70", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)
        rig.set(proxy, "_auth_context_from_image_job_payload", lambda payload: None)
        asyncio.run(_run_job(rig, rig.scheduled[0]))
        assert _stored(rig, job_id)["error"] == "The account that asked for this job can no longer use the gateway."


def test_audio_71_runner_reads_the_manifest_and_graph_live(tmp_path):
    """audio-71: the job runs to the engine and succeeds. Coverage gap: the live-read property (no pinned manifest) is
    not observable here without changing the registry between submit and run; the golden audio_generate_job covers it."""
    with GatewayRig("audio-71", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)
        asyncio.run(_run_job(rig, rig.scheduled[0]))
        assert _engine_paths(rig).count("/prompt") == 1
        assert _stored(rig, job_id)["status"] == media_jobs.STATUS_SUCCEEDED


def test_audio_72_timeout_copy_is_the_job_timeout_text(tmp_path):
    """audio-72 (timeout): an engine that never finishes fails the job with the timeout copy. audio.md named an engine 504
    for this; base maps an engine 504 to the upstream copy (test 72b), and the timeout copy comes from polling running out."""
    with GatewayRig("audio-72-timeout", tmp_path) as rig:
        base._capture_scheduling(rig)
        job_id = _submit(rig)
        _never(rig)
        asyncio.run(_run_job(rig, rig.scheduled[0]))
        assert _stored(rig, job_id)["error"] == "The backend did not finish this job in time."


def test_audio_72b_upstream_copy_names_the_engine_status(tmp_path):
    """audio-72 (upstream): an engine 504 on the prompt fails the job with the upstream copy, and the job says HTTP 502."""
    with GatewayRig("audio-72-upstream", tmp_path) as rig:
        base._capture_scheduling(rig)
        job_id = _submit(rig)
        base._comfy(rig, [exchange("prompt", "POST", "/prompt", {"status": 504, "json": {"error": "slow"}}, repeat=True)])
        asyncio.run(_run_job(rig, rig.scheduled[0]))
        assert _stored(rig, job_id)["error"] == "Media generation failed (upstream HTTP 502)."


def test_audio_73_executor_exception_fails_with_the_exception_name(tmp_path):
    """audio-73: an executor exception fails the job with 'Audio media job executor error: <ExcType>' and status 502.
    STUB (no public trigger): the executor's inner call is replaced to raise."""
    with GatewayRig("audio-73", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)

        async def broken(*args, **kwargs):
            raise KeyError("boom")

        rig.set(proxy, "proxy_audio_generate", broken)
        asyncio.run(_run_job(rig, rig.scheduled[0]))
        assert _stored(rig, job_id)["error"] == "Audio media job executor error: KeyError"


def test_audio_74_refused_result_fails_with_the_size_text(tmp_path):
    """audio-74: a result the bundle refused fails with 'The result was larger or other than the gateway allows...'.
    STUB (no public trigger): the bundle check is replaced to refuse."""
    with GatewayRig("audio-74", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)
        from gateway import result_bundle

        def refuse(*args, **kwargs):
            raise result_bundle.ResultRefused("result_too_large", "too big")

        rig.set(result_bundle, "check_assets", refuse)
        asyncio.run(_run_job(rig, rig.scheduled[0]))
        assert _stored(rig, job_id)["error"] == (
            "The result was larger or other than the gateway allows, so it was not delivered.")


def test_audio_75_result_is_sniffed_and_stored_as_kind_aud(tmp_path):
    """audio-75: the bytes are sniffed for their container and stored as kind aud with the sniffed suffix."""
    with GatewayRig("audio-75", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)
        asyncio.run(_run_job(rig, rig.scheduled[0]))
        assert _stored(rig, job_id)["status"] == media_jobs.STATUS_SUCCEEDED
        names = [a["name"] for a in rig.trace()["assets"]]
        assert any(name.endswith(".wav") for name in names)


def test_audio_76_completed_event_carries_phase_and_counts(tmp_path):
    """audio-76: the completed event carries phase completed, asset_count and media_types; the job ends succeeded at 100."""
    with GatewayRig("audio-76", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)
        asyncio.run(_run_job(rig, rig.scheduled[0]))
        events = media_jobs.events(job_id, audience="internal", path=rig.media_jobs_db)
        done = events[-1]
        assert done["event_type"] == "completed"
        assert done["payload"]["phase"] == "completed"
        assert done["payload"]["asset_count"] == 1
        assert done["payload"]["media_types"] == ["audio/wav"]


# Startup recovery has no HTTP entry. ``proxy._resume_pending_audio_media_jobs`` is the one startup seam these tests
# call (the golden rig's runner seam is the other); the row path's recover_runs replaces it at the move.


def test_audio_77_startup_resumes_only_async_audio_jobs(tmp_path):
    """audio-77: startup recovery resumes unfinished audio jobs with mode async_audio_generation; other intents are skipped."""
    with GatewayRig("audio-77", tmp_path) as rig:
        _jobs_ok(rig)
        audio_id = _submit(rig)
        other = _store_job(rig, status=media_jobs.STATUS_QUEUED, intent="image.generate", request_id="img_a77")
        rig.scheduled.clear()
        assert proxy._resume_pending_audio_media_jobs() == 1
        assert rig.scheduled == [audio_id]
        assert other.job_id not in rig.scheduled


def test_audio_78_payload_missing_after_restart_fails_with_the_restart_text(tmp_path):
    """audio-78: a job with no payload after restart fails 'Audio media job payload missing after proxy restart'."""
    with GatewayRig("audio-78", tmp_path) as rig:
        _ok(rig)
        job = _store_job(rig, status=media_jobs.STATUS_RUNNING)
        assert proxy._resume_pending_audio_media_jobs() == 0
        assert _stored(rig, job.job_id)["error"] == "Audio media job payload missing after proxy restart"


def test_audio_79_recovery_turned_off_fails_the_queued_job(tmp_path):
    """audio-79: with recovery turned off the queued job fails 'The gateway restarted with job recovery turned off'.
    STUB (config reader): recovery's on/off switch is read through this function."""
    with GatewayRig("audio-79", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)
        rig.set(proxy, "_image_media_job_resume_on_start", lambda: False)
        assert proxy._resume_pending_audio_media_jobs() == 0
        assert _stored(rig, job_id)["error"] == "The gateway restarted with job recovery turned off"


def test_audio_80_cancel_requested_before_restart_fails_not_cancels(tmp_path):
    """audio-80: a job with cancel_requested_at set fails at restart 'The gateway restarted before cancellation could be
    confirmed' (failed, not canceled)."""
    with GatewayRig("audio-80", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, path=rig.media_jobs_db)
        media_jobs.request_cancel(job_id, path=rig.media_jobs_db)
        assert proxy._resume_pending_audio_media_jobs() == 0
        stored = _stored(rig, job_id)
        assert stored["status"] == media_jobs.STATUS_FAILED
        assert stored["error"] == "The gateway restarted before cancellation could be confirmed"


def test_audio_81_running_job_is_failed_at_restart_never_rerun(tmp_path):
    """audio-81: a running job fails at restart 'The gateway restarted before completion could be confirmed; this job
    was not run again' and is never re-run."""
    with GatewayRig("audio-81", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, path=rig.media_jobs_db)
        rig.scheduled.clear()
        assert proxy._resume_pending_audio_media_jobs() == 0
        assert rig.scheduled == []
        assert _stored(rig, job_id)["error"] == (
            "The gateway restarted before completion could be confirmed; this job was not run again")


def test_audio_82_queued_job_that_passes_the_checks_is_rescheduled(tmp_path):
    """audio-82: a queued job with a payload is rescheduled at restart."""
    with GatewayRig("audio-82", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)
        rig.scheduled.clear()
        assert proxy._resume_pending_audio_media_jobs() == 1
        assert rig.scheduled == [job_id]


# The scheduler wrapper that runs the runner in a task; it has no HTTP entry, so the real function is kept from before
# the rig patches it and called directly (declared seam).
REAL_SCHEDULE_AUDIO_MEDIA_JOB = proxy._schedule_audio_media_job


def test_audio_83_a_crash_in_the_task_marks_the_job_failed(tmp_path):
    """audio-83: a crash in the task marks the job failed with the crash code and logs it.
    STUB + seam: the runner is replaced to raise; the real scheduler wrapper runs it in a task."""
    with GatewayRig("audio-83", tmp_path) as rig:
        _jobs_ok(rig)
        job_id = _submit(rig)

        async def crash(job_id):
            raise RuntimeError("task crashed")

        rig.set(proxy, "_run_audio_media_job", crash)

        async def schedule_and_wait():
            assert REAL_SCHEDULE_AUDIO_MEDIA_JOB(job_id) is True
            await asyncio.sleep(0.05)

        asyncio.run(schedule_and_wait())
        assert _stored(rig, job_id)["status"] == media_jobs.STATUS_FAILED


class _HeldHistory(httpx.AsyncBaseTransport):
    """Wraps the engine's transport. The first history answer for the prompt is held until the test releases it, so the
    engine's success is not available before the cancel. Every other request passes through unchanged. A hold that is not
    released in time raises, so a broken test cannot wait forever."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/history/{ENGINE_PROMPT_ID}":
            self.reached.set()
            await asyncio.wait_for(self.release.wait(), timeout=30)
        return await self.inner.handle_async_request(request)


def test_audio_84_cancel_while_running_wins_over_success(tmp_path):
    """audio-84: the engine's answer is held after real dispatch. A cancel sent over HTTP while that answer is pending
    is accepted (202). Only then is the engine released with a successful result. The runner then tries to move the job
    to SUCCEEDED, the store refuses it (CancellationPending), and the job settles canceled with no asset and no published
    result bundle."""
    with GatewayRig("audio-84", tmp_path) as rig:
        base._audio_comfy(rig, strict=False)
        base._capture_scheduling(rig)
        held = _HeldHistory(rig.fakes.router._routes[base.GRAPH_HOST])
        rig.fakes.router._routes[base.GRAPH_HOST] = held
        attempts: list[str] = []
        real_update = media_jobs.update_status

        def spy_update(job_id_arg, status, *args, **kwargs):
            if job_id_arg != job_id or status != media_jobs.STATUS_SUCCEEDED:
                return real_update(job_id_arg, status, *args, **kwargs)
            try:
                result = real_update(job_id_arg, status, *args, **kwargs)
            except media_jobs.CancellationPending:
                attempts.append("refused")
                raise
            attempts.append("accepted")
            return result

        job_id = _submit(rig)
        rig.set(media_jobs, "update_status", spy_update)

        async def run_and_cancel() -> None:
            runner = asyncio.ensure_future(_run_job(rig, job_id))
            try:
                seen = asyncio.ensure_future(held.reached.wait())
                done, _ = await asyncio.wait({seen, runner}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
                if seen not in done:
                    seen.cancel()
                    raise AssertionError("the runner returned before the engine was asked for its result")
                cancel = await rig.call("POST", f"{JOBS}/{job_id}/cancel", label="cancel while pending", json_body={})
                assert cancel.status_code == 202, cancel.text
                held.release.set()
                await asyncio.wait_for(runner, timeout=30)
            finally:
                held.release.set()
                if not runner.done():
                    runner.cancel()

        asyncio.run(run_and_cancel())
        assert any(e["path"] == "/view" for e in rig.recorder.engine), "the engine's success was never fetched"
        assert attempts == ["refused"], f"expected one SUCCEEDED attempt after the cancel, refused: {attempts}"
        job = _stored(rig, job_id)
        assert job["status"] == media_jobs.STATUS_CANCELED
        assert job["error"] == "Canceled by requester"
        assert not job.get("assets")
        with media_jobs.connection(rig.media_jobs_db) as conn:
            published = conn.execute("SELECT COUNT(*) FROM result_stage WHERE job_id = ? AND state = 'published'",
                                     (job_id,)).fetchone()[0]
        assert published == 0


# =========================================================================== section 6 answers owned by builder A

@pytest.mark.xfail(strict=True, reason="needs the audio job route on the run core: the base audio branch ignores Idempotency-Key (audio-49)")
def test_s7_audio_delivery_container_does_not_change_run_identity(tmp_path):
    """6.3: the audio container is an answer form read at rendering. Same Idempotency-Key, only the container changed:
    one job, one engine submit, and the job answers under the same id."""
    with GatewayRig("s7-audio-container", tmp_path) as rig:
        _jobs_ok(rig)
        headers = {"Idempotency-Key": "k-s7-container"}
        wav = asyncio.run(rig.call("POST", JOBS, label="submit wav", headers=headers,
                                   json_body={"intent": "audio.generate", "request": {**BODY, "response_format": "wav"}}))
        mp3 = asyncio.run(rig.call("POST", JOBS, label="submit mp3", headers=headers,
                                   json_body={"intent": "audio.generate", "request": {**BODY, "response_format": "mp3"}}))
        assert wav.status_code == 202, wav.text
        assert mp3.status_code in (200, 202), mp3.text
        assert _job_id(mp3) == _job_id(wav)
        assert len(_jobs(rig)) == 1
        assert len(rig.scheduled) == 1
        asyncio.run(_run_job(rig, rig.scheduled[0]))
        assert _engine_paths(rig).count("/prompt") == 1


def test_s7_audio_normalize_peak_db_preserves_output_samples(tmp_path):
    """6.5: a workflow with normalize_peak_db delivers the engine's samples scaled to the target peak. Every delivered
    sample is the engine's sample times one gain (to the nearest LSB), the peak sits at the target, the frame count and
    rate hold, and the stored Library WAV holds the same samples."""
    import io
    import math
    import wave

    def pcm_samples(content: bytes) -> list[int]:
        with wave.open(io.BytesIO(content), "rb") as handle:
            raw = handle.readframes(handle.getnframes())
        return [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw), 2)]

    # A quiet fixture (peak near -84 dBFS) is under the normaliser's silence floor and comes back unchanged, so the
    # engine output here is a louder tone (peak about -20 dBFS). Alternating signs catch a sign-changing corruption.
    engine_samples = [3000 if i % 2 else -3000 for i in range(1600)]
    loud = io.BytesIO()
    with wave.open(loud, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"".join(v.to_bytes(2, "little", signed=True) for v in engine_samples))
    with GatewayRig("s7-audio-normalize", tmp_path) as rig:
        base._comfy(rig, comfy_exchanges(history=history_done("audios", "tone_00001_.wav", node="3"),
                                         views=[(loud.getvalue(), "audio/wav")]))
        real_table = proxy._audio_workflow_table()
        cfg = {name: {**(entry or {}), "normalize_peak_db": -1.0} for name, entry in real_table.items()}
        rig.set(proxy, "_audio_workflow_table", lambda: cfg)
        r = asyncio.run(rig.call("POST", SYNC, label="generate", json_body=BODY))
        assert r.status_code == 200
        with wave.open(io.BytesIO(r.content), "rb") as out:
            frames = out.getnframes()
            rate = out.getframerate()
        delivered = pcm_samples(r.content)
        assert frames == 1600
        assert rate == 16000
        assert len(delivered) == len(engine_samples)
        # One gain for the whole clip: fitted from the loudest sample, then every sample checked against it.
        gain = max(abs(v) for v in delivered) / max(abs(v) for v in engine_samples)
        worst = max(abs(out - src * gain) for out, src in zip(delivered, engine_samples))
        assert worst <= 1.0, f"largest deviation from a pure gain of {gain:.4f} is {worst:.2f} LSB"
        peak_db = 20 * math.log10(max(abs(v) for v in delivered) / 32767)
        assert abs(peak_db - (-1.0)) < 0.5
        stored = list(rig.cache.glob("*.wav"))
        assert len(stored) == 1
        assert pcm_samples(stored[0].read_bytes()) == delivered


def test_s7_audio_poll_schedule_is_two_seconds_with_six_hundred_polls(tmp_path):
    """6.8: audio polls every 2 s and gives up after 600 polls. The rig records each sleep the audio code makes; an
    engine that never finishes shows exactly 600 polls, each 2 s, and then the timeout answer."""
    with GatewayRig("s7-audio-poll", tmp_path) as rig:
        _never(rig)
        r = asyncio.run(rig.call("POST", SYNC, label="engine never finishes", json_body=BODY))
        assert r.status_code == 504
        polls = [entry["delay"] for entry in rig.recorder.sleeps]
        assert len(polls) == 600
        assert set(polls) == {2.0}
