"""Operator measurement: real owner CAS, scoped execution, cancellation and scoring."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import wave
from unittest.mock import AsyncMock

import pytest

from gateway import admin_store, service_registry, stt_chunking, stt_measurement as sm
from gateway.admin_api import AdminContext
from gateway.stt_measurement_api import operate, public_job
from tests.test_engine_admin_routes import _put_registry, _registry
from tests.test_signin_who_can_enter import gateway, owner_and_admin, csrf_for  # noqa: F401
from tests.support.fake_upstream.async_client_stub import install_async_client_stub


@pytest.fixture
def setup(gateway, monkeypatch):
    owner, owner_id, _, admin = owner_and_admin(gateway)
    registry = _registry(gateway)
    record = service_registry.service_from_backend_config("speech-test", {
        "name": "Speech test", "host": "speech.fk.test", "port": 80,
        "capabilities": ["audio", "transcriptions"], "default_model": "test-model",
        "chunking": {"enabled": True, "target_seconds": 36, "threshold_seconds": 40,
                     "search_seconds": 2, "max_segment_seconds": 40},
        "operator_note": "keep this field",
    }, source="ui")
    registry["services"]["speech-test"] = record
    _put_registry(gateway, registry)
    runner = gateway.proxy.STT_MEASUREMENTS
    monkeypatch.setattr(runner, "path", gateway.path.with_name("stt_measurements.db"))
    monkeypatch.setattr(runner, "initialized", False)
    monkeypatch.setattr(runner, "tasks", {})
    monkeypatch.setattr(runner, "admit", None)
    monkeypatch.setattr(runner, "preflight", None)
    monkeypatch.setattr(runner, "active", None)
    ctx = AdminContext(subject=owner_id, actor_id=owner_id, roles=frozenset({"owner"}))
    return gateway, owner, admin, runner, ctx


def test_owner_settings_route_uses_real_store_cas_and_keeps_unnamed_config(setup):
    gw, owner, admin, runner, _ = setup
    before, generation = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    assert runner.record("speech-test")[0]["capabilities"] == ["audio", "transcriptions"]
    path = "/admin/api/engines/speech-test/stt-settings"
    body = {"target_seconds": 25, "search_seconds": 0, "max_segment_seconds": 35, "native_long_audio_offline": True, "native_long_audio_streaming": True}
    refused = admin.patch(path, headers={**csrf_for(admin), "If-Match": f'"os-{generation}"'}, json=body)
    assert refused.status_code == 403
    assert owner.patch(path, headers=csrf_for(owner), json=body).status_code == 412
    saved = owner.patch(path, headers={**csrf_for(owner), "If-Match": f'"os-{generation}"'}, json=body)
    assert saved.status_code == 200, saved.text
    after, current = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    assert current > generation
    assert saved.json()["revision"] == f'"os-{current}"'
    assert after["services"]["speech-test"]["config"]["chunking"] == {**before["services"]["speech-test"]["config"]["chunking"], **body}
    assert after["services"]["speech-test"]["config"]["operator_note"] == "keep this field"
    assert owner.patch(path, headers={**csrf_for(owner), "If-Match": f'"os-{generation}"'}, json=body).status_code == 409


@pytest.mark.parametrize("mode", ["offline", "streaming"])
def test_mode_specific_native_audio_setting_can_be_configured(setup, mode):
    gw, owner, _, runner, _ = setup
    generation = runner.record("speech-test")[1]
    response = owner.patch("/admin/api/engines/speech-test/stt-settings",
        headers={**csrf_for(owner), "If-Match": f'"os-{generation}"'},
        json={f"native_long_audio_{mode}": True})
    assert response.status_code == 200, response.text
    saved = runner.record("speech-test")[0]["config"]["chunking"]
    assert saved[f"native_long_audio_{mode}"] is True


def test_omitted_trial_lengths_use_only_values_within_the_current_safe_cap(setup, monkeypatch):
    gw, _, _, runner, ctx = setup
    registry, _ = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    registry["services"]["speech-test"]["config"]["chunking"].update(
        target_seconds=30, search_seconds=2, max_segment_seconds=40)
    _put_registry(gw, registry)
    async def finish(*_args):
        return None
    monkeypatch.setattr(runner, "_run", finish)

    async def scenario():
        job = await runner.start(ctx, "speech-test", {"sample_ids": ["ami-TS3003a"]})
        await runner.tasks[job["id"]]
        return job

    job = asyncio.run(scenario())
    assert job["lengths"] == [20, 30, 40]


def test_options_returns_only_safe_default_trial_lengths(setup):
    gw, _, _, runner, ctx = setup
    registry, _ = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    registry["services"]["speech-test"]["config"]["chunking"].update(
        target_seconds=30, search_seconds=2, max_segment_seconds=40)
    _put_registry(gw, registry)
    options = asyncio.run(operate(runner, ctx, "speech-test", "options"))
    assert options["trial_lengths"] == [20, 30, 40]


def test_options_explains_when_fewer_than_two_default_lengths_fit(setup):
    gw, _, _, runner, ctx = setup
    registry, _ = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    registry["services"]["speech-test"]["config"]["chunking"].update(
        target_seconds=20, search_seconds=0, max_segment_seconds=20)
    _put_registry(gw, registry)
    options = asyncio.run(operate(runner, ctx, "speech-test", "options"))
    assert options["trial_lengths"] == [20]
    assert "fewer than two" in options["trial_lengths_note"]


@pytest.mark.parametrize("body", [{"target_seconds": 0}, {"search_seconds": -1}, {"native_long_audio": "yes"}, {"other": 1}, {"max_segment_seconds": 10}])
def test_invalid_settings_do_not_write(setup, body):
    gw, owner, _, _, _ = setup
    before = admin_store.get_service_registry_with_generation(path=gw.path)
    generation = before["operator_settings_generation"]
    reply = owner.patch("/admin/api/engines/speech-test/stt-settings", headers={**csrf_for(owner), "If-Match": f'"os-{generation}"'}, json=body)
    assert reply.status_code == 400
    assert admin_store.get_service_registry_with_generation(path=gw.path) == before


def test_options_fetches_manifest_metadata_and_generates_owner_commands(setup):
    _, owner, admin, _, _ = setup
    path = "/admin/api/engines/speech-test/stt-measurements"
    assert admin.get(path).status_code == 403
    reply = owner.get(path)
    assert reply.status_code == 200, reply.text
    options = reply.json()
    assert len(options["samples"]) >= 2
    assert 0 in options["policy"]["search_widths"]
    assert "AZIZAM_OWNER_KEY" in options["commands"]["start"]
    assert "If-Match" in options["commands"]["configure"]
    assert "fingerprint" not in reply.text


def test_options_exposes_unstated_audio_rights_before_sample_selection(setup):
    _, owner, _, _, _ = setup
    options = owner.get("/admin/api/engines/speech-test/stt-measurements").json()
    sample = next(s for s in options["samples"] if s["id"] == "earnings21-4366522")
    assert sample["audio_rights_unstated"] is True
    audio = next(s for s in sample["sources"] if s["name"] == "audio")
    assert audio["audio_rights"]["status"] == "not-stated"
    assert "no separate" in audio["audio_rights"]["note"].lower()
    assert all(s["provenance"] and s["human_transcribed_evidence"] for s in sample["sources"])


def test_options_survives_invalid_saved_chunking_for_repair(setup):
    gw, owner, _, runner, _ = setup
    registry, generation = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    registry["services"]["speech-test"]["config"]["chunking"]["target_seconds"] = "bad"
    _put_registry(gw, registry)
    reply = owner.get("/admin/api/engines/speech-test/stt-measurements")
    assert reply.status_code == 200, reply.text
    assert "need repair" in reply.json()["settings_note"]
    assert reply.json()["settings"]["target_seconds"] == 480
    assert owner.patch("/admin/api/engines/speech-test/stt-settings", headers={**csrf_for(owner), "If-Match": f'"os-{generation + 1}"'}, json={"target_seconds": 25}).status_code == 200


def test_non_object_chunking_can_be_replaced_through_settings_route(setup):
    gw, owner, _, _, _ = setup
    registry, _ = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    registry["services"]["speech-test"]["config"]["chunking"] = "corrupt"
    _put_registry(gw, registry)
    reply = owner.get("/admin/api/engines/speech-test/stt-measurements")
    assert reply.status_code == 200
    current = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()[1]
    saved = owner.patch("/admin/api/engines/speech-test/stt-settings", headers={**csrf_for(owner), "If-Match": f'"os-{current}"'}, json={"target_seconds": 25})
    assert saved.status_code == 200, saved.text
    assert gw.proxy.ADMIN_STORE.get_service_registry_snapshot()[0]["services"]["speech-test"]["config"]["chunking"]["target_seconds"] == 25


def test_oversized_revision_is_precondition_failure(setup):
    _, _, _, runner, ctx = setup
    with pytest.raises(sm.EngineRouteError) as error:
        asyncio.run(operate(runner, ctx, "speech-test", "configure", body={"search_seconds": 0}, revision='"os-' + "9" * 10000 + '"'))
    assert error.value.status_code == 412


@pytest.mark.parametrize("body", [b"{", b"[]", b"null"])
def test_measurement_write_rejects_malformed_or_non_object_json(setup, body):
    _, owner, _, _, _ = setup
    reply = owner.post("/admin/api/engines/speech-test/stt-measurements", content=body,
                       headers={**csrf_for(owner), "content-type": "application/json"})
    assert reply.status_code == 400


def test_start_preflight_runs_before_sample_preparation(setup, monkeypatch):
    _, _, _, runner, ctx = setup
    calls = []
    async def preflight(ctx, service_id, model):
        calls.append((service_id, model))
        raise sm.EngineRouteError(503, "measurement_backend_unavailable", "offline")
    monkeypatch.setattr(runner, "preflight", preflight)
    monkeypatch.setattr(runner, "_prepare", lambda *args: pytest.fail("sample preparation started before preflight"))
    async def run():
        with pytest.raises(sm.EngineRouteError, match="offline"):
            await runner.start(ctx, "speech-test", {"sample_ids": ["ami-TS3003a"], "lengths": [20, 40], "search_widths": [0, 2]})
    asyncio.run(run())
    assert calls == [("speech-test", None)]


def test_trial_length_above_current_hard_maximum_is_rejected_before_job_creation(setup):
    gw, _, _, runner, ctx = setup
    registry, _ = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    registry["services"]["speech-test"]["config"]["chunking"].update(target_seconds=30, search_seconds=2)
    _put_registry(gw, registry)
    with pytest.raises(sm.EngineRouteError, match="current safe piece limit"):
        asyncio.run(runner.start(ctx, "speech-test", {
            "sample_ids": ["ami-TS3003a"], "lengths": [20, 45], "search_widths": [0, 2]}))
    assert runner.get("speech-test") is None


def test_declared_hard_maximum_is_a_valid_measured_trial_length(setup, monkeypatch):
    _, _, _, runner, ctx = setup
    async def finish(*_args):
        return None
    monkeypatch.setattr(runner, "_run", finish)

    async def scenario():
        job = await runner.start(ctx, "speech-test", {
            "sample_ids": ["ami-TS3003a"], "lengths": [20, 40], "search_widths": [0, 2]})
        await runner.tasks[job["id"]]
        return job

    job = asyncio.run(scenario())
    assert job["lengths"] == [20, 40]


def test_trial_search_width_above_half_the_current_target_is_rejected(setup):
    _, _, _, runner, ctx = setup
    with pytest.raises(sm.EngineRouteError, match="search widths"):
        asyncio.run(runner.start(ctx, "speech-test", {
            "sample_ids": ["ami-TS3003a"], "lengths": [20, 40], "search_widths": [0, 19]}))
    assert runner.get("speech-test") is None


def test_search_width_that_cannot_fit_the_shortest_possible_trial_target_is_rejected(setup, monkeypatch):
    _, _, _, runner, ctx = setup
    async def finish(*_args):
        return None
    monkeypatch.setattr(runner, "_run", finish)
    # A 20 s recommended maximum yields a 15 s target. Width 8 is below half
    # the current 30 s target, but from_service would clamp it to 7.5 s.
    with pytest.raises(sm.EngineRouteError, match="conservative trial target"):
        asyncio.run(runner.start(ctx, "speech-test", {
            "sample_ids": ["ami-TS3003a"], "lengths": [20, 40], "search_widths": [0, 8]}))
    assert runner.get("speech-test") is None


def test_revoked_mcp_connection_stops_a_running_measurement(setup, monkeypatch):
    gw, _, _, runner, ctx = setup
    ctx = AdminContext(subject=ctx.subject, actor_id=ctx.actor_id, roles=ctx.roles, connection_id="grant-test")
    grant = {"active": True}
    monkeypatch.setattr(gw.proxy, "MCP_OAUTH_ENABLED", True)
    monkeypatch.setattr(gw.proxy.MCP_OAUTH, "connection_active", lambda grant_id, person_id: grant["active"])
    monkeypatch.setattr(runner, "active", gw.proxy._operator_stt_owner_active)
    started = asyncio.Event()

    async def wait_for_work(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_measure", wait_for_work)

    async def scenario():
        job = await runner.start(ctx, "speech-test", {"sample_ids": ["ami-TS3003a"],
            "lengths": [20, 40], "search_widths": [0, 2]})
        await asyncio.wait_for(started.wait(), 5)
        grant["active"] = False
        await asyncio.wait_for(runner.tasks[job["id"]], 5)
        result = runner.get("speech-test", job["id"])
        assert result["status"] == "cancelled"
        assert result["phase"] == "Owner access ended; measurement stopped."

    asyncio.run(scenario())


def test_owner_role_removal_invalidates_measurement_access(setup, monkeypatch):
    gw, _, _, _, ctx = setup
    monkeypatch.setattr(admin_store, "effective_admin_roles", lambda *args, **kwargs: frozenset())
    assert not gw.proxy._operator_stt_owner_active(ctx)


def test_startup_cleanup_removes_orphaned_workspaces(setup, tmp_path):
    _, _, _, runner, _ = setup
    orphan = tmp_path / "stt-measurement-work" / "job-orphan"
    orphan.mkdir(parents=True)
    (orphan / "sample.wav").write_bytes(b"private")
    runner.startup_cleanup()
    assert not orphan.exists()


def test_gpu_admission_credits_only_real_service_rows(setup, monkeypatch):
    gw, _, _, _, ctx = setup
    import status_endpoint
    registry = {"services": {"speech-test": {"resources": {"gpu_required": True, "vram_gb_estimate": 1}}}}
    monkeypatch.setattr(gw.proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    monkeypatch.setattr(status_endpoint, "_gpus_raw", lambda: [{"index": 0}])
    monkeypatch.setattr(gw.proxy, "_media_job_queue_depth", lambda: 0)
    monkeypatch.setattr(gw.proxy.stt_chunking.GLOBAL_STT_RESOURCE_GATE, "_active", 0)
    monkeypatch.setattr(status_endpoint, "_vram_attribution", lambda *_: {"per_gpu": [{"index": 0, "services": [
        {"service": "speech-test", "used_mb": 700}, {"service": "desktop", "used_mb": 900},
        {"service": "__unattributed", "used_mb": 600}, {"service": "__headroom", "used_mb": 1000},
    ]}]})
    captured = {}
    monkeypatch.setattr(sm, "gpu_admission", lambda *args, **kwargs: captured.update(kwargs) or {"admitted": True})
    asyncio.run(gw.proxy._operator_stt_admit(ctx, "speech-test"))
    assert captured["resident_mb"] == {0: 700}
    assert captured["live_stt"] == 0
    registry["services"]["__unattributed"] = registry["services"].pop("speech-test")
    asyncio.run(gw.proxy._operator_stt_admit(ctx, "__unattributed"))
    assert captured["resident_mb"] == {0: 0}


def _job(runner):
    record, generation = runner.record("speech-test")
    return {"id": "testjob", "service_id": "speech-test", "status": "running", "phase": "trial", "completed": 0,
            "total": 5, "fingerprint": sm.fingerprint(record), "registry_generation": generation,
            "model": None, "report": None, "sample_ids": ["ami-TS3003a"], "lengths": [20, 40, 60], "search_widths": [0, 2]}


def test_durable_progress_interrupted_restart_and_explicit_stale_apply(setup):
    gw, _, _, runner, ctx = setup
    job = _job(runner)
    runner._save(job)
    restarted = sm.Measurements(gw.proxy.ADMIN_STORE, None)
    assert restarted.get("speech-test", job["id"])["status"] == "interrupted"
    job.update(status="succeeded", report={"recommendation": {"target_seconds": 25, "search_seconds": 0, "max_segment_seconds": 35, "native_long_audio_offline": False, "native_long_audio_streaming": False}})
    runner._save(job)
    before, generation = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    assert before["services"]["speech-test"]["config"]["chunking"]["target_seconds"] == 36
    assert "fingerprint" not in public_job(job)
    assert runner.apply(ctx, "speech-test", job["id"], generation)["settings"]["target_seconds"] == 25
    with pytest.raises(sm.EngineRouteError, match="changed"):
        runner.apply(ctx, "speech-test", job["id"], generation + 1)


def test_real_snapshot_accepts_all_length_trials_and_zero_width_without_leaking(setup, monkeypatch):
    gw, _, _, _, _ = setup
    monkeypatch.setattr(gw.proxy, "config", {"backends": {"speech-test": {
        "host": "speech.fk.test", "port": 80,
        "chunking": {"target_seconds": 30, "threshold_seconds": 40, "max_segment_seconds": 40, "search_seconds": 2}}}})
    original = gw.proxy._stt_execution_snapshot("transcriptions", "speech-test")
    for length in [20, 40, 50, 60, 90]:
        token = sm.TRIAL.set({"service_id": "speech-test", "settings": {"enabled": False, "target_seconds": length,
            "max_segment_seconds": length, "threshold_seconds": length + 1, "native_long_audio_offline": True, "native_long_audio_streaming": True, "search_seconds": 0}})
        try:
            execution = gw.proxy._stt_execution_snapshot("transcriptions", "speech-test")
            assert execution.target_seconds == length and execution.search_seconds == 0
            assert not execution.chunking_enabled
        finally:
            sm.TRIAL.reset(token)
    assert gw.proxy._stt_execution_snapshot("transcriptions", "speech-test") == original


@pytest.mark.parametrize("group_already_gone", [False, True])
def test_cancel_during_preparation_kills_owned_process_group_and_reaps(setup, monkeypatch, tmp_path, group_already_gone):
    _, _, _, runner, _ = setup
    entered = asyncio.Event()
    class Process:
        pid = 9876
        returncode = None
        stdout = SimpleNamespace(readline=None)
        async def readline(self):
            entered.set()
            await asyncio.Future()
        async def wait(self):
            self.returncode = -9
            return -9
    process = Process()
    process.stdout.readline = process.readline
    spawn = AsyncMock(return_value=process)
    kill = []
    monkeypatch.setattr(sm.asyncio, "create_subprocess_exec", spawn)
    def kill_group(pid, sig):
        kill.append((pid, sig))
        if group_already_gone:
            raise ProcessLookupError
    monkeypatch.setattr(sm.os, "killpg", kill_group)
    async def scenario():
        job = _job(runner)
        spec = sm.stt_samples.load_manifest()[0]
        task = asyncio.create_task(runner._prepare(job, spec, tmp_path, {"max_download_bytes": 1000}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode == -9
    asyncio.run(scenario())
    assert kill == [(9876, sm.signal.SIGKILL)]
    assert spawn.call_args.kwargs["start_new_session"] is True


def test_cancel_during_http_transcription_unwinds_request_and_persists_terminal_state(setup, monkeypatch, tmp_path):
    _, _, _, runner, ctx = setup
    entered, cleaned = asyncio.Event(), []
    async def transcribe(*args):
        try:
            entered.set()
            await asyncio.Future()
        finally:
            cleaned.append(True)
    monkeypatch.setattr(runner, "transcribe", transcribe)
    async def fake_measure(ctx, job, policy, root):
        return await runner._transcription(ctx, job, root / "never-read.wav", {})
    monkeypatch.setattr(runner, "_measure", fake_measure)
    async def scenario():
        job = _job(runner)
        runner._save(job)
        task = asyncio.create_task(runner._run(ctx, job, {"deadline_seconds": 10}))
        runner.tasks[job["id"]] = task
        await entered.wait()
        result = await runner.cancel("speech-test", job["id"])
        assert result["status"] == "cancelled"
        assert sm.TRIAL.get() is None
    asyncio.run(scenario())
    assert cleaned == [True]
    assert not list((runner.path.parent / "stt-measurement-work").iterdir())


def test_job_deadline_also_cancels_preparation(setup, monkeypatch):
    _, _, _, runner, ctx = setup
    cleaned = []
    async def blocked(*args):
        try:
            await asyncio.Future()
        finally:
            cleaned.append(True)
    monkeypatch.setattr(runner, "_measure", blocked)
    async def scenario():
        job = _job(runner)
        await runner._run(ctx, job, {"deadline_seconds": 0.01})
        assert runner.get("speech-test", job["id"])["status"] == "failed"
    asyncio.run(scenario())
    assert cleaned == [True]


@pytest.mark.parametrize("width", [2, 15])
def test_offset_scoring_has_one_shared_reference_clock_and_no_false_seam_cost(setup, monkeypatch, tmp_path, width, record_property):
    _, _, _, runner, ctx = setup
    references = [sm.measure.RefWord(f"word{i}", i + .1, i + .4) for i in range(120)]
    async def prepare(job, spec, folder, policy):
        path = folder / "audio.wav"
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(b'\0\0' * 16000 * 120)
        return path, references
    starts = []
    real_slice = sm._wave_slice
    def wave_slice(source, destination, start, end):
        starts[:] = [start, end]
        real_slice(source, destination, start, end)
    async def transcribe(ctx, sid, model, path):
        trial = sm.TRIAL.get()
        settings = trial["settings"]
        assert "native_long_audio" not in settings
        assert settings["native_long_audio_offline"] is (not settings["enabled"])
        assert settings["native_long_audio_streaming"] is (not settings["enabled"])
        start, end = starts
        if settings["enabled"]:
            segments = stt_chunking.plan_quietest_frame_segments(duration_seconds=end-start,
                quietest_frame=lambda lo, hi, near: near, target_seconds=settings["target_seconds"],
                search_seconds=settings["search_seconds"], max_segment_seconds=settings["max_segment_seconds"], max_segments=512)
            trial["outcome"] = {"mode": "chunked", "attempts": len(segments)}
        return " ".join(w.word for w in references if start <= w.mid < end)
    monkeypatch.setattr(runner, "_prepare", prepare)
    monkeypatch.setattr(runner, "transcribe", transcribe)
    monkeypatch.setattr(sm, "_wave_slice", wave_slice)
    async def scenario():
        job = _job(runner)
        job["lengths"] = [20, 40]
        job["search_widths"] = [0, width]
        # Small synthetic fixture tests the clock/sweep, not production evidence minima.
        policy = runner.options()["policy"]
        policy["recommendation"].update(min_length_words=20, min_length_blocks=2, min_seam_pairs=2,
                                         min_seam_words=10, min_noise_pairs=2, min_noise_words=10)
        result = await runner._measure(ctx, job, {**policy, "audio_seconds_per_sample": 120, "resamples": 40}, tmp_path)
        record_property("measurement_report", json.dumps(result, sort_keys=True))
        assert [row["search"] for row in result["seam_excess"]] == [0, width]
        assert all(row["deletion_pct"] == 0 for row in result["deletions_by_length"])
        assert all(row["points"] > 0 and row["excess_pp"] == 0 and row["noise"]["excess_pp"] == 0 for row in result["seam_excess"])
        assert result["recommendation"] is not None
        assert "native_long_audio" not in result["recommendation"]
        assert result["recommendation"]["native_long_audio_offline"] is False
        assert result["recommendation"]["native_long_audio_streaming"] is False
        assert result["recommendation"]["search_seconds"] == (2 if width == 2 else 0)
        assert all(row["baseline_length"] == 20 and row["paired_deletion"]["excess_pp"] == 0 for row in result["deletions_by_length"])
        assert result["recommendation_state"] == "recommended"
    asyncio.run(scenario())


def _gateway_trial_config(gw, monkeypatch):
    monkeypatch.setattr(gw.proxy, "config", {"backends": {"speech-test": {
        "host": "speech.fk.test", "port": 80, "capabilities": ["audio", "transcriptions"],
        "chunking": {"target_seconds": 30, "threshold_seconds": 40, "max_segment_seconds": 40}}},
        "model_aliases": {"transcriptions": {"engines": {"test-speech": {
            "service": "speech-test", "model": "test-model"}}}}})
    monkeypatch.setattr(gw.proxy, "_apply_selected_stt_policy", lambda *a, **kw: None)


def test_operator_http_uses_actual_gateway_route_and_admission(setup, monkeypatch, tmp_path):
    gw, _, _, _, ctx = setup
    _gateway_trial_config(gw, monkeypatch)
    from fastapi.responses import JSONResponse
    calls = []
    def admission(intent, **kwargs):
        calls.append((intent, kwargs["service_id"]))
        return JSONResponse({"error": {"code": "resource_admission_denied"}}, status_code=429)
    monkeypatch.setattr(gw.proxy, "_dispatch_admission_response", admission)
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"synthetic; admission must refuse before audio decoding")
    async def scenario():
        with pytest.raises(sm.EngineRouteError) as error:
            await gw.proxy._operator_stt_transcribe(ctx, "speech-test", None, audio)
        assert error.value.status_code == 429
        assert error.value.code == "measurement_request_refused"
    asyncio.run(scenario())
    assert calls == [("speech.transcription", "speech-test")]
    assert not gw.proxy._INTERNAL_IDENTITIES


def test_owner_http_cancellation_propagates_through_asgi_request(setup, monkeypatch, tmp_path):
    gw, _, _, _, ctx = setup
    _gateway_trial_config(gw, monkeypatch)
    entered, cleaned = asyncio.Event(), []
    monkeypatch.setattr(gw.proxy, "_dispatch_admission_response", lambda *args, **kwargs: None)
    async def wait_for_engine(*args, **kwargs):
        try:
            entered.set()
            await asyncio.Future()
        finally:
            cleaned.append(True)
    monkeypatch.setattr(gw.proxy, "_ensure_speech_service_ready", wait_for_engine)
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"synthetic; wait occurs before decoding")
    async def scenario():
        task = asyncio.create_task(gw.proxy._operator_stt_transcribe(ctx, "speech-test", None, audio))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert cleaned == [True]
    assert not gw.proxy._INTERNAL_IDENTITIES


def test_actual_preparation_process_group_cancellation_closes_descendant_pipe(setup, monkeypatch, tmp_path):
    _, _, _, runner, _ = setup
    original_spawn = asyncio.create_subprocess_exec
    processes = []
    # A worker in conversion with a child holding its stdout open. Killing only the
    # worker leaves this pipe open; group ownership must kill the child as well.
    code = "import subprocess,sys,time,json; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(json.dumps({'phase':'Converting','done':1,'total':1}),flush=True); time.sleep(30)"
    async def spawn(*args, **kwargs):
        process = await original_spawn(sm.sys.executable, "-c", code, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(sm.asyncio, "create_subprocess_exec", spawn)
    async def scenario():
        job = _job(runner)
        task = asyncio.create_task(runner._prepare(job, sm.stt_samples.load_manifest()[0], tmp_path, {"max_download_bytes": 1000}))
        for _ in range(100):
            if job["phase"] == "Converting":
                break
            await asyncio.sleep(.01)
        assert job["phase"] == "Converting"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(processes[0].stdout.read(), 1) == b""
        assert processes[0].returncode == -sm.signal.SIGKILL
    asyncio.run(scenario())


@pytest.mark.parametrize("gpus,depth,accepted", [
    ([{"memory_total_mb": 16000, "memory_used_mb": 2000, "instantaneous_util_pct": 0}], 0, True),
    ([{"memory_total_mb": 16000, "memory_used_mb": 15500, "instantaneous_util_pct": 0}], 0, False),
    ([{"memory_total_mb": 16000, "memory_used_mb": 2000, "instantaneous_util_pct": 90}], 0, False),
    ([{"memory_total_mb": 16000, "memory_used_mb": 2000, "instantaneous_util_pct": 0}], 1, False),
    (None, 0, False),
    ([{"memory_total_mb": 16000, "memory_used_mb": 2000, "instantaneous_util_pct": 0}], None, False),
])
def test_gpu_admission_uses_declared_memory_current_gpu_load_and_shared_queue(gpus, depth, accepted):
    record = {"resources": {"gpu_required": True, "vram_gb_estimate": 2}}
    result = sm.gpu_admission(record, gpus, depth, {"max_gpu_utilization_pct": 60, "gpu_min_free_mb": 128})
    assert result["admitted"] is accepted


def test_gpu_admission_requires_declared_resource_data_and_cpu_needs_no_gpu():
    for resources in ({}, {"gpu_required": True}):
        with pytest.raises(sm.EngineRouteError) as error:
            sm.gpu_admission({"resources": resources}, None, None, {})
        assert error.value.code == "measurement_resources_required"
    assert sm.gpu_admission({"resources": {"gpu_required": False}}, None, None, {})["admitted"]


def test_gpu_admission_yields_to_live_stt_even_when_gpu_headroom_is_free():
    record = {"resources": {"gpu_required": True, "vram_gb_estimate": 4}}
    gpu = [{"index": 0, "memory_total_mb": 16000, "memory_used_mb": 1000, "instantaneous_util_pct": 0}]
    policy = {"max_gpu_utilization_pct": 60, "gpu_min_free_mb": 128}
    result = sm.gpu_admission(record, gpu, 0, policy, live_stt=1)
    assert not result["admitted"]


def test_cancellation_interrupts_gpu_admission_wait(setup, monkeypatch):
    _, _, _, runner, ctx = setup
    entered = asyncio.Event()
    async def admit(*args):
        entered.set()
        return {"admitted": False, "reason": "Waiting for GPU memory", "gpu_required": True}
    async def measure(ctx, job, policy, root):
        return await runner._transcription(ctx, job, root / "never-read.wav", {})
    monkeypatch.setattr(runner, "admit", admit)
    monkeypatch.setattr(runner, "_measure", measure)
    transcribe = AsyncMock()
    monkeypatch.setattr(runner, "transcribe", transcribe)
    async def scenario():
        job = _job(runner)
        runner._save(job)
        runner.tasks[job["id"]] = asyncio.create_task(runner._run(ctx, job, {"deadline_seconds": 10}))
        await entered.wait()
        result = await runner.cancel("speech-test", job["id"])
        assert result["status"] == "cancelled"
    asyncio.run(scenario())
    transcribe.assert_not_called()


def test_mcp_configure_uses_owner_operation_boundary_and_real_cas(setup):
    gw, _, _, runner, ctx = setup
    _, generation = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    async def scenario():
        non_owner = AdminContext(subject="helper", roles=frozenset({"admin"}))
        with pytest.raises(sm.EngineRouteError) as error:
            await operate(runner, non_owner, "speech-test", "configure", body={"search_seconds": 0}, revision=f'"os-{generation}"')
        assert error.value.status_code == 403
        result = await operate(runner, ctx, "speech-test", "configure", body={"search_seconds": 0}, revision=f'"os-{generation}"')
        assert result["settings"]["search_seconds"] == 0
    asyncio.run(scenario())
    assert gw.proxy.ADMIN_STORE.get_service_registry_snapshot()[0]["services"]["speech-test"]["config"]["chunking"]["search_seconds"] == 0


def test_seam_settings_pass_real_gateway_snapshot_before_admission(setup, monkeypatch, tmp_path):
    gw, _, _, runner, ctx = setup
    _gateway_trial_config(gw, monkeypatch)
    from fastapi.responses import JSONResponse
    seen = []
    def admission(intent, **kwargs):
        execution = gw.proxy._stt_execution_snapshot("transcriptions", kwargs["service_id"])
        assert execution.threshold_seconds > execution.target_seconds
        assert execution.chunking_enabled and execution.search_seconds == 0
        seen.append(execution)
        return JSONResponse({"error": {"code": "resource_admission_denied"}}, status_code=429)
    monkeypatch.setattr(gw.proxy, "_dispatch_admission_response", admission)
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"synthetic; should reach admission before decoding")
    async def scenario():
        token = sm.TRIAL.set({"service_id": "speech-test", "settings": {"enabled": True,
            "target_seconds": 30, "threshold_seconds": 40, "max_segment_seconds": 40, "search_seconds": 0}})
        try:
            with pytest.raises(sm.EngineRouteError) as error:
                await gw.proxy._operator_stt_transcribe(ctx, "speech-test", None, audio)
            assert error.value.status_code == 429
        finally:
            sm.TRIAL.reset(token)
    asyncio.run(scenario())
    assert len(seen) == 1


def test_actual_mcp_tool_dispatch_configures_for_owner_oauth_and_refuses_app_key(setup):
    gw, _, _, runner, ctx = setup
    from gateway import mcp_server
    _, generation = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    deps = SimpleNamespace(stt_measurement=gw.proxy._mcp_stt_measurement)
    args = {"service_id": "speech-test", "action": "configure", "revision": f'"os-{generation}"', "settings": {"search_seconds": 0}}
    async def scenario():
        app_auth = SimpleNamespace(identity=SimpleNamespace(person_id=ctx.subject, connection_id=None))
        denied = await mcp_server.call_tool(deps, None, app_auth, "stt_measurement", args)
        assert denied["isError"] is True
        oauth_auth = SimpleNamespace(identity=SimpleNamespace(person_id=ctx.subject, connection_id="verified-oauth-grant"))
        saved = await mcp_server.call_tool(deps, None, oauth_auth, "stt_measurement", args)
        assert not saved.get("isError")
        assert saved["structuredContent"]["settings"]["search_seconds"] == 0
    asyncio.run(scenario())


def test_start_rejects_duplicate_samples_and_invalid_widths_before_work(setup, monkeypatch):
    _, _, _, runner, ctx = setup
    called = []
    async def measure(*args):
        called.append(True)
    monkeypatch.setattr(runner, "_run", measure)
    async def scenario():
        for body in ({"sample_ids": ["ami-TS3003a", "ami-TS3003a"]}, {"sample_ids": [[]]},
                     {"search_widths": [-1, 0]}, {"lengths": [0, 20]}, {"lengths": [float('nan'), 20]}):
            with pytest.raises(sm.EngineRouteError):
                await runner.start(ctx, "speech-test", body)
        job = await runner.start(ctx, "speech-test", {"sample_ids": ["ami-TS3003a"], "lengths": [20, 20, 40], "search_widths": [0, 0, 2]})
        assert job["lengths"] == [20, 40] and job["search_widths"] == [0, 2]
        await runner.tasks[job["id"]]
    asyncio.run(scenario())
    assert called == [True]


def test_seed_gpu_resources_survive_service_record_conversion():
    import yaml
    from pathlib import Path
    config = yaml.safe_load((Path(__file__).resolve().parent.parent / "proxy_config.yml").read_text())
    backend = config["backends"]["stt-parakeet-gpu"]
    record = service_registry.service_from_backend_config("stt-parakeet-gpu", backend, source="seed")
    assert record["config"]["resources"] == {"gpu_required": True, "vram_gb_estimate": 3.2, "gpu": {"devices": [0]}}
    assert service_registry.service_record_v2(record)["resources"] == record["config"]["resources"]
    assert sm.gpu_admission(record, [{"index": 0, "memory_total_mb": 16000, "memory_used_mb": 2000, "instantaneous_util_pct": 0}], 0,
                            {"max_gpu_utilization_pct": 60, "gpu_min_free_mb": 128})["admitted"]


def test_legacy_owner_can_declare_measurement_resources_through_existing_rest(setup):
    gw, owner, admin, runner, _ = setup
    record, generation = runner.record("speech-test")
    assert not record.get("resources")
    resources = {"gpu_required": True, "vram_gb_estimate": 3.2}
    path = "/admin/api/operator/services/speech-test"
    assert admin.patch(path, headers={**csrf_for(admin), "If-Match": f'"os-{generation}"'}, json={"resources": resources}).status_code == 403
    reply = owner.patch(path, headers={**csrf_for(owner), "If-Match": f'"os-{generation}"'}, json={"resources": resources})
    assert reply.status_code == 200, reply.text
    record, _ = runner.record("speech-test")
    assert record["config"]["resources"] == resources
    assert service_registry.service_record_v2(record)["resources"] == resources
    assert sm.gpu_admission(record, [{"memory_total_mb": 16000, "memory_used_mb": 2000, "instantaneous_util_pct": 0}], 0,
                            {"max_gpu_utilization_pct": 60, "gpu_min_free_mb": 128})["admitted"]


def test_gpu_admission_honors_declared_devices_and_does_not_guess_on_missing_device():
    record = {"resources": {"gpu_required": True, "vram_gb_estimate": 2, "gpu": {"devices": [0]}}}
    free = {"index": 0, "memory_total_mb": 16000, "memory_used_mb": 2000, "instantaneous_util_pct": 0}
    busy = {"index": 1, "memory_total_mb": 16000, "memory_used_mb": 15900, "instantaneous_util_pct": 100}
    policy = {"max_gpu_utilization_pct": 60, "gpu_min_free_mb": 128}
    assert sm.gpu_admission(record, [free, busy], 0, policy)["admitted"]
    assert not sm.gpu_admission(record, [busy], 0, policy)["admitted"]


@pytest.mark.parametrize("extra, expected_threshold", [({}, 54), ({"max_segment_seconds": 60}, 60), ({"max_segment_seconds": 50}, 51), ({"threshold_seconds": 70}, 70)])
def test_target_patch_above_threshold_preserves_absent_derived_fields(setup, extra, expected_threshold):
    gw, owner, _, runner, _ = setup
    registry = _registry(gw)
    registry["services"]["speech-test"]["config"]["chunking"] = {"target_seconds": 30, "threshold_seconds": 40, "search_seconds": 2}
    _put_registry(gw, registry)
    _, generation = runner.record("speech-test")
    response = owner.patch("/admin/api/engines/speech-test/stt-settings", headers={**csrf_for(owner), "If-Match": f'"os-{generation}"'}, json={"target_seconds": 50, **extra})
    assert response.status_code == 200, response.text
    chunking = runner.record("speech-test")[0]["config"]["chunking"]
    assert chunking["target_seconds"] == 50 and chunking["threshold_seconds"] == expected_threshold
    visible = owner.get("/admin/api/engines/speech-test").json()
    assert visible["engine"]["stt_chunking"]["cut_files_longer_than_seconds"] == min(expected_threshold, extra.get("max_segment_seconds", expected_threshold))
    assert ("max_segment_seconds" in chunking) == ("max_segment_seconds" in extra)
    assert "native_long_audio" not in chunking
    assert stt_chunking.SttExecutionConfig.from_service(service_id="speech-test", service_config=runner.record("speech-test")[0]["config"], config_directory=gw.path.parent).max_segment_seconds == extra.get("max_segment_seconds", 54)


def test_resident_vram_is_credited_only_to_the_measured_service():
    record = {"resources": {"gpu_required": True, "vram_gb_estimate": 4, "gpu": {"devices": [0]}}}
    gpu = {"index": 0, "memory_total_mb": 16000, "memory_used_mb": 15000, "instantaneous_util_pct": 0}
    policy = {"max_gpu_utilization_pct": 60, "gpu_min_free_mb": 128}
    assert not sm.gpu_admission(record, [gpu], 0, policy)["admitted"]
    assert sm.gpu_admission(record, [gpu], 0, policy, resident_mb={0: 4096})["admitted"]
    assert not sm.gpu_admission(record, [gpu], 0, policy, resident_mb={1: 4096})["admitted"]
    assert not sm.gpu_admission(record, [gpu], 0, policy, resident_mb={0: 20000})["admitted"]
    assert not sm.gpu_admission(record, [{**gpu, "memory_used_mb": 15950}], 0, policy, resident_mb={0: 4096})["admitted"]


def test_startup_cleans_orphan_scratch_but_respects_live_worker_lease(setup):
    gw, _, _, runner, _ = setup
    import fcntl
    scratch = runner.path.parent / "stt-measurement-work" / "job-abandoned"
    scratch.mkdir(parents=True)
    (scratch / "partial-download").write_bytes(b"synthetic partial source")
    with (runner.path.parent / "stt-measurement-work.lock").open("a+b") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        assert runner.get("speech-test") is None
        assert scratch.exists()
    # A worker dying just after startup must not leave data until another restart.
    assert runner.get("speech-test") is None
    assert not scratch.exists()


def test_gateway_crash_kills_preparation_worker_and_its_conversion_child(tmp_path):
    import json
    import os
    worker = """import sys,subprocess,time
from pathlib import Path
from gateway.stt_measurement import _arm_parent_death
_arm_parent_death(int(sys.argv[1]))
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path(sys.argv[2]).write_text('ready')
time.sleep(30)
"""
    ready = tmp_path / "conversion-ready"
    gateway_code = f"""import os,sys,subprocess,time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', {worker!r}, str(os.getpid()), {str(ready)!r}], start_new_session=True)
while not Path({str(ready)!r}).exists(): time.sleep(.01)
os._exit(0)
"""
    async def scenario():
        process = await asyncio.create_subprocess_exec(sm.sys.executable, "-c", gateway_code,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        # Both descendants inherit the pipe. EOF proves that neither survives
        # the simulated abrupt gateway exit, without inspecting host processes.
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        assert ready.exists() and stdout == b""
    asyncio.run(scenario())


@pytest.mark.parametrize("status,observed,expected", [
    (500, False, "measurement_request_refused"), (500, True, "measurement_request_refused"),
    (413, False, "measurement_request_refused"), (413, True, "measurement_backend_refused"),
    (504, True, "measurement_timeout"),
])
def test_http_failure_requires_trusted_upstream_size_evidence(setup, monkeypatch, tmp_path, status, observed, expected):
    import httpx
    gw, _, _, _, ctx = setup
    _gateway_trial_config(gw, monkeypatch)
    async def post(*args, **kwargs):
        if observed:
            sm.observe_reply(status, "direct")
        return httpx.Response(status, json={"error": {"code": "stt_backend_error", "message": "private upstream words"}})
    install_async_client_stub(monkeypatch, post)
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"synthetic")
    async def scenario():
        token = sm.TRIAL.set({"service_id": "speech-test", "settings": {}})
        try:
            with pytest.raises(sm.EngineRouteError) as error:
                await gw.proxy._operator_stt_transcribe(ctx, "speech-test", None, audio)
            assert error.value.code == expected
            assert "private upstream" not in error.value.message
        finally:
            sm.TRIAL.reset(token)
    asyncio.run(scenario())


def test_http_transport_timeout_is_not_evidence_of_unsafe_length(setup, monkeypatch, tmp_path):
    import httpx
    gw, _, _, _, ctx = setup
    _gateway_trial_config(gw, monkeypatch)
    async def post(*args, **kwargs):
        raise httpx.ReadTimeout("private details")
    install_async_client_stub(monkeypatch, post)
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"synthetic")
    async def scenario():
        with pytest.raises(sm.EngineRouteError) as error:
            await gw.proxy._operator_stt_transcribe(ctx, "speech-test", None, audio)
        assert error.value.code == "measurement_timeout"
        assert "private details" not in error.value.message
    asyncio.run(scenario())


def test_proxy_gpu_credit_uses_only_the_selected_service_attribution(setup, monkeypatch):
    import status_endpoint
    gw, owner, _, runner, ctx = setup
    _, generation = runner.record("speech-test")
    saved = owner.patch("/admin/api/operator/services/speech-test",
        headers={**csrf_for(owner), "If-Match": f'"os-{generation}"'},
        json={"resources": {"gpu_required": True, "vram_gb_estimate": 4, "gpu": {"devices": [0]}}})
    assert saved.status_code == 200, saved.text
    monkeypatch.setattr(status_endpoint, "_gpus_raw", lambda: [{"index": 0, "memory_total_mb": 16000,
        "memory_used_mb": 15000, "instantaneous_util_pct": 0}])
    monkeypatch.setattr(gw.proxy, "_media_job_queue_depth", lambda: 0)
    attribution = {"per_gpu": [{"index": 0, "services": [{"service": "other-engine", "used_mb": 10000}]}]}
    def attribute(gpus, runtime_config):
        assert runtime_config is gw.proxy.config
        assert "speech-test" in runtime_config["backends"]
        assert runtime_config["backends"]["speech-test"]["resources"]["vram_gb_estimate"] == 4
        return attribution
    monkeypatch.setattr(status_endpoint, "_vram_attribution", attribute)
    async def scenario():
        assert not (await gw.proxy._operator_stt_admit(ctx, "speech-test"))["admitted"]
        attribution["per_gpu"][0]["services"].append({"service": "speech-test", "used_mb": 4096})
        assert (await gw.proxy._operator_stt_admit(ctx, "speech-test"))["admitted"]
    asyncio.run(scenario())


@pytest.mark.parametrize("sparse", [False, True])
def test_default_measurement_pairs_shared_deletions_and_reports_abstention(setup, monkeypatch, tmp_path, sparse, record_property):
    import random
    _, _, _, runner, ctx = setup
    seconds = 300
    references = [sm.measure.RefWord(f"word{i}", i * .4 + .01, i * .4 + .2) for i in range(750)]
    if sparse:
        references = references[:10]
    async def prepare(job, spec, folder, policy):
        path = folder / "audio.wav"
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(b'\0\0' * 16000 * seconds)
        return path, references
    bounds = []
    real_slice = sm._wave_slice
    def wave_slice(source, destination, start, end):
        bounds[:] = [start, end]
        real_slice(source, destination, start, end)
    async def transcribe(ctx, sid, model, path):
        trial = sm.TRIAL.get()
        settings = trial["settings"]
        start, end = bounds
        if settings["enabled"]:
            segments = stt_chunking.plan_quietest_frame_segments(duration_seconds=end-start,
                quietest_frame=lambda lo, hi, near: near, target_seconds=settings["target_seconds"],
                search_seconds=settings["search_seconds"], max_segment_seconds=settings["max_segment_seconds"], max_segments=512)
            trial["outcome"] = {"mode": "chunked", "attempts": len(segments)}
        rng = random.Random(f"{path.parent.name}:{start}:{settings['target_seconds']}:{settings.get('search_seconds', 0)}")
        return " ".join(w.word for i, w in enumerate(references) if start <= w.mid < end
                        and not (int(w.mid // 30) % 2 == 0 and i % 5 == 0) and rng.random() >= .015)
    monkeypatch.setattr(runner, "_prepare", prepare)
    monkeypatch.setattr(runner, "transcribe", transcribe)
    monkeypatch.setattr(sm, "_wave_slice", wave_slice)
    async def scenario():
        policy = runner.options()["policy"]
        job = _job(runner)
        job.update(sample_ids=policy["sample_ids"], lengths=policy["lengths"], search_widths=policy["search_widths"])
        report = await runner._measure(ctx, job, policy, tmp_path)
        record_property("measurement_report", json.dumps(report, sort_keys=True))
        assert job["completed"] <= policy["max_gateway_requests"]
        assert all(row["baseline_length"] == 20 for row in report["deletions_by_length"])
        assert "Gross-harm" in report["recommendation_note"]
        if sparse:
            assert report["recommendation"] is None and report["recommendation_state"] == "inconclusive"
        else:
            assert report["recommendation_state"] == "recommended"
            assert report["recommendation"]["max_segment_seconds"] == 50
            assert next(row for row in report["deletions_by_length"] if row["length"] == 60)["state"] != "recommended"
            assert report["recommendation"]["search_seconds"] == 1
            assert all(row["paired_deletion"]["words"] >= policy["recommendation"]["min_length_words"] for row in report["deletions_by_length"])
            assert any(row["high_pct"] - row["low_pct"] > 1 for row in report["deletions_by_length"])
    asyncio.run(scenario())


@pytest.mark.parametrize("required", [False, True])
def test_measurement_yields_to_live_stt_for_every_resource_type(required):
    record = {"resources": {"gpu_required": required, "vram_gb_estimate": 2}}
    result = sm.gpu_admission(record, None, None, {}, live_stt=1)
    assert result["admitted"] is False
    assert "live" in result["reason"].lower()


def test_cpu_measurement_observes_live_gate_through_public_activity(setup, monkeypatch, tmp_path):
    gw, _, _, runner, ctx = setup
    registry, _ = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
    registry["services"]["speech-test"]["config"]["resources"] = {"gpu_required": False}
    _put_registry(gw, registry)
    gate = stt_chunking.SttResourceGate()
    execution = stt_chunking.SttExecutionConfig.from_service(service_id="speech-test",
        service_config={"chunking": {"spool_dir": str(tmp_path), "min_free_bytes": 1}},
        config_directory=tmp_path)
    class PublicActivity:
        @property
        def active_count(self):
            return gate.active_count
    monkeypatch.setattr(stt_chunking, "GLOBAL_STT_RESOURCE_GATE", PublicActivity())
    async def scenario():
        lease = gate.acquire(request_id="live-user", config=execution)
        try:
            result = await gw.proxy._operator_stt_admit(ctx, "speech-test")
            assert result["admitted"] is False
        finally:
            lease.release()
        assert (await gw.proxy._operator_stt_admit(ctx, "speech-test"))["admitted"] is True
    asyncio.run(scenario())


def test_never_shipped_native_audio_key_is_rejected():
    with pytest.raises(sm.EngineRouteError) as error:
        sm.settings_patch({"native_long_audio": True})
    assert error.value.code == "invalid_stt_settings"


@pytest.mark.parametrize("invalid_saved", [False, True])
def test_measurement_options_use_only_mode_specific_native_settings(setup, invalid_saved):
    gw, owner, _, _, _ = setup
    if invalid_saved:
        registry, _ = gw.proxy.ADMIN_STORE.get_service_registry_snapshot()
        registry["services"]["speech-test"]["config"]["chunking"]["target_seconds"] = "invalid"
        _put_registry(gw, registry)
    response = owner.get("/admin/api/engines/speech-test/stt-measurements")
    assert response.status_code == 200
    settings = response.json()["settings"]
    assert "native_long_audio" not in settings
    assert "native_long_audio_offline" in settings
    assert "native_long_audio_streaming" in settings
