"""Hermetic contracts for generic, bounded long-audio STT execution."""

from __future__ import annotations

from contextlib import nullcontext

import asyncio
import json
import os
import shutil
import struct
import tempfile
import unittest
import wave
import zlib
import gzip
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

import proxy
from gateway import stt_chunking
from gateway import service_registry
from gateway.backends.cancellation import CancellationRegistry


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

    def test_a_service_row_drops_its_engine_diagnostics_before_the_merge_check(self) -> None:
        timing = [{"kind": "drop-params", "phase": "response", "drop": ["timing"]}]
        body = json.dumps({"text": "hello", "timing": {"wall_ms": 12.5, "rtf": 0.01}}).encode()
        payload = stt_chunking._parse_mergeable_chunk_payload(body, timing)
        self.assertEqual(stt_chunking.merge_transcription_payloads([payload, payload]), {"text": "hello hello"})
        with self.assertRaises(stt_chunking.SttChunkResponseError):
            stt_chunking._parse_mergeable_chunk_payload(json.dumps({"text": "x", "segments": []}).encode(), timing)

    def test_without_a_row_no_field_is_engine_knowledge(self) -> None:
        # A diagnostic is unmergeable unless the service's data says to drop it.
        with self.assertRaises(stt_chunking.SttChunkResponseError):
            stt_chunking._parse_mergeable_chunk_payload(json.dumps({"text": "x", "timing": {}}).encode())
        # Another engine's diagnostic needs only a different row.
        latency = [{"kind": "drop-params", "phase": "response", "drop": ["latency_ms"]}]
        body = json.dumps({"text": "x", "latency_ms": 4}).encode()
        self.assertEqual(stt_chunking._parse_mergeable_chunk_payload(body, latency), {"text": "x"})
        # A request-phase row does not touch chunk responses.
        request_row = [{"kind": "drop-params", "drop": ["latency_ms"]}]
        with self.assertRaises(stt_chunking.SttChunkResponseError):
            stt_chunking._parse_mergeable_chunk_payload(body, request_row)

    def test_merges_each_optional_sdk_token_detail_without_inventing_absent_values(self) -> None:
        payload = stt_chunking.merge_transcription_payloads([
            {"text": "one", "usage": {"type": "tokens", "input_tokens": 1, "output_tokens": 0, "total_tokens": 1, "input_token_details": {"audio_tokens": 1}}},
            {"text": "two", "usage": {"type": "tokens", "input_tokens": 2, "output_tokens": 0, "total_tokens": 2, "input_token_details": {"audio_tokens": 3}}},
        ])
        self.assertEqual(payload["usage"]["input_token_details"], {"audio_tokens": 4})
        with self.assertRaises(stt_chunking.SttChunkResponseError):
            stt_chunking._parse_mergeable_chunk_payload(b'{"text":"x","usage":{"type":"tokens","input_tokens":1,"output_tokens":0,"total_tokens":1,"extra":1}}')
        with self.assertRaises(stt_chunking.SttChunkResponseError):
            stt_chunking._parse_mergeable_chunk_payload(b'{"text":"x","usage":{"type":"tokens","input_tokens":1,"output_tokens":0,"total_tokens":1,"input_token_details":{"unknown":1}}}')

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

    def test_sdk_exact_usage_null_integer_and_optional_detail_contracts(self) -> None:
        parsed = [
            stt_chunking._parse_mergeable_chunk_payload(b'{"text":"one","usage":null}'),
            stt_chunking._parse_mergeable_chunk_payload(b'{"text":"two","usage":null}'),
        ]
        self.assertEqual(
            stt_chunking.merge_transcription_payloads(parsed),
            {"text": "one two"},
        )
        parsed.append(stt_chunking._parse_mergeable_chunk_payload(
            b'{"text":"three","usage":{"type":"duration","seconds":1}}'
        ))
        with self.assertRaises(stt_chunking.SttChunkResponseError):
            stt_chunking.merge_transcription_payloads(parsed)

        self.assertEqual(
            stt_chunking.merge_transcription_payloads([{"text": "one", "usage": None}, {"text": "two", "usage": None}]),
            {"text": "one two"},
        )
        self.assertEqual(
            stt_chunking.merge_transcription_payloads([
                {"text": "one", "usage": {"type": "duration", "seconds": 1}},
                {"text": "two", "usage": {"type": "duration", "seconds": 2.5}},
            ])["usage"],
            {"type": "duration", "seconds": 3.5},
        )
        for details, expected in (
            ({}, None),
            ({"audio_tokens": 2}, {"audio_tokens": 7}),
            ({"text_tokens": 2}, {"text_tokens": 7}),
            ({"audio_tokens": 2, "text_tokens": 3}, {"audio_tokens": 7, "text_tokens": 9}),
            ({"audio_tokens": None}, None),
        ):
            with self.subTest(details=details):
                second = {
                    key: value + 3 if isinstance(value, int) else value
                    for key, value in details.items()
                }
                payload = stt_chunking.merge_transcription_payloads([
                    {"text": "one", "usage": {"type": "tokens", "input_tokens": 1, "output_tokens": 0, "total_tokens": 1, "input_token_details": details}},
                    {"text": "two", "usage": {"type": "tokens", "input_tokens": 2, "output_tokens": 0, "total_tokens": 2, "input_token_details": second}},
                ])
                self.assertEqual(payload["usage"].get("input_token_details"), expected)

        invalid = (
            {"type": "tokens", "input_tokens": 1.5, "output_tokens": 0, "total_tokens": 1},
            {"type": "tokens", "input_tokens": True, "output_tokens": 0, "total_tokens": 1},
            {"type": "tokens", "input_tokens": 1, "output_tokens": 0, "total_tokens": 1, "input_token_details": {"audio_tokens": 1.5}},
            {"type": "duration", "seconds": float("inf")},
        )
        for usage in invalid:
            with self.subTest(usage=usage), self.assertRaises(stt_chunking.SttChunkResponseError):
                stt_chunking._parse_mergeable_chunk_payload(json.dumps({"text": "x", "usage": usage}).encode())

        heterogeneous = (
            [{"text": "one", "usage": None}, {"text": "two", "usage": {"type": "duration", "seconds": 1}}],
            [
                {"text": "one", "usage": {"type": "tokens", "input_tokens": 1, "output_tokens": 0, "total_tokens": 1}},
                {"text": "two", "usage": {"type": "tokens", "input_tokens": 2, "output_tokens": 0, "total_tokens": 2, "input_token_details": {"audio_tokens": 2}}},
            ],
            [
                {"text": "one", "usage": {"type": "tokens", "input_tokens": 1, "output_tokens": 0, "total_tokens": 1, "input_token_details": {"audio_tokens": None}}},
                {"text": "two", "usage": {"type": "tokens", "input_tokens": 2, "output_tokens": 0, "total_tokens": 2, "input_token_details": {"audio_tokens": 2}}},
            ],
            [
                {"text": "one", "usage": {"type": "tokens", "input_tokens": 1, "output_tokens": 0, "total_tokens": 1, "input_token_details": {"audio_tokens": 1}}},
                {"text": "two", "usage": {"type": "tokens", "input_tokens": 2, "output_tokens": 0, "total_tokens": 2, "input_token_details": {"text_tokens": 2}}},
            ],
        )
        for payloads in heterogeneous:
            with self.subTest(payloads=payloads), self.assertRaises(stt_chunking.SttChunkResponseError):
                stt_chunking.merge_transcription_payloads(payloads)

    def test_decoded_response_headers_never_describe_the_upstream_representation(self) -> None:
        for encoding in ("gzip", "deflate", "gzip, deflate"):
            with self.subTest(encoding=encoding):
                headers = stt_chunking.filtered_response_headers({
                    "content-type": "application/json",
                    "content-encoding": encoding,
                    "etag": "upstream-tag",
                    "digest": "sha-256=upstream",
                    "x-safe": "retained",
                })
                self.assertEqual(headers, {"x-safe": "retained"})


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

    async def test_partial_upload_obeys_the_outer_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _config(Path(temp_dir))
            _body, content_type = _multipart_body()

            async def receive():
                yield_message = {"type": "http.request", "body": b"--stt-test-boundary\r\n", "more_body": True}
                if not hasattr(receive, "sent"):
                    receive.sent = True
                    return yield_message
                await asyncio.Future()

            request = Request({"type": "http", "method": "POST", "headers": [(b"content-type", content_type.encode())]}, receive)
            deadline = asyncio.get_running_loop().time() + 0.02
            with self.assertRaises(stt_chunking.SttDeadlineExceeded):
                await stt_chunking.parse_bounded_multipart(request, config, deadline_at=deadline)


class ChunkExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_child_is_killed_and_reaped_on_task_cancellation(self) -> None:
        class BlockingReader:
            def __init__(self, entered: asyncio.Event) -> None:
                self._entered = entered

            async def read(self, _size: int) -> bytes:
                self._entered.set()
                await asyncio.Future()

        class FakeProcess:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.killed = False
                self.reaped = False
                self.entered = asyncio.Event()
                self.stdout = BlockingReader(self.entered)
                self.stderr = BlockingReader(self.entered)

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

    async def test_chunk_typed_terminal_causes_and_redirects_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.wav"
            source.write_bytes(b"source")
            config = _config(Path(temp_dir))

            async def extract(_source, destination, **_kwargs):
                Path(destination).write_bytes(b"RIFFchunk")

            async def timeout_post(_path, _filename):
                raise stt_chunking.SttBackendTimeout("timed out")

            async def redirect_post(_path, _filename):
                return stt_chunking.UpstreamResponse(302, b"redirect", {})

            common = dict(
                source_path=source, source_stem="source", fields=[("response_format", "json")], config=config,
                cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None,
            )
            with patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(75, 8000, 1))), patch.object(stt_chunking, "detect_silences", new=AsyncMock(return_value=[(19, 21), (39, 41), (59, 61)])), patch.object(stt_chunking, "extract_segment", new=AsyncMock(side_effect=extract)):
                with self.assertRaises(stt_chunking.SttBackendTimeout):
                    await stt_chunking.execute_chunking(post_file=timeout_post, **common)
                outcome = await stt_chunking.execute_chunking(post_file=redirect_post, **common)
            self.assertEqual(outcome.mode, "upstream_error")
            self.assertFalse(stt_chunking.upstream_response_succeeded(302))
            self.assertFalse(stt_chunking.upstream_response_succeeded(outcome.upstream_response.status_code))

    async def test_service_rows_run_on_chunks_and_a_row_that_cannot_run_fails_before_upload(self) -> None:
        async def extract(_source, destination, **_kwargs):
            Path(destination).write_bytes(b"RIFFchunk")

        for rows, policy, expected in (
            ([{"kind": "drop-params", "phase": "response", "drop": ["timing"]}], "passthrough", "chunked"),
            ([{"kind": "no-such-kind"}], "passthrough", "passthrough"),
            (["no-such-transform"], "error", stt_chunking.SttSetupFailure),
        ):
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                base = _config(root, setup_failure_policy=policy)
                config = stt_chunking.SttExecutionConfig.from_service(
                    service_id="stt-test", config_directory=root,
                    service_config={**base.backend_config, "transforms": rows},
                )
                source = root / "source.wav"
                source.write_bytes(b"source")
                posted: list[str] = []

                async def post(_path, filename):
                    posted.append(filename)
                    return stt_chunking.UpstreamResponse(200, b'{"text":"part","timing":{"wall_ms":1}}', {})

                run = stt_chunking.execute_chunking(
                    source_path=source, source_stem="source", fields=[("response_format", "json")], config=config,
                    post_file=post, cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10,
                    on_cancel=None,
                )
                with (
                    patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(75, 8000, 1))),
                    patch.object(stt_chunking, "detect_silences", new=AsyncMock(return_value=[(19, 21), (39, 41), (59, 61)])),
                    patch.object(stt_chunking, "extract_segment", new=AsyncMock(side_effect=extract)),
                ):
                    if isinstance(expected, type):
                        with self.assertRaises(expected):
                            await run
                        self.assertEqual(posted, [])
                        continue
                    outcome = await run
                self.assertEqual(outcome.mode, expected)
                if expected == "chunked":
                    self.assertEqual(outcome.merged_payload, {"text": "part part part"})
                else:
                    self.assertEqual((outcome.fallback_reason, posted), ("setup_failed_before_attempt", []))

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


def _cpu_parakeet_only(test: unittest.TestCase) -> None:
    """These tests exercise the default (always-on, CPU) STT service through `whisper-1`; the GPU engine ahead of it in the tier is on demand."""
    patcher = patch.dict(proxy.config["backends"])
    patcher.start()
    test.addCleanup(patcher.stop)
    proxy.config["backends"].pop("stt-parakeet-gpu", None)


class RequestWorkspaceIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _cpu_parakeet_only(self)

    async def test_two_live_routes_cannot_cross_workspace_paths_audio_or_unlinks(self) -> None:
        """Interleave both first chunks at the shipped process concurrency limit.

        Old shared ``segment-000.wav`` allocation made both downstream bodies
        observe the second request's audio. The route now proves isolation at
        the actual multipart, extract, send, unlink, and workspace boundary.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, global_concurrency=2)
            first_extracts = 0
            both_first_chunks_written = asyncio.Event()
            observed: list[tuple[Path, bytes]] = []

            async def extract(source: Path, destination: Path, **_kwargs: object) -> None:
                nonlocal first_extracts
                destination.write_bytes(source.read_bytes())
                if destination.name == "segment-000.wav":
                    first_extracts += 1
                    if first_extracts == 2:
                        both_first_chunks_written.set()
                    else:
                        await asyncio.wait_for(both_first_chunks_written.wait(), timeout=1)

            class Response:
                status_code = 200
                headers = {"content-type": "application/json"}

                def aiter_bytes(self):
                    async def chunks():
                        yield b'{"text":"chunk"}'
                    return chunks()

                async def aclose(self) -> None:
                    return None

            class Client:
                closed = 0

                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def build_request(self, _method: str, _url: str, *, files: list[tuple[str, tuple[str, object, str]]], **_kwargs: object) -> object:
                    return files[0][1][1]

                async def send(self, handle: object, *, stream: bool) -> Response:
                    del stream
                    path = Path(getattr(handle, "name"))
                    observed.append((path, getattr(handle, "read")()))
                    return Response()

                async def aclose(self) -> None:
                    self.__class__.closed += 1

            def request_for(marker: bytes) -> Request:
                body, content_type = _multipart_body(content=marker)
                sent = False

                async def receive():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    await asyncio.Future()

                return Request(
                    {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", content_type.encode())]},
                    receive,
                )

            with (
                patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                patch.object(proxy.httpx, "AsyncClient", Client),
                patch.object(stt_chunking, "GLOBAL_STT_RESOURCE_GATE", stt_chunking.SttResourceGate()),
                patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(75, 8000, 1))),
                patch.object(stt_chunking, "detect_silences", new=AsyncMock(return_value=[])),
                patch.object(stt_chunking, "plan_silence_only_segments", return_value=[(0.0, 1.0), (1.0, 2.0)]),
                patch.object(stt_chunking, "extract_segment", new=AsyncMock(side_effect=extract)),
                patch.object(proxy.receipt_store, "begin"),
                patch.object(proxy.receipt_store, "finalize", return_value=True),
            ):
                responses = await asyncio.gather(
                    proxy.proxy_stt_request(request_for(b"request-A-private-audio"), proxy.NO_AUTH_CONTEXT),
                    proxy.proxy_stt_request(request_for(b"request-B-private-audio"), proxy.NO_AUTH_CONTEXT),
                )

            self.assertEqual([response.status_code for response in responses], [200, 200])
            self.assertEqual(len(observed), 4)
            self.assertEqual({body for _path, body in observed}, {b"request-A-private-audio", b"request-B-private-audio"})
            paths = [path for path, _body in observed]
            self.assertEqual(len(set(paths)), 4)
            self.assertEqual(len({path.parent for path in paths}), 2)
            self.assertTrue(all(path.is_relative_to(config.spool_dir) for path in paths))
            self.assertTrue(all(not path.exists() for path in paths))
            self.assertEqual(list(config.spool_dir.iterdir()), [])


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

    async def test_probe_falls_back_to_packet_timestamps_when_format_duration_is_missing(self) -> None:
        """Regression test: a Chrome MediaRecorder webm with no trailing Cues
        element reports no format-level duration at all (not merely an
        unparsable one). Without a fallback, probe_audio() raised
        SttSetupFailure for every such recording, which — under the
        operator's setup_failure_policy: passthrough default — sent the
        whole file to the STT backend in one request instead of letting
        threshold/silence-aware chunking engage."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.webm"
            source.write_bytes(b"not examined by the fake probe")
            config = _config(
                root,
                max_duration_seconds=1000,
                max_decoded_bytes=64 * 1024 * 1024,
            )
            format_probe_payload = json.dumps({
                "format": {},
                "streams": [{
                    "codec_type": "audio",
                    "sample_rate": "16000",
                    "channels": 2,
                }],
            }).encode()
            packet_probe_stdout = (
                b"0.020000,0.020000\n"
                b"683.100000,0.060000\n"
                b"683.164000,0.060000\n"
            )
            with patch.object(
                stt_chunking,
                "run_media_command",
                new=AsyncMock(
                    side_effect=[(format_probe_payload, b""), (packet_probe_stdout, b"")]
                ),
            ) as mock_run:
                probed = await stt_chunking.probe_audio(
                    source,
                    config=config,
                    cancellation_waiters=(),
                    deadline_at=asyncio.get_running_loop().time() + 1,
                    on_cancel=None,
                )
            self.assertAlmostEqual(probed.duration_seconds, 683.224, places=3)
            self.assertEqual(probed.sample_rate, 16000)
            self.assertEqual(probed.channels, 2)
            self.assertEqual(mock_run.await_count, 2)
            fallback_command = mock_run.await_args_list[1].args[0]
            self.assertIn("-select_streams", fallback_command)

    async def test_probe_raises_setup_failure_when_packet_fallback_also_finds_no_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.webm"
            source.write_bytes(b"not examined by the fake probe")
            config = _config(root)
            format_probe_payload = json.dumps({
                "format": {},
                "streams": [{
                    "codec_type": "audio",
                    "sample_rate": "48000",
                    "channels": 1,
                }],
            }).encode()
            with patch.object(
                stt_chunking,
                "run_media_command",
                new=AsyncMock(side_effect=[(format_probe_payload, b""), (b"", b"")]),
            ):
                with self.assertRaises(stt_chunking.SttSetupFailure):
                    await stt_chunking.probe_audio(
                        source,
                        config=config,
                        cancellation_waiters=(),
                        deadline_at=asyncio.get_running_loop().time() + 1,
                        on_cancel=None,
                    )

    async def test_real_offline_ffmpeg_duration_less_webm_recovers_true_duration(self) -> None:
        """End-to-end with real ffmpeg/ffprobe: encoding audio to webm on a
        pipe (rather than a seekable file) reproduces the exact container
        shape a Chrome MediaRecorder session leaves behind when a recording
        ends without a clean stop — no trailing Cues/duration element. This
        proves the fallback recovers a real, correct duration from the same
        binary this module shells out to, not just from a mocked payload."""
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("ffmpeg and ffprobe are required for the local media fixture")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.webm"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=16000",
                "-ac", "1", "-c:a", "libopus", "-f", "webm", "pipe:1",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await proc.communicate()
            await proc.wait()
            self.assertEqual(proc.returncode, 0)
            source.write_bytes(stdout)

            # Confirm the fixture actually reproduces the bug precondition:
            # ffprobe's format-level duration read must come back empty.
            direct_probe = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", os.fspath(source),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            probe_stdout, _ = await direct_probe.communicate()
            self.assertNotIn(b"duration", probe_stdout)

            # libopus always reports a 48 kHz stream regardless of the
            # requested encode rate (Opus operates internally at 48 kHz).
            config = _config(root, max_duration_seconds=60, max_sample_rate=48_000)
            probed = await stt_chunking.probe_audio(
                source,
                config=config,
                cancellation_waiters=(),
                deadline_at=asyncio.get_running_loop().time() + 30,
                on_cancel=None,
            )
            self.assertGreater(probed.duration_seconds, 4.5)
            self.assertLess(probed.duration_seconds, 5.5)
            self.assertEqual(probed.channels, 1)

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


class OutputAndCleanupBoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_bytes_are_bounded_and_every_cleanup_is_attempted(self) -> None:
        class Response:
            def aiter_bytes(self):
                async def chunks():
                    yield b"abc"
                    yield b"def"
                return chunks()

        with self.assertRaises(stt_chunking.SttResponseLimitExceeded):
            await stt_chunking.read_bounded_response(
                Response(), max_bytes=5, cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 1, on_cancel=None
            )

        calls: list[str] = []
        owner = stt_chunking.SttCleanupOwner()

        def broken() -> None:
            calls.append("broken")
            raise OSError("closed")

        def released() -> None:
            calls.append("released")

        owner.add("released", released)
        owner.add("broken", broken)
        await owner.aclose()
        self.assertEqual(calls, ["broken", "released"])
        self.assertTrue(owner.failed)

    async def test_route_terminalizes_once_when_cancellation_registry_is_full(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _config(Path(temp_dir))
            body, content_type = _multipart_body()
            sent = False
            begun, finalized = [], []

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Future()

            request = Request(
                {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", content_type.encode())]},
                receive,
            )
            with (
                patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                patch.object(proxy, "cancellation_registry", return_value=CancellationRegistry(max_entries=0)),
                patch.object(proxy.receipt_store, "begin", side_effect=begun.append),
                patch.object(proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True),
            ):
                response = await proxy.proxy_stt_request(request, proxy.NO_AUTH_CONTEXT)

            self.assertEqual(response.status_code, 503)
            self.assertEqual(json.loads(response.body)["error"]["code"], "stt_cancellation_capacity")
            self.assertEqual(finalized, begun)
            self.assertEqual(len(finalized), 1)
            self.assertEqual(finalized[0].status, "failed")

    async def test_route_multipart_parse_error_releases_parser_uploads_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, max_input_bytes=64)
            body, content_type = _multipart_body(content=b"a" * 65)
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Future()

            request = Request(
                {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", content_type.encode())]},
                receive,
            )
            with (
                patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                patch.object(stt_chunking, "GLOBAL_STT_RESOURCE_GATE", stt_chunking.SttResourceGate()),
                patch.object(proxy.receipt_store, "begin"),
                patch.object(proxy.receipt_store, "finalize", return_value=True),
            ):
                response = await proxy.proxy_stt_request(request, proxy.NO_AUTH_CONTEXT)

            self.assertEqual(response.status_code, 413)
            self.assertEqual(list(config.spool_dir.iterdir()), [])


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
    def setUp(self) -> None:
        _cpu_parakeet_only(self)

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

                    async def aiter_bytes(self):
                        if boundary == "response_read":
                            entered.set()
                            await asyncio.Future()
                        yield b'{"text":"chunk"}'

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
    def setUp(self) -> None:
        _cpu_parakeet_only(self)

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

    async def test_decoded_direct_success_error_and_redirect_drop_representation_headers(self) -> None:
        real_async_client = httpx.AsyncClient
        for status, encoding in ((200, "gzip"), (500, "deflate"), (302, "gzip, deflate")):
            with self.subTest(status=status, encoding=encoding), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = _config(root, enabled=False)
                body, content_type = _multipart_body(content=b"RIFF-encoded")
                sent = False
                decoded = b'{"text":"decoded"}'
                encoded = gzip.compress(decoded) if encoding == "gzip" else zlib.compress(decoded)
                if encoding == "gzip, deflate":
                    encoded = zlib.compress(gzip.compress(decoded))

                async def receive():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    await asyncio.Future()

                async def transport_handler(_outgoing: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        status,
                        content=encoded,
                        headers={
                            "content-type": "application/json",
                            "content-encoding": encoding,
                            "etag": "encoded-upstream",
                            "digest": "sha-256=encoded-upstream",
                            "x-safe": "retained",
                        },
                    )

                request = Request(
                    {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", content_type.encode())]},
                    receive,
                )
                with (
                    patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                    patch.object(proxy.httpx, "AsyncClient", side_effect=lambda *_args, **_kwargs: real_async_client(transport=httpx.MockTransport(transport_handler))),
                    patch.object(proxy.receipt_store, "begin"),
                    patch.object(proxy.receipt_store, "finalize", return_value=True),
                ):
                    response = await proxy.proxy_stt_request(request, proxy.NO_AUTH_CONTEXT)

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.body, decoded)
                self.assertEqual(response.headers["x-safe"], "retained")
                for header in ("content-encoding", "etag", "digest"):
                    self.assertNotIn(header, response.headers)
                self.assertEqual(response.headers["content-length"], str(len(decoded)))


class HotReloadAndCapabilityTests(unittest.TestCase):
    def test_existing_registry_global_concurrency_value_loads_exactly(self) -> None:
        """The persisted field keeps its original value; no renamed default applies."""
        with tempfile.TemporaryDirectory() as temp_dir:
            execution = stt_chunking.SttExecutionConfig.from_service(
                service_id="persisted-stt",
                service_config={
                    "host": "127.0.0.1",
                    "port": 9876,
                    "chunking": {
                        "spool_dir": "data/stt-spool",
                        "global_concurrency": 7,
                    },
                },
                config_directory=Path(temp_dir),
            )
        self.assertEqual(execution.global_concurrency, 7)

    def test_service_selection_keeps_one_registry_snapshot_during_reload_interleaving(self) -> None:
        old = {
            "stt-old": {"host": "127.0.0.1", "port": 9011, "priority": 10, "enabled": True, "capabilities": ["audio", "transcriptions"]},
        }
        new = {
            "stt-new": {"host": "127.0.0.1", "port": 9012, "priority": 10, "enabled": True, "capabilities": ["audio", "transcriptions"]},
        }

        class ReloadingBackends(dict):
            def items(self):
                proxy.reload_runtime_config()
                return super().items()

        original_config = proxy.config
        original_derived = {
            name: getattr(proxy, name)
            for name in proxy._derive_backend_constants(original_config)
        }
        try:
            with patch.object(proxy, "config", {"backends": ReloadingBackends(old)}), patch.object(
                proxy, "_load_effective_runtime_config", return_value={"backends": new}
            ):
                selected = proxy.service_for_capability("audio.transcriptions")
                self.assertEqual(selected[0], "stt-old")
                self.assertEqual(proxy.service_for_capability("audio.transcriptions")[0], "stt-new")
        finally:
            proxy.config = original_config
            for name, value in original_derived.items():
                setattr(proxy, name, value)

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
        ), nullcontext():
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

    def test_selected_backend_policy_and_receipt_never_use_the_legacy_stt_literal(self) -> None:
        receipt = proxy._stt_receipt(
            request=type("Request", (), {"url": type("Url", (), {"path": "/v1/audio/transcriptions"})()})(),
            auth_context=proxy.NO_AUTH_CONTEXT,
            operation="transcriptions",
            service_id="stt-selected",
        )
        seen = []

        class Decision:
            denied = False

        with patch.object(proxy, "evaluate_and_apply", side_effect=lambda _identity, policy, applied: seen.append((policy.backend, applied.backend)) or Decision()):
            proxy._apply_selected_stt_policy(receipt, proxy.NO_AUTH_CONTEXT, operation="transcriptions")
        self.assertEqual(seen, [("stt-selected", "stt-selected")])

    def test_receipt_shape_omits_hostile_upload_data(self) -> None:
        receipt = proxy._stt_receipt(
            request=type("Request", (), {"url": type("Url", (), {"path": "/v1/audio/transcriptions"})()})(),
            auth_context=proxy.NO_AUTH_CONTEXT,
            operation="transcriptions",
            service_id="stt-selected",
        )
        hostile = "private-prompt-filename-transcript-audio-marker"
        proxy._record_stt_input_shape(receipt, [(hostile, hostile), ("prompt", hostile)], [(hostile, object())])
        stored = json.dumps(receipt.to_dict(), sort_keys=True)
        self.assertNotIn(hostile, stored)
        self.assertEqual(receipt.accepted_inputs, ["prompt", "file"])

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


class AdmissionEvidenceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_preserves_openai_shape_and_records_owner_evidence(self) -> None:
        from gateway.resource_claims import AdmissionRequest, CapacitySnapshot, ResourceClaim, evaluate_admission
        from gateway.receipts import RouteReceipt
        decision = evaluate_admission(AdmissionRequest("req", ResourceClaim(disk_mb=20)),
                                     CapacitySnapshot(total_disk_mb=10, used_disk_mb=0, observed_at=123))
        receipt = RouteReceipt(protocol="openai.audio", intent="audio.transcribe")
        request = Request({"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
                           "headers": [(b"content-type", b"multipart/form-data; boundary=test")]})
        with tempfile.TemporaryDirectory() as temp:
            with (patch.object(proxy, "_service_availability_response", return_value=None),
                  patch.object(proxy.receipt_store, "begin"),
                  patch.object(proxy.receipt_store, "finalize") as finalize,
                  patch.object(stt_chunking.GLOBAL_STT_RESOURCE_GATE, "acquire",
                               side_effect=stt_chunking.SttResourceLimitExceeded("private quantities 20 10", decision=decision))):
                response = await proxy.proxy_stt_request(request, proxy.NO_AUTH_CONTEXT,
                                                        execution_snapshot=_config(Path(temp)), route_receipt=receipt)
        self.assertEqual(response.status_code, 413)
        error = json.loads(response.body)["error"]
        self.assertEqual(error["code"], "stt_resource_limit")
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertIsNone(error["param"])
        self.assertNotIn("private", json.dumps(error))
        self.assertNotIn("facts", error["decision"])
        self.assertEqual(error["decision"]["next_actions"][0]["action"], "wait")
        self.assertEqual(finalize.call_count, 1)
        self.assertEqual(receipt.resource_decision["facts"][0]["value"], 20)
