"""S7 parity: every observable behaviour of the image routes, old route against frontend row.

One test per item of ``docs/design/c2d-s7-parity/images.md``; the test name carries the item id (``images-07`` is
``test_images_07_*``, ``images-E03`` is ``test_images_E03_*``). Each test sends the same request to the old route and to
its frontend row (``tests/support/s7_parity_images_rig.py``), over the golden rig's fake engine, and compares the named
facts (``tests/support/s7_parity_images_facts.py``). The old route is the oracle: a test fails when the row path answers
differently. Where the design allows a difference (``CODE_TO_DATA_S7_PARITY.md`` section 4) the test asserts the new
behaviour and names the delta in its docstring.

A guard (``old_has``) in each test checks the scenario reaches the behaviour on the old route, so a test cannot pass by
comparing two empty answers.
"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

import proxy
from tests import test_adapter_differential as base
from tests import test_adapter_differential_s7 as s7
from tests.support.access import authenticated, identity, plan_for
from tests.support.adapter_differential_rig import comfy_exchanges, exchange, history_done
from tests.support.s7_parity_images_facts import (
    assert_same, asset_files, data_items, doc, engine, engine_bodies, engine_without_probes, failure, header, headers_with,
    item_keys, media_jobs, old_has, origins, receipt_fields, receipt_row, receipt_summary, receipts, retention_destinations,
    sleeps, status, top_keys,
)
from tests.support.s7_parity_images_rig import ROW_PREFIX, observe

GEN = "/v1/images/generations"
EDIT = "/v1/images/edits"
VAR = "/v1/images/variations"
JOB = "/gateway/v1/media/jobs/image-generations"
BG = base.BG

FAILED = s7.FAILED_HISTORY
NEVER = s7.NEVER_HISTORY


# ------------------------------------------------------------------------------------------------ scenarios
def pair(tmp_path, path, request, setup, **kw):
    """The same request on the old route and on its row."""
    return (observe(tmp_path, path, request, setup, row=False, **kw), observe(tmp_path, path, request, setup, row=True, **kw))


def ok(rig, n=1, **kw):
    base._comfy(rig, comfy_exchanges(views=[(base._png((40 * i, 30, 200 - 40 * i)), "image/png") for i in range(n)], **kw))


def edit_ok(rig):
    base._comfy(rig, comfy_exchanges(views=[(base._png((20, 20, 20)), "image/png")], history=history_done(node="13")))


def variation_ok(rig):
    async def no_cutout(*_a, **_k):
        return proxy.ImageBackgroundRemovalDecision(False)

    rig.set(proxy, "resolve_image_background_decision", no_cutout)
    base._comfy(rig, comfy_exchanges(views=[(base._png((70, 70, 160)), "image/png")], history=history_done(node="10")))


def disabled(rig):
    """The image service is off, and the engine would refuse any call."""
    base._comfy_down(rig)
    rig.monkeypatch.setitem(proxy.runtime().derived, "COMFY_ENABLED", False)
    for record in (proxy.config.get("backends") or {}).values():
        if isinstance(record, dict) and record.get("protocol") == "node-graph":
            rig.monkeypatch.setitem(record, "enabled", False)


def denied(rig):
    base._comfy_down(rig)
    rig._exit.enter_context(authenticated(identity(plan_for(caps=("chat",)))))


def timeout(rig):
    s7.ImageGenerate("ig", "POST", GEN, mode="sync").engine_never(rig)


def body(**fields):
    return {"json_body": {"prompt": "a red fox", "response_format": "b64_json", **BG, **fields}}


def raw_json(content):
    return {"content": content, "headers": {"content-type": "application/json"}}


def form(files=None, **data):
    out = {"data": {"prompt": "make the fox blue", "response_format": "b64_json", **BG, **data}}
    out["files"] = files if files is not None else {"image": ("in.png", base._png(), "image/png")}
    return out


def cell(tmp_path, code, outcome):
    """One S7 cell (the golden rig's route x outcome recipe) on the old route and on its row."""
    results = []
    for use_row in (False, True):
        route = copy.copy(s7.ROUTES[code])
        if use_row:
            route.path = ROW_PREFIX + route.path
        setup, drive, _ = s7.build(route, outcome)
        results.append(observe(tmp_path, route.path, drive, setup, row=use_row, name=f"{code}-{outcome}"))
    return results


# ------------------------------------------------------------------------------------------------ 1. images.generate
def test_images_01_disabled_service_503(tmp_path):
    old, row = pair(tmp_path, GEN, body(), disabled)
    old_has(old, "503", status(old) == 503)
    assert_same(old, row, status, failure, ("retry-after", lambda o: header(o, "retry-after")), engine)


def test_images_02_body_not_json_400(tmp_path):
    old, row = pair(tmp_path, GEN, raw_json(b"{"), ok)
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure, engine)


def test_images_03_body_not_object_400(tmp_path):
    old, row = pair(tmp_path, GEN, raw_json(b"[1]"), ok)
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure, engine)


def test_images_04_unknown_field_refused(tmp_path):
    old, row = pair(tmp_path, GEN, body(not_a_field=1), ok)
    assert_same(old, row, status, failure, engine, ("accepted_inputs", lambda o: receipt_fields(o, "accepted_inputs", "rejected_inputs", "ignored_inputs")))


@pytest.mark.parametrize("fields", [{"size": "abc"}, {"background": "purple"}, {"n": 0}, {"response_format": "gif"}], ids=lambda f: next(iter(f)))
def test_images_05_validation_before_engine(tmp_path, fields):
    old, row = pair(tmp_path, GEN, body(**fields), ok)
    old_has(old, "a refusal before the engine", status(old) == 400 and not engine(old))
    assert_same(old, row, status, failure, engine)


def test_images_05_n_above_ten_is_clamped_with_a_warning(tmp_path):
    old, row = pair(tmp_path, GEN, body(n=11), lambda rig: ok(rig, 11))
    old_has(old, "an answer", status(old) == 200)
    assert_same(old, row, status, ("returned", lambda o: len(data_items(o))), ("warning", lambda o: header(o, "x-proxy-warning")),
                ("rounds", lambda o: header(o, "x-image-rounds")))


def test_images_06_missing_prompt_400(tmp_path):
    old, row = pair(tmp_path, GEN, {"json_body": {"response_format": "b64_json", **BG}}, ok)
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure, engine)


def test_images_07_policy_denial_403_with_plan_details(tmp_path):
    old, row = pair(tmp_path, GEN, body(), denied)
    old_has(old, "403", status(old) == 403)
    assert_same(old, row, status, failure, engine, ("retention facts", retention_destinations))


def test_images_08_admission_refusal_response(tmp_path):
    old, row = cell(tmp_path, "ig", "admission_refusal")
    assert_same(old, row, status, failure, ("retry-after", lambda o: header(o, "retry-after")), engine)


def test_images_09_model_resolves_to_a_workflow_on_the_receipt(tmp_path):
    old, row = pair(tmp_path, GEN, body(model="krea2-turbo"), ok)
    old_has(old, "a receipt", receipt_row(old))
    assert_same(old, row, status, ("receipt", lambda o: receipt_fields(o, "resolved_workflow", "requested_workflow", "manifest_id", "manifest_hash", "manifest_version")))


def test_images_10_b64_json_answer(tmp_path):
    old, row = pair(tmp_path, GEN, body(), ok)
    old_has(old, "b64", data_items(old) and "b64_json" in data_items(old)[0])
    assert_same(old, row, status, ("b64", lambda o: [i.get("b64_json") for i in data_items(o)]), ("origin", origins))


def test_images_11_url_answer(tmp_path):
    old, row = pair(tmp_path, GEN, body(response_format="url"), ok)
    old_has(old, "url", data_items(old) and "url" in data_items(old)[0])
    assert_same(old, row, status, item_keys, ("url path", lambda o: [i["url"].split("?")[0].split("/", 3)[-1] for i in data_items(o)]))


def test_images_12_batch_runs_rounds_until_n(tmp_path):
    old, row = pair(tmp_path, GEN, body(n=3), lambda rig: ok(rig, 3))
    old_has(old, "three images", len(data_items(old)) == 3)
    assert_same(old, row, status, ("returned", lambda o: len(data_items(o))), engine_without_probes,
                ("rounds", lambda o: header(o, "x-image-rounds")), ("returned header", lambda o: header(o, "x-image-returned")))


def test_images_13_output_format_background_quality_echoed(tmp_path):
    old, row = pair(tmp_path, GEN, body(output_format="jpeg", quality="high"), ok)
    old_has(old, "top-level fields", top_keys(old))
    assert_same(old, row, status, top_keys, item_keys, ("fields", lambda o: {k: v for k, v in (doc(o) or {}).items() if k != "data" and k != "created"}))


def test_images_14_size_adjustment_is_reported_and_recorded(tmp_path):
    old, row = pair(tmp_path, GEN, body(size="768X512"), ok)
    old_has(old, "an adjustment", header(old, "x-azizam-size-adjusted") or (receipt_fields(old, "metadata") or {}).get("metadata"))
    assert_same(old, row, status, ("header", lambda o: header(o, "x-azizam-size-adjusted")),
                ("receipt", lambda o: receipt_fields(o, "metadata", "warnings")), ("size", lambda o: (doc(o) or {}).get("size")))


def test_images_15_adjustment_text_is_requested_arrow_effective(tmp_path):
    old, row = pair(tmp_path, GEN, body(size="768X512"), ok)
    old_has(old, "the arrow text", "->" in (header(old, "x-azizam-size-adjusted") or ""))
    assert_same(old, row, ("text", lambda o: header(o, "x-azizam-size-adjusted")), ("warnings", lambda o: receipt_fields(o, "warnings")))


def test_images_16_background_removal_headers(tmp_path):
    old, row = pair(tmp_path, GEN, {"json_body": {"prompt": "a red fox", "response_format": "b64_json", "background": "transparent"}}, ok)
    old_has(old, "the removal header", header(old, "x-image-background-removal"))
    assert_same(old, row, status, ("headers", lambda o: headers_with(o, "x-image-background")), engine_without_probes)


def test_images_17_engine_traffic_of_one_round(tmp_path):
    old, row = pair(tmp_path, GEN, body(), ok)
    assert_same(old, row, status, engine)


def test_images_18_poll_schedule(tmp_path):
    old, row = pair(tmp_path, GEN, body(), lambda rig: ok(rig, running_polls=3))
    old_has(old, "a poll schedule", len(sleeps(old)) >= 3)
    assert_same(old, row, status, sleeps, engine_without_probes)


def test_images_19_out_of_memory_frees_models_and_retries(tmp_path):
    oom = {"p1": {"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"node_type": "KSampler", "node_id": "7", "exception_message": "CUDA out of memory"}]]}, "outputs": {}}}

    def setup(rig):
        base._comfy(rig, comfy_exchanges(views=[(base._png(), "image/png")], history=oom, strict=False,
                                         extra=[exchange("free", "POST", "/free", {"json": {}}, times=1)]))
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "a /free call", any(path == "/free" for _m, path in engine(old)))
    assert_same(old, row, status, failure, engine_without_probes, ("freed header", lambda o: header(o, "x-comfyui-models-freed")),
                ("receipt metadata", lambda o: receipt_fields(o, "metadata")))


def test_images_20_an_empty_round_ends_the_batch(tmp_path):
    old, row = pair(tmp_path, GEN, body(n=3), lambda rig: ok(rig, 1))
    old_has(old, "an answer", status(old))
    assert_same(old, row, status, failure, ("returned", lambda o: len(data_items(o))), engine_without_probes, ("warning", lambda o: header(o, "x-proxy-warning")))


def test_images_21_generation_timeout_504_with_queue_position(tmp_path):
    old, row = pair(tmp_path, GEN, body(), timeout)
    old_has(old, "504", status(old) == 504)
    assert_same(old, row, status, failure, engine_without_probes, sleeps)


def _failing(history=None, prompt=None, down=False, extra=None):
    def setup(rig):
        if down:
            base._comfy_down(rig)
        else:
            kw = {}
            if history is not None:
                kw["history"] = history
            if prompt is not None:
                kw["prompt"] = prompt
            base._comfy(rig, comfy_exchanges(strict=False, **kw))
    return setup


UNREACHABLE = _failing(down=True)
HTTP_500 = _failing(prompt={"status": 500, "json": {"error": {"type": "server_error", "message": "boom"}}})
NO_PROMPT_ID = _failing(prompt={"json": {"node_errors": {}}})
EXEC_ERROR = _failing(history=FAILED)


@pytest.mark.parametrize("name,setup", [("unreachable", UNREACHABLE), ("http_500", HTTP_500), ("no_prompt_id", NO_PROMPT_ID)])
def test_images_22_engine_failures_at_submit(tmp_path, name, setup):
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "a 5xx", (status(old) or 0) >= 500)
    assert_same(old, row, status, failure, engine_without_probes)


def test_images_23_execution_error_502(tmp_path):
    old, row = pair(tmp_path, GEN, body(), EXEC_ERROR)
    old_has(old, "502", status(old) == 502)
    assert_same(old, row, status, failure, engine_without_probes)


def test_images_24_transient_history_errors_are_retried(tmp_path):
    def setup(rig):
        ok(rig, extra=[exchange("h-503", "GET", "/history/p1", {"status": 503, "json": {}}, times=2)])
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "an answer", status(old))
    assert_same(old, row, status, failure, engine_without_probes, sleeps)


def test_images_25_view_missing_fails_the_round(tmp_path):
    def setup(rig):
        base._comfy(rig, comfy_exchanges(views=[], strict=False))
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "an error", (status(old) or 0) >= 400)
    assert_same(old, row, status, failure, engine_without_probes)


def test_images_26_client_disconnect_during_poll_cancels_and_answers_499(tmp_path):
    def setup(rig):
        timeout(rig)

    def request(rig, row=False):
        raise NotImplementedError

    def mk(use_row):
        async def go(rig):
            target = (ROW_PREFIX if use_row else "") + GEN
            polls = lambda: sum(1 for e in rig.recorder.engine if e["path"].startswith("/history/")) >= 2
            await rig.call_disconnecting("POST", target, label="disconnect", json_body=body()["json_body"], disconnect_when=polls)
        return go
    old = observe(tmp_path, GEN, mk(False), setup, row=False)
    row = observe(tmp_path, GEN, mk(True), setup, row=True)
    old_has(old, "a cancel call", any("cancel" in path for _m, path in engine(old)))
    assert_same(old, row, status, failure, engine_without_probes)


def test_images_27_task_cancellation_cancels_the_engine_job(tmp_path):
    old, row = cell(tmp_path, "ig", "cancel_queued")
    old_has(old, "a cancel call", any("cancel" in path for _m, path in engine(old)))
    assert_same(old, row, engine_without_probes, ("cancel body", lambda o: engine_bodies(o, "/api/jobs/p1/cancel")), ("receipts", receipt_summary))


def test_images_28_no_health_probe_before_submit(tmp_path):
    """writers-62: the only ``/system_stats`` calls are the capacity samples; none comes before the first prompt."""
    old, row = pair(tmp_path, GEN, body(), ok)
    assert_same(old, row, engine)


def test_images_29_receipt_opens_before_the_route_policy(tmp_path):
    old, row = pair(tmp_path, GEN, body(), denied)
    old_has(old, "a rejected receipt", receipt_summary(old))
    assert_same(old, row, receipt_summary, ("retention", retention_destinations))


def test_images_30_failure_texts(tmp_path):
    results = [pair(tmp_path / n, GEN, body(), s) for n, s in (("a", UNREACHABLE), ("b", NO_PROMPT_ID), ("c", EXEC_ERROR), ("d", timeout))]
    for old, row in results:
        assert_same(old, row, status, failure)


def test_images_31_receipt_closes_with_the_outcome(tmp_path):
    ok_pair = pair(tmp_path / "ok", GEN, body(), ok)
    bad_pair = pair(tmp_path / "bad", GEN, raw_json(b"{"), ok)
    err_pair = pair(tmp_path / "err", GEN, body(), EXEC_ERROR)
    for old, row in (ok_pair, bad_pair, err_pair):
        assert_same(old, row, receipt_summary, ("metadata", lambda o: receipt_fields(o, "metadata", "warnings")))


def test_images_32_idempotency_key_replays_the_first_answer(tmp_path):
    """Allowed delta ``idempotency_acceptance``: the old sync route ran the engine for every request; the row answers the
    second request with the first run."""
    key = {"Idempotency-Key": "k-1"}

    def go(use_row):
        async def run(rig):
            target = (ROW_PREFIX if use_row else "") + GEN
            await rig.call("POST", target, label="first", headers=key, **body())
            await rig.call("POST", target, label="same key", headers=key, **body())
            await rig.call("POST", target, label="other body", headers=key, **body(prompt="a red wolf"))
        return run
    row = observe(tmp_path, GEN, go(True), ok, row=True)
    prompts = [b for b in engine_bodies(row, "/prompt")]
    assert len(prompts) == 1, f"the row ran the engine {len(prompts)} times for one key"
    assert status(row, 0) == 200 and status(row, 1) == 200 and status(row, 2) == 409
    assert doc(row, 0)["data"] == doc(row, 1)["data"]


def test_images_33_cancel_of_queued_and_of_finished(tmp_path):
    queued = cell(tmp_path / "q", "ig", "cancel_queued")
    finished = cell(tmp_path / "f", "ig", "cancel_finished")
    assert_same(*queued, engine_without_probes, ("cancel bodies", lambda o: engine_bodies(o, "/api/jobs/p1/cancel")))
    assert_same(*finished, engine_without_probes, receipt_summary, ("statuses", lambda o: [status(o, i) for i in range(len(o.trace["caller"]))]))


def test_images_34_wait_timeout_answers_and_closes_the_receipt(tmp_path):
    old, row = cell(tmp_path, "ig", "wait_timeout")
    assert_same(old, row, status, failure, sleeps, engine_without_probes, receipt_summary)


def test_images_35_history_summary_carries_no_run_facts(tmp_path):
    """writers-30: no image route sets run facts, so the kept History summary has none. The row must not add any."""
    old, row = pair(tmp_path, GEN, body(), ok)
    assert_same(old, row, ("history run", lambda o: [r.get("receipt_json", {}).get("metadata", {}).get("run") for r in receipts(o)]))


def test_images_36_library_asset_origin(tmp_path):
    old, row = pair(tmp_path, GEN, body(), ok)
    old_has(old, "an origin", any(origins(old)))
    assert_same(old, row, origins, asset_files)


def test_images_37_response_headers(tmp_path):
    old, row = pair(tmp_path, GEN, body(n=2), lambda rig: ok(rig, 2))
    old_has(old, "image headers", headers_with(old, "x-image"))
    assert_same(old, row, ("headers", lambda o: headers_with(o, "x-image", "x-proxy", "x-comfyui", "x-azizam")))


def test_images_38_sync_route_keeps_no_job_row(tmp_path):
    old, row = pair(tmp_path, GEN, body(), ok)
    assert_same(old, row, ("job rows", lambda o: [(j["intent"], j["status"]) for j in media_jobs(o)]), ("retention", retention_destinations))
