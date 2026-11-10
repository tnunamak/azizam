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

KEYS = ("TTS_HOST", "STT_HOST", "TTS_ENABLED", "STT_ENABLED", "TTS_TIMEOUT", "STT_TIMEOUT")


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


def _derived(cfg: dict[str, Any]) -> dict[str, Any]:
    derived = proxy._derive_backend_constants(cfg)
    return {key: derived[key] for key in KEYS}


def test_renamed_services_derive_the_same_settings_as_tts_and_stt():
    legacy = _derived(_config(tts=_speech(18101), stt=_transcription(18102)))
    renamed = _derived(_config(**{"voice-one": _speech(18101), "ears-one": _transcription(18102)}))
    assert renamed == legacy
    assert legacy["TTS_HOST"] == "http://127.0.0.1:18101" and legacy["TTS_ENABLED"] is True
    assert legacy["STT_HOST"] == "http://127.0.0.1:18102" and legacy["STT_ENABLED"] is True
    # A record that names no timeout takes the role's release default, whatever its id.
    assert legacy["TTS_TIMEOUT"] == renamed["TTS_TIMEOUT"] == 120


def test_a_record_with_the_old_id_but_no_capability_is_not_the_speech_service():
    derived = _derived(_config(tts={"host": "127.0.0.1", "port": 18101, "capabilities": ["embeddings"]}))
    assert derived["TTS_HOST"] == "" and derived["TTS_ENABLED"] is False
    assert derived["STT_HOST"] == "" and derived["STT_ENABLED"] is False


def test_the_highest_priority_service_is_selected_and_a_tie_goes_to_the_lower_id():
    derived = _derived(_config(**{
        "voice-one": _speech(18101, priority=100), "voice-two": _speech(18103, priority=50),
    }))
    assert derived["TTS_HOST"] == "http://127.0.0.1:18103"
    derived = _derived(_config(**{"voice-b": _speech(18104), "voice-a": _speech(18105)}))
    assert derived["TTS_HOST"] == "http://127.0.0.1:18105"


def test_a_disabled_selected_service_is_off_and_no_other_takes_its_place():
    derived = _derived(_config(**{"voice-one": _speech(18101, priority=10, enabled=False), "voice-two": _speech(18103)}))
    assert derived["TTS_HOST"] == "http://127.0.0.1:18101" and derived["TTS_ENABLED"] is False


def test_a_selected_service_without_an_address_is_not_configured_and_no_other_takes_its_place():
    derived = _derived(_config(**{
        "voice-one": {"capabilities": ["audio.speech"], "priority": 10}, "voice-two": _speech(18103),
    }))
    assert derived["TTS_HOST"] == "" and derived["TTS_ENABLED"] is False


def test_the_derived_settings_come_from_the_candidate_not_the_live_config(monkeypatch):
    monkeypatch.setattr(proxy, "config", _config(tts=_speech(18101), stt=_transcription(18102)))
    derived = _derived(_config(**{"voice-one": _speech(18201), "ears-one": _transcription(18202)}))
    assert derived["TTS_HOST"] == "http://127.0.0.1:18201"
    assert derived["STT_HOST"] == "http://127.0.0.1:18202"


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

@pytest.mark.parametrize("check, record, role", [
    ("check_tts_health", _speech, "TTS_ENABLED"), ("check_stt_health", _transcription, "STT_ENABLED"),
])
def test_the_health_check_probes_the_selected_service(monkeypatch, check, record, role):
    import asyncio

    probed: list[str] = []

    async def probe(service_id: str) -> bool:
        probed.append(service_id)
        return True

    monkeypatch.setattr(proxy, "config", _config(**{"renamed-one": record(18101)}))
    monkeypatch.setattr(proxy, "check_service_health", probe)
    with derived_patch(role, True):
        assert asyncio.run(getattr(proxy, check)()) is True
    assert probed == ["renamed-one"]
    monkeypatch.setattr(proxy, "config", _config())
    with derived_patch(role, True):
        assert asyncio.run(getattr(proxy, check)()) is False
    assert probed == ["renamed-one"]


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
