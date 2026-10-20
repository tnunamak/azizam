"""Round 4 regressions for saved settings and decoded-duration admission."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gateway import stt_chunking as stt, stt_chunking_upgrade as upgrade


def config(root, **changes):
    return stt.SttExecutionConfig.from_service(
        service_id="test", config_directory=root,
        service_config={"chunking": {"spool_dir": str(root / "spool"), "target_seconds": 30,
            "threshold_seconds": 40, "max_segment_seconds": 40, **changes}},
    )


def test_only_per_mode_native_keys_are_exposed(tmp_path):
    execution = config(tmp_path, native_long_audio=True)
    assert not hasattr(execution, "native_long_audio")
    assert "native_long_audio" not in execution.chunking_view()
    assert execution.native_long_audio_offline is False
    assert execution.native_long_audio_streaming is False


@pytest.mark.parametrize("threshold,target", [(110, 60), (180, 150)])
def test_saved_threshold_controls_cutting_without_declared_max(tmp_path, threshold, target):
    execution = config(tmp_path, threshold_seconds=threshold, target_seconds=target, max_segment_seconds=None)
    assert execution.chunk_above_seconds == threshold


def test_gate_reports_active_count_without_allowing_assignment(tmp_path):
    gate = stt.SttResourceGate()
    assert gate.active_count == 0
    with patch.object(stt, "_disk_free_bytes", return_value=10**12):
        lease = gate.acquire(request_id="live", config=config(tmp_path))
    assert gate.active_count == 1
    with pytest.raises(AttributeError):
        gate.active_count = 0
    lease.release()
    assert gate.active_count == 0


def test_threshold_uses_decoded_length_and_reuses_the_decode(tmp_path):
    async def run():
        source = tmp_path / "vbr.mp3"
        source.write_bytes(b"upload")
        pieces = []
        async def decode(_source, destination, **_kwargs):
            destination.write_bytes(b"\0\0" * (190 * 16000))
        async def extract(_source, destination, *, start_seconds, end_seconds, **_kwargs):
            pieces.append((start_seconds, end_seconds))
            destination.write_bytes(b"piece")
        with patch.object(stt, "probe_audio", new=AsyncMock(return_value=stt.ProbedAudio(34.2, 16000, 1))), \
             patch.object(stt, "decode_planning_pcm", new=AsyncMock(side_effect=decode)) as decoder, \
             patch.object(stt, "extract_segment", new=AsyncMock(side_effect=extract)):
            outcome = await stt.execute_chunking(source_path=source, source_stem="vbr", fields=[],
                config=config(tmp_path), post_file=AsyncMock(return_value=stt.UpstreamResponse(200, b'{"text":"x"}', {})),
                cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time()+10, on_cancel=None)
        assert outcome.mode == "chunked"
        assert pieces[-1][1] == 190
        assert max(end-start for start,end in pieces) <= 40
        decoder.assert_awaited_once()
    asyncio.run(run())


def test_uncut_refusal_uses_decoded_length_and_cleans_pcm(tmp_path):
    async def run():
        async def decode(_source, destination, **_kwargs):
            destination.write_bytes(b"\0\0" * (190 * 16000))
        with patch.object(stt, "probe_audio", new=AsyncMock(return_value=stt.ProbedAudio(34.2, 16000, 1))), \
             patch.object(stt, "decode_planning_pcm", new=AsyncMock(side_effect=decode)):
            with pytest.raises(stt.SttAudioNotSplittable):
                await stt.guard_uncut_uploads([tmp_path / "upload.mp3"], config=config(tmp_path),
                    cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time()+10, on_cancel=None)
        assert not list(tmp_path.glob("*.pcm"))
    asyncio.run(run())


@pytest.mark.parametrize("service,seed", [("stt", 120), ("stt-canary", 40), ("stt-nemotron", 30)])
def test_upgrade_drops_only_seeded_unused_search(service, seed):
    rules = upgrade.load_upgrades()
    original = {"silence_search_seconds": seed, "threshold_seconds": 777, "custom": "kept"}
    assert upgrade.upgraded_chunking(service, original, rules) == {"threshold_seconds": 777, "custom": "kept"}
    assert upgrade.upgraded_chunking(service, {**original, "silence_search_seconds": seed+1}, rules) is None
    assert upgrade.upgraded_chunking("unrelated", original, rules) is None


def test_migration_audit_names_actual_schema(tmp_path):
    import json
    import sqlite3
    from gateway import admin_store
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    registry = {"services": {"stt-parakeet-gpu": {"config": {"chunking": {
        "enabled": True, "target_seconds": 30, "threshold_seconds": 40, "silence_search_seconds": 10}}}}}
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('service_registry',?,0)",
                     (json.dumps(registry),))
        with patch.object(admin_store, "_record_audit_in_transaction") as audit:
            upgrade.migrate_registry(conn)
        assert audit.call_args.kwargs["actor_label"] == "Schema 15 migration"


def test_planning_decode_stops_at_the_existing_decoded_byte_budget(tmp_path):
    import shutil
    import wave
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required for the bounded decode fixture")
    source, destination = tmp_path / "long.wav", tmp_path / "bounded.pcm"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * (2 * 16000))
    async def run():
        with pytest.raises(stt.SttResourceLimitExceeded):
            await stt.decode_planning_pcm(source, destination,
                config=config(tmp_path, max_decoded_bytes=16000), cancellation_waiters=(),
                deadline_at=asyncio.get_running_loop().time()+10, on_cancel=None)
    asyncio.run(run())
    assert 16000 < destination.stat().st_size <= 16002
