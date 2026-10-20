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
from tests.support.access import access_context
from tests.support.stt_pcm import pcm_bytes, planning_pcm, write_pcm
from gateway import stt_chunking
from gateway import client_errors
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

    def _quietest(self, duration: float, quiet_at=(), **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "planning.pcm"
        write_pcm(path, duration, quiet_at, **kwargs)
        return stt_chunking.quietest_frame_reader(path)

    def test_quietest_frame_is_the_lowest_energy_frame_and_ties_go_to_the_nearest(self) -> None:
        quietest = self._quietest(60, quiet_at=[12.3, 40.0])
        self.assertAlmostEqual(quietest(10, 15, 10.0), 12.3, delta=0.16)
        self.assertAlmostEqual(quietest(38, 42, 41.9), 40.0, delta=0.16)
        # A window with no quiet place and equal frames: the frame nearest `near`.
        self.assertAlmostEqual(quietest(20, 25, 22.0), 22.0, delta=0.006)
        self.assertAlmostEqual(quietest(20, 25, 20.0), 20.0, delta=0.006)
        # The result always lies inside the window, also past the end of the data.
        self.assertTrue(55 <= quietest(55, 70, 69.0) <= 70)
        self.assertEqual(quietest(100, 110, 105.0), 105.0)

    def test_the_quietest_frame_of_a_lower_floor_beats_a_nearer_quiet_one(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "planning.pcm"
        loud = pcm_bytes(30, quiet_at=[], )
        near_gap = pcm_bytes(30, quiet_at=[14.0], floor=300)
        far_gap = pcm_bytes(30, quiet_at=[17.0], floor=0)
        merged = bytearray(loud)
        merged[int(13.8 * 32000):int(14.2 * 32000)] = near_gap[int(13.8 * 32000):int(14.2 * 32000)]
        merged[int(16.8 * 32000):int(17.2 * 32000)] = far_gap[int(16.8 * 32000):int(17.2 * 32000)]
        path.write_bytes(bytes(merged))
        self.assertAlmostEqual(stt_chunking.quietest_frame_reader(path)(12, 20, 14.0), 17.0, delta=0.16)

    def test_plan_cuts_at_the_quiet_place_near_each_target(self) -> None:
        for quiet_at, expected in (
            ([], [(0.0, 30), (30, 60), (60, 90), (90, 125)]),
            ([30.0, 90.0], [(0.0, 30), (30, 60), (60, 90), (90, 125)]),
            ([25.0, 95.0], [(0.0, 25), (25, 60), (60, 95), (95, 125)]),
        ):
            with self.subTest(quiet_at=quiet_at):
                segments = stt_chunking.plan_quietest_frame_segments(
                    duration_seconds=125, quietest_frame=self._quietest(125, quiet_at),
                    target_seconds=30, search_seconds=10, max_segment_seconds=50, max_segments=64,
                )
                self.assertEqual(len(segments), len(expected))
                for (start, end), (want_start, want_end) in zip(segments, expected):
                    self.assertAlmostEqual(start, want_start, delta=0.2)
                    self.assertAlmostEqual(end, want_end, delta=0.2)

    def test_plan_only_looks_near_the_target_and_does_not_chase_a_distant_quiet_place(self) -> None:
        # Search 2 s: the quiet place at 24 s is out of reach of the 30 s target, so the cut stays at 30 s.
        segments = stt_chunking.plan_quietest_frame_segments(
            duration_seconds=70, quietest_frame=self._quietest(70, [24.0, 36.5]),
            target_seconds=30, search_seconds=2, max_segment_seconds=40, max_segments=64,
        )
        self.assertAlmostEqual(segments[0][1], 30.0, delta=0.02)

    def test_the_record_search_width_is_not_silently_narrowed(self) -> None:
        segments = stt_chunking.plan_quietest_frame_segments(
            duration_seconds=75, quietest_frame=self._quietest(75, [20.0]),
            target_seconds=30, search_seconds=12, max_segment_seconds=50, max_segments=64,
        )
        self.assertAlmostEqual(segments[0][1], 20.0, delta=0.16)
        self.assertLessEqual(max(end - start for start, end in segments), 50)

    def test_plan_tiles_the_file_without_overlap_and_never_exceeds_the_hard_maximum(self) -> None:
        for duration, target, search, maximum in (
            (80, 20, 2, 24), (80, 20, 0, 20), (31, 2, 0.5, 2.5), (60, 30, 2, 40), (30.001, 30, 2, 40), (125, 45, 2, 60),
            (1321.62, 30, 2, 40), (4 * 3600.3, 30, 2, 40), (200, 30, 10, 30), (200, 30, 2, 31), (36, 30, 2, 40),
        ):
            with self.subTest(duration=duration, target=target, maximum=maximum):
                segments = stt_chunking.plan_quietest_frame_segments(
                    duration_seconds=duration, quietest_frame=lambda lo, hi, near: near,
                    target_seconds=target, search_seconds=search, max_segment_seconds=maximum, max_segments=100_000,
                )
                self.assertEqual(segments[0][0], 0)
                self.assertEqual(segments[-1][1], duration)
                self.assertEqual([end for _, end in segments[:-1]], [start for start, _ in segments[1:]])
                self.assertLessEqual(max(end - start for start, end in segments), maximum)
                self.assertTrue(all(end - start >= 0.5 or len(segments) == 1 for start, end in segments))
                self.assertAlmostEqual(sum(end - start for start, end in segments), duration)

    def test_plan_keeps_the_hard_maximum_against_an_adversarial_signal(self) -> None:
        # The reader answers the least helpful place in the range it is given: always the far end, always the near end.
        for name, reader in (
            ("far end", lambda lo, hi, near: hi),
            ("near end", lambda lo, hi, near: lo),
            ("away from target", lambda lo, hi, near: hi if near - lo < hi - near else lo),
        ):
            for duration in (47.3, 500.0, 3601.7):
                with self.subTest(reader=name, duration=duration):
                    segments = stt_chunking.plan_quietest_frame_segments(
                        duration_seconds=duration, quietest_frame=reader,
                        target_seconds=30, search_seconds=2, max_segment_seconds=32, max_segments=10_000,
                    )
                    self.assertLessEqual(max(end - start for start, end in segments), 32)
                    self.assertEqual(segments[-1][1], duration)
                    self.assertEqual([end for _, end in segments[:-1]], [start for start, _ in segments[1:]])

    def test_plan_keeps_the_hard_maximum_on_audio_with_no_quiet_place_and_with_a_single_loud_burst(self) -> None:
        for name, quiet_at, floor in (("no quiet place", [], 0), ("quiet at the window edges", [28.0, 32.0, 58.0, 62.0, 88.0, 92.0], 0)):
            with self.subTest(case=name):
                segments = stt_chunking.plan_quietest_frame_segments(
                    duration_seconds=150, quietest_frame=self._quietest(150, quiet_at),
                    target_seconds=30, search_seconds=2, max_segment_seconds=40, max_segments=64,
                )
                self.assertLessEqual(max(end - start for start, end in segments), 40)
                self.assertEqual([end for _, end in segments[:-1]], [start for start, _ in segments[1:]])

    def test_plan_rejects_a_maximum_below_the_target_and_excessive_work(self) -> None:
        with self.assertRaises(stt_chunking.SttConfigurationError):
            stt_chunking.plan_quietest_frame_segments(
                duration_seconds=100, quietest_frame=lambda lo, hi, near: near,
                target_seconds=30, search_seconds=2, max_segment_seconds=29, max_segments=64,
            )
        calls = []

        def counted(lo, hi, near):
            calls.append(near)
            return near

        self.assertEqual(len(stt_chunking.plan_quietest_frame_segments(
            duration_seconds=125, quietest_frame=counted, target_seconds=30, search_seconds=2,
            max_segment_seconds=40, max_segments=4,
        )), 4)
        calls.clear()
        with self.assertRaises(stt_chunking.SttResourceLimitExceeded):
            stt_chunking.plan_quietest_frame_segments(
                duration_seconds=1321.62, quietest_frame=counted, target_seconds=0.6,
                search_seconds=0.1, max_segment_seconds=1.0, max_segments=3,
            )
        self.assertLessEqual(len(calls), 3)

    def test_record_fields_default_and_say_where_the_hard_maximum_came_from(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            declared = _config(root, target_seconds=30, threshold_seconds=40, search_seconds=2, max_segment_seconds=40, native_long_audio_offline="true")
            self.assertEqual(
                (declared.target_seconds, declared.search_seconds, declared.max_segment_seconds, declared.native_long_audio_offline),
                (30.0, 2.0, 40.0, True),
            )
            self.assertEqual(declared.chunking_view()["from_bundled_defaults"], [])
            undeclared = _config(root, target_seconds=30, threshold_seconds=60)
            self.assertEqual((undeclared.search_seconds, undeclared.max_segment_seconds, undeclared.native_long_audio_offline), (2.0, 34.0, False))  # target + 2 * search: the bundled `derive`
            self.assertFalse(undeclared.max_segment_declared)
            self.assertEqual(undeclared.chunking_view()["from_bundled_defaults"], ["search_seconds", "max_segment_seconds"])
            # An undeclared derived piece bound must not override the saved cutting threshold.
            self.assertEqual(undeclared.chunk_above_seconds, 60.0)
            self.assertEqual(declared.chunk_above_seconds, 40.0)
            with self.assertRaises(stt_chunking.SttConfigurationError):
                _config(root, target_seconds=30, threshold_seconds=60, max_segment_seconds=20)
            self.assertEqual(_config(root, target_seconds=30, threshold_seconds=40, search_seconds=0).search_seconds, 0.0)
            # Keys of the silence-based planner are no longer read; a record that still holds them gets the defaults.
            legacy = _config(root, target_seconds=30, threshold_seconds=40, silence_search_seconds=10, silence_noise_db="-30dB", silence_min_seconds=0.5)
            self.assertEqual((legacy.search_seconds, legacy.max_segment_seconds), (2.0, 34.0))

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
    async def test_extraction_refuses_a_piece_whose_actual_duration_exceeds_the_hard_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, target_seconds=30, threshold_seconds=40, max_segment_seconds=40)
            destination = root / "piece.wav"

            async def fake_command(args, **_kwargs):
                destination.write_bytes(b"RIFF" + b"x" * 100)
                return b"", b""

            with (
                patch.object(stt_chunking, "run_media_command", new=AsyncMock(side_effect=fake_command)),
                patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(40.05, 16000, 1))),
            ):
                with self.assertRaises(stt_chunking.SttSetupFailure):
                    await stt_chunking.extract_segment(
                        root / "source.wav", destination, start_seconds=0, end_seconds=40, config=config,
                        cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None,
                    )

    async def test_the_quietest_places_decide_the_cuts_and_every_segment_stays_bounded(self) -> None:
        for quiet_at, expected in (
            ([], [(0.0, 30), (30, 60), (60, 90), (90, 125)]),
            ([30.0, 90.0], [(0.0, 30), (30, 60), (60, 90), (90, 125)]),
            ([25.0, 95.0], [(0.0, 25), (25, 60), (60, 95), (95, 125)]),
        ):
            with self.subTest(quiet_at=quiet_at), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = root / "source.wav"
                source.write_bytes(b"original")
                config = _config(root, target_seconds=30, threshold_seconds=40, search_seconds=10, max_duration_seconds=150)
                extracted = []
                posted = []

                async def extract(_source, destination, *, start_seconds, end_seconds, **_kwargs):
                    extracted.append((start_seconds, end_seconds))
                    Path(destination).write_bytes(b"chunk")

                async def post(path, _filename):
                    self.assertNotEqual(path, source)
                    self.assertEqual(Path(path).read_bytes(), b"chunk")
                    posted.append(path)
                    return stt_chunking.UpstreamResponse(200, b'{"text":"repeat"}', {})

                with patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(125, 8000, 1))), planning_pcm(125, quiet_at), patch.object(stt_chunking, "extract_segment", new=AsyncMock(side_effect=extract)):
                    outcome = await stt_chunking.execute_chunking(
                        source_path=source, source_stem="source", fields=[("response_format", "json")],
                        config=config, cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10,
                        on_cancel=None, post_file=post,
                    )
                self.assertEqual(outcome.mode, "chunked")
                self.assertEqual(outcome.attempts_started, 4)
                self.assertEqual(len(extracted), len(expected))
                for (start, end), (want_start, want_end) in zip(extracted, expected):
                    self.assertAlmostEqual(start, want_start, delta=0.2)
                    self.assertAlmostEqual(end, want_end, delta=0.2)
                self.assertLessEqual(max(end - start for start, end in extracted), config.max_segment_seconds)
                self.assertEqual(len(posted), 4)
                self.assertEqual(outcome.merged_payload, {"text": "repeat repeat repeat repeat"})
                self.assertFalse(any(root.glob("planning-*")))

    async def _run_unplannable(self, *, probed, decode_error=None, probe_error=None, **limits):
        """Run execute_chunking on a file whose planning fails; return (outcome or raised error, posts)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            source.write_bytes(b"source")
            config_limits = dict(limits)
            max_segment_seconds = config_limits.pop("max_segment_seconds", 40)
            config = _config(root, target_seconds=30, threshold_seconds=40, max_segment_seconds=max_segment_seconds,
                             max_duration_seconds=3000, **config_limits)
            posts = []

            async def post(_path, filename):
                posts.append(filename)
                return stt_chunking.UpstreamResponse(200, b'{"text":"x"}', {})

            patches = [patch.object(stt_chunking, "probe_audio", new=AsyncMock(
                return_value=probed, side_effect=probe_error))]
            patches.append(patch.object(stt_chunking, "decode_planning_pcm", new=AsyncMock(side_effect=decode_error)))
            with patches[0], patches[1]:
                try:
                    result = await stt_chunking.execute_chunking(
                        source_path=source, source_stem="source", fields=[("response_format", "json")], config=config,
                        post_file=post, cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None,
                    )
                except stt_chunking.SttExecutionError as error:
                    result = error
            return result, posts

    async def test_a_file_above_the_maximum_that_cannot_be_planned_is_refused_not_sent_whole(self) -> None:
        long_file = stt_chunking.ProbedAudio(1320, 16000, 1)
        for name, kwargs, reason in (
            ("probe cannot determine a duration", {"probed": None, "probe_error": stt_chunking.SttMediaCommandFailed("x")}, "unknown_duration"),
            ("a local tool is missing", {"probed": long_file, "decode_error": stt_chunking.SttSetupFailure("x")}, "split_failed"),
            ("the decoded audio cannot be read", {"probed": long_file, "decode_error": OSError("disk")}, "split_failed"),
        ):
            with self.subTest(name):
                result, posts = await self._run_unplannable(**kwargs)
                self.assertIsInstance(result, stt_chunking.SttAudioNotSplittable)
                self.assertEqual((result.reason, result.limit_seconds, posts), (reason, 40.0, []))

    async def test_probeable_short_file_with_undecodable_codec_is_invalid_audio_in_both_paths(self) -> None:
        # ffprobe reads the WAV duration; ffmpeg cannot decode its unknown codec tag.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "unknown-codec.wav"
            samples = b"\0" * 80_000
            fmt = struct.pack("<HHIIHH", 0x7777, 1, 8000, 8000, 1, 8)
            body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(samples)) + samples
            source.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
            config = _config(root, target_seconds=30, threshold_seconds=40, max_segment_seconds=40)
            deadline = asyncio.get_running_loop().time() + 30
            probed = await stt_chunking.probe_audio(source, config=config, cancellation_waiters=(), deadline_at=deadline, on_cancel=None)
            self.assertAlmostEqual(probed.duration_seconds, 10, delta=0.01)
            post = AsyncMock()
            with self.assertRaises(stt_chunking.SttInvalidAudio):
                await stt_chunking.execute_chunking(
                    source_path=source, source_stem="unknown-codec", fields=(("response_format", "json"),),
                    config=config, post_file=post, cancellation_waiters=(), deadline_at=deadline, on_cancel=None,
                )
            with self.assertRaises(stt_chunking.SttInvalidAudio):
                await stt_chunking.guard_uncut_uploads(
                    [source], config=config, cancellation_waiters=(), deadline_at=deadline, on_cancel=None,
                )
            post.assert_not_awaited()

    async def test_unknown_duration_refusal_does_not_claim_the_file_is_long(self) -> None:
        result, posts = await self._run_unplannable(
            probed=None, probe_error=stt_chunking.SttMediaCommandFailed("secret ffprobe output")
        )
        self.assertEqual(posts, [])
        self.assertIsInstance(result, stt_chunking.SttAudioNotSplittable)
        response = client_errors.stt_audio_cannot_be_split(result.reason, result.limit_seconds)
        self.assertEqual(response.status_code, 422)
        message = json.loads(response.body)["error"]["message"]
        self.assertIn("could not determine", message)
        self.assertIn("40-second maximum", message)
        self.assertNotIn("longer than", message)
        self.assertNotIn("secret", message)
        self.assertIn("recorded or measured", message)
        self.assertNotIn("just now", message)

    async def test_missing_declared_maximum_does_not_refuse_a_request_that_cannot_be_cut(self) -> None:
        result, posts = await self._run_unplannable(
            probed=stt_chunking.ProbedAudio(1320, 16000, 1), decode_error=stt_chunking.SttSetupFailure("x"),
            max_segment_seconds=None,
        )
        self.assertEqual((result.mode, result.fallback_reason, posts), ("passthrough", "setup_failed_before_attempt", []))

    async def test_uncut_upload_without_a_recorded_or_measured_maximum_is_allowed_whole(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, target_seconds=30, threshold_seconds=40, max_segment_seconds=None)
            with patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(4000, 16000, 1))):
                await stt_chunking.guard_uncut_uploads(
                    [root / "upload.wav"], config=config, cancellation_waiters=(),
                    deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None,
                )

    async def test_planner_uses_decoded_pcm_length_when_probe_duration_is_shorter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            source.write_bytes(b"source")
            config = _config(root, target_seconds=30, threshold_seconds=40, max_segment_seconds=40, max_duration_seconds=150)
            posted = []

            async def fake_decode(_source, destination, **_kwargs):
                Path(destination).write_bytes(b"\0\0" * (75 * 16_000))

            async def extract(_source, destination, *, start_seconds, end_seconds, **_kwargs):
                posted.append((start_seconds, end_seconds))
                Path(destination).write_bytes(b"chunk")

            with patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(45, 8000, 1))), \
                 patch.object(stt_chunking, "decode_planning_pcm", new=AsyncMock(side_effect=fake_decode)), \
                 patch.object(stt_chunking, "extract_segment", new=AsyncMock(side_effect=extract)), \
                 patch.object(stt_chunking, "quietest_frame_reader", return_value=lambda _lo, _hi, near: near):
                outcome = await stt_chunking.execute_chunking(
                    source_path=source, source_stem="source", fields=[("response_format", "json")], config=config,
                    post_file=AsyncMock(return_value=stt_chunking.UpstreamResponse(200, b'{"text":"x"}', {})),
                    cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None,
                )
            self.assertEqual(outcome.mode, "chunked")
            self.assertAlmostEqual(posted[-1][1], 75, delta=0.01)

    async def test_native_offline_allows_decode_failure_but_a_short_header_does_not_prove_the_file_fits(self) -> None:
        native, posts = await self._run_unplannable(
            probed=stt_chunking.ProbedAudio(1320, 16000, 1),
            decode_error=stt_chunking.SttSetupFailure("x"), native_long_audio_offline=True,
        )
        self.assertEqual((native.mode, native.fallback_reason, posts), ("passthrough", "setup_failed_before_attempt", []))
        unverified, posts = await self._run_unplannable(
            probed=stt_chunking.ProbedAudio(41, 16000, 1),
            decode_error=stt_chunking.SttSetupFailure("x"), max_segment_seconds=60,
        )
        self.assertIsInstance(unverified, stt_chunking.SttAudioNotSplittable)
        self.assertEqual(posts, [])

    async def test_the_error_policy_still_raises_the_setup_failure_itself(self) -> None:
        result, posts = await self._run_unplannable(
            probed=stt_chunking.ProbedAudio(1320, 16000, 1), decode_error=stt_chunking.SttSetupFailure("x"), setup_failure_policy="error",
        )
        self.assertIs(type(result), stt_chunking.SttSetupFailure)
        self.assertEqual(posts, [])

    async def test_a_short_file_is_decoded_once_before_the_threshold_decision(self) -> None:
        async def write_short_pcm(_source, destination, **_kwargs):
            write_pcm(destination, 40)
        decode = AsyncMock(side_effect=write_short_pcm)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            source.write_bytes(b"source")
            with patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(40, 16000, 1))), patch.object(
                stt_chunking, "decode_planning_pcm", new=decode,
            ):
                outcome = await stt_chunking.execute_chunking(
                    source_path=source, source_stem="source", fields=[("response_format", "json")],
                    config=_config(root, target_seconds=30, threshold_seconds=40, max_segment_seconds=40),
                    post_file=AsyncMock(), cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None,
                )
        self.assertEqual((outcome.mode, outcome.fallback_reason), ("passthrough", "below_threshold"))
        decode.assert_awaited_once()

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
            with patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(75, 8000, 1))), planning_pcm(75, [20.0, 40.0, 60.0]), patch.object(stt_chunking, "extract_segment", new=AsyncMock(side_effect=extract)):
                with self.assertRaises(stt_chunking.SttBackendTimeout):
                    await stt_chunking.execute_chunking(post_file=timeout_post, **common)
                outcome = await stt_chunking.execute_chunking(post_file=redirect_post, **common)
            self.assertEqual(outcome.mode, "upstream_error")
            self.assertFalse(stt_chunking.upstream_response_succeeded(302))
            self.assertFalse(stt_chunking.upstream_response_succeeded(outcome.upstream_response.status_code))

    async def test_service_rows_run_on_chunks_and_a_row_that_cannot_run_fails_before_upload(self) -> None:
        async def extract(_source, destination, **_kwargs):
            Path(destination).write_bytes(b"RIFFchunk")

        for rows, policy, limits, expected in (
            ([{"kind": "drop-params", "phase": "response", "drop": ["timing"]}], "passthrough", {}, "chunked"),
            # A larger declared maximum allows the whole 75 s upload; an unknown maximum allows today's whole-file fallback.
            ([{"kind": "no-such-kind"}], "passthrough", {"max_segment_seconds": 100}, "passthrough"),
            # This invalid chunk-only transform cannot justify refusing a whole upload when no maximum is known.
            ([{"kind": "no-such-kind"}], "passthrough", {}, "passthrough"),
            ([{"kind": "no-such-kind"}], "passthrough", {"max_segment_seconds": 40}, stt_chunking.SttSetupFailure),
            (["no-such-transform"], "error", {}, stt_chunking.SttSetupFailure),
        ):
            with self.subTest(rows=rows, limits=limits), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                base = _config(root, setup_failure_policy=policy, **limits)
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
                    planning_pcm(75, [20.0, 40.0, 60.0]),
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
                    self.assertEqual(outcome.merged_payload, {"text": "part part part part"})
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
                planning_pcm(75, [20.0, 40.0, 60.0]),
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

            async def decode_for_planning(_source: Path, destination: Path, **_kwargs: object) -> None:
                Path(destination).write_bytes(b"\0\0" * (75 * 16_000))

            with (
                patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                patch.object(proxy.httpx, "AsyncClient", Client),
                patch.object(stt_chunking, "GLOBAL_STT_RESOURCE_GATE", stt_chunking.SttResourceGate()),
                patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(75, 8000, 1))),
                patch.object(stt_chunking, "decode_planning_pcm", new=AsyncMock(side_effect=decode_for_planning)),
                patch.object(stt_chunking, "plan_quietest_frame_segments", return_value=[(0.0, 1.0), (1.0, 2.0)]),
                patch.object(stt_chunking, "extract_segment", new=AsyncMock(side_effect=extract)),
                patch.object(proxy.receipt_store, "begin"),
                patch.object(proxy.receipt_store, "finalize", return_value=True),
            ):
                responses = await asyncio.gather(
                    proxy.proxy_stt_request(request_for(b"request-A-private-audio"), access_context()),
                    proxy.proxy_stt_request(request_for(b"request-B-private-audio"), access_context()),
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
    async def test_probe_enforces_rate_and_channel_limits_but_duration_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            source.write_bytes(b"not examined by the fake probe")
            cases = (
                ("sample rate", _config(root, max_sample_rate=8000), 1, 8001, 1),
                ("channels", _config(root, max_channels=1), 1, 8000, 2),
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
        threshold/quiet-place chunking engage."""
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

    async def test_probe_leaves_duration_unknown_when_packets_also_have_no_duration(self) -> None:
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
                probed = await stt_chunking.probe_audio(
                    source,
                    config=config,
                    cancellation_waiters=(),
                    deadline_at=asyncio.get_running_loop().time() + 1,
                    on_cancel=None,
                )
                self.assertEqual(probed.duration_seconds, 0)

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
        # The same admission contract covers cuts at a quiet place and cuts with none.
        for quiet_at in ([], [20.0, 40.0]):
            with self.subTest(quiet_at=quiet_at), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = root / "source.wav"
                source.write_bytes(b"source")
                config = _config(root, max_segments=2)
                extract = AsyncMock()
                post = AsyncMock()
                with (
                    patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(75, 8000, 1))),
                    planning_pcm(75, quiet_at),
                    patch.object(stt_chunking, "extract_segment", new=extract),
                ):
                    with self.assertRaises(stt_chunking.SttResourceLimitExceeded):
                        await stt_chunking.execute_chunking(
                            source_path=source, source_stem="source", fields=[("response_format", "json")],
                            config=config, post_file=post, cancellation_waiters=(),
                            deadline_at=asyncio.get_running_loop().time() + 1, on_cancel=None,
                        )
                extract.assert_not_awaited()
                post.assert_not_awaited()
        with self.assertRaises(stt_chunking.SttDeadlineExceeded):
            await stt_chunking.await_with_cancellation(
                asyncio.Future(), deadline_at=asyncio.get_running_loop().time() + 0.01,
            )

    async def test_real_offline_ffmpeg_fixture_covers_segments_and_cleanup(self) -> None:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("ffmpeg and ffprobe are required for the local media fixture")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, max_decoded_bytes=4 * 1024 * 1024)
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
            self.assertEqual(outcome.merged_payload, {"text": "part-1 part-2 part-3 part-4"})
            self.assertEqual(len(uploaded), 4)
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
                response = await proxy.proxy_stt_request(request, access_context())

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
                response = await proxy.proxy_stt_request(request, access_context())

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
                task = asyncio.create_task(proxy.proxy_stt_request(request, access_context()))
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
            config = _config(root, enabled=False, native_long_audio_offline=True)
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
                task = asyncio.create_task(proxy.proxy_stt_request(request, access_context()))
                await asyncio.wait_for(_BlockingSttClient.entered.wait(), 1)
                disconnect.set()
                response = await asyncio.wait_for(task, 1)

            self.assertEqual(response.status_code, 499)
            self.assertEqual(finalized, began)
            self.assertEqual(finalized[0].status, "cancelled")
            self.assertEqual(finalized[0].metadata["stt"]["attempts_started"], 1)  # the whole-file request was started
            self.assertTrue(_BlockingSttClient.closed)
            self.assertEqual(list((root / "spool").glob("*")), [])

    async def test_real_asgi_disconnect_wins_every_stt_await_boundary(self) -> None:
        """The persistent ASGI watcher wins probe, media, upload, and read races."""
        for boundary in ("probe", "plan", "extract", "chunk_post", "response_read", "fallback_post"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = _config(root, enabled=boundary != "fallback_post", native_long_audio_offline=boundary == "fallback_post")
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
                    if "s16le" in command:
                        await block_or_return("plan", cancellation_waiters, deadline_at, on_cancel)
                        Path(command[-1]).write_bytes(pcm_bytes(75, [20.0, 40.0]))
                        return b"", b""
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
                    task = asyncio.create_task(proxy.proxy_stt_request(request, access_context()))
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
                content = b"RIFF-direct"
                if kind == "short":
                    source = root / "short.wav"
                    with wave.open(os.fspath(source), "wb") as handle:
                        handle.setnchannels(1)
                        handle.setsampwidth(2)
                        handle.setframerate(8000)
                        handle.writeframes(b"\0" * 32_000)
                    content = source.read_bytes()
                body, content_type = _multipart_body(content=content, fields=fields)
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
                    self.assertIn(content, await outgoing.aread())
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
                finalized = []
                with (
                    patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                    patch.object(proxy.httpx, "AsyncClient", side_effect=client_factory),
                    patch.object(stt_chunking, "probe_audio", new=probe),
                    patch.object(proxy.receipt_store, "begin"),
                    patch.object(proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True),
                ):
                    response = await proxy.proxy_stt_request(request, access_context())

                # The whole-file request is an upstream attempt, so the receipt does not say "0 attempts".
                self.assertEqual((finalized[0].metadata["stt"]["attempts_started"], finalized[0].metadata["stt"]["chunked"]), (1, False))
                self.assertNotIn("chunk_attempts", finalized[0].metadata["stt"])
                self.assertEqual(response.status_code, 203)
                self.assertEqual(response.body, b'{"text":"direct","segments":[]}')
                self.assertEqual(response.headers["x-local-transport"], "yes")
                self.assertNotIn("connection", response.headers)
                self.assertEqual(len(received), 1)
                self.assertTrue(str(received[0].url).endswith("/v1/audio/transcriptions"))
                # Eligible short audio is probed for planning; an ineligible envelope without a declared
                # maximum goes straight through without a duration probe.
                self.assertEqual(probe.await_count, 1 if kind == "short" else 0)
                self.assertEqual(list((root / "spool").glob("*")), [])

    async def test_an_ineligible_long_request_is_refused_before_an_upstream_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, threshold_seconds=40, target_seconds=30, max_segment_seconds=40)
            body, content_type = _multipart_body(content=b"RIFF-long", fields=(("model", "whisper-1"),
                                                                              ("response_format", "verbose_json")))
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Future()

            contacted = []

            class NeverCalled:
                def __init__(self, *args, **kwargs):
                    pass

                async def post(self, *args, **kwargs):
                    contacted.append("post")

                async def aclose(self):
                    return None

            request = Request(
                {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
                 "headers": [(b"content-type", content_type.encode())]}, receive)
            finalized = []
            with (
                patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                patch.object(proxy.httpx, "AsyncClient", NeverCalled),
                patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(120, 16000, 1))),
                planning_pcm(120),
                patch.object(proxy.receipt_store, "begin"),
                patch.object(proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True),
            ):
                response = await proxy.proxy_stt_request(request, access_context())

            assert response.status_code == 422
            assert json.loads(response.body)["error"]["code"] == "stt_audio_cannot_be_split"
            assert contacted == []
            assert finalized[0].metadata["stt"]["attempts_started"] == 0

    async def test_the_route_refuses_a_long_unplannable_file_in_the_gateways_words_and_posts_nothing(self) -> None:
        for reason, error, status, code in (
            ("undecodable_media", stt_chunking.SttMediaCommandFailed("secret-path /srv/x ffmpeg said sk-1234"), 400, "invalid_value"),
            ("split_failed", stt_chunking.SttSetupFailure("secret-path /srv/x"), 502, "stt_audio_cannot_be_split"),
        ):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = _config(root, target_seconds=30, threshold_seconds=40, max_segment_seconds=40, max_duration_seconds=3000)
                body, content_type = _multipart_body(content=b"RIFF-long", fields=(("model", "whisper-1"),))
                sent = False

                async def receive():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    await asyncio.Future()

                contacted = []

                class NeverCalled:
                    def __init__(self, *args, **kwargs):
                        pass

                    def build_request(self, *args, **kwargs):
                        contacted.append("build_request")

                    async def post(self, *args, **kwargs):
                        contacted.append("post")

                    async def send(self, *args, **kwargs):
                        contacted.append("send")

                    async def aclose(self):
                        return None

                request = Request(
                    {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", content_type.encode())]},
                    receive,
                )
                finalized = []
                with (
                    patch.object(proxy, "_stt_execution_snapshot", return_value=config),
                    patch.object(proxy.httpx, "AsyncClient", NeverCalled),
                    patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(1320, 16000, 1))),
                    patch.object(stt_chunking, "decode_planning_pcm", new=AsyncMock(side_effect=error)),
                    patch.object(proxy.receipt_store, "begin"),
                    patch.object(proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True),
                ):
                    response = await proxy.proxy_stt_request(request, access_context())

                self.assertEqual(response.status_code, status)
                payload = json.loads(response.body)["error"]
                self.assertEqual(payload["code"], code)
                if status == 400:
                    self.assertEqual(payload["message"], "Audio file might be corrupted or unsupported")
                else:
                    self.assertIn("recorded or measured 40-second maximum", payload["message"])
                    self.assertNotIn("longer than", payload["message"])
                self.assertNotIn("secret-path", response.body.decode())
                self.assertNotIn("sk-1234", response.body.decode())
                self.assertEqual((finalized[0].status, finalized[0].metadata["stt"]["attempts_started"]), ("failed", 0))
                self.assertEqual(contacted, [])
                self.assertEqual(list((root / "spool").glob("*")), [])

    async def test_decoded_direct_success_error_and_redirect_drop_representation_headers(self) -> None:
        real_async_client = httpx.AsyncClient
        for status, encoding in ((200, "gzip"), (500, "deflate"), (302, "gzip, deflate")):
            with self.subTest(status=status, encoding=encoding), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = _config(root, enabled=False, native_long_audio_offline=True)
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
                    response = await proxy.proxy_stt_request(request, access_context())

                self.assertEqual(response.status_code, status)
                if status >= 400:
                    # an engine's error reply is not relayed, its words or its headers (decision D44)
                    self.assertNotIn(b"decoded", response.body)
                    self.assertNotIn("x-safe", response.headers)
                    for header in ("content-encoding", "etag", "digest"):
                        self.assertNotIn(header, response.headers)
                    self.assertEqual(json.loads(response.body)["error"]["message"], "The engine failed while handling the request.")
                    continue
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
                access_context(),
                snapshot.service_id,
                "/v1/audio/transcriptions",
                backend_config=snapshot.backend_config,
            )
        self.assertEqual(headers["x-stt-key"], "configured-only-for-test")
        self.assertNotIn("authorization", {name.lower() for name in headers})

    def test_selected_backend_policy_and_receipt_never_use_the_legacy_stt_literal(self) -> None:
        receipt = proxy._stt_receipt(
            request=type("Request", (), {"url": type("Url", (), {"path": "/v1/audio/transcriptions"})()})(),
            auth_context=access_context(),
            operation="transcriptions",
            service_id="stt-selected",
        )
        seen = []

        class Decision:
            denied = False

        with patch.object(proxy, "evaluate_and_apply", side_effect=lambda _identity, policy, applied: seen.append((policy.backend, applied.backend)) or Decision()):
            proxy._apply_selected_stt_policy(receipt, access_context(), operation="transcriptions")
        self.assertEqual(seen, [("stt-selected", "stt-selected")])

    def test_receipt_shape_omits_hostile_upload_data(self) -> None:
        receipt = proxy._stt_receipt(
            request=type("Request", (), {"url": type("Url", (), {"path": "/v1/audio/transcriptions"})()})(),
            auth_context=access_context(),
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
                response = await proxy.proxy_stt_request(request, access_context(),
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
