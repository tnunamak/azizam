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

    def test_the_default_services_operator_override_still_blocks_an_alias_request(self) -> None:
        from unittest.mock import patch

        import proxy

        # The override that blocks every speech request is the one on the selected default speech service.
        default_id = proxy._speech_service()[0]
        self.assertNotEqual(default_id, proxy.resolve_model_for_operation("speech", "chatterbox").alias.service)
        overrides = {"service_availability": {default_id: {"state": "maintenance", "retry_after_seconds": 31}}, "media_job_admission": {}}
        with patch.object(proxy.ADMIN_STORE, "operator_overrides", return_value=overrides):
            resp = self.speak("alloy", model="chatterbox")
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.headers["retry-after"], "31")
        self.assertEqual(_FakeClient.requests, [])
