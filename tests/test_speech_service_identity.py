"""The speech and transcription services are the services selected for their capabilities, not records of a fixed id.

An install may name its speech engine "voice-one" and its transcription engine "ears-one". The gateway then serves
them exactly as it serves records named "tts" and "stt" that declare the same capabilities.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

import proxy

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
