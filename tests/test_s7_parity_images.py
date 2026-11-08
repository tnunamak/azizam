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

NEEDS_RH = pytest.mark.xfail(strict=True, reason="needs RH open_receipt")
# The row path does not yet match the old route: builder I's steps 2-4 (node-graph runner, result-processing stages,
# VRAM and batching hooks, the route move) turn these green, and remove the mark on each one that passes.
ROW_PENDING = pytest.mark.xfail(strict=True, reason="row path pending: builder I steps 2-4")

FAILED = s7.FAILED_HISTORY
NEVER = s7.NEVER_HISTORY


# ------------------------------------------------------------------------------------------------ scenarios
def pair(tmp_path, path, request, setup, **kw):
    """The same request on the old route and on its row."""
    old = observe(tmp_path, path, request, setup, row=False, **kw)
    assert "exception" not in old.extra, f"the old route crashed: the scenario is wrong ({old.extra['exception']})"
    return old, observe(tmp_path, path, request, setup, row=True, **kw)


def ok(rig, n=1, **kw):
    kw.setdefault("strict", False)       # a refusal scenario leaves the engine exchanges uncalled; the fake still refuses a stray call
    base._comfy(rig, comfy_exchanges(views=[(base._png((40 * i, 30, 200 - 40 * i)), "image/png") for i in range(n)], **kw))


def engine_answered(obs, method, path, status_code):
    """Guard: the fake engine really answered ``method path`` with ``status_code`` (the failure the scenario injects fired).
    ``extra`` exchanges that cannot match leave the well-behaved ones in charge; this tells the two apart."""
    return [e for e in obs.engine_requests if e["method"] == method and e["path"] == path
            and (e.get("outcome") or {}).get("status") == status_code]


def edit_ok(rig):
    base._comfy(rig, comfy_exchanges(views=[(base._png((20, 20, 20)), "image/png")], history=history_done(node="13"), strict=False))


def variation_ok(rig):
    async def no_cutout(*_a, **_k):
        return proxy.ImageBackgroundRemovalDecision(False)

    rig.set(proxy, "resolve_image_background_decision", no_cutout)
    base._comfy(rig, comfy_exchanges(views=[(base._png((70, 70, 160)), "image/png")], history=history_done(node="10"), strict=False))


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
@ROW_PENDING
def test_images_01_disabled_service_503(tmp_path):
    old, row = pair(tmp_path, GEN, body(), disabled)
    old_has(old, "503", status(old) == 503)
    assert_same(old, row, status, failure, ("retry-after", lambda o: header(o, "retry-after")), engine)


def test_images_02_body_not_json_400(tmp_path):
    old, row = pair(tmp_path, GEN, raw_json(b"{"), ok)
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure, engine)


@ROW_PENDING
def test_images_03_body_not_object_400(tmp_path):
    old, row = pair(tmp_path, GEN, raw_json(b"[1]"), ok)
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure, engine)


@NEEDS_RH
def test_images_04_unknown_field_refused(tmp_path):
    old, row = pair(tmp_path, GEN, body(not_a_field=1), ok)
    assert_same(old, row, status, failure, engine, ("accepted_inputs", lambda o: receipt_fields(o, "accepted_inputs", "rejected_inputs", "ignored_inputs")))


@pytest.mark.parametrize("fields", [{"background": "purple"}, {"n": 0}, {"n": 11}, {"response_format": "gif"}], ids=lambda f: next(iter(f)))
@ROW_PENDING
def test_images_05_validation_before_engine(tmp_path, fields):
    """``n`` above 10 is a 400 at the route; the clamp to ``MAX_IMAGES_PER_REQUEST`` inside the batching loop is not
    reachable from the wire."""
    old, row = pair(tmp_path, GEN, body(**fields), ok)
    old_has(old, "a refusal before the engine", status(old) == 400 and not engine(old))
    assert_same(old, row, status, failure, engine)


@ROW_PENDING
def test_images_05_size_text_that_is_not_a_size_falls_back_to_1024(tmp_path):
    old, row = pair(tmp_path, GEN, body(size="abc"), ok)
    old_has(old, "an answer", status(old) == 200)
    assert_same(old, row, status, ("size", lambda o: (doc(o) or {}).get("size")), ("adjusted", lambda o: header(o, "x-azizam-size-adjusted")))


@ROW_PENDING
def test_images_06_missing_prompt_400(tmp_path):
    old, row = pair(tmp_path, GEN, {"json_body": {"response_format": "b64_json", **BG}}, ok)
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure, engine)


@NEEDS_RH
def test_images_07_policy_denial_403_with_plan_details(tmp_path):
    old, row = pair(tmp_path, GEN, body(), denied)
    old_has(old, "403", status(old) == 403)
    assert_same(old, row, status, failure, engine, ("retention facts", retention_destinations))


@ROW_PENDING
def test_images_08_admission_refusal_response(tmp_path):
    old, row = cell(tmp_path, "ig", "admission_refusal")
    assert_same(old, row, status, failure, ("retry-after", lambda o: header(o, "retry-after")), engine)


@NEEDS_RH
def test_images_09_model_resolves_to_a_workflow_on_the_receipt(tmp_path):
    old, row = pair(tmp_path, GEN, body(model="krea2-turbo"), ok)
    old_has(old, "a receipt", receipt_row(old))
    assert_same(old, row, status, ("receipt", lambda o: receipt_fields(o, "resolved_workflow", "requested_workflow", "manifest_id", "manifest_hash", "manifest_version")))


def test_images_10_b64_json_answer(tmp_path):
    old, row = pair(tmp_path, GEN, body(), ok)
    old_has(old, "b64", data_items(old) and "b64_json" in data_items(old)[0])
    assert_same(old, row, status, ("b64", lambda o: [i.get("b64_json") for i in data_items(o)]), ("origin", origins))


@ROW_PENDING
def test_images_11_url_answer(tmp_path):
    old, row = pair(tmp_path, GEN, body(response_format="url"), ok)
    old_has(old, "url", data_items(old) and "url" in data_items(old)[0])
    assert_same(old, row, status, item_keys, ("url path", lambda o: [str(i.get("url")).split("?")[0].split("/", 3)[-1] for i in data_items(o)]))


@ROW_PENDING
def test_images_12_batch_runs_rounds_until_n(tmp_path):
    old, row = pair(tmp_path, GEN, body(n=3), lambda rig: ok(rig, 3))
    old_has(old, "three images", len(data_items(old)) == 3)
    assert_same(old, row, status, ("returned", lambda o: len(data_items(o))), engine_without_probes,
                ("rounds", lambda o: header(o, "x-image-rounds")), ("returned header", lambda o: header(o, "x-image-returned")))


@ROW_PENDING
def test_images_13_output_format_background_quality_echoed(tmp_path):
    old, row = pair(tmp_path, GEN, body(output_format="jpeg", quality="high"), ok)
    old_has(old, "top-level fields", top_keys(old))
    assert_same(old, row, status, top_keys, item_keys, ("fields", lambda o: {k: v for k, v in (doc(o) or {}).items() if k != "data" and k != "created"}))


@NEEDS_RH
def test_images_14_size_adjustment_is_reported_and_recorded(tmp_path):
    old, row = pair(tmp_path, GEN, body(size="768X512"), ok)
    old_has(old, "an adjustment", header(old, "x-azizam-size-adjusted") or (receipt_fields(old, "metadata") or {}).get("metadata"))
    assert_same(old, row, status, ("header", lambda o: header(o, "x-azizam-size-adjusted")),
                ("receipt", lambda o: receipt_fields(o, "metadata", "warnings")), ("size", lambda o: (doc(o) or {}).get("size")))


@NEEDS_RH
def test_images_15_adjustment_text_is_requested_arrow_effective(tmp_path):
    old, row = pair(tmp_path, GEN, body(size="768X512"), ok)
    old_has(old, "the arrow text", "->" in (header(old, "x-azizam-size-adjusted") or ""))
    assert_same(old, row, ("text", lambda o: header(o, "x-azizam-size-adjusted")), ("warnings", lambda o: receipt_fields(o, "warnings")))


@ROW_PENDING
def test_images_16_background_removal_headers(tmp_path):
    old, row = pair(tmp_path, GEN, {"json_body": {"prompt": "a red fox", "response_format": "b64_json", "background": "transparent"}}, ok)
    old_has(old, "the removal header", header(old, "x-image-background-removal"))
    assert_same(old, row, status, ("headers", lambda o: headers_with(o, "x-image-background")), engine_without_probes)


def test_images_17_engine_traffic_of_one_round(tmp_path):
    old, row = pair(tmp_path, GEN, body(), ok)
    assert_same(old, row, status, engine)


@ROW_PENDING
def test_images_18_poll_schedule(tmp_path):
    old, row = pair(tmp_path, GEN, body(), lambda rig: ok(rig, running_polls=3))
    old_has(old, "a poll schedule", len(sleeps(old)) >= 3)
    assert_same(old, row, status, sleeps, engine_without_probes)


@NEEDS_RH
def test_images_19_out_of_memory_frees_models_and_retries(tmp_path):
    def setup(rig):
        ok(rig, extra=[exchange("prompt-oom", "POST", "/prompt",
                                {"status": 500, "body": [{"text": "CUDA out of memory. Tried to allocate 2.00 GiB"}]}, times=1)])
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "a /free call", any(path == "/free" for _m, path in engine(old)))
    assert_same(old, row, status, failure, engine_without_probes, ("freed header", lambda o: header(o, "x-comfyui-models-freed")),
                ("receipt metadata", lambda o: receipt_fields(o, "metadata")))


@ROW_PENDING
def test_images_20_an_empty_round_ends_the_batch(tmp_path):
    old, row = pair(tmp_path, GEN, body(n=3), lambda rig: ok(rig, 1))
    old_has(old, "an answer", status(old))
    assert_same(old, row, status, failure, ("returned", lambda o: len(data_items(o))), engine_without_probes, ("warning", lambda o: header(o, "x-proxy-warning")))


@ROW_PENDING
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
@ROW_PENDING
def test_images_22_engine_failures_at_submit(tmp_path, name, setup):
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "a 5xx", (status(old) or 0) >= 500)
    assert_same(old, row, status, failure, engine_without_probes)


def test_images_23_execution_error_502(tmp_path):
    old, row = pair(tmp_path, GEN, body(), EXEC_ERROR)
    old_has(old, "502", status(old) == 502)
    assert_same(old, row, status, failure, engine_without_probes)


@ROW_PENDING
def test_images_24_transient_history_errors_are_retried(tmp_path):
    def setup(rig):
        ok(rig, extra=[exchange("h-503", "GET", "/history/p1", {"status": 503, "json": {}}, times=2)])
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "two 503 history answers", len(engine_answered(old, "GET", "/history/p1", 503)) == 2)
    assert_same(old, row, status, failure, engine_without_probes, sleeps)


def test_images_25_view_missing_fails_the_round(tmp_path):
    def setup(rig):
        base._comfy(rig, comfy_exchanges(views=[(b"", "text/plain")], strict=False, extra=[
            exchange("view-missing", "GET", "/view", {"status": 404, "json": {"error": "not found"}}, times=1, required=False)]))
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "a 404 on the download", engine_answered(old, "GET", "/view", 404) and (status(old) or 0) >= 400)
    assert_same(old, row, status, failure, engine_without_probes)


@ROW_PENDING
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


@NEEDS_RH
def test_images_29_receipt_opens_before_the_route_policy(tmp_path):
    old, row = pair(tmp_path, GEN, body(), denied)
    old_has(old, "a rejected receipt", receipt_summary(old))
    assert_same(old, row, receipt_summary, ("retention", retention_destinations))


@ROW_PENDING
def test_images_30_failure_texts(tmp_path):
    results = [pair(tmp_path / n, GEN, body(), s) for n, s in (("a", UNREACHABLE), ("b", NO_PROMPT_ID), ("c", EXEC_ERROR), ("d", timeout))]
    for old, row in results:
        assert_same(old, row, status, failure)


@NEEDS_RH
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


@NEEDS_RH
def test_images_33_cancel_of_queued_and_of_finished(tmp_path):
    queued = cell(tmp_path / "q", "ig", "cancel_queued")
    finished = cell(tmp_path / "f", "ig", "cancel_finished")
    assert_same(*queued, engine_without_probes, ("cancel bodies", lambda o: engine_bodies(o, "/api/jobs/p1/cancel")))
    assert_same(*finished, engine_without_probes, receipt_summary, ("statuses", lambda o: [status(o, i) for i in range(len(o.trace["caller"]))]))


@NEEDS_RH
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


@ROW_PENDING
def test_images_37_response_headers(tmp_path):
    old, row = pair(tmp_path, GEN, body(n=2), lambda rig: ok(rig, 2))
    old_has(old, "image headers", headers_with(old, "x-image"))
    assert_same(old, row, ("headers", lambda o: headers_with(o, "x-image", "x-proxy", "x-comfyui", "x-azizam")))


@NEEDS_RH
def test_images_38_sync_route_keeps_no_job_row(tmp_path):
    old, row = pair(tmp_path, GEN, body(), ok)
    assert_same(old, row, ("job rows", lambda o: [(j["intent"], j["status"]) for j in media_jobs(o)]), ("retention", retention_destinations))


# ------------------------------------------------------------------------------------------------ 2. images.edit
MASK = ("mask.png", base._png((255, 255, 255)), "image/png")
IMG = ("in.png", base._png(), "image/png")
NOT_AN_IMAGE = ("in.png", b"not an image", "image/png")


def edit_form(files=None, **data):
    out = form(files=files, **data)
    return out


def edit_masked(**data):
    return form(files={"image": IMG, "mask": MASK}, **data)


def _edit_compare(old, row, *extra):
    assert_same(old, row, status, failure, engine_without_probes, *extra)


@ROW_PENDING
def test_images_E01_disabled_service_503(tmp_path):
    old, row = pair(tmp_path, EDIT, form(), disabled)
    old_has(old, "503", status(old) == 503)
    _edit_compare(old, row, ("retry-after", lambda o: header(o, "retry-after")))


@ROW_PENDING
def test_images_E02_invalid_form_400(tmp_path):
    old, row = pair(tmp_path, EDIT, {"content": b"not multipart", "headers": {"content-type": "multipart/form-data; boundary=zz"}}, edit_ok)
    old_has(old, "400", status(old) == 400)
    _edit_compare(old, row)


@ROW_PENDING
def test_images_E03_extensions_not_json_400(tmp_path):
    old, row = pair(tmp_path, EDIT, form(extensions="{"), edit_ok)
    old_has(old, "400", status(old) == 400)
    _edit_compare(old, row)


@ROW_PENDING
def test_images_E04_size_grammar(tmp_path):
    old, row = pair(tmp_path, EDIT, form(size="12 by 4"), edit_ok)
    old_has(old, "400", status(old) == 400)
    _edit_compare(old, row)


@ROW_PENDING
def test_images_E05_missing_image_400(tmp_path):
    old, row = pair(tmp_path, EDIT, form(files={"other": ("x.txt", b"x", "text/plain")}), edit_ok)
    old_has(old, "400", status(old) == 400)
    _edit_compare(old, row)


@NEEDS_RH
@pytest.mark.parametrize("model", ["not-a-real-model", "image-identity"])
def test_images_E06_model_resolution(tmp_path, model):
    old, row = pair(tmp_path, EDIT, edit_masked(model=model), edit_ok)
    old_has(old, "an answer", status(old))
    _edit_compare(old, row, ("receipt", lambda o: receipt_fields(o, "resolved_workflow", "requested_workflow")))


@ROW_PENDING
def test_images_E07_default_size_from_the_input_image(tmp_path):
    old, row = pair(tmp_path, EDIT, edit_masked(), edit_ok)
    old_has(old, "an answer", status(old) == 200)
    _edit_compare(old, row, ("size", lambda o: (doc(o) or {}).get("size")), ("prompt bodies", lambda o: engine_bodies(o, "/prompt")))


@ROW_PENDING
def test_images_E08_missing_prompt_400(tmp_path):
    request = form(files={"image": IMG})
    request["data"].pop("prompt")
    old, row = pair(tmp_path, EDIT, request, edit_ok)
    old_has(old, "400", status(old) == 400)
    _edit_compare(old, row)


@ROW_PENDING
def test_images_E09_missing_required_image(tmp_path):
    old, row = pair(tmp_path, EDIT, form(files={"mask": MASK}, model="krea2-turbo"), edit_ok)
    old_has(old, "400", status(old) == 400)
    _edit_compare(old, row)


@ROW_PENDING
def test_images_E10_too_many_images(tmp_path):
    request = {"data": form()["data"], "files": [("image", IMG), ("image", IMG), ("image", IMG), ("image", IMG), ("image", IMG)]}
    old, row = pair(tmp_path, EDIT, request, edit_ok)
    old_has(old, "an answer", status(old))
    _edit_compare(old, row)


@ROW_PENDING
def test_images_E11_mask_required_and_missing(tmp_path):
    old, row = pair(tmp_path, EDIT, form(files={"image": IMG}), edit_ok)
    old_has(old, "an answer", status(old))
    _edit_compare(old, row, ("headers", lambda o: headers_with(o, "x-image")))


@NEEDS_RH
def test_images_E12_policy_and_admission_cells(tmp_path):
    for outcome in ("policy_refusal", "admission_refusal"):
        old, row = cell(tmp_path / outcome, "ie", outcome)
        old_has(old, "a refusal", (status(old) or 0) >= 400)
        assert_same(old, row, status, failure, engine, receipt_summary, ("retention", retention_destinations))


@NEEDS_RH
def test_images_E13_refused_image(tmp_path):
    old, row = cell(tmp_path / "v", "ie", "validation_refusal")
    assert_same(old, row, status, failure, engine, receipt_summary)
    old2, row2 = pair(tmp_path / "bad", EDIT, form(files={"image": NOT_AN_IMAGE, "mask": MASK}), edit_ok)
    old_has(old2, "a refusal", (status(old2) or 0) >= 400)
    assert_same(old2, row2, status, failure, engine_without_probes)
    old3, row3 = pair(tmp_path / "mask", EDIT, form(files={"image": IMG, "mask": ("mask.png", b"junk", "image/png")}), edit_ok)
    assert_same(old3, row3, status, failure, engine_without_probes)


def _upload_refused(rig, response=None):
    base._comfy(rig, comfy_exchanges(strict=False, extra=[
        exchange("upload-refused", "POST", "/upload/image", response or {"status": 500, "json": {"error": "disk full"}}, times=1, required=False)]))


@ROW_PENDING
def test_images_E14_image_upload_refused_502(tmp_path):
    old, row = pair(tmp_path, EDIT, form(), _upload_refused)
    old_has(old, "502 after a refused upload", status(old) == 502 and engine_answered(old, "POST", "/upload/image", 500))
    _edit_compare(old, row)


@ROW_PENDING
def test_images_E15_upload_without_filename_502(tmp_path):
    old, row = pair(tmp_path, EDIT, form(), lambda rig: _upload_refused(rig, {"json": {}}))
    old_has(old, "an error after an upload answer with no name", (status(old) or 0) >= 400 and engine_answered(old, "POST", "/upload/image", 200))
    _edit_compare(old, row)


@ROW_PENDING
def test_images_E16_mask_upload_failure_502(tmp_path):
    def setup(rig):
        base._comfy(rig, comfy_exchanges(strict=False, extra=[
            exchange("mask-upload-refused", "POST", "/upload/image", {"status": 500, "json": {"error": "disk full"}}, times=1, required=False)]))
    old, row = pair(tmp_path, EDIT, edit_masked(), setup)
    old_has(old, "a refused upload", status(old) == 502 and engine_answered(old, "POST", "/upload/image", 500))
    _edit_compare(old, row)


@NEEDS_RH
def test_images_E17_mask_headers(tmp_path):
    old, row = pair(tmp_path, EDIT, edit_masked(), edit_ok)
    old_has(old, "mask headers", headers_with(old, "x-image-mask"))
    assert_same(old, row, status, ("mask headers", lambda o: headers_with(o, "x-image-mask")), ("warnings", lambda o: receipt_fields(o, "warnings")))


def test_images_E18_post_composite_mask(tmp_path):
    old, row = pair(tmp_path, EDIT, edit_masked(model="krea2-turbo"), edit_ok)
    old_has(old, "an answer", status(old))
    assert_same(old, row, status, ("mask headers", lambda o: headers_with(o, "x-image-mask")), ("warnings", lambda o: receipt_fields(o, "warnings")),
                ("b64", lambda o: [i.get("b64_json") for i in data_items(o)]))


@ROW_PENDING
def test_images_E19_response_headers(tmp_path):
    old, row = pair(tmp_path, EDIT, edit_masked(size="768X512"), edit_ok)
    old_has(old, "image headers", headers_with(old, "x-image"))
    assert_same(old, row, ("headers", lambda o: headers_with(o, "x-image", "x-azizam", "x-proxy", "x-comfyui")))


@ROW_PENDING
def test_images_E20_round_loop_traffic(tmp_path):
    old, row = pair(tmp_path, EDIT, edit_masked(), lambda rig: base._comfy(rig, comfy_exchanges(
        views=[(base._png((20, 20, 20)), "image/png")], history=history_done(node="13"), running_polls=3, strict=False)))
    old_has(old, "polls", len(sleeps(old)) >= 3)
    assert_same(old, row, status, engine_without_probes, sleeps)


@NEEDS_RH
def test_images_E21_receipt_records_inputs_and_workflow(tmp_path):
    old, row = pair(tmp_path, EDIT, edit_masked(), edit_ok)
    old_has(old, "a receipt", receipt_row(old))
    assert_same(old, row, receipt_summary, ("receipt", lambda o: receipt_fields(
        o, "accepted_inputs", "rejected_inputs", "ignored_inputs", "resolved_workflow", "requested_workflow", "manifest_id", "metadata")),
                origins, asset_files)


# ------------------------------------------------------------------------------------------------ 3. images.variations
def var_form(files=None, **data):
    out = {"data": {"response_format": "b64_json", **data}}
    out["files"] = files if files is not None else {"image": IMG}
    return out


@ROW_PENDING
def test_images_V01_disabled_service_503(tmp_path):
    old, row = pair(tmp_path, VAR, var_form(), disabled)
    old_has(old, "503", status(old) == 503)
    assert_same(old, row, status, failure, engine_without_probes)


@ROW_PENDING
def test_images_V02_invalid_form_400(tmp_path):
    old, row = pair(tmp_path, VAR, {"content": b"nope", "headers": {"content-type": "multipart/form-data; boundary=zz"}}, variation_ok)
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure, engine_without_probes)


@ROW_PENDING
def test_images_V03_missing_image_400(tmp_path):
    old, row = pair(tmp_path, VAR, var_form(files={"other": ("x.txt", b"x", "text/plain")}), variation_ok)
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure, engine_without_probes)


@ROW_PENDING
def test_images_V04_exactly_one_image(tmp_path):
    old, row = pair(tmp_path, VAR, {"data": {"response_format": "b64_json"}, "files": [("image", IMG), ("image", IMG)]}, variation_ok)
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure, engine_without_probes)


@NEEDS_RH
def test_images_V05_delegated_prompt_workflow_and_denoise(tmp_path):
    old, row = pair(tmp_path, VAR, var_form(), variation_ok)
    old_has(old, "an answer", status(old) == 200)
    assert_same(old, row, status, ("prompt bodies", lambda o: engine_bodies(o, "/prompt")), engine_without_probes,
                ("receipt", lambda o: receipt_fields(o, "resolved_workflow", "requested_workflow", "metadata")))


@NEEDS_RH
def test_images_V06_receipt_names_the_delegated_workflow(tmp_path):
    old, row = pair(tmp_path, VAR, var_form(), variation_ok)
    old_has(old, "the metadata key", "variations_delegated_workflow" in json.dumps(receipt_fields(old, "metadata") or {}))
    assert_same(old, row, ("receipt", lambda o: receipt_fields(o, "metadata")))


@ROW_PENDING
def test_images_V07_offset_inpaint_needs_its_workflows(tmp_path):
    old, row = pair(tmp_path, GEN, body(model="flux2-klein-4b-seamless-tile"), ok)
    old_has(old, "an answer", status(old))
    assert_same(old, row, status, failure, engine_without_probes)


@ROW_PENDING
def test_images_V08_offset_inpaint_success_headers(tmp_path):
    old, row = pair(tmp_path, GEN, body(model="flux2-klein-4b-seamless-tile"), ok)
    old_has(old, "the strategy header", header(old, "x-image-seamless-tile-strategy"))
    assert_same(old, row, status, ("headers", lambda o: headers_with(o, "x-image")))


def test_images_V09_tile_composition_error_maps_to_502(tmp_path):
    failed = {"p1": {"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"node_type": "N", "node_id": "7", "exception_message": "gateway tile composition error"}]]}, "outputs": {}}}
    old, row = pair(tmp_path, JOB, {"json_body": {"prompt": "tile", "response_format": "url", **BG}, "unordered_json": True},
                    lambda rig: (base._capture_scheduling(rig), base._comfy(rig, comfy_exchanges(history=failed, strict=False))))
    assert_same(old, row, status)


@NEEDS_RH
def test_images_V10_size_adjustment_on_variations(tmp_path):
    old, row = pair(tmp_path, VAR, var_form(size="768X512"), variation_ok)
    old_has(old, "an answer", status(old) == 200)
    assert_same(old, row, status, ("header", lambda o: header(o, "x-azizam-size-adjusted")), ("receipt", lambda o: receipt_fields(o, "metadata", "warnings")))


# ------------------------------------------------------------------------------------------------ 4. image job route
def job_body(**fields):
    return {"json_body": {"prompt": "a green door", "response_format": "url", **BG, **fields}, "unordered_json": True}


def job_setup(inner=None):
    def setup(rig):
        base._capture_scheduling(rig)
        (inner or (lambda r: base._comfy(r, comfy_exchanges(views=[(base._png((90, 160, 40)), "image/png")], strict=False))))(rig)
    return setup


def _job_id(response):
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return None
    return (data.get("job_id") or data.get("id")) if isinstance(data, dict) else None


def job_flow(request, after=None, run=True):
    """Submit a job, let its executor finish (the old route's captured scheduler, or the row core), then ``after(rig, id)``."""
    def mk(use_row):
        async def go(rig):
            target = (ROW_PREFIX if use_row else "") + JOB
            first = await rig.call("POST", target, label="submit", **request)
            job_id = _job_id(first) if first is not None else None
            if run:
                if not use_row and rig.scheduled:
                    await proxy._run_image_media_job(rig.scheduled[-1])
                else:
                    for _ in range(200):
                        await asyncio.sleep(0)
            if after is not None and job_id:
                await after(rig, job_id)
        return go
    return mk


def job_pair(tmp_path, request, setup, after=None, run=True):
    mk = job_flow(request, after, run)
    return (observe(tmp_path, JOB, mk(False), setup, row=False), observe(tmp_path, JOB, mk(True), setup, row=True))


def job_get(suffix=""):
    async def after(rig, job_id):
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}{suffix}", label=f"GET {suffix or 'job'}")
    return after


def job_everything(rig, job_id):
    async def go():
        for suffix in ("", "/results", "/events", "/content"):
            await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}{suffix}", label=f"GET {suffix or 'job'}")
    return go()


def _job_doc(o, i=1):
    d = doc(o, i) or {}
    # ``intent`` is left out: the core files every workflow run under one intent (delta ``job_row_intent``, see J06).
    return {k: d.get(k) for k in ("status", "workflow", "progress", "error", "retryable", "attempts", "mode") if k in d}


def test_images_J01_body_not_json_400(tmp_path):
    old, row = pair(tmp_path, JOB, raw_json(b"{"), job_setup())
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure)


@ROW_PENDING
def test_images_J02_body_not_object_400(tmp_path):
    old, row = pair(tmp_path, JOB, raw_json(b"[1]"), job_setup())
    old_has(old, "400", status(old) == 400)
    assert_same(old, row, status, failure)


@ROW_PENDING
def test_images_J03_metadata_control_is_checked(tmp_path):
    old, row = job_pair(tmp_path, job_body(metadata="nonsense"), job_setup(), run=False)
    old_has(old, "an answer", status(old))
    assert_same(old, row, status, failure, ("jobs", lambda o: len(media_jobs(o))))


@NEEDS_RH
def test_images_J04_policy_then_admission(tmp_path):
    for outcome in ("policy_refusal", "admission_refusal"):
        old, row = cell(tmp_path / outcome, "ij", outcome)
        old_has(old, "a refusal", (status(old) or 0) >= 400)
        assert_same(old, row, status, failure, ("retry-after", lambda o: header(o, "retry-after")), receipt_summary)


@NEEDS_RH
def test_images_J05_admission_refusal_writes_a_receipt(tmp_path):
    old, row = cell(tmp_path, "ij", "admission_refusal")
    old_has(old, "a rejected receipt", receipt_summary(old))
    assert_same(old, row, receipt_summary, ("resource decision", lambda o: receipt_fields(o, "resource_decision", "metadata")))


def test_images_J06_job_row_intent_workflow_payload(tmp_path):
    """DELTA ``job_row_intent``: the old job row filed its intent as the image intent; the core files every workflow run under
    one intent and the row names the workflow. The workflow and status are unchanged."""
    old, row = job_pair(tmp_path, job_body(), job_setup(), run=False)
    old_has(old, "a job row", media_jobs(old))
    assert [(j["intent"], j.get("workflow"), j["status"]) for j in media_jobs(old)] == [("image.generate", "image.generate.krea2-turbo", "queued")]
    assert [(j["intent"], j.get("workflow"), j["status"]) for j in media_jobs(row)] == [("workflow.run", "image.generate.krea2-turbo", "queued")]


def _job_rows(o):
    return [{k: v for k, v in j.items() if k != "job_id"} for j in media_jobs(o)]


@ROW_PENDING
def test_images_J07_job_metadata(tmp_path):
    old, row = job_pair(tmp_path, job_body(n=3, size="512x512"), job_setup(), run=False)
    old_has(old, "a job row", media_jobs(old))
    assert_same(old, row, ("job rows", _job_rows))


def test_images_J08_job_id_forms(tmp_path):
    def kind(o):
        ids = [j["job_id"] for j in media_jobs(o)]
        return [i.split("_")[0] + ("_idem" if "_idem_" in i else "_seeded" if "_seeded_" in i else "") for i in ids]
    keyed = dict(job_body(), headers={"Idempotency-Key": "k-8"})
    old, row = job_pair(tmp_path / "idem", keyed, job_setup(), run=False)
    old_has(old, "a job id", kind(old))
    assert_same(old, row, ("id form", kind))
    old2, row2 = job_pair(tmp_path / "plain", job_body(), job_setup(), run=False)
    assert_same(old2, row2, ("id form", kind))


@ROW_PENDING
def test_images_J09_replay_returns_the_existing_job(tmp_path):
    old, row = cell(tmp_path, "ij", "replay_same")
    old_has(old, "two answers", len(old.trace["caller"]) >= 2)
    assert_same(old, row, ("statuses", lambda o: [status(o, i) for i in range(len(o.trace["caller"]))]), ("jobs", lambda o: len(media_jobs(o))),
                ("notes", lambda o: o.trace["notes"]))


def test_images_J10_replay_with_a_different_body_conflicts(tmp_path):
    """DELTA ``idempotency_conflict``: the old route gave the first job back for a repeated key whatever the body; the row
    refuses a repeated key with a different body (409) and runs the engine once."""
    old, row = cell(tmp_path, "ij", "replay_conflict")
    old_has(old, "a second 202 for the changed body", [status(old, 0), status(old, 1)] == [202, 202])
    assert [status(row, 0), status(row, 1)] == [202, 409]
    assert failure(row, 1)["code"] == "idempotency_conflict"
    assert len(media_jobs(row)) == 1 and engine_without_probes(row) == engine_without_probes(old)


@ROW_PENDING
def test_images_J11_job_store_failure_500(tmp_path):
    from gateway import media_jobs as store

    def setup(rig):
        job_setup()(rig)

        def boom(*_a, **_k):
            raise RuntimeError("store down")
        rig.set(store, "create", boom)
    old, row = pair(tmp_path, JOB, job_body(), setup)
    old_has(old, "500", status(old) == 500)
    assert_same(old, row, status, failure)


@ROW_PENDING
def test_images_J12_accepted_202_with_links(tmp_path):
    old, row = job_pair(tmp_path, job_body(), job_setup(), run=False)
    old_has(old, "202", status(old) == 202)
    link = lambda o: {"location": (header(o, "location") or "").rsplit("/", 1)[0],
                      "keys": sorted((doc(o) or {}).keys()),
                      "poll": ((doc(o) or {}).get("poll_url") or "").rsplit("/", 1)[0],
                      "content": ((doc(o) or {}).get("content_url") or "").rsplit("/", 2)[0]}
    assert_same(old, row, status, ("links", link))


def test_images_J13_a_queued_event_then_scheduling(tmp_path):
    old, row = job_pair(tmp_path, job_body(), job_setup(), after=job_get("/events"), run=False)
    old_has(old, "events", doc(old, 1))
    # The old scheduler is captured, so its job shows only the queued event; the row runs the job at once, so later events may
    # already be there. The fact is that the first event is the queued one.
    assert_same(old, row, ("first event", lambda o: doc(o, 1)["events"][0]), ("scheduled", lambda o: len(o.trace["notes"])))


@NEEDS_RH
def test_images_J14_refusals_leave_no_job(tmp_path):
    for outcome in ("validation_refusal", "policy_refusal"):
        old, row = cell(tmp_path / outcome, "ij", outcome)
        assert_same(old, row, status, failure, ("jobs", lambda o: len(media_jobs(o))), receipt_summary)


@NEEDS_RH
def test_images_J15_admission_refusal_leaves_no_job(tmp_path):
    old, row = cell(tmp_path, "ij", "admission_refusal")
    assert_same(old, row, status, failure, ("jobs", lambda o: len(media_jobs(o))), receipt_summary)


@ROW_PENDING
def test_images_J16_wait_timeout_keeps_the_job(tmp_path):
    old, row = cell(tmp_path, "ij", "wait_timeout")
    assert_same(old, row, status, ("job states", lambda o: [(j["status"]) for j in media_jobs(o)]), engine_without_probes)


@NEEDS_RH
def test_images_J17_cancel_queued_and_finished(tmp_path):
    for outcome in ("cancel_queued", "cancel_finished"):
        old, row = cell(tmp_path / outcome, "ij", outcome)
        old_has(old, "calls", len(old.trace["caller"]) >= 3)
        assert_same(old, row, ("statuses", lambda o: [status(o, i) for i in range(len(o.trace["caller"]))]),
                    ("job states", lambda o: [j["status"] for j in media_jobs(o)]), ("cancel notes", lambda o: o.trace["notes"]), receipt_summary)


@ROW_PENDING
def test_images_J18_disconnect_does_not_cancel_the_job(tmp_path):
    old, row = cell(tmp_path, "ij", "disconnect")
    assert_same(old, row, ("job states", lambda o: [j["status"] for j in media_jobs(o)]), engine_without_probes)


@NEEDS_RH
def test_images_J19_success_mirrors_assets_and_drops_payload(tmp_path):
    old, row = cell(tmp_path, "ij", "success")
    old_has(old, "a succeeded job", any(j["status"] == "succeeded" for j in media_jobs(old)))
    assert_same(old, row, ("job states", lambda o: [j["status"] for j in media_jobs(o)]), origins, asset_files, engine_without_probes, receipt_summary)


@ROW_PENDING
def test_images_J20_failure_records_error_and_event(tmp_path):
    old, row = cell(tmp_path, "ij", "upstream_failure")
    old_has(old, "a failed job", any(j["status"] == "failed" for j in media_jobs(old)))
    assert_same(old, row, ("job states", lambda o: [(j["status"], j.get("error")) for j in media_jobs(o)]),
                ("polls", lambda o: [status(o, i) for i in range(len(o.trace["caller"]))]), ("job doc", lambda o: _job_doc(o, 1)))


@pytest.mark.parametrize("name,setup", [("execution", EXEC_ERROR), ("unreachable", UNREACHABLE), ("http_500", HTTP_500), ("timeout", timeout)])
@ROW_PENDING
def test_images_J21_failure_copy_per_category(tmp_path, name, setup):
    old, row = job_pair(tmp_path, job_body(), job_setup(setup), after=job_get())
    old_has(old, "a failed job", any(j["status"] == "failed" for j in media_jobs(old)))
    assert_same(old, row, ("job doc", lambda o: _job_doc(o, 1)), ("errors", lambda o: [(j["status"], j.get("error")) for j in media_jobs(o)]))


@ROW_PENDING
def test_images_J22_oom_retries_with_events(tmp_path):
    def inner(rig):
        base._comfy(rig, comfy_exchanges(views=[(base._png((60, 160, 120)), "image/png")], strict=False, extra=[
            exchange("prompt-oom", "POST", "/prompt", {"status": 500, "body": [{"text": "CUDA out of memory. Tried to allocate 2.00 GiB"}]}, times=1, required=False)]))
    old, row = job_pair(tmp_path, job_body(), job_setup(inner), after=job_get("/events"))
    old_has(old, "a retry", any(path == "/free" for _m, path in engine(old)))
    assert_same(old, row, engine_without_probes, sleeps, ("jobs", lambda o: [j["status"] for j in media_jobs(o)]), ("events", lambda o: doc(o, 1)))


def test_images_J23_job_engine_traffic_matches_the_sync_path(tmp_path):
    old, row = job_pair(tmp_path, job_body(), job_setup(), after=job_get())
    sync = observe(tmp_path / "sync", GEN, body(), ok, row=False)
    assert engine_without_probes(row) == engine_without_probes(sync), "the row job's engine traffic differs from the sync path"
    assert_same(old, row, engine_without_probes, sleeps)


def test_images_J24_concurrency_is_bounded(tmp_path):
    def two(use_row):
        async def go(rig):
            target = (ROW_PREFIX if use_row else "") + JOB
            ids = [_job_id(await rig.call("POST", target, label=f"submit {i}", **job_body(prompt=f"door {i}"))) for i in range(2)]
            if not use_row:
                for scheduled in list(rig.scheduled):
                    await proxy._run_image_media_job(scheduled)
            else:
                for _ in range(300):
                    await asyncio.sleep(0)
        return go
    old = observe(tmp_path, JOB, two(False), job_setup(lambda r: ok(r, 2)), row=False)
    row = observe(tmp_path, JOB, two(True), job_setup(lambda r: ok(r, 2)), row=True)
    assert_same(old, row, ("job states", lambda o: sorted(j["status"] for j in media_jobs(o))), engine_without_probes)


@pytest.mark.parametrize("suffix", ["", "/results", "/events"])
def test_images_J25_J27_read_routes(tmp_path, suffix):
    old, row = job_pair(tmp_path, job_body(), job_setup(), after=job_get(suffix))
    old_has(old, "an answer", status(old, 1))
    assert_same(old, row, ("status", lambda o: status(o, 1)), ("keys", lambda o: sorted((doc(o, 1) or {}).keys()) if isinstance(doc(o, 1), dict) else type(doc(o, 1)).__name__))


def test_images_J25_unknown_job_404(tmp_path):
    def req(rig):
        async def go(r):
            return await r.call("GET", "/gateway/v1/media/jobs/nope", label="unknown job")
        return go
    old = observe(tmp_path, JOB, req(None), job_setup(), row=False)
    row = observe(tmp_path, JOB, req(None), job_setup(), row=True)
    assert_same(old, row, status, failure)


def test_images_J28_cancel_route_answers(tmp_path):
    async def after(rig, job_id):
        await rig.call("POST", f"/gateway/v1/media/jobs/{job_id}/cancel", label="cancel the finished job", json_body={})
        await rig.call("POST", "/gateway/v1/media/jobs/nope/cancel", label="cancel unknown", json_body={})
        await rig.call("POST", f"/gateway/v1/media/jobs/{job_id}/cancel", label="cancel with a bad body", json_body=[1])
    old, row = job_pair(tmp_path, job_body(), job_setup(), after=after)
    assert_same(old, row, ("statuses", lambda o: [status(o, i) for i in range(len(o.trace["caller"]))]), ("failures", lambda o: [failure(o, i) for i in range(1, len(o.trace["caller"]))]))


@ROW_PENDING
def test_images_J29_content_states(tmp_path):
    async def before_and_after(rig, job_id):
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/content", label="content")
    old, row = job_pair(tmp_path / "queued", job_body(), job_setup(), after=before_and_after, run=False)
    assert_same(old, row, ("statuses", lambda o: [status(o, i) for i in range(len(o.trace["caller"]))]), ("not ready", lambda o: failure(o, 1)))
    old2, row2 = job_pair(tmp_path / "failed", job_body(), job_setup(EXEC_ERROR), after=before_and_after)
    assert_same(old2, row2, ("statuses", lambda o: [status(o, i) for i in range(len(o.trace["caller"]))]), ("content", lambda o: failure(o, 1)))


@ROW_PENDING
def test_images_J30_content_shape(tmp_path):
    old, row = job_pair(tmp_path, job_body(), job_setup(), after=job_get("/content"))
    old_has(old, "content", data_items(old, 1))
    assert_same(old, row, ("status", lambda o: status(o, 1)), ("item keys", lambda o: item_keys(o, 1)), ("top keys", lambda o: top_keys(o, 1)))


def test_images_J31_progress_when_the_job_starts(tmp_path):
    old, row = job_pair(tmp_path, job_body(), job_setup(), after=job_get())
    assert_same(old, row, ("progress", lambda o: (doc(o, 1) or {}).get("progress")))


# ------------------------------------------------------------------------------------------------ 5. restart and recovery
def _restart(held_setup, mutate=None):
    """Submit a job whose execution is held, restart (run the startup recovery), then read the job."""
    def mk(use_row):
        async def go(rig):
            target = (ROW_PREFIX if use_row else "") + JOB
            if use_row:
                rig.set(proxy, "_workflow_run_semaphore", asyncio.Semaphore(0))
            first = await rig.call("POST", target, label="submit", **job_body())
            job_id = _job_id(first)
            if mutate is not None:
                mutate(rig, job_id)
            proxy._recover_media_jobs()
            for _ in range(100):
                await asyncio.sleep(0)
            if not use_row and rig.scheduled:
                for scheduled in rig.scheduled[1:]:
                    await proxy._run_image_media_job(scheduled)
            if job_id:
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="job after restart")
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="events after restart")
        return go
    return mk


def restart_pair(tmp_path, mutate=None, setup=None):
    mk = _restart(None, mutate)
    setup = setup or job_setup()
    return (observe(tmp_path, JOB, mk(False), setup, row=False), observe(tmp_path, JOB, mk(True), setup, row=True))


def _states(o):
    return [(j["status"], j.get("error")) for j in media_jobs(o)]


@ROW_PENDING
def test_images_R01_startup_scan_resumes_or_fails_the_job(tmp_path):
    old, row = restart_pair(tmp_path)
    old_has(old, "a job", media_jobs(old))
    assert_same(old, row, ("job states", _states), ("polls", lambda o: _job_doc(o, 1)))


def test_images_R02_payload_missing_after_restart(tmp_path):
    def mutate(rig, job_id):
        proxy._delete_image_media_job_payload(job_id)
    old, row = restart_pair(tmp_path, mutate)
    assert_same(old, row, ("job states", _states))


@ROW_PENDING
def test_images_R03_recovery_turned_off(tmp_path):
    def setup(rig):
        job_setup()(rig)
        rig.monkeypatch.setitem(proxy.runtime().derived, "JOB_RECOVERY_ENABLED", False)
    old, row = restart_pair(tmp_path, setup=setup)
    assert_same(old, row, ("job states", _states))


def test_images_R04_restart_during_cancellation(tmp_path):
    def mutate(rig, job_id):
        proxy.media_jobs.request_cancel(job_id, path=proxy.media_jobs.DB_PATH) if hasattr(proxy.media_jobs, "request_cancel") else None
    old, row = restart_pair(tmp_path, mutate)
    assert_same(old, row, ("job states", _states))


@ROW_PENDING
def test_images_R05_restart_during_the_run(tmp_path):
    def mutate(rig, job_id):
        proxy.media_jobs.mark_running(job_id, path=proxy.media_jobs.DB_PATH) if hasattr(proxy.media_jobs, "mark_running") else None
    old, row = restart_pair(tmp_path, mutate)
    assert_same(old, row, ("job states", _states))


def test_images_R06_crash_mid_run_copy(tmp_path):
    """DELTA ``executor_crash_text``: a worker that dies with an unexpected error. The old job stored "Image media job executor
    error: <type>" with HTTP 502 and retried; the core stores its fixed sentence for an unexpected failure and does not
    retry, because only an adapter's named retry reason retries."""
    from gateway import node_graph_runner

    def setup(rig):
        job_setup()(rig)

        async def crash(*_a, **_k):
            raise RuntimeError("worker died")
        rig.set(proxy, "proxy_image_request", crash)
        rig.set(node_graph_runner.NodeGraphRunner, "_rounds", crash)
    old, row = job_pair(tmp_path, job_body(), setup, after=job_get())
    old_has(old, "the old executor text", _states(old) == [("failed", "Image media job executor error: RuntimeError")])
    assert [s for s, _ in _states(row)] == ["failed"] and "worker died" not in str(_states(row))
    assert _job_doc(row, 1)["error"] == "The job failed."
    assert len(engine_bodies(row, "/prompt")) == 0


def test_images_R07_restart_closes_open_receipts(tmp_path):
    """Allowed delta ``restart_closes_receipt``: the row's receipt is closed by the recovery of the run; the old
    route left it for ``close_orphan``. The test asserts the receipt is no longer open."""
    old, row = restart_pair(tmp_path)
    assert receipts(row) and all(r["status"] != "open" for r in receipts(row)), f"a receipt stayed open on the row path: {receipt_summary(row)}"


# ------------------------------------------------------------------------------------------------ 6. failure texts
@ROW_PENDING
def test_images_F01_engine_unreachable(tmp_path):
    old, row = pair(tmp_path, GEN, body(), UNREACHABLE)
    old_has(old, "a 5xx", (status(old) or 0) >= 500)
    assert_same(old, row, status, failure)


@ROW_PENDING
def test_images_F02_prompt_http_500(tmp_path):
    old, row = pair(tmp_path, GEN, body(), HTTP_500)
    old_has(old, "a 5xx", (status(old) or 0) >= 500)
    assert_same(old, row, status, failure)


def test_images_F03_prompt_without_id(tmp_path):
    old, row = pair(tmp_path, GEN, body(), NO_PROMPT_ID)
    old_has(old, "a 5xx", (status(old) or 0) >= 500)
    assert_same(old, row, status, failure)


def test_images_F04_execution_error(tmp_path):
    old, row = pair(tmp_path, GEN, body(), EXEC_ERROR)
    old_has(old, "502", status(old) == 502)
    assert_same(old, row, status, failure)


@ROW_PENDING
def test_images_F05_timeout(tmp_path):
    old, row = pair(tmp_path, GEN, body(), timeout)
    old_has(old, "504", status(old) == 504)
    assert_same(old, row, status, failure, sleeps)


@ROW_PENDING
def test_images_F06_out_of_memory_stays_out_of_memory(tmp_path):
    def setup(rig):
        base._comfy(rig, comfy_exchanges(views=[(base._png(), "image/png")], strict=False, extra=[
            exchange("oom-always", "POST", "/prompt", {"status": 500, "body": [{"text": "CUDA out of memory. Tried to allocate 2.00 GiB"}]}, times=20, required=False)]))
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "an answer", status(old))
    assert_same(old, row, status, failure, engine_without_probes)


def test_images_F07_view_missing(tmp_path):
    def setup(rig):
        base._comfy(rig, comfy_exchanges(views=[(b"", "text/plain")], strict=False, extra=[
            exchange("view-missing", "GET", "/view", {"status": 404, "json": {"error": "not found"}}, times=1, required=False)]))
    old, row = pair(tmp_path, GEN, body(), setup)
    old_has(old, "a 404 on the download", engine_answered(old, "GET", "/view", 404) and (status(old) or 0) >= 400)
    assert_same(old, row, status, failure)


# ------------------------------------------------------------------------------------------------ 7. design review answers (S7 section 6)
import base64
import io

from PIL import Image


def _pixels(o, i=0):
    """The delivered pictures of a ``b64_json`` answer, decoded: (size, mode, every pixel)."""
    out = []
    for item in data_items(o, i):
        if "b64_json" in item:
            image = Image.open(io.BytesIO(base64.b64decode(item["b64_json"])))
            out.append((image.size, image.mode, list(image.convert("RGBA").getdata())))
    return out


def _half_mask():
    image = Image.new("L", (8, 8), 0)             # the golden input is 8 x 8
    for x in range(4, 8):
        for y in range(8):
            image.putpixel((x, y), 255)
    out = io.BytesIO()
    image.save(out, "PNG")
    return ("mask.png", out.getvalue(), "image/png")


@ROW_PENDING
def test_s7_edit_post_composite_mask_changes_delivered_and_stored_pixels(tmp_path):
    """Section 6.5: masking runs in the adapter before the bundle commits, so the delivered pictures and the stored
    Library file are the masked bytes, and equal to what the old route delivers and stores."""
    request = form(files={"image": IMG, "mask": _half_mask()})

    def no_source_alpha(rig):
        # No shipped edit workflow lacks a mask input, so the rig turns the manifest's mask facts off: the same data
        # question the route asks (``_manifest_uses_source_alpha_mask``, ``_manifest_route_managed_mask_input``).
        edit_ok(rig)
        rig.set(proxy, "_manifest_uses_source_alpha_mask", lambda manifest: False)
        rig.set(proxy, "_manifest_route_managed_mask_input", lambda manifest: None)
    old, row = pair(tmp_path, EDIT, request, no_source_alpha)
    old_has(old, "post-composite", header(old, "x-image-mask") == "post-composite")
    old_has(old, "a picture", _pixels(old))
    engine_output = Image.open(io.BytesIO(base._png((20, 20, 20)))).convert("RGBA").getdata()
    old_has(old, "pixels that the mask changed", set(_pixels(old)[0][2]) != set(engine_output))
    assert_same(old, row, status, ("delivered pixels", _pixels), ("stored files", asset_files), ("mask headers", lambda o: headers_with(o, "x-image-mask")))


@ROW_PENDING
def test_s7_background_fallback_and_offset_inpaint_preserve_composition(tmp_path):
    transparent = {"json_body": {"prompt": "a red fox", "response_format": "b64_json", "background": "transparent"}}
    old, row = pair(tmp_path / "cutout", GEN, transparent, ok)
    old_has(old, "a picture", _pixels(old))
    assert_same(old, row, status, ("delivered pixels", _pixels), ("stored files", asset_files),
                ("background headers", lambda o: headers_with(o, "x-image-background")))
    old2, row2 = pair(tmp_path / "tile", GEN, body(model="flux2-klein-4b-seamless-tile"), ok)
    old_has(old2, "an answer", status(old2))
    assert_same(old2, row2, status, ("delivered pixels", _pixels), ("stored files", asset_files),
                ("tile headers", lambda o: headers_with(o, "x-image-seam")), failure)


@ROW_PENDING
def test_s7_audio_and_video_poll_schedules_are_not_the_image_round_schedule(tmp_path):
    """Section 6.8: each operation keeps its own cadence. The audio route sleeps 2 s per poll, the video media job 5 s,
    and neither is the image round's 0.05 s doubling to 0.5 s."""
    image_round = {0.05, 0.1, 0.2, 0.4, 0.5}

    def audio(rig):
        base._audio_comfy(rig, running_polls=3, strict=False)
    a_old, a_row = pair(tmp_path / "audio", "/v1/audio/generate", {"json_body": {"prompt": "a door creaks", "model": "tangoflux", "duration": 4}}, audio)
    old_has(a_old, "audio poll sleeps", sleeps(a_old))
    assert set(sleeps(a_old)).isdisjoint(image_round), "the old audio route uses the image schedule?"
    assert_same(a_old, a_row, status, sleeps, engine_without_probes)

    v_old, v_row = cell(tmp_path / "video", "vc", "success")
    old_has(v_old, "video poll sleeps", sleeps(v_old))
    assert set(sleeps(v_old)).isdisjoint(image_round)
    assert_same(v_old, v_row, sleeps, engine_without_probes)


@pytest.mark.xfail(strict=True, reason="the failed-job message: the old job stored a fixed sentence while the sync caller got the engine words (needs two texts, integrator decision); delays, limit and events match")
def test_s7_image_job_retry_preserves_delay_events_and_attempt_limit(tmp_path):
    """Section 6.6: a retryable failure is retried on the same job up to the attempt limit, waiting 5 s then doubling
    to at most 30 s, and appends the retry event each time."""
    def setup(rig):
        job_setup(lambda r: base._comfy(r, comfy_exchanges(views=[(base._png(), "image/png")], strict=False, extra=[
            exchange("no-prompt-id-always", "POST", "/prompt", {"status": 200, "json": {}}, times=20, required=False)])))(rig)

    def mk(use_row):
        async def go(rig):
            target = (ROW_PREFIX if use_row else "") + JOB
            first = await rig.call("POST", target, label="submit", **job_body())
            job_id = _job_id(first)
            if use_row:
                for _ in range(2000):
                    await asyncio.sleep(0)
            else:
                ran = 0
                while ran < len(rig.scheduled):
                    await proxy._run_image_media_job(rig.scheduled[ran])
                    ran += 1
            await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="job")
            await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="events")
        return go
    old = observe(tmp_path, JOB, mk(False), setup, row=False)
    row = observe(tmp_path, JOB, mk(True), setup, row=True)
    old_has(old, "a retry wait", 5.0 in sleeps(old))
    assert_same(old, row, ("job states", _states), ("events", lambda o: doc(o, 2)), sleeps, ("prompts", lambda o: len(engine_bodies(o, "/prompt"))))
