"""Canaries for temporary files (docs/design/REQUEST_RETENTION.md 16.2, rows 4-5 of 5.1; build slice 4).

A distinct marker rides in a multipart upload and in STT audio. While the request runs, the marker may sit only inside
the gateway's private temporary directory. After it ends, under every outcome, the marker is nowhere the process
wrote: not the private directory, not the operating system's default temporary directory, not the old STT spool, and
not an open or deleted file the process still holds. Temporary files never depend on what the request keeps, so the
"after" cell is the same for the kept and the forget policies.

Positive controls: each scan must find the marker during the request, or the "after" scan proves nothing.
"""

from __future__ import annotations

import asyncio
import io
import os
import stat
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

import proxy
from gateway import engine_process, log_scrub, private_tmp, provider_sidecar, request_forms, stt_chunking
from tests.support.access import access_context
from tests.support.fake_upstream import HandlerTransport, HostRouter

MARKER = "canary-temp-file-7c1f9e2a"
BIG = 2 * 1024 * 1024  # over Starlette's 1 MiB spill threshold


class World:
    """A private directory, an operating-system default directory and the old STT spool, all on the host disk."""

    def __init__(self, root: Path) -> None:
        self.os_tmp = root / "os-tmp"
        self.os_tmp.mkdir()
        self.legacy_spool = root / "data" / "stt-spool"
        self.legacy_spool.mkdir(parents=True)
        (root / "data").chmod(0o700)
        self.private = root / "data" / "private-tmp"

    @property
    def roots(self) -> list[Path]:
        return [self.private, self.os_tmp, self.legacy_spool]

    def files_holding(self, *roots: Path) -> list[Path]:
        forms = [form.encode() for form in log_scrub.forms(MARKER)]
        found = []
        for root in roots or self.roots:
            for path, _dirs, names in os.walk(root):
                for name in names:
                    candidate = Path(path, name)
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    data = candidate.read_bytes()
                    if any(form in data for form in forms):
                        found.append(candidate)
        return found

    def files_anywhere(self) -> list[Path]:
        return [Path(path, name) for root in self.roots for path, _dirs, names in os.walk(root) for name in names if name != ".owner.lock"]

    def held_files_holding(self) -> list[str]:
        """Open descriptors of this process (deleted files included) that point into a place under test and hold the marker.

        Only descriptors that point into the world's own directories are read: a full-suite process also holds many
        unrelated files, some large, and an anonymous spill file shows as ``<dir>/#<inode> (deleted)`` under its directory.
        """
        forms = [form.encode() for form in log_scrub.forms(MARKER)]
        prefixes = tuple(str(root) + os.sep for root in self.roots)
        found = []
        for entry in os.listdir("/proc/self/fd"):
            link = f"/proc/self/fd/{entry}"
            try:
                target = os.readlink(link)
                if not target.startswith(prefixes) or not stat.S_ISREG(os.stat(link).st_mode):
                    continue
                with open(link, "rb") as handle:  # reopening by /proc works for a deleted file
                    data = handle.read(8 * 1024 * 1024)
            except OSError:
                continue
            if any(form in data for form in forms):
                found.append(target)
        return found

    def assert_nothing_left(self) -> None:
        assert self.files_holding() == []
        assert self.held_files_holding() == []
        assert self.files_anywhere() == [], "every temporary file is removed when the request ends"


@pytest.fixture
def world(host_disk_dir: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = World(host_disk_dir)
    monkeypatch.setattr(tempfile, "tempdir", str(built.os_tmp))  # what /tmp is for this process before the gateway starts
    monkeypatch.setattr(private_tmp, "_active", None)
    monkeypatch.setattr(private_tmp, "_lock_fd", None)
    monkeypatch.setattr(private_tmp, "_lock_pid", None)
    monkeypatch.delenv(private_tmp.ENV_LOCK_FD, raising=False)
    monkeypatch.setenv(private_tmp.ENV_PREPARED, "")
    for name in ("TMPDIR", "TMP", "TEMP", private_tmp.ENV_DIR):
        monkeypatch.setenv(name, str(built.os_tmp))
    private_tmp.activate(built.private)
    yield built
    if private_tmp._lock_fd is not None:
        os.close(private_tmp._lock_fd)


@pytest.fixture
def cpu_stt_only():
    """The default CPU transcription service answers ``whisper-1``; the on-demand GPU engine ahead of it is removed."""
    with patch.dict(proxy.config["backends"]):
        proxy.config["backends"].pop("stt-parakeet-gpu", None)
        yield


def _multipart(content: bytes, *, filename: str = "upload.bin") -> tuple[bytes, str]:
    boundary = "canary-boundary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="note"\r\n\r\nhello\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _large_upload() -> bytes:
    return MARKER.encode() + b"." * BIG + MARKER.encode()


def _stt_wav() -> bytes:
    """A valid two-second 8 kHz mono WAV whose samples hold the marker."""
    frames = MARKER.encode() + b"\0" * (32_000 - 2 * len(MARKER)) + MARKER.encode()
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(frames[: len(frames) // 2 * 2])
    return out.getvalue()


# ---- multipart uploads: Starlette spills a part over 1 MiB ---------------------------------------------------------


def _upload_app(world: World, samples: list[dict], *, then: Callable[[], None] | None = None, keep: list | None = None) -> FastAPI:
    app = FastAPI()

    @app.post("/upload")
    async def upload(request: Request):
        form = await request_forms.read_form(request)
        samples.append({"held": world.held_files_holding(), "private": world.files_holding(world.private),
                        "elsewhere": world.files_holding(world.os_tmp, world.legacy_spool)})
        if keep is not None:
            keep.append(form)  # something still refers to the form when the request ends
        if then is not None:
            then()
        return JSONResponse({"size": len(await form["file"].read())})

    return app


async def _post(app: FastAPI, body: bytes, content_type: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        return await client.post("/upload", content=body, headers={"content-type": content_type})


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_a_large_multipart_part_lives_only_in_the_private_directory_and_is_gone_after(world: World, outcome: str) -> None:
    samples: list[dict] = []

    def fail() -> None:
        raise HTTPException(status_code=422, detail="refused after reading the form")

    body, content_type = _multipart(_large_upload())
    response = asyncio.run(_post(_upload_app(world, samples, then=fail if outcome == "failure" else None), body, content_type))
    assert response.status_code == (200 if outcome == "success" else 422)
    (during,) = samples
    assert during["held"], "positive control: the spilled part is held open during the request"
    assert all(target.startswith(str(world.private) + os.sep) for target in during["held"]), during["held"]
    assert during["elsewhere"] == []
    world.assert_nothing_left()


def test_spilled_parts_are_closed_when_the_request_ends_even_if_the_form_is_still_referenced(world: World) -> None:
    kept: list = []
    body, content_type = _multipart(_large_upload())
    assert asyncio.run(_post(_upload_app(world, [], keep=kept), body, content_type)).status_code == 200
    assert kept, "the form outlives the request"
    world.assert_nothing_left()


def test_a_client_that_hangs_up_mid_upload_leaves_nothing(world: World) -> None:
    body, content_type = _multipart(_large_upload())
    sent = 0
    mid_upload: list[list[str]] = []
    disconnect = asyncio.Event()

    async def receive():
        nonlocal sent
        if sent < 3:
            chunk = body[sent * 700_000:(sent + 1) * 700_000]
            sent += 1
            return {"type": "http.request", "body": chunk, "more_body": True}
        mid_upload.append(world.held_files_holding())
        disconnect.set()
        return {"type": "http.disconnect"}

    app = _upload_app(world, [])
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "path": "/upload", "raw_path": b"/upload",
             "query_string": b"", "root_path": "", "scheme": "http", "server": ("gateway", 80), "client": ("127.0.0.1", 1),
             "headers": [(b"content-type", content_type.encode()), (b"host", b"gateway")], "state": {}}

    async def send(_message):
        return None

    async def call() -> str:
        try:
            await app(scope, receive, send)
        except Exception as error:  # the framework reports a hang-up as ClientDisconnect
            return type(error).__name__
        return "answered"

    assert asyncio.run(call()) == "ClientDisconnect"
    assert disconnect.is_set()
    assert mid_upload and mid_upload[0] and all(t.startswith(str(world.private) + os.sep) for t in mid_upload[0])
    world.assert_nothing_left()


def test_a_small_part_never_touches_a_file(world: World) -> None:
    samples: list[dict] = []
    body, content_type = _multipart(MARKER.encode() + b"small")
    response = asyncio.run(_post(_upload_app(world, samples), body, content_type))
    assert response.status_code == 200
    assert samples[0]["held"] == [] and samples[0]["private"] == []
    world.assert_nothing_left()


# ---- STT: the request workspace -------------------------------------------------------------------------------------


def _stt_config(world: World, **chunking: object) -> stt_chunking.SttExecutionConfig:
    settings = {
        "threshold_seconds": 30, "target_seconds": 20, "max_input_bytes": 8 * 1024 * 1024, "max_decoded_bytes": 64 * 1024 * 1024,
        "max_duration_seconds": 120, "min_free_bytes": 1, "global_concurrency": 2, "backend_timeout_seconds": 30,
        "deadline_seconds": 60, **chunking,
    }
    config = stt_chunking.SttExecutionConfig.from_service(
        service_id="stt-test",
        service_config={"host": "127.0.0.1", "port": 9876, "default_model": "local-stt", "chunking": settings},
        config_directory=world.private.parent,
    )
    assert config.spool_dir == world.private / "stt"
    return config


def _stt_request(body: bytes, content_type: str, receive) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
                    "headers": [(b"content-type", content_type.encode())]}, receive)


def _whole_body_then(tail: Callable[[], object] | None = None):
    body, content_type = _multipart(_stt_wav(), filename="audio.wav")
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if tail is not None:
            return await tail()
        await asyncio.Future()

    return _stt_request(body, content_type, receive)


async def _run_stt(config: stt_chunking.SttExecutionConfig, request: Request) -> proxy.Response:
    with (
        patch.object(proxy, "_stt_execution_snapshot", return_value=config),
        patch.object(stt_chunking, "GLOBAL_STT_RESOURCE_GATE", stt_chunking.SttResourceGate()),
        patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(2, 8000, 1))),
        patch.object(proxy.receipt_store, "begin"),
        patch.object(proxy.receipt_store, "finalize", return_value=True),
    ):
        return await proxy.proxy_stt_request(request, access_context())


ENGINE = "127.0.0.1:9876"  # the host and port _stt_config gives the transcription service


@pytest.mark.parametrize("outcome", ["success", "engine_failure"])
def test_stt_audio_lives_only_in_the_private_directory_and_is_gone_after(
    world: World, cpu_stt_only: None, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    during: list[dict] = []

    async def engine(outgoing: httpx.Request) -> httpx.Response:
        await outgoing.aread()
        during.append({"private": world.files_holding(world.private), "elsewhere": world.files_holding(world.os_tmp, world.legacy_spool),
                       "held": world.held_files_holding()})
        if outcome == "engine_failure":
            return httpx.Response(500, content=b'{"error":"engine failed"}', headers={"content-type": "application/json"})
        return httpx.Response(200, content=b'{"text":"ok","segments":[]}', headers={"content-type": "application/json"})

    HostRouter(monkeypatch).add_transport(ENGINE, HandlerTransport(engine))
    response = asyncio.run(_run_stt(_stt_config(world), _whole_body_then()))
    assert response.status_code == (200 if outcome == "success" else 500), response.body
    assert during, "the engine was reached"
    first = during[0]
    assert first["private"], "positive control: the uploaded audio is in a file under the private directory during the request"
    assert all(path.is_relative_to(world.private / "stt") for path in first["private"])
    assert first["elsewhere"] == []
    assert all(target.startswith(str(world.private) + os.sep) for target in first["held"])
    world.assert_nothing_left()


def test_stt_audio_is_gone_after_a_cancelled_request(world: World, cpu_stt_only: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> proxy.Response:
        disconnect = asyncio.Event()
        engine_called = asyncio.Event()

        async def engine(outgoing: httpx.Request) -> httpx.Response:
            await outgoing.aread()
            engine_called.set()
            await asyncio.Future()  # the engine never answers
            raise AssertionError("unreachable")

        HostRouter(monkeypatch).add_transport(ENGINE, HandlerTransport(engine))

        async def tail():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        task = asyncio.create_task(_run_stt(_stt_config(world, enabled=False, native_long_audio_offline=True), _whole_body_then(tail)))
        await asyncio.wait_for(engine_called.wait(), 5)
        assert world.files_holding(world.private), "positive control: the audio is on disk while the engine call is pending"
        assert world.files_holding(world.os_tmp, world.legacy_spool) == []
        disconnect.set()
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(scenario()).status_code == 499
    world.assert_nothing_left()


def test_stt_upload_cut_off_mid_body_leaves_nothing(world: World, cpu_stt_only: None) -> None:
    body, content_type = _multipart(_stt_wav(), filename="audio.wav")

    async def scenario() -> proxy.Response:
        halves = iter((body[: len(body) // 2],))
        disconnect = asyncio.Event()

        async def receive():
            try:
                return {"type": "http.request", "body": next(halves), "more_body": True}
            except StopIteration:
                await disconnect.wait()
                return {"type": "http.disconnect"}

        task = asyncio.create_task(_run_stt(_stt_config(world), _stt_request(body, content_type, receive)))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if world.files_holding(world.private):
                break
        assert world.files_holding(world.private), "positive control: the half-received audio is on disk"
        assert world.files_holding(world.os_tmp, world.legacy_spool) == []
        disconnect.set()
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(scenario()).status_code == 499
    world.assert_nothing_left()


# ---- child processes write where the gateway writes ----------------------------------------------------------------------


def test_children_the_gateway_starts_write_temporary_files_to_the_private_directory(world: World) -> None:
    probe = "import os, tempfile\nwith tempfile.NamedTemporaryFile() as handle:\n    print(os.path.dirname(handle.name))"

    async def media_child() -> str:
        # ffmpeg and ffprobe for STT run through this function, with the gateway's own environment.
        out, _err = await stt_chunking.run_media_command(
            [sys.executable, "-c", probe], timeout_seconds=10, cancellation_waiters=(), deadline_at=time.monotonic() + 60, on_cancel=None)
        return out.decode().strip()

    assert asyncio.run(media_child()) == str(world.private)
    # An engine the gateway starts, and the LiteLLM sidecar, are given the same directory.
    assert engine_process.inherited_command_environment()["TMPDIR"] == str(world.private)
    sidecar = provider_sidecar.build_sidecar_env(db_path=world.private.parent / "admin.db", registry={})
    assert {sidecar[name] for name in ("TMPDIR", "TMP", "TEMP")} == {str(world.private)}


# ---- every form read goes through the closing helper -----------------------------------------------------------------


def test_no_route_reads_a_form_without_closing_it() -> None:
    """A direct ``request.form()`` leaves its spilled files open until garbage collection; ``read_form`` closes them."""
    root = Path(__file__).resolve().parent.parent
    allowed = {  # file: how many direct calls may remain, and why
        "gateway/mcp_oauth.py": 2,  # urlencoded forms: no file parts, nothing spills
    }
    found: dict[str, int] = {}
    for path in [root / "proxy.py", *sorted((root / "gateway").rglob("*.py"))]:
        if path.name == "request_forms.py":
            continue
        count = path.read_text(encoding="utf-8").count("request.form(")
        if count:
            found[str(path.relative_to(root))] = count
    assert found == allowed


@pytest.mark.parametrize("reader", [request_forms.read_form, proxy._read_video_create_form_body, proxy._read_video_request_body])
@pytest.mark.parametrize("failure", ["disconnect", "cancel"])
def test_interrupted_multipart_closes_spill_with_traceback_kept(world: World, reader, failure: str) -> None:
    body, content_type = _multipart(_large_upload())
    retained = []

    async def scenario():
        sent = False
        waiting = asyncio.Event()

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body[:-100], "more_body": True}
            assert world.held_files_holding(), "positive control: spill before interruption"
            waiting.set()
            if failure == "disconnect":
                return {"type": "http.disconnect"}
            await asyncio.Future()

        request = Request({"type": "http", "method": "POST", "path": "/upload",
                           "headers": [(b"content-type", content_type.encode())]}, receive)
        task = asyncio.create_task(reader(request))
        await asyncio.wait_for(waiting.wait(), 5)
        if failure == "cancel":
            task.cancel()
        try:
            await task
        except BaseException as error:
            retained.append(error)
        assert retained and retained[0].__traceback__ is not None
        world.assert_nothing_left()

    asyncio.run(scenario())
    assert retained  # traceback remains alive after the assertion, too


def test_video_probe_cancellation_waits_for_private_scratch_cleanup(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = threading.Event(), threading.Event()
    paths = []

    def probe(command, **kwargs):
        paths.append(Path(command[-1]))
        started.set()
        assert release.wait(5)
        return type("Result", (), {"returncode": 1, "stdout": b""})()

    monkeypatch.setattr(proxy.shutil, "which", lambda name: "/fake/ffprobe")
    monkeypatch.setattr(proxy.subprocess, "run", probe)
    monkeypatch.setattr(proxy, "_resolve_video_workflow_asset", AsyncMock(return_value=(MARKER.encode(), "source.mp4", "video/mp4")))

    async def scenario():
        task = asyncio.create_task(proxy._resolve_video_workflow_assets(
            {"extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"source": "a video"}}},
            {"inputs": {"source": {"type": "asset"}},
             "bindings": {proxy._GRAPH_BINDING: {"map": {"source": "1.file"}}}},
            {"source_video_input": "source"}, None))
        for _ in range(1000):
            if started.is_set():
                break
            if task.done():
                await task
                raise AssertionError("request did not probe the video")
            await asyncio.sleep(0.001)
        assert started.is_set(), "video probe did not start"
        try:
            assert paths[0].is_file()
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), "cancel must wait for the worker's finally block"
            assert all(path.is_relative_to(world.private) for path in paths)
        finally:
            release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert task.cancelled()
        assert all(not path.exists() for path in paths)
        world.assert_nothing_left()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["mp3", "opus", "peak", "sample"])
def test_conversion_cancellation_waits_for_cleanup_even_when_repeated(world: World, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    from gateway import voice_audio

    started, release = threading.Event(), threading.Event()
    paths = []

    def convert(command, **kwargs):
        paths.append(Path(command[command.index("-i") + 1]))
        started.set()
        assert release.wait(5)
        raise RuntimeError("conversion failed after cancellation")

    monkeypatch.setattr(proxy.subprocess, "run", convert)
    function, args = {
        "mp3": (proxy.convert_wav_to_mp3, (MARKER.encode(),)),
        "opus": (proxy.convert_wav_to_audio_format, (MARKER.encode(), "opus")),
        "peak": (proxy._peak_normalize_audio, (MARKER.encode(), "audio/wav", -1)),
        "sample": (voice_audio.process_sample, (MARKER.encode(), voice_audio.SampleLimits())),
    }[kind]

    async def scenario():
        task = asyncio.create_task(private_tmp.run_worker(function, *args))
        for _ in range(1000):
            if started.is_set():
                break
            if task.done():
                await task
            await asyncio.sleep(0.001)
        try:
            assert started.is_set() and world.files_holding(world.private)
            assert paths and all(path.is_relative_to(world.private) for path in paths)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0.01)
                assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        world.assert_nothing_left()

    asyncio.run(scenario())
