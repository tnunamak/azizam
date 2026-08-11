"""Hermetic contracts for generic, bounded long-audio STT execution."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import struct
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

import proxy
from gateway import stt_chunking
from gateway import service_registry


def _config(root: Path, **overrides: object) -> stt_chunking.SttExecutionConfig:
    chunking = {
        "spool_dir": str(root / "spool"),
        "threshold_seconds": 30,
        "target_seconds": 20,
        "max_input_bytes": 4 * 1024 * 1024,
        "max_decoded_bytes": 8 * 1024 * 1024,
        "max_duration_seconds": 120,
        "max_sample_rate": 16_000,
        "max_channels": 2,
        "max_segments": 8,
        "min_free_bytes": 1,
        "global_concurrency": 2,
        "backend_timeout_seconds": 30,
        "deadline_seconds": 60,
        "setup_failure_policy": "passthrough",
    }
    chunking.update(overrides)
    return stt_chunking.SttExecutionConfig.from_service(
        service_id="stt-test",
        service_config={
            "host": "127.0.0.1",
            "port": 9876,
            "default_model": "local-stt",
            "chunking": chunking,
        },
        config_directory=root,
    )


def _multipart_body(
    *,
    filename: str = "audio.wav",
    content: bytes = b"RIFFtest",
    fields: tuple[tuple[str, str], ...] = (("model", "whisper-1"),),
) -> tuple[bytes, str]:
    boundary = "stt-test-boundary"
    field_body = b"".join(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
        for name, value in fields
    )
    body = field_body + (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class SttChunkMergeTests(unittest.TestCase):
    def test_merges_the_two_sdk_usage_variants(self) -> None:
        duration = stt_chunking.merge_transcription_payloads([
            {"text": "first", "usage": {"type": "duration", "seconds": 1.25}},
            {"text": "second", "usage": {"type": "duration", "seconds": 2.75}},
        ])
        self.assertEqual(duration, {
            "text": "first second",
            "usage": {"type": "duration", "seconds": 4.0},
        })
        tokens = stt_chunking.merge_transcription_payloads([
            {
                "text": "one",
                "usage": {
                    "type": "tokens",
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                    "input_token_details": {"audio_tokens": 1, "text_tokens": 1},
                },
            },
            {
                "text": "two",
                "usage": {
                    "type": "tokens",
                    "input_tokens": 7,
                    "output_tokens": 11,
                    "total_tokens": 18,
                    "input_token_details": {"audio_tokens": 4, "text_tokens": 3},
                },
            },
        ])
        self.assertEqual(tokens["usage"]["total_tokens"], 23)
        self.assertEqual(tokens["usage"]["input_token_details"], {"audio_tokens": 5, "text_tokens": 4})

    def test_rejects_sdk_fields_that_need_richer_merge_semantics(self) -> None:
        with self.assertRaises(stt_chunking.SttChunkResponseError):
            stt_chunking._parse_mergeable_chunk_payload(b'{"text":"x","logprobs":[]}')
        self.assertFalse(stt_chunking.request_supports_chunking(
            [("response_format", "json"), ("chunking_strategy", "auto")], file_count=1
        ))
        self.assertFalse(stt_chunking.request_supports_chunking(
            [("response_format", "verbose_json")], file_count=1
        ))

    def test_silence_only_plan_never_hard_cuts(self) -> None:
        self.assertIsNone(stt_chunking.plan_silence_only_segments(
            duration_seconds=80,
            silences=[(18, 19)],
            target_seconds=20,
            search_seconds=8,
        ))
        segments = stt_chunking.plan_silence_only_segments(
            duration_seconds=80,
            silences=[(19, 21), (39, 41), (59, 61)],
            target_seconds=20,
            search_seconds=8,
        )
        self.assertEqual(segments, [(0.0, 20.0), (20.0, 40.0), (40.0, 80)])


class BoundedMultipartTests(unittest.IsolatedAsyncioTestCase):
    async def test_spools_to_configured_disk_root_and_closes_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, max_input_bytes=1024)
            body, content_type = _multipart_body(content=b"a" * 512)
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Future()

            request = Request({"type": "http", "method": "POST", "headers": [(b"content-type", content_type.encode())]}, receive)
            parsed = await stt_chunking.parse_bounded_multipart(request, config)
            _fields, files = stt_chunking.upload_file_parts(parsed.form)
            path = stt_chunking.source_path(files[0][1])
            self.assertTrue(path.is_relative_to(config.spool_dir))
            self.assertEqual(path.stat().st_size, 512)
            await parsed.aclose()
            self.assertFalse(path.exists())

    async def test_rejects_before_a_large_file_becomes_a_bytes_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _config(Path(temp_dir), max_input_bytes=64)
            body, content_type = _multipart_body(content=b"a" * 65)
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Future()

            request = Request({"type": "http", "method": "POST", "headers": [(b"content-type", content_type.encode())]}, receive)
            with self.assertRaises(stt_chunking.MultiPartException):
                await stt_chunking.parse_bounded_multipart(request, config)
            self.assertEqual(list(config.spool_dir.glob("*")) if config.spool_dir.exists() else [], [])


class ChunkExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_child_is_killed_and_reaped_on_task_cancellation(self) -> None:
        class FakeProcess:
            returncode: int | None = None
            killed = False
            reaped = False
            entered = asyncio.Event()

            async def communicate(self):
                self.entered.set()
                await asyncio.Future()

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            async def wait(self) -> None:
                self.reaped = True

        process = FakeProcess()
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            task = asyncio.create_task(stt_chunking.run_media_command(
                ["local-media-command"],
                timeout_seconds=61,
                cancellation_waiters=(),
                deadline_at=asyncio.get_running_loop().time() + 62,
                on_cancel=None,
            ))
            await asyncio.wait_for(process.entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertTrue(process.killed)
        self.assertTrue(process.reaped)

    async def test_partial_failure_never_replays_the_whole_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            source = root / "source.wav"
            source.write_bytes(b"source")
            attempts: list[str] = []

            async def extract(_source, destination, **_kwargs):
                Path(destination).write_bytes(b"RIFFchunk")

            async def post(path: Path, filename: str):
                attempts.append(filename)
                if len(attempts) == 1:
                    return stt_chunking.UpstreamResponse(200, b'{"text":"first"}', {})
                raise httpx.ConnectError("offline", request=httpx.Request("POST", "http://test.invalid"))

            with (
                patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(75, 8000, 1))),
                patch.object(stt_chunking, "detect_silences", new=AsyncMock(return_value=[(19, 21), (39, 41), (59, 61)])),
                patch.object(stt_chunking, "extract_segment", new=AsyncMock(side_effect=extract)),
            ):
                with self.assertRaises(stt_chunking.SttChunkAttemptError):
                    await stt_chunking.execute_chunking(
                        source_path=source,
                        source_stem="source",
                        fields=[("response_format", "json")],
                        config=config,
                        post_file=post,
                        cancellation_waiters=(),
                        deadline_at=asyncio.get_running_loop().time() + 10,
                        on_cancel=None,
                    )
            self.assertEqual(attempts, ["source.chunk000.wav", "source.chunk001.wav"])
            self.assertFalse(any(root.glob("segment-*.wav")))


class MediaLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_enforces_duration_rate_channel_and_decoded_byte_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            source.write_bytes(b"not examined by the fake probe")
            cases = (
                ("duration", _config(root, max_duration_seconds=60), 61, 8000, 1),
                ("sample rate", _config(root, max_sample_rate=8000), 1, 8001, 1),
                ("channels", _config(root, max_channels=1), 1, 8000, 2),
                ("decoded bytes", _config(root, max_decoded_bytes=10_000), 1, 8000, 1),
            )
            for name, config, duration, sample_rate, channels in cases:
                with self.subTest(name=name):
                    payload = json.dumps({
                        "format": {"duration": str(duration)},
                        "streams": [{
                            "codec_type": "audio",
                            "sample_rate": str(sample_rate),
                            "channels": channels,
                        }],
                    }).encode()
                    with patch.object(
                        stt_chunking,
                        "run_media_command",
                        new=AsyncMock(return_value=(payload, b"")),
                    ):
                        with self.assertRaises(stt_chunking.SttResourceLimitExceeded):
                            await stt_chunking.probe_audio(
                                source,
                                config=config,
                                cancellation_waiters=(),
                                deadline_at=asyncio.get_running_loop().time() + 1,
                                on_cancel=None,
                            )

    async def test_segment_limit_and_deadline_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            source.write_bytes(b"source")
            config = _config(root, max_segments=2)
            with (
                patch.object(
                    stt_chunking,
                    "probe_audio",
                    new=AsyncMock(return_value=stt_chunking.ProbedAudio(75, 8000, 1)),
                ),
                patch.object(
                    stt_chunking,
                    "detect_silences",
                    new=AsyncMock(return_value=[(19, 21), (39, 41)]),
                ),
            ):
                with self.assertRaises(stt_chunking.SttResourceLimitExceeded):
                    await stt_chunking.execute_chunking(
                        source_path=source,
                        source_stem="source",
                        fields=[("response_format", "json")],
                        config=config,
                        post_file=AsyncMock(),
                        cancellation_waiters=(),
                        deadline_at=asyncio.get_running_loop().time() + 1,
                        on_cancel=None,
                    )
            with self.assertRaises(stt_chunking.SttDeadlineExceeded):
                await stt_chunking.await_with_cancellation(
                    asyncio.Future(),
                    deadline_at=asyncio.get_running_loop().time() + 0.01,
                )

    async def test_real_offline_ffmpeg_fixture_covers_segments_and_cleanup(self) -> None:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("ffmpeg and ffprobe are required for the local media fixture")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, max_decoded_bytes=2 * 1024 * 1024)
            source = root / "fixture.wav"
            _write_silenced_wav(source)
            uploaded: list[bytes] = []

            async def post(path: Path, filename: str):
                self.assertTrue(filename.endswith(".wav"))
                content = path.read_bytes()
                self.assertEqual(content[:4], b"RIFF")
                uploaded.append(content)
                return stt_chunking.UpstreamResponse(200, json.dumps({"text": f"part-{len(uploaded)}"}).encode(), {})

            outcome = await stt_chunking.execute_chunking(
                source_path=source,
                source_stem="fixture",
                fields=[("response_format", "json")],
                config=config,
                post_file=post,
                cancellation_waiters=(),
                deadline_at=asyncio.get_running_loop().time() + 30,
                on_cancel=None,
            )
            self.assertEqual(outcome.mode, "chunked")
            self.assertEqual(outcome.merged_payload, {"text": "part-1 part-2 part-3"})
            self.assertEqual(len(uploaded), 3)
            self.assertFalse(any(root.glob("segment-*.wav")))


class ResourceGateTests(unittest.TestCase):
    def test_global_concurrency_rejects_before_spooling_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _config(
                Path(temp_dir),
                global_concurrency=1,
                max_input_bytes=1024,
                max_decoded_bytes=1024,
            )
            gate = stt_chunking.SttResourceGate()
            first = gate.acquire(
                request_id="first",
                config=config,
            )
            with self.assertRaises(stt_chunking.SttResourceLimitExceeded):
                gate.acquire(request_id="second", config=config)
            first.release()
            third = gate.acquire(
                request_id="third",
                config=config,
            )
            third.release()


class _BlockingSttClient:
    entered = asyncio.Event()
    closed = False

    def __init__(self, *args, **kwargs) -> None:
        pass

    def build_request(self, *args, **kwargs):
        return object()

    async def send(self, _request, *, stream):
        self.__class__.entered.set()
        await asyncio.Future()

    async def aclose(self):
        self.__class__.closed = True


class DisconnectPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_asgi_disconnect_during_spooling_cleans_up_and_terminalizes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            body, content_type = _multipart_body(content=b"a" * 1024)
            disconnect = asyncio.Event()
            messages = iter((
                {"type": "http.request", "body": body[:len(body) // 2], "more_body": True},
            ))

            async def receive():
                try:
                    return next(messages)
                except StopIteration:
                    await disconnect.wait()
                    return {"type": "http.disconnect"}

            request = Request(
                {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", content_type.encode())]},
                receive,
            )
            finalized = []
            with (
                patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                patch.object(proxy.receipt_store, "begin"),
                patch.object(proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True),
            ):
                task = asyncio.create_task(proxy.proxy_stt_request(request, proxy.NO_AUTH_CONTEXT))
                await asyncio.sleep(0)
                disconnect.set()
                response = await asyncio.wait_for(task, 1)

            self.assertEqual(response.status_code, 499)
            self.assertEqual(len(finalized), 1)
            self.assertEqual(finalized[0].status, "cancelled")
            self.assertEqual(list((root / "spool").glob("*")), [])

    async def test_real_asgi_disconnect_cancels_delayed_downstream_upload_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, enabled=False)
            body, content_type = _multipart_body()
            disconnect = asyncio.Event()
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await disconnect.wait()
                return {"type": "http.disconnect"}

            request = Request(
                {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", content_type.encode())]},
                receive,
            )
            began, finalized = [], []
            _BlockingSttClient.entered = asyncio.Event()
            _BlockingSttClient.closed = False
            with (
                patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                patch.object(proxy.httpx, "AsyncClient", _BlockingSttClient),
                patch.object(proxy.receipt_store, "begin", side_effect=began.append),
                patch.object(proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True),
            ):
                task = asyncio.create_task(proxy.proxy_stt_request(request, proxy.NO_AUTH_CONTEXT))
                await asyncio.wait_for(_BlockingSttClient.entered.wait(), 1)
                disconnect.set()
                response = await asyncio.wait_for(task, 1)

            self.assertEqual(response.status_code, 499)
            self.assertEqual(finalized, began)
            self.assertEqual(finalized[0].status, "cancelled")
            self.assertEqual(finalized[0].metadata["stt"]["attempts_started"], 0)
            self.assertTrue(_BlockingSttClient.closed)
            self.assertEqual(list((root / "spool").glob("*")), [])

    async def test_real_asgi_disconnect_wins_every_stt_await_boundary(self) -> None:
        """The persistent ASGI watcher wins probe, media, upload, and read races."""
        for boundary in ("probe", "silence", "extract", "chunk_post", "response_read", "fallback_post"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = _config(root, enabled=boundary != "fallback_post")
                body, content_type = _multipart_body()
                disconnect = asyncio.Event()
                sent = False
                entered = asyncio.Event()
                finalized = []

                async def receive():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    await disconnect.wait()
                    return {"type": "http.disconnect"}

                async def block_or_return(label: str, waiters, deadline_at, on_cancel):
                    if label != boundary:
                        return None
                    entered.set()
                    await stt_chunking.await_with_cancellation(
                        asyncio.Future(),
                        cancellation_waiters=waiters,
                        deadline_at=deadline_at,
                        on_cancel=on_cancel,
                    )

                async def fake_media(command, *, cancellation_waiters, deadline_at, on_cancel, **_kwargs):
                    if command[0] == "ffprobe":
                        await block_or_return("probe", cancellation_waiters, deadline_at, on_cancel)
                        duration = "20" if "segment-" in str(command[-1]) else "75"
                        return (
                            f'{{"format":{{"duration":"{duration}"}},"streams":[{{"codec_type":"audio","sample_rate":"8000","channels":1}}]}}'.encode(),
                            b"",
                        )
                    if any("silencedetect" in str(part) for part in command):
                        await block_or_return("silence", cancellation_waiters, deadline_at, on_cancel)
                        return b"", b"silence_start: 19\nsilence_end: 21 | silence_duration: 2\nsilence_start: 39\nsilence_end: 41 | silence_duration: 2\n"
                    await block_or_return("extract", cancellation_waiters, deadline_at, on_cancel)
                    Path(command[-1]).write_bytes(b"RIFF" + b"x" * 64)
                    return b"", b""

                class DelayedResponse:
                    status_code = 200
                    headers = {"content-type": "application/json"}

                    async def aread(self):
                        if boundary == "response_read":
                            entered.set()
                            await asyncio.Future()
                        return b'{"text":"chunk"}'

                    async def aclose(self):
                        return None

                class DelayedClient:
                    closed = False

                    def __init__(self, *args, **kwargs):
                        pass

                    def build_request(self, *args, **kwargs):
                        return object()

                    async def send(self, _request, *, stream):
                        if boundary in {"chunk_post", "fallback_post"}:
                            entered.set()
                            await asyncio.Future()
                        return DelayedResponse()

                    async def aclose(self):
                        self.__class__.closed = True

                request = Request(
                    {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", content_type.encode())]},
                    receive,
                )
                with (
                    patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                    patch.object(proxy.httpx, "AsyncClient", DelayedClient),
                    patch.object(stt_chunking, "run_media_command", new=AsyncMock(side_effect=fake_media)),
                    patch.object(proxy.receipt_store, "begin"),
                    patch.object(proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True),
                ):
                    task = asyncio.create_task(proxy.proxy_stt_request(request, proxy.NO_AUTH_CONTEXT))
                    await asyncio.wait_for(entered.wait(), 1)
                    disconnect.set()
                    response = await asyncio.wait_for(task, 1)

                self.assertEqual(response.status_code, 499)
                self.assertEqual(len(finalized), 1)
                self.assertEqual(finalized[0].status, "cancelled")
                self.assertTrue(DelayedClient.closed)
                self.assertEqual(list((root / "spool").glob("*")), [])


class PassthroughContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_and_ineligible_requests_use_the_local_async_transport_unchanged(self) -> None:
        real_async_client = httpx.AsyncClient
        for kind, fields in (
            ("short", (("model", "whisper-1"),)),
            ("ineligible", (("model", "whisper-1"), ("response_format", "verbose_json"))),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = _config(root)
                body, content_type = _multipart_body(content=b"RIFF-direct", fields=fields)
                received: list[httpx.Request] = []
                finished_body = False

                async def receive():
                    nonlocal finished_body
                    if not finished_body:
                        finished_body = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    await asyncio.Future()

                async def transport_handler(outgoing: httpx.Request) -> httpx.Response:
                    received.append(outgoing)
                    self.assertIn(b"RIFF-direct", await outgoing.aread())
                    return httpx.Response(
                        203,
                        content=b'{"text":"direct","segments":[]}',
                        headers={"content-type": "application/json", "x-local-transport": "yes", "connection": "close"},
                    )

                def client_factory(*_args, **_kwargs):
                    return real_async_client(transport=httpx.MockTransport(transport_handler))

                request = Request(
                    {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", content_type.encode())]},
                    receive,
                )
                probe = AsyncMock(return_value=stt_chunking.ProbedAudio(2, 8000, 1))
                with (
                    patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                    patch.object(proxy.httpx, "AsyncClient", side_effect=client_factory),
                    patch.object(stt_chunking, "probe_audio", new=probe),
                    patch.object(proxy.receipt_store, "begin"),
                    patch.object(proxy.receipt_store, "finalize", return_value=True),
                ):
                    response = await proxy.proxy_stt_request(request, proxy.NO_AUTH_CONTEXT)

                self.assertEqual(response.status_code, 203)
                self.assertEqual(response.body, b'{"text":"direct","segments":[]}')
                self.assertEqual(response.headers["x-local-transport"], "yes")
                self.assertNotIn("connection", response.headers)
                self.assertEqual(len(received), 1)
                self.assertTrue(str(received[0].url).endswith("/v1/audio/transcriptions"))
                self.assertEqual(probe.await_count, 1 if kind == "short" else 0)
                self.assertEqual(list((root / "spool").glob("*")), [])


class HotReloadAndCapabilityTests(unittest.TestCase):
    def test_snapshot_freezes_selected_service_identity_auth_and_new_requests_see_reload(self) -> None:
        old = {
            "stt-a": {
                "host": "127.0.0.1", "port": 9011, "priority": 10, "enabled": True,
                "default_model": "old-model",
                "capabilities": ["audio", "transcriptions"],
                "auth_inject": [{"header": "x-stt-key", "secret_env": "STT_TEST_KEY"}],
                "chunking": {"spool_dir": "data/stt-spool", "global_concurrency": 1},
            },
            "stt-b": {
                "host": "127.0.0.1", "port": 9012, "priority": 20, "enabled": True,
                "capabilities": ["audio", "transcriptions"],
            },
        }
        new = {
            **old,
            "stt-a": {
                **old["stt-a"],
                "port": 9021,
                "default_model": "new-model",
                "chunking": {"spool_dir": "data/stt-spool", "global_concurrency": 3},
            },
        }
        with patch.dict(proxy.config, {"backends": old}, clear=False):
            first = proxy._stt_execution_snapshot("transcriptions")
            self.assertEqual(first.service_id, "stt-a")
            self.assertEqual(first.host, "http://127.0.0.1:9011")
            self.assertEqual(first.global_concurrency, 1)
            self.assertEqual(first.default_model, "old-model")
            proxy.config["backends"] = new
            second = proxy._stt_execution_snapshot("transcriptions")
            self.assertEqual(second.host, "http://127.0.0.1:9021")
            self.assertEqual(second.global_concurrency, 3)
            self.assertEqual(second.default_model, "new-model")
            self.assertEqual(first.host, "http://127.0.0.1:9011")
            self.assertEqual(first.global_concurrency, 1)
            self.assertEqual(first.default_model, "old-model")

    def test_snapshot_backend_auth_uses_the_capability_selected_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"STT_TEST_KEY": "configured-only-for-test"}, clear=False
        ), patch.object(proxy, "AUTH_ENABLED", True):
            snapshot = _config(Path(temp_dir))
            snapshot = replace(
                snapshot,
                service_id="stt-selected",
                backend_config=stt_chunking._freeze_mapping({
                    "auth_inject": [{"header": "x-stt-key", "secret_env": "STT_TEST_KEY"}],
                }),
            )
            headers = proxy.prepare_downstream_headers(
                {"authorization": "Bearer caller-key"},
                proxy.NO_AUTH_CONTEXT,
                snapshot.service_id,
                "/v1/audio/transcriptions",
                backend_config=snapshot.backend_config,
            )
        self.assertEqual(headers["x-stt-key"], "configured-only-for-test")
        self.assertNotIn("authorization", {name.lower() for name in headers})

    def test_registry_seed_and_existing_record_backfill_keep_operator_chunk_limits(self) -> None:
        bootstrap = {
            "backends": {
                "stt": {
                    "host": "127.0.0.1",
                    "port": 5092,
                    "capabilities": ["audio", "transcriptions"],
                    "chunking": {"spool_dir": "data/stt-spool", "max_segments": 16},
                }
            }
        }
        seeded = service_registry.build_service_registry_seed(bootstrap)
        self.assertEqual(seeded["services"]["stt"]["config"]["chunking"]["max_segments"], 16)
        existing = service_registry.build_service_registry_seed({
            "backends": {
                "stt": {
                    "host": "127.0.0.1",
                    "port": 5092,
                    "capabilities": ["audio", "transcriptions"],
                    "chunking": {"max_segments": 4},
                }
            }
        })
        backfilled = service_registry.backfill_seed_catalog_metadata(existing, proxy_config=bootstrap)
        effective = service_registry.apply_service_registry_to_config(bootstrap, backfilled)
        self.assertEqual(effective["backends"]["stt"]["chunking"]["max_segments"], 4)
        self.assertEqual(effective["backends"]["stt"]["chunking"]["spool_dir"], "data/stt-spool")


def _write_silenced_wav(path: Path) -> None:
    sample_rate = 8000
    intervals = ((20.0, 21.0), (40.0, 41.0), (60.0, 61.0))
    with wave.open(os.fspath(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        for index in range(sample_rate * 75):
            second = index / sample_rate
            silent = any(start <= second < end for start, end in intervals)
            sample = 0 if silent else int(8000 * ((index % 31) - 15) / 15)
            handle.writeframesraw(struct.pack("<h", sample))
