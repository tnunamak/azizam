"""A job compiles from the definitions it pinned at accept, not from the live workflow files (S2.2).

The audio job route is the first user. The rig is the adapter-differential one: the real app, a fake ComfyUI engine and
a temporary jobs database. Every test names a way the live files can differ from the pin, and checks the prompt the
engine really received.

The goldens with the flag on are in tests/test_adapter_differential.py (``test_audio_generate_job_compiled_from_pin``
plays the same golden as the flag-off run).
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import proxy
from gateway import media_jobs, pinned_definitions
from gateway.definition_store import DefinitionUnavailable
from gateway.job_envelope import build_job_envelope
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, history_done, scenario
from tests.support.config_snapshot import publish_section
from tests.test_adapter_differential import _wav

COMFY_HOST = "comfy.fk.test"
CAPABILITY = proxy._AUDIO_GRAPH_CAPABILITY


def set_flag(rig: GatewayRig, on: bool) -> None:
    publish_section(rig.monkeypatch, "workflow_execution", {"compile_from_pin": {CAPABILITY: on}})


@pytest.fixture
def rig(tmp_path: Path):
    with GatewayRig("compile-from-pin", tmp_path) as made:
        made.set(proxy, "_schedule_audio_media_job", lambda job_id: made.scheduled.append(job_id) or True)
        fake = made.engine("comfy", scenario("comfyui", "compile-from-pin", comfy_exchanges(
            history=history_done("audio", "tone_00001_.wav"), views=[(_wav(), "audio/wav")], strict=False)),
            host=COMFY_HOST)
        made.set(proxy, "COMFY_HOST", fake.base_url)
        yield made


async def accept(rig: GatewayRig, prompt: str = "a door creaks", **request: Any) -> str:
    made = await rig.call("POST", "/gateway/v1/media/jobs", label="submit the audio job", unordered_json=True,
                          json_body={"intent": "audio.generate",
                                     "request": {"prompt": prompt, "model": "tangoflux", "duration": 4, **request}})
    assert made.status_code == 202, made.text
    return made.json()["job_id"]


def job(job_id: str) -> dict[str, Any]:
    found = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
    assert found is not None
    return found


def prompts(rig: GatewayRig) -> list[dict[str, Any]]:
    """The graphs the engine received, in order."""
    return [json.loads(entry["body"]["text"])["prompt"] for entry in rig.recorder.engine if entry["path"] == "/prompt"]


def forbid_live_reads(rig: GatewayRig) -> None:
    """After accept the workflow definitions are gone: any read of the registry or a workflow file raises."""
    def gone(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the run read a live workflow definition")

    rig.set(proxy, "_load_audio_workflow_manifest", gone)
    rig.set(proxy, "_load_audio_workflow_file", gone)
    rig.set(proxy, "_resolve_workflow_artifact_path", gone)


def run(coroutine) -> Any:
    return asyncio.run(coroutine)


# ---------------------------------------------------------------------------------------------- crash

@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "after-restart"])
def test_a_job_runs_from_its_pin_when_the_workflow_definitions_are_gone(rig: GatewayRig, restart: bool):
    set_flag(rig, True)

    async def scenario_() -> str:
        job_id = await accept(rig)
        forbid_live_reads(rig)
        if restart:
            # The gateway restarts after accept: the executor is scheduled again from the database alone.
            rig.scheduled.clear()
            assert proxy._resume_pending_audio_media_jobs() == 1
        assert rig.scheduled == [job_id]
        await proxy._run_audio_media_job(job_id)
        return job_id

    job_id = run(scenario_())
    assert job(job_id)["status"] == media_jobs.STATUS_SUCCEEDED, job(job_id).get("error")
    (sent,) = prompts(rig)
    template = json.loads((Path(proxy.__file__).parent / "workflows/audio/tangoflux.json").read_text())
    assert set(sent) == set(template)
    assert sent["2"]["inputs"]["prompt"] == "a door creaks" and sent["2"]["inputs"]["duration"] == 4
    assert sent["1"] == template["1"], "the unbound nodes are the pinned template's"


def test_without_the_flag_a_job_reads_the_live_definitions(rig: GatewayRig):
    """Control for the crash test: with the flag off the same loss of the files fails the job."""
    set_flag(rig, False)

    async def scenario_() -> str:
        job_id = await accept(rig)
        forbid_live_reads(rig)
        await proxy._run_audio_media_job(job_id)
        return job_id

    job_id = run(scenario_())
    assert job(job_id)["status"] == media_jobs.STATUS_FAILED
    assert prompts(rig) == []


# ---------------------------------------------------------------------------------------------- pin

def test_two_jobs_accepted_around_an_edit_each_use_their_own_pin(rig: GatewayRig, tmp_path: Path):
    set_flag(rig, True)
    manifest = proxy._load_audio_workflow_manifest("tangoflux")
    template = json.loads((Path(proxy.__file__).parent / "workflows/audio/tangoflux.json").read_text())
    graph_file = tmp_path / "graph.json"
    state: dict[str, Any] = {"manifest": manifest}

    def edit(steps: int, threshold: float) -> None:
        """The owner edits the manifest (a default) and the graph file (an unbound constant)."""
        edited = copy.deepcopy(manifest)
        edited["inputs"]["steps"]["default"] = steps
        state["manifest"] = edited
        graph = copy.deepcopy(template)
        graph["1"]["inputs"]["rel_l1_thresh"] = threshold
        graph_file.write_text(json.dumps(graph))

    rig.set(proxy, "_load_audio_workflow_manifest", lambda _name: copy.deepcopy(state["manifest"]))
    rig.set(proxy, "_resolve_workflow_artifact_path", lambda _record, _artifact: graph_file)

    async def scenario_() -> tuple[str, str]:
        edit(steps=20, threshold=0.25)
        before = await accept(rig, "before the edit")
        edit(steps=30, threshold=0.5)
        after = await accept(rig, "after the edit")
        # The later job runs first, so a pin that followed "the latest" would show on the earlier one.
        await proxy._run_audio_media_job(after)
        await proxy._run_audio_media_job(before)
        return before, after

    before, after = run(scenario_())
    assert job(before)["status"] == job(after)["status"] == media_jobs.STATUS_SUCCEEDED
    sent_after, sent_before = prompts(rig)
    assert (sent_before["2"]["inputs"]["steps"], sent_before["1"]["inputs"]["rel_l1_thresh"]) == (20, 0.25)
    assert (sent_after["2"]["inputs"]["steps"], sent_after["1"]["inputs"]["rel_l1_thresh"]) == (30, 0.5)
    assert sent_before["2"]["inputs"]["prompt"] == "before the edit"


# ---------------------------------------------------------------------------------------------- no fallback

def drop_envelope(job_id: str) -> None:
    with sqlite3.connect(media_jobs.DB_PATH) as conn:
        conn.execute("DELETE FROM job_envelope WHERE job_id = ?", (job_id,))


def drop_blobs(job_id: str) -> None:
    with sqlite3.connect(media_jobs.DB_PATH) as conn:
        conn.execute("DELETE FROM definition_blob")


@pytest.mark.parametrize("damage", [drop_envelope, drop_blobs], ids=["no-envelope", "no-blob"])
def test_a_job_whose_pin_is_gone_fails_and_never_reads_the_live_file(rig: GatewayRig, damage):
    set_flag(rig, True)

    async def scenario_() -> str:
        job_id = await accept(rig)
        damage(job_id)
        await proxy._run_audio_media_job(job_id)
        return job_id

    job_id = run(scenario_())
    failed = job(job_id)
    assert failed["status"] == media_jobs.STATUS_FAILED
    assert failed["error_code"] == "definition_unavailable" and failed["retryable"] is False
    assert prompts(rig) == [], "no request reached the engine"


def test_a_route_that_compiles_from_the_pin_refuses_a_job_it_cannot_pin(rig: GatewayRig):
    rig.set(proxy, "_manifest_job_envelope", lambda *_a, **_k: None)

    async def submit(on: bool) -> int:
        set_flag(rig, on)
        made = await rig.call("POST", "/gateway/v1/media/jobs", label=f"submit with the flag {on}", unordered_json=True,
                              json_body={"intent": "audio.generate",
                                         "request": {"prompt": f"p{on}", "model": "tangoflux", "duration": 4}})
        return made.status_code

    async def both() -> tuple[int, int]:
        return await submit(False), await submit(True)

    assert run(both()) == (202, 503), "with the flag off a job without a pin is accepted as it always was"


# ---------------------------------------------------------------------------------------------- the flag

def test_the_flag_is_read_from_the_published_config_on_each_call(monkeypatch: pytest.MonkeyPatch):
    publish_section(monkeypatch, "workflow_execution", {"compile_from_pin": {CAPABILITY: True}})
    assert proxy._compile_from_pin(CAPABILITY) is True
    publish_section(monkeypatch, "workflow_execution", {"compile_from_pin": {CAPABILITY: False}})
    assert proxy._compile_from_pin(CAPABILITY) is False


@pytest.mark.parametrize("config", [
    {}, {"workflow_execution": None}, {"workflow_execution": {}}, {"workflow_execution": {"compile_from_pin": []}},
    {"workflow_execution": {"compile_from_pin": {"images": True}}},
    {"workflow_execution": {"compile_from_pin": {CAPABILITY: "true"}}},
    {"workflow_execution": {"compile_from_pin": {CAPABILITY: 1}}},
])
def test_only_a_literal_true_for_the_route_turns_the_flag_on(config):
    assert pinned_definitions.compile_from_pin_enabled(config, CAPABILITY) is False


def test_the_shipped_default_for_audio():
    assert proxy.config["workflow_execution"]["compile_from_pin"][CAPABILITY] is False


# ---------------------------------------------------------------------------------------------- the reader

@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    return path


MANIFEST = {"schema_version": 1, "id": "wf", "intent": "x.make", "inputs": {"a": {"type": "string"}}, "outputs": {}}


def pin(db: Path, *, contract: dict[str, Any] = MANIFEST, template: bytes | None = b'{"1": {"class_type": "N", "inputs": {}}}'):
    built = build_job_envelope(
        source_kind="workflow", source_id="wf", contract=contract, binding={"map": {}}, template=template,
        protocol_id="p", adapter_id="a", interface_version=1, service_id="s", origin="http://192.0.2.1:1",
    )
    return media_jobs.create(media_jobs.MediaJob(intent="x.make", request_id="r"), envelope=built, path=db).job_id


def test_the_reader_returns_what_was_pinned(db: Path):
    pinned = pinned_definitions.load(pin(db), db)
    assert pinned.manifest("graph") == {**MANIFEST, "bindings": {"graph": {"map": {}}}}
    assert pinned.graph() == {"1": {"class_type": "N", "inputs": {}}}


def test_a_job_without_an_envelope_is_unavailable(db: Path):
    job_id = media_jobs.create(media_jobs.MediaJob(intent="x.make", request_id="r"), path=db).job_id
    with pytest.raises(DefinitionUnavailable, match="pinned no definitions"):
        pinned_definitions.load(job_id, db)


def test_a_damaged_blob_is_unavailable(db: Path):
    job_id = pin(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE definition_blob SET body = ? WHERE body = ?", (b'{"tampered": 1}', b'{"1": {"class_type": "N", "inputs": {}}}'))
    with pytest.raises(DefinitionUnavailable, match="damaged"):
        pinned_definitions.load(job_id, db)


def test_a_contract_that_is_not_a_manifest_is_unavailable_as_one(db: Path):
    pinned = pinned_definitions.load(pin(db, contract={"workflow": {"file": "x.json"}}), db)
    with pytest.raises(DefinitionUnavailable, match="not a manifest"):
        pinned.manifest("graph")


@pytest.mark.parametrize("template, message", [(None, "no template"), (b"not json", "not JSON"), (b"[1]", "not an object")])
def test_a_template_that_cannot_be_used_is_unavailable(db: Path, template: bytes | None, message: str):
    pinned = pinned_definitions.load(pin(db, template=template), db)
    with pytest.raises(DefinitionUnavailable, match=message):
        pinned.graph()
