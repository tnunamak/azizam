"""Validate the grader and isolation tripwires before trusting an eval score."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .fixtures import PNG, TEXT, TOKENS, WAV
from .grader import CorrectnessError, grade
from .host_fixtures import ffmpeg_metadata


class GraderTests(unittest.TestCase):
    def test_degraded_status_html_is_rejected(self):
        case = {"id": "status", "kind": "status"}
        headers = {"content-type": "text/html"}
        for message in ("status build failed", "status render failed"):
            with self.subTest(message=message):
                with self.assertRaisesRegex(CorrectnessError, "status: degraded status response"):
                    grade(case, 200, headers, f"<html>{message}</html>".encode(), [])

    def test_stream_rejects_wrong_media_type_before_reading_events(self):
        case = {"id": "chat_stream", "kind": "chat", "body": {"stream": True}}
        with self.assertRaisesRegex(CorrectnessError, "chat_stream: wrong SSE media type"):
            grade(case, 200, {"content-type": "application/json"}, b"", [])

    def test_bad_status_and_wrong_audio_are_rejected(self):
        case = {"id": "tts", "kind": "tts"}
        with self.assertRaises(CorrectnessError):
            grade(case, 500, {}, b"", [])
        with self.assertRaises(CorrectnessError):
            grade(case, 200, {"content-type": "audio/wav"}, b"wrong", [])
        grade(case, 200, {"content-type": "audio/wav"}, WAV, [])

    def test_stream_requires_every_token_terminal_and_usage(self):
        case = {"id": "chat_stream", "kind": "chat", "body": {"stream": True, "model": "demo-chat"}}
        parts = [{"object": "chat.completion.chunk", "model": "demo-chat", "choices": [{"delta": {"content": token}, "finish_reason": None}]} for token in TOKENS]
        parts += [{"object": "chat.completion.chunk", "model": "demo-chat", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 40}}]
        events = [json.dumps(p) for p in parts] + ["[DONE]"]
        headers = {"content-type": "text/event-stream"}
        grade(case, 200, headers, b"", events)
        for broken in (events[:-1], events[1:]):
            with self.assertRaises(CorrectnessError):
                grade(case, 200, headers, b"", broken)

    def test_responses_error_null_is_valid_but_error_object_is_not(self):
        case = {"id": "responses", "kind": "responses", "body": {"model": "demo-chat"}}
        payload = {"object": "response", "status": "completed", "model": "demo-chat", "error": None,
                   "output": [{"content": [{"text": TEXT}]}], "usage": {"total_tokens": 40}}
        grade(case, 200, {"content-type": "application/json"}, json.dumps(payload).encode(), [])
        payload["error"] = {"code": "failed"}
        with self.assertRaises(CorrectnessError):
            grade(case, 200, {"content-type": "application/json"}, json.dumps(payload).encode(), [])

    def test_health_checks_use_the_correct_status_for_each_path(self):
        case = {"id": "health", "kind": "health"}
        headers = {"content-type": "application/json"}
        grade(case, 200, headers, b'{"status":"ok"}', [], direct=True)
        grade(case, 200, headers, b'{"status":"healthy"}', [], direct=False)
        with self.assertRaisesRegex(CorrectnessError, "health: wrong health"):
            grade(case, 200, headers, b'{"status":"ok"}', [], direct=False)
        with self.assertRaisesRegex(CorrectnessError, "health: wrong health"):
            grade(case, 200, headers, b'{"status":"healthy"}', [], direct=True)

    def test_responses_stream_without_events_reports_incomplete_lifecycle(self):
        case = {"id": "responses_stream", "kind": "responses", "body": {"stream": True}}
        headers = {"content-type": "text/event-stream"}
        for events in ([], ["[DONE]"]):
            with self.subTest(events=events):
                with self.assertRaisesRegex(CorrectnessError, "^responses_stream: incomplete Responses lifecycle$"):
                    grade(case, 200, headers, b"", events)

    def test_responses_stream_requires_created_before_completed(self):
        case = {"id": "responses_stream", "kind": "responses", "body": {"stream": True}}
        headers = {"content-type": "text/event-stream"}
        events = [json.dumps({"type": "response.completed"}), json.dumps({"type": "response.created"})]
        with self.assertRaisesRegex(CorrectnessError, "responses_stream: incomplete Responses lifecycle"):
            grade(case, 200, headers, b"", events)

    def test_valid_image_decodes_and_wrong_image_rejected(self):
        case = {"id": "image", "kind": "image"}
        grade(case, 200, {"content-type": "image/png"}, PNG, [], direct=True)
        with self.assertRaises(Exception):
            grade(case, 200, {"content-type": "image/png"}, b"not png", [], direct=True)

    def test_transcription_with_wrong_text_is_rejected(self):
        case = {"id": "stt", "kind": "stt"}
        headers = {"content-type": "application/json"}
        grade(case, 200, headers, b'{"text":"controlled transcription"}', [])
        with self.assertRaisesRegex(CorrectnessError, "stt: wrong transcription"):
            grade(case, 200, headers, b'{"text":"a different transcription"}', [])

    def test_image_success_envelope_requires_an_output(self):
        case = {"id": "image", "kind": "image"}
        with self.assertRaisesRegex(CorrectnessError, "image: wrong image count"):
            grade(case, 200, {"content-type": "application/json"}, b'{"data":[]}', [])

    def test_native_wav_fixture_preserves_pcm_after_metadata_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            src, tagged, stripped = [Path(folder) / p for p in ("source.wav", "tagged.wav", "stripped.wav")]
            src.write_bytes(WAV)
            ffmpeg_metadata(["ffmpeg", "-i", str(src), "-metadata", "comment=fixture", str(tagged)])
            self.assertIn(b"fixture", tagged.read_bytes())
            ffmpeg_metadata(["ffmpeg", "-i", str(tagged), "-map_metadata", "-1", str(stripped)])
            self.assertEqual(stripped.read_bytes(), WAV)


class GraderFailureTests(unittest.TestCase):
    def test_catalog_rejects_wrong_row_kind_with_all_expected_names(self):
        from .fixtures import MODELS

        case = {"id": "catalog", "kind": "models"}
        headers = {"content-type": "application/json"}
        payload = {"object": "list", "data": [{"id": name, "object": "model"} for name in MODELS]}
        grade(case, 200, headers, json.dumps(payload).encode(), [], direct=True)
        payload["data"][0]["object"] = "list"
        with self.assertRaisesRegex(CorrectnessError, "catalog: wrong model shape"):
            grade(case, 200, headers, json.dumps(payload).encode(), [], direct=True)

    def test_transcription_rejects_empty_text_on_http_success(self):
        from .fixtures import TRANSCRIPT

        case = {"id": "transcription", "kind": "stt"}
        headers = {"content-type": "application/json"}
        grade(case, 200, headers, json.dumps({"text": TRANSCRIPT}).encode(), [])
        with self.assertRaisesRegex(CorrectnessError, "transcription: wrong transcription"):
            grade(case, 200, headers, b'{"text":""}', [])


class BoundaryTests(unittest.TestCase):
    def test_children_cannot_connect_or_launch_commands_or_write_outside_state(self):
        code = '''
import socket,sys,tempfile,os
from pathlib import Path
from bench.server import install_boundary
with tempfile.TemporaryDirectory() as folder:
    state=Path(folder).resolve()
    install_boundary(state)
    blocked=0
    for action in (lambda: socket.socket().connect(("127.0.0.1",5000)),
                   lambda: os.system("false"),
                   lambda: (state.parent/"must-not-exist-benchmark").write_text("bad")):
        try: action()
        except RuntimeError: blocked+=1
    assert blocked==3, blocked
'''
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class MissingMetricComparisonTests(unittest.TestCase):
    @staticmethod
    def _report():
        from .compare import METADATA_KEYS, PROVENANCE_REQUIRED
        metadata = dict.fromkeys(METADATA_KEYS, "fixture")
        metadata.update(profile=False, mix_hashes={"first": "hash-first"},
                        concurrency=1, reps=1, warmup=0)
        provenance = dict.fromkeys(PROVENANCE_REQUIRED, "fixture")
        provenance["network_namespace"] = False
        return {
            "schema_version": 1,
            "correctness": {"passed": True, "score": 1},
            "metadata": metadata,
            "provenance": provenance,
            "metrics": [{"mix": "first", "case": "case", "concurrency": 1,
                         "metric": "latency", "unit": "ms", "statistic": "mean",
                         "samples": [1.0, 1.0]}],
        }

    def _assert_rejected(self, bad, message):
        from contextlib import redirect_stderr, redirect_stdout
        from .compare import compare_documents, main
        valid = self._report()
        # Each constituent run must fail, even if another run has usable rows.
        for baseline, new in ((bad, bad), (valid, bad), (bad, valid),
                              ({"runs": [valid, bad]}, valid)):
            with self.subTest(baseline=baseline, new=new):
                with self.assertRaisesRegex(ValueError, message):
                    compare_documents(baseline, new, bootstrap=1)
        with tempfile.TemporaryDirectory() as folder:
            baseline = Path(folder) / "baseline.json"
            new = Path(folder) / "new.json"
            baseline.write_text(json.dumps(bad), encoding="utf-8")
            new.write_text(json.dumps(bad), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main([str(baseline), str(new)])
            self.assertEqual(status, 1)
            self.assertRegex(stderr.getvalue(), message)
            self.assertEqual(stdout.getvalue(), "")

    def test_report_readers_reject_invalid_metric_identity_types(self):
        from .compare import compare_documents
        from .report import aggregate_documents

        valid = self._report()
        for field, value, expected in (
            ("case", ["chat"], "str"),
            ("concurrency", True, "int"),
        ):
            with self.subTest(field=field):
                invalid = self._report()
                invalid["metrics"][0][field] = value
                message = f"metric identity {field} must be a {expected}"
                with self.assertRaisesRegex(ValueError, message):
                    aggregate_documents([invalid])
                with self.assertRaisesRegex(ValueError, message):
                    compare_documents(valid, invalid, bootstrap=1)

    def test_empty_metrics_are_rejected(self):
        report = self._report()
        report["metrics"] = []
        self._assert_rejected(report, "metrics must be a nonempty list")

    def test_advertised_mix_without_metrics_is_rejected(self):
        report = self._report()
        report["metadata"]["mix_hashes"]["second"] = "hash-second"
        self._assert_rejected(report, "mixes without metric rows: second")

    def test_metric_mix_absent_from_metadata_is_rejected(self):
        report = self._report()
        report["metrics"].append({**report["metrics"][0], "mix": "unlisted"})
        self._assert_rejected(report, "metric mix 'unlisted' is absent from metadata")

    def test_valid_multi_mix_preserves_comparison(self):
        from .compare import compare_documents
        first = self._report()
        second = self._report()
        second["metadata"]["mix_hashes"] = {"second": "hash-second"}
        second["metrics"][0]["mix"] = "second"
        second["metrics"][0]["samples"] = [2.0, 2.0]
        combined = self._report()
        combined["metadata"]["mix_hashes"].update(second["metadata"]["mix_hashes"])
        combined["metrics"] += second["metrics"]
        expected = (compare_documents(first, first, bootstrap=1)
                    + compare_documents(second, second, bootstrap=1))
        self.assertEqual(compare_documents(combined, combined, bootstrap=1), expected)
        self.assertEqual(compare_documents({"runs": [first, second]},
                                           {"runs": [first, second]}, bootstrap=1),
                         expected)


class UnreadableReportTests(unittest.TestCase):
    def test_non_utf8_input_error_identifies_the_report(self):
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch
        from .compare import main as compare_main
        from .report import main as report_main

        for main, args in (
            (compare_main, ["run-a.json", "run-b.json"]),
            (report_main, ["run-a.json", "--json", "pooled.json"]),
        ):
            with self.subTest(command=main.__module__):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(Path, "open", return_value=io.TextIOWrapper(
                    io.BytesIO(b'{"unit":"\xe9"}'), encoding="utf-8",
                )), redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(args), 1)
                self.assertIn("cannot read run-a.json:", stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
