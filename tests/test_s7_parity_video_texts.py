"""Every refusal text the video routes send, pinned exactly from docs/design/c2d-s7-parity/video-texts.md.

The table is the oracle: each row is one case with a status, type, param, code and message. The cases below build the
request that reaches each row's branch (a source the caller owns, a configured model, a held engine). A row with no case,
or a case with no row, fails ``test_the_table_and_the_cases_name_the_same_refusals``. Missing-source 404 is its own case.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

import proxy
from tests.support import video_rig
from tests.support.adapter_differential_rig import GatewayRig
from tests.support.config_snapshot import publish_section
from tests.support.video_rig import JOB_PREFIX, MODEL, drive, run

TABLE = Path(__file__).resolve().parents[1] / "docs" / "design" / "c2d-s7-parity" / "video-texts.md"
NS = "com.azizam.workflow"


def table() -> dict[str, dict]:
    rows = {}
    for line in TABLE.read_text().splitlines():
        if not line.startswith("| ") or line.startswith("| case") or line.startswith("|---"):
            continue
        case, status, kind, param, code, message = (cell.strip() for cell in line.strip().strip("|").split(" | ", 5))
        cleaned = lambda cell: None if cell == "-" else cell.strip("`")
        rows[case] = {"status": int(status), "type": cleaned(kind), "param": cleaned(param), "code": cleaned(code),
                      "message": message.replace("\\|", "|")}
    return rows


ROWS = table()
POST = "POST"
J = {"Content-Type": "application/json"}

# case -> (needs, method, path, request keywords). ``{vid}`` is a video the caller made; ``needs`` lists the setup.
CASES: dict[str, tuple] = {
    "invalid_json": ((), POST, "/v1/videos", {"content": b"{", "headers": J}),
    "prompt_number": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": 7}}),
    "prompt_missing": ((), POST, "/v1/videos", {"json_body": {"model": MODEL}}),
    "prompt_empty": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": ""}}),
    "seconds_low": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x", "seconds": "0.1"}}),
    "seconds_high": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x", "seconds": "9999"}}),
    "seconds_text": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x", "seconds": "abc"}}),
    "seconds_negative": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x", "seconds": "-3"}}),
    "model_unknown": ((), POST, "/v1/videos", {"json_body": {"model": "nope-model", "prompt": "x"}}),
    "i2v_without_input_reference": ((), POST, "/v1/videos", {"json_body": {"model": "ltx-2.5-i2v", "prompt": "x"}}),
    "h3_i2v_without_input_reference": ((), POST, "/v1/videos", {"json_body": {"model": "minimax-h3-i2v", "prompt": "x"}}),
    "unknown_field": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x", "bogus_field": 1}}),
    "shots_on_t2v": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x", "shots": [{"prompt": "a"}]}}),
    "seconds_past_native": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x", "seconds": "60"}}),
    "scail_without_source": ((), POST, "/v1/videos", {"json_body": {"model": "scail-2-replace", "prompt": "x"}}),
    "audio_text": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x", "audio": "maybe"}}),
    "extensions_text": ((), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x", "extensions": "x"}}),
    "multipart_bad": ((), POST, "/v1/videos", {"content": b"--x\r\nbroken",
                                                "headers": {"content-type": "multipart/form-data; boundary=x"}}),
    "input_reference_bad": ((), POST, "/v1/videos", {"files": [
        ("prompt", (None, "x")), ("model", (None, "ltx-2.5-i2v")), ("input_reference", ("a.png", b"notanimage", "image/png"))]}),
    "status_invalid": ((), "GET", "/v1/videos/nonsense", {}),
    "status_unknown": ((), "GET", "/v1/videos/video_nosuch", {}),
    "content_invalid": ((), "GET", "/v1/videos/nonsense/content", {}),
    "content_unknown": ((), "GET", "/v1/videos/video_nosuch/content", {}),
    "delete_invalid": ((), "DELETE", "/v1/videos/nonsense", {}),
    "delete_unknown": ((), "DELETE", "/v1/videos/video_nosuch", {}),
    "list_limit_zero": ((), "GET", "/v1/videos", {"params": {"limit": "0"}}),
    "list_limit_text": ((), "GET", "/v1/videos", {"params": {"limit": "many"}}),
    "list_limit_high": ((), "GET", "/v1/videos", {"params": {"limit": "101"}}),
    "list_order": ((), "GET", "/v1/videos", {"params": {"order": "sideways"}}),
    "list_after_malformed": ((), "GET", "/v1/videos", {"params": {"after": "not-a-video"}}),
    "list_after_unknown": ((), "GET", "/v1/videos", {"params": {"after": "video_evicted"}}),
    "edit_no_prompt": ((), POST, "/v1/videos/edits", {"json_body": {"video": {"id": "video_x"}}}),
    "edit_no_source": ((), POST, "/v1/videos/edits", {"json_body": {"prompt": "x"}}),
    "content_not_ready": (("video",), "GET", "/v1/videos/{vid}/content", {}),
    "content_view_error": (("video", "completed", "view_error"), "GET", "/v1/videos/{vid}/content", {}),
    "extend_seconds_text": (("video", "completed"), POST, "/v1/videos/extensions",
                            {"json_body": {"video": {"id": "{vid}"}, "prompt": "p", "seconds": "abc"}}),
    "extend_no_prompt": (("video", "completed"), POST, "/v1/videos/extensions", {"json_body": {"video": {"id": "{vid}"}}}),
    "extend_no_source": ((), POST, "/v1/videos/extensions", {"json_body": {"prompt": "p"}}),
    "extend_unknown_source": ((), POST, "/v1/videos/extensions", {"json_body": {"video": {"id": "video_nosuch"}, "prompt": "p"}}),
    "extend_bad_shots": (("video", "completed"), POST, "/v1/videos/extensions",
                         {"json_body": {"video": {"id": "{vid}"}, "prompt": "p", "shots": "x"}}),
    "extend_unknown_option": (("video", "completed"), POST, "/v1/videos/extensions",
                              {"json_body": {"video": {"id": "{vid}"}, "prompt": "p", "extensions": {NS: {"zzz": 1}}}}),
    "disabled": (("disabled",), POST, "/v1/videos", {"json_body": {"model": MODEL, "prompt": "x"}}),
    # --- reach the branch with a source the caller owns and a configured model (V5d recheck item 7) ---
    "remix_unconfigured": (("video", "completed", "remix_off"), POST, "/v1/videos/{vid}/remix", {"json_body": {"prompt": "new dusk"}}),
    "edit_unconfigured": (("video", "completed", "remix_off"), POST, "/v1/videos/edits",
                          {"json_body": {"prompt": "new dusk", "video": {"id": "{vid}"}}}),
    "remix_source_video_extension": (("video", "completed", "remix_on"), POST, "/v1/videos/{vid}/remix",
                                     {"json_body": {"prompt": "x", "extensions": {NS: {"source_video": "x"}}}}),
    "create_unknown_extension_input": ((), POST, "/v1/videos",
                                       {"json_body": {"model": MODEL, "prompt": "x", "extensions": {NS: {"nope": 1}}}}),
    "long_video_not_supported_shots": (("no_chain",), POST, "/v1/videos",
                                       {"json_body": {"model": MODEL, "prompt": "x", "shots": [{"prompt": "a", "seconds": 2}]}}),
    "invalid_shots_not_json": ((), POST, "/v1/videos", {"json_body": {"model": "ltx-2.5-long", "prompt": "x", "shots": "not json"}}),
    "invalid_shots_empty": ((), POST, "/v1/videos", {"json_body": {"model": "ltx-2.5-long", "prompt": "x", "shots": []}}),
    "invalid_shots_not_object": ((), POST, "/v1/videos", {"json_body": {"model": "ltx-2.5-long", "prompt": "x", "shots": [5]}}),
    "invalid_shots_no_prompt": ((), POST, "/v1/videos",
                                {"json_body": {"model": "ltx-2.5-long", "prompt": "x", "shots": [{"seconds": 2}]}}),
    "invalid_shots_no_seconds": ((), POST, "/v1/videos",
                                 {"json_body": {"model": "ltx-2.5-long", "prompt": "x", "shots": [{"prompt": "a"}]}}),
    "long_video_unknown_option": ((), POST, "/v1/videos",
                                  {"json_body": {"model": "ltx-2.5-long", "prompt": "x", "seconds": "60",
                                                 "extensions": {NS: {"zzz": 1}}}}),
    "content_no_video_output": (("video", "completed", "no_video_file"), "GET", "/v1/videos/{vid}/content", {}),
    # --- a source that does not exist: its own case, never a stand-in for the 501 ---
    "remix_missing_source": ((), POST, "/v1/videos/video_nosuch/remix", {"json_body": {"prompt": "x"}}),
    "edit_missing_source": ((), POST, "/v1/videos/edits", {"json_body": {"prompt": "x", "video": {"id": "video_nosuch"}}}),
}
# create_engine_unavailable needs a rig whose engine is down; it has its own test below.
OWN_TEST = {"create_engine_unavailable"}


def fill(value, vid):
    if isinstance(value, str):
        return value.replace("{vid}", vid)
    if isinstance(value, dict):
        return {k: fill(v, vid) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, vid) for v in value]
    return value


async def make_video(w) -> str:
    """A video the caller owns, on either kind of store (a job, or an old record)."""
    response = await w.call("POST", "/v1/videos", json_body={"model": MODEL, "prompt": "waves"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def finish(w, vid):
    if vid.startswith("video_" + JOB_PREFIX.removeprefix("video_")):
        await drive(w, vid, "completed")
    else:
        w.engine.states[vid.removeprefix("video_")] = "completed"


def test_the_table_and_the_cases_name_the_same_refusals():
    assert set(ROWS) == set(CASES) | OWN_TEST, (sorted(set(ROWS) ^ (set(CASES) | OWN_TEST)))


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_refusal_is_exactly_the_row(tmp_path, case):
    needs, method, path, kwargs = CASES[case]
    want = ROWS[case]
    seen = {}

    async def exercise(w):
        vid = await make_video(w) if "video" in needs else ""
        if "completed" in needs:
            await finish(w, vid)
        if "remix_off" in needs or "remix_on" in needs:
            publish_section(w.rig.monkeypatch, "videos", {**proxy.config["videos"],
                            "remix_workflow": None if "remix_off" in needs else "video-ltx-2.5-t2v"})
        if "no_chain" in needs:
            workflows = copy.deepcopy(proxy.config["videos"]["workflows"])
            for entry in workflows.values():
                entry.pop("chain_workflow", None)
            publish_section(w.rig.monkeypatch, "videos", {**proxy.config["videos"], "workflows": workflows})
        if "view_error" in needs:
            w.engine.view_error = True
        if "no_video_file" in needs:
            original = video_rig.history_done
            w.rig.monkeypatch.setattr(video_rig, "history_done", lambda kind, name, **k: original(kind, "clip.png", **k))
        if "disabled" in needs:
            w.rig.set(proxy, "COMFY_ENABLED", False)
        response = await asyncio.wait_for(w.call(method, fill(path, vid), **fill(kwargs, vid)), timeout=20)
        seen["status"], seen["body"] = response.status_code, response.json()

    run(tmp_path, exercise)
    error = seen["body"].get("error") or {}
    got = {"status": seen["status"], "type": error.get("type"), "param": error.get("param"), "code": error.get("code"),
           "message": error.get("message")}
    if case == "content_not_ready":      # the row names the queued wording the old reader sent
        got["message"] = got["message"]
    assert got == want, case


def test_create_engine_unavailable_is_exactly_the_row(tmp_path):
    from tests.test_adapter_differential import _comfy_down
    want = ROWS["create_engine_unavailable"]
    with GatewayRig("texts-engine-down", tmp_path) as rig:
        _comfy_down(rig)

        async def go():
            return await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": MODEL})
        response = asyncio.run(go())
    error = response.json()["error"]
    assert {"status": response.status_code, "type": error.get("type"), "param": error.get("param"),
            "code": error.get("code"), "message": error.get("message")} == want
