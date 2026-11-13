"""POST /gateway/v1/workflows/{id}/run through the real route, the real worker and the real job store (builder C).

The snapshot and the projections are the test doubles of the run-core tests; the engine is a fake whose call counter
proves "no engine call". The route, the port, the request spool, the claim table and the media-job routes are real.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import MappingProxyType, SimpleNamespace

import pytest

import proxy
from gateway import api, media_jobs, receipt_store, request_spool, result_bundle
from tests.support.gateway import gateway_client, use_admin_db
from tests.test_workflow_run import CONTRACT, Projections, Provider
from tests.workflow_runtime_fakes import FakeSnapshot, entry

# The plan maps an intent to a capability; an intent it does not know is refused, so the route contract uses a known one.
ROUTE_CONTRACT = {**CONTRACT, "intent": "image.generate"}
ROUTE = "/gateway/v1/workflows/demo.run/run"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def served(monkeypatch, tmp_path):
    use_admin_db(monkeypatch, tmp_path)
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    receipt_store.init_db(receipt_store.DB_PATH)
    monkeypatch.setattr(proxy, "_WORKFLOW_RUN_SPOOL_ROOT", tmp_path / "spool")
    monkeypatch.setattr(proxy, "_WORKFLOW_JOB_SPOOL", request_spool.JobSpool(tmp_path / "spool"))
    monkeypatch.setattr(proxy, "_workflow_core_cache", None)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    snap = FakeSnapshot(1, MappingProxyType({"demo.run": entry(ROUTE_CONTRACT)}))
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", Provider(snap))
    monkeypatch.setattr(proxy, "WORKFLOW_PROJECTIONS", Projections())
    stub_protocol = SimpleNamespace(id="proto", revision=1, adapter_identity="adapter")
    stub_adapter = SimpleNamespace(interface_version=1)
    engine = ("svc", {"host": "engine.test", "port": 1}, stub_protocol, stub_adapter)
    monkeypatch.setattr(proxy, "_workflow_engine", lambda adapter_id, service_id=None, family=None: engine)
    state = SimpleNamespace(engine_calls=0, gate=None)

    async def run_adapter(ctx):
        state.engine_calls += 1
        if state.gate is not None:
            await state.gate.wait()
        limits = result_bundle.ResultLimits()
        result_bundle.complete_job(
            ctx.job_id, lambda conn, attempt: result_bundle.stage_json_in(
                conn, ctx.job_id, attempt, "result", b'{"ok": true}', limits=limits),
            limits=limits, path=media)

    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "run_adapter", run_adapter)
    state.media = media
    return state


def call(method: str, path: str, **kwargs):
    async def go():
        async with gateway_client() as client:
            return await getattr(client, method)(path, **kwargs)
    return asyncio.run(go())


def test_unconfigured_runtime_answers_503_and_runs_nothing(served, monkeypatch):
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", None)
    response = call("post", ROUTE, json={"inputs": {"prompt": "hi"}})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "workflow_runtime_unavailable"
    assert served.engine_calls == 0


def test_json_submit_runs_once_and_the_media_job_providers_serve_the_job(served):
    created = call("post", ROUTE, json={"inputs": {"prompt": "hi"}}, headers={"Prefer": "wait=10"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "succeeded" and body["disposition"] == "new"
    assert body["results"][0]["output"] == "result"
    assert created.headers["location"] == f"/gateway/v1/media/jobs/{body['job_id']}"
    assert served.engine_calls == 1
    # The media-job routes read the job store through these providers (the router captures the live store path at import,
    # so the test builds them over its own store): a workflow job is served as any media job is.
    _, getter, lister, event_lister, _, canceler = api._build_media_job_providers(
        media_job_creator=None, media_job_getter=None, media_job_lister=None, media_job_event_lister=None,
        media_job_event_appender=None, media_job_canceler=None, media_job_store_path=served.media)
    row = getter(body["job_id"], audience="internal")
    assert row["intent"] == "workflow.run" and row["status"] == "succeeded" and row["results"]
    assert [r["job_id"] for r in lister(audience="internal", limit=10)] == [body["job_id"]]
    assert event_lister(body["job_id"], audience="internal", limit=10)
    with pytest.raises(ValueError):          # the existing rule: a finished job cannot be canceled
        canceler(body["job_id"], audience="internal")


def test_a_retry_with_the_same_key_replays_the_job_without_an_engine_call(served):
    headers = {"Idempotency-Key": "k-1", "Prefer": "wait=10"}
    first = call("post", ROUTE, json={"inputs": {"prompt": "hi"}}, headers=headers)
    second = call("post", ROUTE, json={"inputs": {"prompt": "hi"}}, headers=headers)
    other = call("post", ROUTE, json={"inputs": {"prompt": "different"}}, headers=headers)
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["disposition"] == "replay"
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"
    assert served.engine_calls == 1


def test_without_wait_the_answer_is_202_with_the_queued_job(served):
    served.gate = None
    response = call("post", ROUTE, json={"inputs": {"prompt": "hi"}})
    assert response.status_code in (200, 202)
    assert response.json()["status"] in ("queued", "running", "succeeded")
    assert response.headers["location"].endswith(response.json()["job_id"])


def test_every_input_problem_is_reported_at_once_and_nothing_runs(served):
    response = call("post", ROUTE, json={"inputs": {"seed": -5, "extra": 1}})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_inputs"
    assert len(error["problems"]) >= 2
    assert served.engine_calls == 0


def test_unknown_workflow_is_404_and_a_body_with_other_keys_is_422(served):
    assert call("post", "/gateway/v1/workflows/nope/run", json={"inputs": {}}).status_code == 404
    assert call("post", ROUTE, json={"inputs": {}, "model": "x"}).status_code == 422
    assert served.engine_calls == 0


def test_a_multipart_upload_reaches_the_adapter_with_its_declared_type(served):
    seen = {}

    async def run_adapter(ctx):
        seen["media"] = ctx.media
        seen["bytes"] = ctx.spool.path(ctx.job_id, ctx.media["photo"][0].spool_key).read_bytes()
        served.engine_calls += 1
        limits = result_bundle.ResultLimits()
        result_bundle.complete_job(
            ctx.job_id, lambda conn, attempt: result_bundle.stage_json_in(
                conn, ctx.job_id, attempt, "result", b'{}', limits=limits),
            limits=limits, path=served.media)

    proxy._WORKFLOW_RUN_PORT.run_adapter = run_adapter  # undone with the fixture's monkeypatch of the same name
    response = call("post", ROUTE, data={"inputs": json.dumps({"prompt": "hi"})},
                    files={"photo": ("p.png", PNG, "image/png")}, headers={"Prefer": "wait=10"})
    assert response.status_code == 200, response.text
    ref = seen["media"]["photo"][0]
    assert (ref.media_type, ref.wire_media_type, ref.size) == ("image/png", "image/png", len(PNG))
    assert seen["bytes"] == PNG


def _capture_inputs(served):
    seen = {}

    async def run_adapter(ctx):
        seen["inputs"] = dict(ctx.inputs)
        served.engine_calls += 1
        limits = result_bundle.ResultLimits()
        result_bundle.complete_job(
            ctx.job_id, lambda conn, attempt: result_bundle.stage_json_in(
                conn, ctx.job_id, attempt, "result", b'{}', limits=limits),
            limits=limits, path=served.media)

    proxy._WORKFLOW_RUN_PORT.run_adapter = run_adapter  # undone with the fixture's monkeypatch of the same name
    return seen


def test_a_declared_value_input_sent_as_its_own_form_part_is_read_as_its_declared_type(served):
    # A plain form (the Try page, curl -F) sends each value input as a part named by the input, beside the file parts.
    seen = _capture_inputs(served)
    response = call("post", ROUTE, data={"prompt": "hi", "seed": "7"},
                    files={"photo": ("p.png", PNG, "image/png")}, headers={"Prefer": "wait=10"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert seen["inputs"]["prompt"] == "hi" and seen["inputs"]["seed"] == 7
    assert served.engine_calls == 1


def test_form_parts_and_the_inputs_part_meet(served):
    seen = _capture_inputs(served)
    response = call("post", ROUTE, data={"inputs": json.dumps({"prompt": "hi"}), "seed": "3"},
                    files={"photo": ("p.png", PNG, "image/png")}, headers={"Prefer": "wait=10"})
    assert response.status_code == 200, response.text
    assert seen["inputs"]["prompt"] == "hi" and seen["inputs"]["seed"] == 3


@pytest.mark.parametrize("data, files, message", [
    ({"inputs": json.dumps({"prompt": "hi"}), "prompt": "again"}, None, "prompt: sent both as a form part and inside inputs"),
    ({"prompt": "hi", "colour": "red"}, None, "colour: a value part must be named inputs, binding or a declared value input"),
    ({"prompt": "hi", "photo": "not a file"}, None, "photo: a value part must be named inputs, binding or a declared value input"),
    (None, [("prompt", (None, "a")), ("prompt", (None, "b"))], "prompt: a value input is sent as one part"),
])
def test_a_form_part_that_is_not_one_declared_value_input_is_refused_and_runs_nothing(served, data, files, message):
    response = call("post", ROUTE, data=data, files=files or {"photo": ("p.png", PNG, "image/png")})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["message"] == message
    assert served.engine_calls == 0


def test_a_form_value_that_does_not_read_as_its_type_is_named_by_validation(served):
    response = call("post", ROUTE, data={"prompt": "hi", "seed": "seven"}, files={"photo": ("p.png", PNG, "image/png")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_inputs"
    assert served.engine_calls == 0


def test_a_file_part_below_a_top_level_name_is_media_path_unsupported(served):
    response = call("post", ROUTE, data={"inputs": json.dumps({"prompt": "hi"})},
                    files={"photo.sub": ("p.png", PNG, "image/png")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_path_unsupported"
    assert served.engine_calls == 0


def test_a_file_of_no_known_type_is_refused_by_the_input_and_leaves_no_bytes(served, tmp_path):
    response = call("post", ROUTE, data={"inputs": json.dumps({"prompt": "hi"})},
                    files={"photo": ("p.bin", b"plain words", "image/png")})
    assert response.status_code == 422          # the declared type is not believed: the bytes are not a picture
    assert not [p for p in (tmp_path / "spool").rglob("*") if p.is_file()]
    assert served.engine_calls == 0


def test_a_disabled_workflow_is_409_and_runs_nothing(served, monkeypatch):
    snap = FakeSnapshot(1, MappingProxyType({"demo.run": entry(ROUTE_CONTRACT, state="disabled")}))
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", Provider(snap))
    response = call("post", ROUTE, json={"inputs": {"prompt": "hi"}})
    assert response.status_code == 409 and response.json()["error"]["code"] == "workflow_disabled"
    assert served.engine_calls == 0


def test_restart_recovery_runs_a_queued_workflow_job_from_its_pin(served, monkeypatch):
    scheduled: list[str] = []
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "schedule", lambda job_id: scheduled.append(job_id))
    created = call("post", ROUTE, json={"inputs": {"prompt": "hi"}})
    job_id = created.json()["job_id"]
    assert scheduled == [job_id]            # the route scheduled it (the fake did not run it)
    scheduled.clear()
    proxy._recover_workflow_runs()
    assert scheduled == [job_id]            # a restart schedules the unfinished job again
    assert served.engine_calls == 0




# ---------------------------------------------------------------------------------------------- the request controls
# The run form takes the controls every job route takes: ``extensions`` as a JSON object (a form part or a JSON body
# key) and the bracket form ``extensions[<namespace>][<key>]``. The prompt-memory control reaches the job's payload,
# where the prompt-memory rules decide what is kept; ``x-azizam-prompts: forget`` does the same.

from gateway import prompt_memory  # noqa: E402

SENTINEL = "private sentinel prompt 7f3a"
FORGET = {prompt_memory.NAMESPACE: {"mode": "forget"}}
BRACKET_FORGET = f"extensions[{prompt_memory.NAMESPACE}][mode]"


def text_parts(**parts: str) -> dict:
    """A multipart form of text parts only (httpx sends a form as multipart when ``files`` is given)."""
    return {name: (None, value) for name, value in parts.items()}


def forms_of_forget():
    inputs = json.dumps({"prompt": SENTINEL})
    return {
        "json_part": (dict(files=text_parts(inputs=inputs, extensions=json.dumps(FORGET))), {}),
        "bracket": (dict(files=text_parts(inputs=inputs, **{BRACKET_FORGET: "forget"})), {}),
        "json_body": (dict(json={"inputs": {"prompt": SENTINEL}, "extensions": FORGET}), {}),
        "header": (dict(files=text_parts(inputs=inputs)), {"x-azizam-prompts": "forget"}),
    }


def disk_bytes(root) -> bytes:
    return b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())


@pytest.fixture
def queued(served, monkeypatch):
    """The route takes the job and schedules it; the worker does not run, so the payload stays where the route put it."""
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "schedule", lambda job_id: None)
    return served


def test_without_a_control_the_prompt_waits_on_disk_with_the_queued_job(queued, tmp_path):
    response = call("post", ROUTE, files=text_parts(inputs=json.dumps({"prompt": SENTINEL})))
    assert response.status_code == 202, response.text
    payload = proxy._load_image_media_job_payload(response.json()["job_id"])
    assert payload["body"]["inputs"]["prompt"] == SENTINEL
    assert SENTINEL.encode() in disk_bytes(tmp_path / "payloads"), "positive control: the prompt was not kept"


@pytest.mark.parametrize("form", list(forms_of_forget()))
def test_a_forget_control_in_any_form_keeps_no_prompt_on_disk(queued, tmp_path, form):
    kwargs, headers = forms_of_forget()[form]
    response = call("post", ROUTE, headers=headers, **kwargs)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    payload = proxy._load_image_media_job_payload(job_id)
    assert payload["body"]["inputs"]["prompt"] == SENTINEL, "the queued job still runs from its payload"
    assert payload["body"]["extensions"][prompt_memory.NAMESPACE] == {"mode": "forget"}
    assert SENTINEL.encode() not in disk_bytes(tmp_path), "a forget run wrote its prompt to disk"
    assert SENTINEL.encode() not in queued.media.read_bytes()


@pytest.mark.parametrize("form", list(forms_of_forget()))
def test_a_finished_forget_run_keeps_no_prompt_anywhere(served, tmp_path, form):
    kwargs, headers = forms_of_forget()[form]
    response = call("post", ROUTE, headers={**headers, "Prefer": "wait=10"}, **kwargs)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded" and served.engine_calls == 1
    assert SENTINEL not in json.dumps(body)
    assert SENTINEL.encode() not in disk_bytes(tmp_path)
    assert SENTINEL.encode() not in served.media.read_bytes()


def test_the_json_part_and_the_bracket_form_meet_and_the_bracket_wins(queued):
    response = call("post", ROUTE, files=text_parts(
        inputs=json.dumps({"prompt": "hi"}),
        extensions=json.dumps({prompt_memory.NAMESPACE: {"mode": "remember"}, "com.example.other": {"a": 1}}),
        **{BRACKET_FORGET: "forget"}))
    assert response.status_code == 202, response.text
    extensions = proxy._load_image_media_job_payload(response.json()["job_id"])["body"]["extensions"]
    assert extensions[prompt_memory.NAMESPACE] == {"mode": "forget"}


def test_a_namespace_the_gateway_does_not_know_is_kept_with_the_job_as_on_the_other_job_routes(queued):
    response = call("post", ROUTE, files=text_parts(
        inputs=json.dumps({"prompt": "hi"}), extensions=json.dumps({"com.example.other": {"a": 1}})))
    assert response.status_code == 202, response.text
    extensions = proxy._load_image_media_job_payload(response.json()["job_id"])["body"]["extensions"]
    assert extensions == {"com.example.other": {"a": 1}}


@pytest.mark.parametrize("extensions", ["not json", "[]", "null-ish", "42"])
def test_an_extensions_part_that_is_not_a_json_object_is_refused_and_runs_nothing(served, extensions):
    response = call("post", ROUTE, files=text_parts(inputs=json.dumps({"prompt": "hi"}), extensions=extensions))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_extensions"
    assert served.engine_calls == 0


def test_an_extensions_body_key_that_is_not_an_object_is_refused(served):
    response = call("post", ROUTE, json={"inputs": {"prompt": "hi"}, "extensions": [1]})
    # An outer layer that reads the same control may refuse first (invalid_prompt_mode); either way nothing runs.
    assert response.status_code == 400
    assert response.json()["error"]["code"] in ("invalid_extensions", "invalid_prompt_mode")
    assert served.engine_calls == 0


def test_a_prompt_mode_the_gateway_does_not_know_is_refused_as_on_the_other_job_routes(served):
    response = call("post", ROUTE, json={"inputs": {"prompt": "hi"},
                                         "extensions": {prompt_memory.NAMESPACE: {"mode": "bogus"}}})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_prompt_mode"
    assert served.engine_calls == 0


def test_a_control_sent_as_a_file_is_refused(served):
    response = call("post", ROUTE, files={"inputs": (None, json.dumps({"prompt": "hi"})),
                                          "extensions": ("e.json", json.dumps(FORGET), "application/json")})
    assert response.status_code == 422, response.text
    assert served.engine_calls == 0


# ------------------------------------------------------------------------------- the key and the prompt-memory decision
# The effective retention decision (header, body and member policy combined) is part of what an Idempotency-Key is bound
# to. A second submit with the same key and inputs but a forget decision must not return, or continue under, the first
# run that was remembered.

KEY = {"Idempotency-Key": "k-memory"}


def remember_first(**extra):
    return call("post", ROUTE, json={"inputs": {"prompt": SENTINEL}}, headers={**KEY, **extra})


@pytest.mark.parametrize("form", list(forms_of_forget()))
def test_a_forget_submit_on_a_key_used_by_a_queued_remembered_run_is_a_conflict(queued, tmp_path, form):
    first = remember_first()
    assert first.status_code == 202, first.text
    kwargs, headers = forms_of_forget()[form]
    second = call("post", ROUTE, headers={**KEY, **headers}, **kwargs)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "idempotency_conflict"
    assert "job_id" not in second.json()
    with media_jobs.connection(queued.media) as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0] == 1
    # The remembered run is untouched: nothing turned its payload into a forgotten one.
    payload = proxy._load_image_media_job_payload(first.json()["job_id"])
    assert prompt_memory.NAMESPACE not in (payload["body"].get("extensions") or {})


@pytest.mark.parametrize("form", list(forms_of_forget()))
def test_a_forget_submit_on_a_key_used_by_a_completed_remembered_run_is_a_conflict(served, form):
    first = remember_first(Prefer="wait=10")
    assert first.status_code == 200 and first.json()["status"] == "succeeded", first.text
    kwargs, headers = forms_of_forget()[form]
    second = call("post", ROUTE, headers={**KEY, **headers, "Prefer": "wait=10"}, **kwargs)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "idempotency_conflict"
    assert "job_id" not in second.json() and "results" not in second.json()
    assert served.engine_calls == 1


@pytest.mark.parametrize("form", list(forms_of_forget()))
def test_a_remember_submit_on_a_key_used_by_a_forgotten_run_is_a_conflict_too(queued, form):
    kwargs, headers = forms_of_forget()[form]
    first = call("post", ROUTE, headers={**KEY, **headers}, **kwargs)
    assert first.status_code == 202, first.text
    second = remember_first()
    assert second.status_code == 409 and second.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize("form", list(forms_of_forget()))
def test_the_same_forget_decision_twice_on_one_key_still_replays(served, form):
    kwargs, headers = forms_of_forget()[form]
    first = call("post", ROUTE, headers={**KEY, **headers, "Prefer": "wait=10"}, **kwargs)
    second = call("post", ROUTE, headers={**KEY, **headers, "Prefer": "wait=10"}, **kwargs)
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    assert first.json()["job_id"] == second.json()["job_id"] and second.json()["disposition"] == "replay"
    assert served.engine_calls == 1


def test_the_same_forget_decision_in_two_spellings_is_one_request(queued):
    """The identity holds the effective decision, not the spelling: a header forget and a body forget are one."""
    kwargs, headers = forms_of_forget()["header"]
    first = call("post", ROUTE, headers={**KEY, **headers}, **kwargs)
    kwargs, headers = forms_of_forget()["json_body"]
    second = call("post", ROUTE, headers={**KEY, **headers}, **kwargs)
    assert first.status_code == 202 and second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"] and second.json()["disposition"] == "replay"


# ------------------------------------------------------------------------------------------------- restart and receipts


def _after_a_restart_with_a_run_on_the_job_store(queued):
    """A run accepted by the stopped gateway: its job is queued, its receipt is open, and no owner holds the receipt."""
    created = call("post", ROUTE, json={"inputs": {"prompt": "hi"}})
    assert created.status_code == 202, created.text
    request_id = created.headers["x-request-id"]
    proxy._RUN_RECEIPT_OWNERS.pop(request_id, None)
    return created.json()["job_id"], request_id


def test_a_run_recovery_replays_from_its_committed_result_keeps_its_receipt_succeeded(queued):
    job_id, request_id = _after_a_restart_with_a_run_on_the_job_store(queued)
    limits = result_bundle.ResultLimits()
    with media_jobs.connection(queued.media) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, job_id, 1, "result", b"{}", limits=limits)
        result_bundle.publish_in(conn, job_id, 1, limits=limits)
        conn.commit()

    proxy._recover_workflow_runs()

    assert media_jobs.get(job_id, audience="internal", path=queued.media)["status"] == "succeeded"
    closed = receipt_store.get(request_id)
    assert (closed["status"], closed["metadata"]["terminal_reason"]) == ("succeeded", "succeeded")


def test_a_run_recovery_cannot_start_closes_its_receipt_even_though_the_job_is_no_longer_unfinished(queued):
    job_id, request_id = _after_a_restart_with_a_run_on_the_job_store(queued)
    with sqlite3.connect(queued.media) as conn:
        conn.execute("DELETE FROM job_envelope WHERE job_id=?", (job_id,))

    proxy._recover_workflow_runs()

    job = media_jobs.get(job_id, audience="internal", path=queued.media)
    assert (job["status"], job["error_code"]) == ("failed", "pin_not_recorded"), job
    assert receipt_store.get(request_id)["status"] == "failed"


def test_the_run_core_follows_the_jobs_database_it_was_built_for(served, monkeypatch, tmp_path):
    """A core built while another jobs database was current is not served: an earlier test's deleted directory once
    reached a later test through the cached core (gate 38b)."""
    first = proxy._workflow_run_core()
    assert first is not None and first._path == served.media
    other = tmp_path / "other" / "media_jobs.db"
    other.parent.mkdir()
    media_jobs.init_db(other)
    monkeypatch.setattr(media_jobs, "DB_PATH", other)
    second = proxy._workflow_run_core()
    assert second is not first
    assert second._path == other
    assert proxy._workflow_run_core() is second


def test_deleted_job_get_and_content_are_not_found(served):
    created = call("post", ROUTE, json={"inputs": {"prompt": "hi"}}, headers={"Prefer": "wait=10"})
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    media_jobs.tombstone_job(job_id, path=served.media)
    for suffix in ("", "/content"):
        response = call("get", f"/gateway/v1/media/jobs/{job_id}{suffix}")
        assert response.status_code == 404, response.text
    assert media_jobs.get(job_id, include_deleted=True, path=served.media) is not None
    assert served.engine_calls == 1
