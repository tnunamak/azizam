"""Diarized transcription: `gpt-4o-transcribe-diarize` and `stt-diarize` route to the diarize service unchanged."""

from __future__ import annotations

import json
import unittest

import httpx

from tests.speech_tier_helpers import SttCase, _Chunks

DIARIZED = {
    "task": "transcribe",
    "duration": 4.2,
    "text": "Hello there. Hi.",
    "segments": [
        {"type": "transcript.text.segment", "id": "seg_001", "start": 0.0, "end": 1.9, "speaker": "A", "text": "Hello there."},
        {"type": "transcript.text.segment", "id": "seg_002", "start": 2.1, "end": 3.0, "speaker": "B", "text": "Hi."},
    ],
    "usage": {"type": "duration", "seconds": 4.2},
}


class DiarizeRoutingTests(SttCase):
    def _diarize_upstream(self):
        return self.use_upstream(lambda r: httpx.Response(200, json=DIARIZED))

    def test_every_diarize_name_routes_to_the_diarize_service(self) -> None:
        seen = self._diarize_upstream()
        for model in ("gpt-4o-transcribe-diarize", "gpt-4o-transcribe-diarize-2026-01-01", "stt-diarize", "nemotron-diarize"):
            with self.subTest(model=model):
                resp = self._post([("model", model), ("response_format", "diarized_json")])
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(seen[-1].url.port, 8014)
                self.assertIn(b"\r\n\r\nnemotron-3-diarization\r\n", seen[-1].content)

    def test_diarized_json_passes_through_untouched(self) -> None:
        self._diarize_upstream()
        resp = self._post([("model", "gpt-4o-transcribe-diarize"), ("response_format", "diarized_json"), ("chunking_strategy", "auto")])
        self.assertEqual(resp.json(), DIARIZED)

    def test_request_fields_reach_the_service_including_repeated_known_speakers(self) -> None:
        seen = self._diarize_upstream()
        resp = self._post([
            ("model", "gpt-4o-transcribe-diarize"), ("response_format", "diarized_json"), ("chunking_strategy", "auto"),
            ("known_speaker_names[]", "agent"), ("known_speaker_names[]", "customer"),
            ("known_speaker_references[]", "data:audio/wav;base64,AAAA"), ("known_speaker_references[]", "data:audio/wav;base64,BBBB"),
        ])
        self.assertEqual(resp.status_code, 200, resp.text)
        body = seen[0].content
        self.assertEqual(body.count(b'name="known_speaker_names[]"'), 2)
        self.assertEqual(body.count(b'name="known_speaker_references[]"'), 2)
        self.assertIn(b'name="chunking_strategy"', body)
        self.assertIn(b"\r\n\r\ndiarized_json\r\n", body)

    def test_known_speaker_references_may_be_large_data_urls(self) -> None:
        # The default multipart field limit is 64 KiB; a 10 s WAV sample is about 430 KB as base64.
        seen = self._diarize_upstream()
        big = "data:audio/wav;base64," + "A" * 600_000
        resp = self._post([("model", "stt-diarize"), ("response_format", "diarized_json"), ("known_speaker_names[]", "a"), ("known_speaker_references[]", big)])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn(big.encode(), seen[0].content)

    def test_the_gateway_never_chunks_a_diarize_request(self) -> None:
        # Gateway chunking would give every chunk its own speaker labels.
        self.assertFalse(self.cfg["backends"]["stt-diarize"]["chunking"]["enabled"])
        seen = self._diarize_upstream()
        resp = self._post([("model", "gpt-4o-transcribe-diarize"), ("response_format", "json")])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(seen), 1)

    def test_the_diarize_service_is_started_on_demand(self) -> None:
        self.health.return_value = False
        self._diarize_upstream()
        self.assertEqual(self._post([("model", "stt-diarize")]).status_code, 200)
        self.ensure.assert_awaited_once_with("stt-diarize", None)

    def test_segment_events_stream_through_unchanged(self) -> None:
        events = [
            b'data: {"type":"transcript.text.delta","delta":"Hello there. ","segment_id":"seg_001"}\n\n',
            b'data: {"type":"transcript.text.segment","id":"seg_001","start":0.0,"end":1.9,"speaker":"A","text":"Hello there."}\n\n',
            b'data: {"type":"transcript.text.done","text":"Hello there."}\n\n',
            b"data: [DONE]\n\n",
        ]
        seen = self.use_upstream(lambda r: httpx.Response(200, stream=_Chunks(events), headers={"content-type": "text/event-stream"}))
        resp = self._post([("model", "gpt-4o-transcribe-diarize"), ("response_format", "diarized_json"), ("stream", "true")])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.headers["content-type"].startswith("text/event-stream"))
        self.assertIn(b'name="stream"', seen[0].content)
        kinds = [json.loads(line[6:])["type"] for line in resp.text.splitlines() if line.startswith("data: {")]
        self.assertEqual(kinds, ["transcript.text.delta", "transcript.text.segment", "transcript.text.done"])

    def test_service_400_is_returned_as_is(self) -> None:
        error = {"error": {"message": "known speakers", "type": "invalid_request_error", "param": "known_speaker_names[]", "code": "invalid_known_speakers"}}
        self.use_upstream(lambda r: httpx.Response(400, json=error))
        resp = self._post([("model", "gpt-4o-transcribe-diarize"), ("known_speaker_names[]", "a")])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), error)


class DiarizeNotDeployedTests(SttCase):
    def setUp(self) -> None:
        super().setUp()
        del self.cfg["backends"]["stt-diarize"]

    def test_without_the_service_the_diarize_names_are_a_clear_400_not_plain_text(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(200, json={"text": "no speakers"}))
        for model in ("gpt-4o-transcribe-diarize", "stt-diarize"):
            with self.subTest(model=model):
                resp = self._post([("model", model), ("response_format", "diarized_json")])
                self.assertEqual(resp.status_code, 400, resp.text)
                self.assertEqual(resp.json()["error"]["code"], "model_not_deployed")
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
