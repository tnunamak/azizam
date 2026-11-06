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

    def test_valid_image_decodes_and_wrong_image_rejected(self):
        case = {"id": "image", "kind": "image"}
        grade(case, 200, {"content-type": "image/png"}, PNG, [], direct=True)
        with self.assertRaises(Exception):
            grade(case, 200, {"content-type": "image/png"}, b"not png", [], direct=True)

    def test_native_wav_fixture_preserves_pcm_after_metadata_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            src, tagged, stripped = [Path(folder) / p for p in ("source.wav", "tagged.wav", "stripped.wav")]
            src.write_bytes(WAV)
            ffmpeg_metadata(["ffmpeg", "-i", str(src), "-metadata", "comment=fixture", str(tagged)])
            self.assertIn(b"fixture", tagged.read_bytes())
            ffmpeg_metadata(["ffmpeg", "-i", str(tagged), "-map_metadata", "-1", str(stripped)])
            self.assertEqual(stripped.read_bytes(), WAV)


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


if __name__ == "__main__":
    unittest.main()
