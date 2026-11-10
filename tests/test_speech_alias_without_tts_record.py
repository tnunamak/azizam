"""A speech model whose alias names its own service does not need a "tts" record.

An engine address rule makes a record that names no address "not configured", so DERIVED.TTS_ENABLED is False when
the gateway has no "tts" record. A request for a model whose alias names another speech service, with an address,
must still reach that service; only a request for "tts" itself needs the "tts" record.
"""

from __future__ import annotations

from tests.support.config_snapshot import derived_patch
from tests.test_custom_voice_speech import CustomVoiceRouteCase, _FakeClient


class SpeechWithoutTtsRecordTests(CustomVoiceRouteCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg["backends"].pop("tts", None)
        patcher = derived_patch("TTS_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_alias_engine_with_an_address_is_reached(self) -> None:
        resp = self.speak("alloy", model="chatterbox")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(_FakeClient.requests[-1]["url"], "http://127.0.0.1:8015/v1/audio/speech")

    def test_a_request_for_the_tts_record_itself_is_still_refused(self) -> None:
        resp = self.client.post("/v1/audio/speech", json={"model": "no-such-speech-model", "voice": "alloy", "input": "hello"})
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertIn("TTS not enabled", resp.text)
        self.assertEqual(_FakeClient.requests, [])
