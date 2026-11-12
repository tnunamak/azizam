"""An audio job runs from the definitions it pinned at accept, never from the live workflow files (S2.2). Always: the
live deployment has run this way since S2.2, and S6 removed the switch, so there is no flag to turn it off.

The audio job route is the first user. The rig is the adapter-differential one: the real app, a fake ComfyUI engine and
a temporary jobs database. Every test names a way the live files can differ from the pin, and checks the prompt the
engine really received.

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
from gateway import definition_store, media_jobs
from gateway.discovery import WorkflowRegistry
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, history_done, scenario
from tests.support.config_snapshot import publish_section
from tests.test_adapter_differential import _wav

COMFY_HOST = "comfy.fk.test"


@pytest.fixture
def rig(tmp_path: Path):
    with GatewayRig("audio-from-pin", tmp_path) as made:
        made.set(proxy, "_schedule_audio_media_job", lambda job_id: made.scheduled.append(job_id) or True)
        fake = made.engine("comfy", scenario("comfyui", "audio-from-pin", comfy_exchanges(
            history=history_done("audios", "tone_00001_.wav", node="3"), views=[(_wav(), "audio/wav")], strict=False)),
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


# ---------------------------------------------------------------------------------------------- pin

def test_two_jobs_accepted_around_an_edit_each_use_their_own_pin(rig: GatewayRig, tmp_path: Path):
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


def job_count() -> int:
    with sqlite3.connect(media_jobs.DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0]


def test_a_manifest_with_a_pin_refuses_a_job_it_cannot_pin(rig: GatewayRig):
    rig.set(proxy, "_manifest_job_envelope", lambda *_a, **_k: None)

    async def submit() -> int:
        made = await rig.call("POST", "/gateway/v1/media/jobs", label="submit with no envelope", unordered_json=True,
                              json_body={"intent": "audio.generate",
                                         "request": {"prompt": "p", "model": "tangoflux", "duration": 4}})
        return made.status_code

    assert run(submit()) == 503
    assert job_count() == 0 and rig.scheduled == []


def test_a_job_whose_pin_cannot_be_written_is_refused_and_leaves_no_row(rig: GatewayRig, monkeypatch: pytest.MonkeyPatch):
    """The envelope builds, the store then refuses it (here: raises): a 503 and no job row, not a 202 for a job that
    would fail ``definition_unavailable`` later."""
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("the store refused the envelope")

    monkeypatch.setattr(definition_store, "record_envelope_in", refuse)

    async def submit() -> int:
        made = await rig.call("POST", "/gateway/v1/media/jobs", label="submit with an unwritable pin", unordered_json=True,
                              json_body={"intent": "audio.generate",
                                         "request": {"prompt": "pinned", "model": "tangoflux", "duration": 4}})
        return made.status_code

    assert run(submit()) == 503
    assert job_count() == 0, "the refused job left no row"
    assert rig.scheduled == [], "nothing was scheduled"


def set_audio_workflows(rig: GatewayRig, table: dict) -> None:
    """Publish the audio section with ``table`` as its workflows, as a reload does. The test's cleanup restores the snapshot."""
    publish_section(rig.monkeypatch, "audio", {**proxy.config["audio"], "workflows": table})


def add_audio_workflow(rig: GatewayRig, name: str, *, max_seconds: int, graph_file: str = "workflows/audio/tangoflux.json") -> None:
    """An owner-added audio workflow in this test's own config and registry: it reuses ``graph_file`` and has its own
    manifest id ``audio.generate.<name>``. The shipped data is not touched."""
    set_audio_workflows(rig, {
        **proxy._audio_workflow_table(), name: {"file": graph_file, "min_seconds": 1, "max_seconds": max_seconds}})
    manifest = proxy._load_audio_workflow_manifest("tangoflux")
    manifest["id"] = f"audio.generate.{name}"
    manifest["inputs"]["duration"]["maximum"] = max_seconds
    registry = WorkflowRegistry()
    for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records():
        registry.add_manifest(record.manifest, source_path=record.source_path)
    registry.add_manifest(manifest)
    rig.set(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry)


def test_a_pinned_job_whose_model_now_resolves_to_another_workflow_fails_instead_of_mixing(rig: GatewayRig):
    """After an alias or ``audio.workflows`` edit and a restart, a resumed job must not compile its pinned graph with
    the limits, policy and receipt of whatever workflow the model resolves to now. The other workflow's limits fit
    the request, so only the resolved-workflow check stops the run."""
    add_audio_workflow(rig, "tangoflux-b", max_seconds=30)

    async def scenario_() -> str:
        job_id = await accept(rig, model="tangoflux-sfx")
        rig.set(proxy, "AUDIO_ALIASES", {**proxy.AUDIO_ALIASES, "tangoflux-sfx": "tangoflux-b"})
        await proxy._run_audio_media_job(job_id)
        return job_id

    job_id = run(scenario_())
    failed = job(job_id)
    assert failed["status"] == media_jobs.STATUS_FAILED
    assert failed["error_code"] == "definition_unavailable" and failed["retryable"] is False
    assert prompts(rig) == [], "no request reached the engine"


def test_a_job_pinned_under_an_owner_added_workflow_fails_when_its_model_becomes_an_alias_for_the_graph_files_workflow(
    rig: GatewayRig,
):
    """``tangoflux-long`` reuses tangoflux's graph file. Once ``tangoflux-long`` is an alias for ``tangoflux`` the pinned
    manifest still names that file, but the job must fail: it would run with tangoflux's limits, policy and receipt."""
    add_audio_workflow(rig, "tangoflux-long", max_seconds=60)

    async def scenario_() -> str:
        job_id = await accept(rig, model="tangoflux-long")
        assert job(job_id)["workflow"] == "audio.generate.tangoflux-long"
        # The owner removes the workflow and points its name at tangoflux; the gateway restarts.
        set_audio_workflows(rig, {k: v for k, v in proxy._audio_workflow_table().items() if k != "tangoflux-long"})
        rig.set(proxy, "AUDIO_ALIASES", {**proxy.AUDIO_ALIASES, "tangoflux-long": "tangoflux"})
        rig.scheduled.clear()
        assert proxy._resume_pending_audio_media_jobs() == 1
        await proxy._run_audio_media_job(job_id)
        return job_id

    job_id = run(scenario_())
    failed = job(job_id)
    assert failed["status"] == media_jobs.STATUS_FAILED
    assert failed["error_code"] == "definition_unavailable" and failed["retryable"] is False
    assert prompts(rig) == [], "no request reached the engine"


def test_a_pinned_job_still_runs_when_its_alias_is_renamed_to_the_same_workflow(rig: GatewayRig):
    """Control for the test above: the check is on the workflow the model resolves to, not on the alias spelling."""

    async def scenario_() -> str:
        job_id = await accept(rig, model="tangoflux-sfx")
        rig.set(proxy, "AUDIO_ALIASES", {"tangoflux-sfx": "tangoflux"})
        await proxy._run_audio_media_job(job_id)
        return job_id

    job_id = run(scenario_())
    assert job(job_id)["status"] == media_jobs.STATUS_SUCCEEDED, job(job_id).get("error")
    assert len(prompts(rig)) == 1


def test_a_pinned_job_keeps_an_owner_identity_whose_config_key_is_its_artifact_stem(rig: GatewayRig):
    """An operator can give a workflow its own id while retaining the graph's config key."""
    registry = WorkflowRegistry()
    for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records():
        manifest = copy.deepcopy(dict(record.manifest))
        if record.id == "audio.generate.tangoflux":
            manifest["id"] = "audio.generate.owner-named-sound"
        registry.add_manifest(manifest, source_path=record.source_path)
    rig.set(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry)

    async def scenario_() -> str:
        job_id = await accept(rig)
        assert job(job_id)["workflow"] == "audio.generate.owner-named-sound"
        forbid_live_reads(rig)
        await proxy._run_audio_media_job(job_id)
        return job_id

    job_id = run(scenario_())
    assert job(job_id)["status"] == media_jobs.STATUS_SUCCEEDED
    assert len(prompts(rig)) == 1
