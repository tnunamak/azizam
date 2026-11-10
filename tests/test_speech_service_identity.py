"""The speech and transcription services are the services selected for their capabilities, not records of a fixed id.

An install may name its speech engine "voice-one" and its transcription engine "ears-one". The gateway then serves
them exactly as it serves records named "tts" and "stt" that declare the same capabilities.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

import proxy
from tests.support.config_snapshot import derived_patch

def _config(**backends: Any) -> dict[str, Any]:
    """The test config with only *backends* as speech and transcription services."""
    cfg = dict(copy.deepcopy(proxy.config))
    others = dict(cfg.get("backends") or {})
    for capability in ("audio.speech", "audio.transcriptions"):
        for service_id in proxy.backends_for_capability(capability, backends=others, include_disabled=True):
            others.pop(service_id)
    others.pop("tts", None)
    others.pop("stt", None)
    cfg["backends"] = {**others, **backends}
    return cfg


def _speech(port: int, **extra: Any) -> dict[str, Any]:
    return {"host": "127.0.0.1", "port": port, "capabilities": ["audio", "speech"], **extra}


def _transcription(port: int, **extra: Any) -> dict[str, Any]:
    return {"host": "127.0.0.1", "port": port, "capabilities": ["audio", "transcriptions"], **extra}


@pytest.mark.parametrize("reader, capability_record, compat_key", [
    ("tts_backend_model", _speech, "speech"),
    ("stt_backend_model", _transcription, "transcriptions"),
])
def test_the_backend_model_is_the_selected_records_default_model(monkeypatch, reader, capability_record, compat_key):
    monkeypatch.setattr(proxy, "model_compat_defaults", lambda: {})
    monkeypatch.setattr(proxy, "config", _config(**{"renamed-one": capability_record(18101, default_model="model-a")}))
    assert getattr(proxy, reader)() == "model-a"
    monkeypatch.setattr(proxy, "model_compat_defaults", lambda: {compat_key: "model-b"})
    assert getattr(proxy, reader)() == "model-b"


def test_the_module_holds_no_captured_speech_record():
    for name in ("tts_cfg", "stt_cfg", "_TTS_BACKEND_ID"):
        assert not hasattr(proxy, name), name


# -- health, default voices and translation read the selected service ------------------------------------------------

def test_the_default_voices_are_the_selected_speech_services_engine(monkeypatch):
    cfg = _config(**{"voice-one": _speech(18101), "voice-two": _speech(18102, priority=200)})
    cfg["model_aliases"] = {**(cfg.get("model_aliases") or {}), "speech": {"engines": {
        "engine-two": {"service": "voice-two", "voice_map": {"b": "b"}},
        "engine-one": {"service": "voice-one", "voice_map": {"a": "a"}},
    }}}
    monkeypatch.setattr(proxy, "config", cfg)
    assert proxy._default_speech_engine()["service"] == "voice-one"
    monkeypatch.setattr(proxy, "config", _config())
    assert proxy._default_speech_engine() == {}


def test_translation_support_is_read_from_the_selected_transcription_service(monkeypatch):
    monkeypatch.setattr(proxy, "config", _config(**{"ears-one": _transcription(18101, capabilities=[
        "audio", "transcriptions", "translations"])}))
    assert proxy.stt_backend_supports_translation() is True
    monkeypatch.setattr(proxy, "config", _config(**{
        "ears-one": _transcription(18101), "ears-two": _transcription(18102, priority=200, capabilities=[
            "audio", "transcriptions", "translations"])}))
    assert proxy.stt_backend_supports_translation() is False


# -- speech routing and operator overrides follow the selected service -----------------------------------------------

from unittest.mock import patch  # noqa: E402

from tests.test_custom_voice_speech import CustomVoiceRouteCase, _FakeClient  # noqa: E402


class RenamedDefaultSpeechRouteTests(CustomVoiceRouteCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg["backends"].pop("tts", None)
        self.cfg["backends"]["voice-one"] = _speech(18101, priority=10)
        patcher = derived_patch("TTS_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # A model with no alias of its own: the selected default service serves it.
        resolution = patch.object(proxy, "resolve_model_for_operation", lambda op, model: proxy.ModelResolution(model, model, op))
        resolution.start()
        self.addCleanup(resolution.stop)

    def test_the_selected_service_receives_the_default_model_request(self) -> None:
        self.assertEqual(proxy._speech_service()[0], "voice-one")
        resp = self.speak("alloy", model="tts-1")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(_FakeClient.requests[-1]["url"], "http://127.0.0.1:18101/v1/audio/speech")

    def test_the_selected_services_operator_override_blocks_the_request(self) -> None:
        self.assertEqual(proxy._speech_service()[0], "voice-one")
        next(p for p in self.patches if getattr(p, "attribute", None) == "_service_availability_response").stop()
        overrides = {
            "service_availability": {"voice-one": {"state": "maintenance", "retry_after_seconds": 31}},
            "media_job_admission": {},
        }
        with patch.object(proxy.ADMIN_STORE, "operator_overrides", return_value=overrides):
            resp = self.speak("alloy", model="tts-1")
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.headers["retry-after"], "31")
        self.assertEqual(_FakeClient.requests, [])

    def test_an_override_on_the_old_service_id_does_not_block_the_request(self) -> None:
        self.assertEqual(proxy._speech_service()[0], "voice-one")
        next(p for p in self.patches if getattr(p, "attribute", None) == "_service_availability_response").stop()
        overrides = {
            "service_availability": {"tts": {"state": "maintenance", "retry_after_seconds": 31}},
            "media_job_admission": {},
        }
        with patch.object(proxy.ADMIN_STORE, "operator_overrides", return_value=overrides):
            resp = self.speak("alloy", model="tts-1")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(_FakeClient.requests[-1]["url"], "http://127.0.0.1:18101/v1/audio/speech")

    def test_with_no_speech_service_selected_the_request_is_refused_and_nothing_is_sent(self) -> None:
        for service_id in proxy.backends_for_capability("audio.speech", backends=self.cfg["backends"], include_disabled=True):
            self.cfg["backends"].pop(service_id)
        self.assertIsNone(proxy._speech_service())
        resp = self.speak("alloy", model="tts-1")
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(_FakeClient.requests, [])

    def test_a_disabled_selected_service_is_refused_and_no_lower_service_takes_its_place(self) -> None:
        self.cfg["backends"]["voice-zero"] = _speech(18105, priority=5, enabled=False)
        self.assertEqual(proxy._speech_service()[0], "voice-zero")
        # The derived settings of this config say speech is off (test_service_role_contract.py: test_a_disabled_winner_does_not_fall_through).
        with derived_patch("TTS_ENABLED", False):
            resp = self.speak("alloy", model="tts-1")
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(_FakeClient.requests, [])
