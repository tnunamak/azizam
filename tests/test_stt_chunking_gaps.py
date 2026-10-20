"""Gap coverage for STT chunking: extraction fallback, cancellation ties, cleanup, multipart limits, media output, disk reservations."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from gateway import stt_chunking
from test_stt_chunking import _config
from tests.support.stt_pcm import pcm_bytes


class SttChunkingGapTests(unittest.IsolatedAsyncioTestCase):
    async def test_extraction_failure_policy_depends_on_started_attempts(self):
        # Only the process boundary is faked: a scripted ffprobe/ffmpeg whose
        # N-th segment extraction exits non-zero.  Probe, the quiet-place search,
        # planning and extraction run for real.
        class Pipe:
            def __init__(self, data):
                self.data = data

            async def read(self, size):
                data, self.data = self.data[:size], self.data[size:]
                return data

        class Process:
            def __init__(self, stdout=b"", stderr=b"", returncode=0):
                self.stdout, self.stderr = Pipe(stdout), Pipe(stderr)
                self.returncode = returncode

            async def wait(self):
                return self.returncode

            def kill(self):
                raise AssertionError("scripted process already exited")

        for failure_index in (0, 1):
            for policy in ("passthrough", "error"):
                with self.subTest(failure_index=failure_index, policy=policy):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        root = Path(temp_dir)
                        source = root / "source.wav"
                        source.write_bytes(b"original-audio")
                        config = _config(root, spool_dir=str(root.parent), setup_failure_policy=policy, max_segment_seconds=100)
                        extracted = []
                        durations = {}
                        posted = []
                        extracted_payloads = []
                        started = []

                        async def spawn(*command, **kwargs):
                            tool, args = command[0], list(command)
                            target = Path(args[-1])
                            if tool == "ffprobe":
                                duration = durations.get(target.name, 75)
                                payload = {
                                    "format": {"duration": str(duration)},
                                    "streams": [
                                        {"codec_type": "audio", "sample_rate": "8000", "channels": 1}
                                    ],
                                }
                                return Process(json.dumps(payload).encode())
                            if target.suffix == ".pcm":
                                input_name = Path(args[args.index("-i") + 1]).name
                                target.write_bytes(pcm_bytes(durations.get(input_name, 75), [20.0, 40.0], sample_rate=int(args[args.index("-ar")+1]), channels=int(args[args.index("-ac")+1])))
                                return Process()
                            extracted.append(target.name)
                            if len(extracted) - 1 == failure_index:
                                return Process(returncode=1)
                            start = float(args[args.index("-ss") + 1])
                            length = float(args[args.index("-t") + 1])
                            durations[target.name] = length
                            with wave.open(str(target), "wb") as audio:
                                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                                audio.writeframes(pcm_bytes(length))
                            extracted_payloads.append(target.read_bytes())
                            return Process()

                        async def post(path, filename):
                            posted.append((filename, path.read_bytes()))
                            return stt_chunking.UpstreamResponse(
                                200, b'{"text":"accepted"}', {}
                            )

                        with patch.object(
                            stt_chunking.asyncio, "create_subprocess_exec", new=spawn
                        ):
                            run = stt_chunking.execute_chunking(
                                source_path=source,
                                source_stem="source",
                                fields=[("response_format", "json")],
                                config=config,
                                post_file=post,
                                cancellation_waiters=(),
                                deadline_at=asyncio.get_running_loop().time() + 10,
                                on_cancel=None,
                                on_attempt_started=started.append,
                            )
                            if failure_index == 0 and policy == "passthrough":
                                result = await run
                                self.assertEqual(
                                    (result.mode, result.attempts_started, result.fallback_reason),
                                    ("passthrough", 0, "setup_failed_before_attempt"),
                                )
                            else:
                                with self.assertRaises(stt_chunking.SttChunkAttemptError):
                                    await run

                        self.assertEqual(
                            posted,
                            [] if failure_index == 0 else [("source.chunk000.wav", extracted_payloads[0])],
                        )
                        self.assertEqual(started, [] if failure_index == 0 else [1])
                        self.assertEqual(len(extracted), failure_index + 1)
                        self.assertEqual(list(root.glob("segment-*.wav")), [])
                        self.assertEqual(source.read_bytes(), b"original-audio")

    async def test_cancellation_wins_an_exact_operation_completion_tie(self):
        cancellation_calls = []
        returned = object()

        async def operation():
            return returned

        async def disconnect():
            return None

        async def all_ready(tasks, **kwargs):
            # Deterministically expose a done set containing BOTH completions.
            # No sleep or scheduler timing is used to manufacture the tie.
            self.assertEqual(kwargs["return_when"], asyncio.FIRST_COMPLETED)
            await asyncio.gather(*tasks)
            return set(tasks), set()

        with patch.object(stt_chunking.asyncio, "wait", side_effect=all_ready):
            with self.assertRaises(stt_chunking.SttCancelled):
                await stt_chunking.await_with_cancellation(
                    operation(),
                    cancellation_waiters=(disconnect,),
                    on_cancel=lambda: cancellation_calls.append("cancel"),
                )
        self.assertEqual(cancellation_calls, ["cancel"])

    async def test_cancelled_cleanup_finishes_inflight_and_remaining_actions_once(self):
        owner = stt_chunking.SttCleanupOwner()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def slow_cleanup():
            entered.set()
            await release.wait()
            calls.append("slow")

        owner.add("remaining", lambda: calls.append("remaining"))
        owner.add("slow", slow_cleanup)
        task = asyncio.create_task(owner.aclose())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(calls, ["slow", "remaining"])
        self.assertFalse(owner.failed)
        await owner.aclose()
        self.assertEqual(calls, ["slow", "remaining"])

    async def test_multipart_limits_cover_aggregate_files_counts_and_utf8_bytes(self):
        # Each part is (field name, optional filename, raw bytes).
        cases = (
            ("aggregate-exact", {"max_input_bytes": 6},
             [("file", "one.wav", b"abc"), ("file", "two.wav", b"def")], False),
            ("aggregate-over", {"max_input_bytes": 6},
             [("file", "one.wav", b"abcd"), ("file", "two.wav", b"efg")], True),
            ("file-count", {"max_files": 1},
             [("file", "one.wav", b"a"), ("file", "two.wav", b"b")], True),
            ("field-count", {"max_fields": 1},
             [("prompt", None, b"a"), ("language", None, b"en")], True),
            ("utf8-exact", {"max_field_bytes": 4},
             [("prompt", None, "éé".encode()), ("file", "../../escape.wav", b"a")], False),
            ("utf8-over", {"max_field_bytes": 4},
             [("file", "one.wav", b"a"), ("prompt", None, "ééé".encode())], True),
        )
        for name, overrides, parts, rejected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = _config(root, **overrides)
                boundary = "gap-boundary"
                body = bytearray()
                for field_name, filename, content in parts:
                    body.extend(("--" + boundary + "\r\n").encode())
                    disposition = 'Content-Disposition: form-data; name="' + field_name + '"'
                    if filename is not None:
                        disposition += '; filename="' + filename + '"'
                    body.extend((disposition + "\r\n\r\n").encode())
                    body.extend(content)
                    body.extend(b"\r\n")
                body.extend(("--" + boundary + "--\r\n").encode())
                chunks = iter(bytes(body[i:i + 3]) for i in range(0, len(body), 3))

                async def receive():
                    try:
                        chunk = next(chunks)
                    except StopIteration:
                        return {"type": "http.request", "body": b"", "more_body": False}
                    return {"type": "http.request", "body": chunk, "more_body": True}

                request = Request({
                    "type": "http", "method": "POST",
                    "headers": [(b"content-type",
                                 ("multipart/form-data; boundary=" + boundary).encode())],
                }, receive)
                if rejected:
                    with self.assertRaises(stt_chunking.MultiPartException):
                        await stt_chunking.parse_bounded_multipart(request, config)
                else:
                    parsed = await stt_chunking.parse_bounded_multipart(request, config)
                    try:
                        fields, files = stt_chunking.upload_file_parts(parsed.form)
                        self.assertEqual(
                            fields,
                            [(n, value.decode()) for n, filename, value in parts if filename is None],
                        )
                        self.assertEqual(
                            [await upload.read() for _, upload in files],
                            [value for _, filename, value in parts if filename is not None],
                        )
                        for _, upload in files:
                            self.assertTrue(
                                stt_chunking.source_path(upload).is_relative_to(parsed.workspace)
                            )
                        self.assertLessEqual(
                            parsed.parser.max_parsed_field_bytes, config.max_field_bytes
                        )
                    finally:
                        await parsed.aclose()
                self.assertEqual(list(config.spool_dir.iterdir()), [])
                self.assertFalse((root / "escape.wav").exists())

    async def test_media_stdout_and_stderr_are_bounded_and_overflow_reaps_process(self):
        class Reader:
            def __init__(self, chunks):
                self.chunks = iter(chunks)

            async def read(self, size):
                return next(self.chunks, b"")

        class Process:
            def __init__(self, stdout, stderr):
                self.stdout = Reader(stdout)
                self.stderr = Reader(stderr)
                self.returncode = None
                self.kills = 0
                self.waits = 0

            def kill(self):
                self.kills += 1
                self.returncode = -9

            async def wait(self):
                self.waits += 1
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        for overflowing in (None, "stdout", "stderr"):
            with self.subTest(overflowing=overflowing):
                process = Process(
                    [b"ab", b"cd", b"e"] if overflowing == "stdout" else [b"ab", b"cd"],
                    [b"12", b"34", b"5"] if overflowing == "stderr" else [b"12", b"34"],
                )
                with patch.object(
                    stt_chunking.asyncio, "create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                ):
                    run = stt_chunking.run_media_command(
                        ["fake-media"], timeout_seconds=10,
                        cancellation_waiters=(),
                        deadline_at=asyncio.get_running_loop().time() + 20,
                        on_cancel=None, max_output_bytes=4,
                    )
                    if overflowing is None:
                        self.assertEqual(await run, (b"abcd", b"1234"))
                    else:
                        with self.assertRaises(stt_chunking.SttResponseLimitExceeded):
                            await run
                self.assertEqual(process.kills, int(overflowing is not None))
                self.assertEqual(process.waits, 1)


class SttReservationGapTests(unittest.TestCase):
    def test_reserved_disk_headroom_and_idempotent_release(self):
        mib = 1024 * 1024
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _config(
                Path(temp_dir), global_concurrency=10,
                max_input_bytes=mib, max_decoded_bytes=mib, min_free_bytes=mib,
            )
            gate = stt_chunking.SttResourceGate()
            # The first request grows from its upload bound to a 9 MiB workspace.
            # Another 1 MiB upload must still leave the 1 MiB free-space floor.
            with patch.object(stt_chunking, "_disk_free_bytes", return_value=10 * mib):
                first = gate.acquire(request_id="first", config=config)
                first.resize(9 * mib)
                try:
                    with self.assertRaisesRegex(
                        stt_chunking.SttResourceLimitExceeded, "reserved capacity"
                    ):
                        gate.acquire(request_id="second", config=config)
                    self.assertEqual((gate._active, gate._reserved_bytes), (1, 9 * mib))
                finally:
                    first.release()
                first.release()
                self.assertEqual((gate._active, gate._reserved_bytes), (0, 0))
                recovered = gate.acquire(request_id="recovered", config=config)
                recovered.release()

            with patch.object(stt_chunking, "_disk_free_bytes", return_value=2 * mib - 1):
                with self.assertRaisesRegex(
                    stt_chunking.SttResourceLimitExceeded, "free capacity"
                ):
                    gate.acquire(request_id="below-floor", config=config)
            self.assertEqual((gate._active, gate._reserved_bytes), (0, 0))
            with patch.object(stt_chunking, "_disk_free_bytes", return_value=2 * mib):
                exact = gate.acquire(request_id="exact-floor", config=config)
                exact.release()
            self.assertEqual((gate._active, gate._reserved_bytes), (0, 0))
