"""Every child process the gateway starts, and /health's SQLite read, run off the event loop thread.

On uvloop a spawn forks the whole gateway on the loop thread, which takes about 0.05 ms per MB of
resident memory, and the loop waits for the fork. Each test here records where a spawn (a
subprocess.Popen call) or a read starts, and fails when it starts on the loop thread. Each site runs
with a fake binary or a short script, so the spawn itself is real. None of these tests is a
wall-clock bound, so host load cannot flip them.
"""
import asyncio
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import lifecycle_drivers, speech_stream, stt_chunking, stt_measurement


def _on_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _record_spawns(monkeypatch) -> list[bool]:
    """One entry per subprocess.Popen call: True when the call ran on the event loop thread."""
    on_loop: list[bool] = []
    real_popen = subprocess.Popen

    class RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            on_loop.append(_on_loop_thread())
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    return on_loop


def _executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _put_first_on_path(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")


def _record_kills(monkeypatch) -> list[tuple[int, int]]:
    """The hermetic sandbox fails every os.kill, so a kill is recorded here instead of sent: (pid, signal) per call."""
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    return killed


def _substitute_command(monkeypatch, command: str, stand_in: str) -> None:
    """The hermetic sandbox refuses to run host-control commands such as systemctl, so a call to `command` runs stand_in."""
    from gateway import off_loop_spawn

    real_run = off_loop_spawn.run

    async def run(argv, **kwargs):
        return await real_run([stand_in if argv[0] == command else argv[0], *argv[1:]], **kwargs)

    monkeypatch.setattr(off_loop_spawn, "run", run)


# --- the helper ----------------------------------------------------------------------------------

def test_start_spawns_the_child_on_a_worker_thread_and_its_pipes_work(monkeypatch):
    from gateway import off_loop_spawn

    on_loop = _record_spawns(monkeypatch)

    async def scenario():
        child = await off_loop_spawn.start(["cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        child.stdin.write(b"ping")
        await child.stdin.drain()
        child.stdin.close()
        return await child.stdout.read(), await child.wait()

    assert asyncio.run(scenario()) == (b"ping", 0)
    assert on_loop and not any(on_loop), "the spawn ran on the event loop thread"


def test_run_returns_the_exit_code_and_output_from_a_worker_thread(monkeypatch):
    from gateway import off_loop_spawn

    on_loop = _record_spawns(monkeypatch)

    result = asyncio.run(off_loop_spawn.run(["sh", "-c", "printf out; printf err >&2; exit 3"], capture_output=True))

    assert (result.returncode, result.stdout, result.stderr) == (3, b"out", b"err")
    assert on_loop and not any(on_loop), "the spawn ran on the event loop thread"


def test_a_child_that_outlives_run_timeout_is_killed_and_reported(monkeypatch):
    from gateway import off_loop_spawn

    killed = _record_kills(monkeypatch)
    with pytest.raises(subprocess.TimeoutExpired):
        asyncio.run(off_loop_spawn.run(["sleep", "1"], timeout=0.1))
    assert len(killed) == 1, "the child past its timeout was not killed"


def test_a_start_cancelled_while_it_spawns_kills_the_child_it_started(monkeypatch):
    """The caller is cancelled while the worker is still spawning. The child that spawn produces must not outlive the
    cancelled request: it is killed and reaped."""
    from gateway import off_loop_spawn

    real_popen = subprocess.Popen
    started, release = threading.Event(), threading.Event()
    spawned: list[subprocess.Popen] = []

    class SlowPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)  # the child exists from here on
            spawned.append(self)
            started.set()
            release.wait(10)  # the spawn finishes only after the caller has been cancelled

    monkeypatch.setattr(subprocess, "Popen", SlowPopen)
    killed = _record_kills(monkeypatch)

    async def scenario():
        task = asyncio.ensure_future(off_loop_spawn.start(["sleep", "1"]))
        await asyncio.to_thread(started.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(500):
            if spawned and spawned[0].poll() is not None:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert spawned and [pid for pid, _ in killed] == [spawned[0].pid], "a cancelled start did not kill its child"
    assert spawned[0].poll() is not None, "the killed child was not reaped"


# --- the sites -----------------------------------------------------------------------------------

def test_the_systemctl_call_on_the_swap_path_runs_off_the_loop(monkeypatch, tmp_path):
    on_loop = _record_spawns(monkeypatch)
    stand_in = _executable(tmp_path / "stand-in-for-systemctl", "exit 0")
    _substitute_command(monkeypatch, "systemctl", str(stand_in))

    ok = asyncio.run(lifecycle_drivers.SystemdDriver()._run_systemctl(["systemctl", "--user", "start", "x.service"]))

    assert ok is True
    assert on_loop and not any(on_loop), "systemctl was spawned on the event loop thread"


def test_the_speech_transcoder_spawns_ffmpeg_off_the_loop(monkeypatch, tmp_path):
    on_loop = _record_spawns(monkeypatch)
    _put_first_on_path(monkeypatch, _fake_bin_dir(tmp_path, {"ffmpeg": "cat"}))

    async def chunks():
        yield b"\x00\x01" * 240

    async def collect():
        return [piece async for piece in speech_stream.transcode_pcm_stream(chunks(), src_rate=24000, response_format="mp3")]

    assert asyncio.run(collect())
    assert on_loop and not any(on_loop), "ffmpeg was spawned on the event loop thread"


def test_the_stt_media_command_is_spawned_off_the_loop(monkeypatch):
    on_loop = _record_spawns(monkeypatch)

    async def scenario():
        return await stt_chunking.run_media_command(
            ["sh", "-c", "printf ok"], timeout_seconds=10, cancellation_waiters=(),
            deadline_at=time.monotonic() + 10, on_cancel=None)

    stdout, _ = asyncio.run(scenario())

    assert stdout == b"ok"
    assert on_loop and not any(on_loop), "the STT media command was spawned on the event loop thread"


def test_the_measurement_sample_process_is_spawned_off_the_loop(monkeypatch, tmp_path):
    """The sample process is this interpreter running `-m gateway.stt_measurement prepare`. A stand-in
    interpreter writes an empty reference and exits 0, which is all _prepare reads back."""
    on_loop = _record_spawns(monkeypatch)
    stand_in = _executable(tmp_path / "python", 'mkdir -p "$5"; printf "[]" > "$5/reference.json"')
    monkeypatch.setattr(sys, "executable", str(stand_in))
    measurements = stt_measurement.Measurements(SimpleNamespace(path=tmp_path / "admin.db"), None)
    monkeypatch.setattr(measurements, "_save", lambda job: None)

    async def scenario():
        return await measurements._prepare(
            {"id": "job-1", "service_id": "svc"}, SimpleNamespace(id="sample-1"), tmp_path / "ws", {"max_download_bytes": 1000})

    _, words = asyncio.run(scenario())

    assert words == []
    assert on_loop and not any(on_loop), "the measurement sample was spawned on the event loop thread"


def test_the_workflow_drift_check_is_spawned_off_the_loop(monkeypatch, tmp_path):
    on_loop = _record_spawns(monkeypatch)
    script = tmp_path / "check_workflow_drift.py"
    script.write_text("print('no drift')\n", encoding="utf-8")
    monkeypatch.setattr(proxy, "_DRIFT_SCRIPT_PATH", script)

    result = asyncio.run(proxy._run_workflow_drift_once())

    assert result["status"] == "ok"
    assert on_loop and not any(on_loop), "the drift check was spawned on the event loop thread"


# --- /health -------------------------------------------------------------------------------------

def test_health_reads_the_app_credential_count_off_the_event_loop(monkeypatch):
    seen_on_loop: list[bool] = []

    def count() -> int:
        seen_on_loop.append(_on_loop_thread())
        return 0

    monkeypatch.setattr(proxy, "active_app_credential_count", count)

    response = TestClient(proxy.app).get("/health")

    assert response.status_code == 200
    assert seen_on_loop and not any(seen_on_loop), "/health read SQLite on the event loop thread"


def _fake_bin_dir(tmp_path: Path, scripts: dict[str, str]) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for name, body in scripts.items():
        _executable(directory / name, body)
    return directory
