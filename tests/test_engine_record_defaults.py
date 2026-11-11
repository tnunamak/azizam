"""Class B defaults of an engine record are release data, and the record's own value wins (S8, part A2)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

import proxy
from tests.support.speech_roles import declared
from gateway import engine_record_defaults

ROOT = Path(__file__).resolve().parent.parent
NO_ADDRESS = {}


def _derive(**records):
    return proxy._derive_backend_constants({"backends": declared(records)})


def test_the_release_defaults_give_the_values_the_code_gave():
    derived = _derive(tts=NO_ADDRESS, stt=NO_ADDRESS, embeddings=NO_ADDRESS, comfyui=NO_ADDRESS)
    assert (derived["STT_TIMEOUT"], derived["TTS_TIMEOUT"], derived["EMBEDDINGS_TIMEOUT"]) == (120, 120, 120)
    assert derived["EMBEDDINGS_HEALTH_ENDPOINT"] == "/health"
    assert derived["EMBEDDINGS_BACKEND_MODEL"] == "nomic-embed-text-v1.5"
    assert (derived["COMFY_TIMEOUT"], derived["COMFY_VIDEO_TIMEOUT"]) == (300, 1800)
    assert derived["COMFY_DEFAULT_MODEL"] == "flux2-klein-4b"
    assert derived["COMFY_WORKFLOWS_DIR"] == (ROOT / "workflows" / "images").resolve()
    assert derived["COMFY_VIDEO_WORKFLOWS_DIR"] == (ROOT / "workflows").resolve()


def test_a_value_in_the_record_beats_the_release_default():
    derived = _derive(
        stt={"timeout": 7}, embeddings={"health_endpoint": "/up", "model_id": "m", "timeout": 9},
        comfyui={"timeout": 5, "video_timeout": 6, "default_model": "x", "workflows_dir": "/w/i", "video_workflows_dir": "/w/v"})
    assert derived["STT_TIMEOUT"] == 7
    assert (derived["EMBEDDINGS_HEALTH_ENDPOINT"], derived["EMBEDDINGS_BACKEND_MODEL"], derived["EMBEDDINGS_TIMEOUT"]) == ("/up", "m", 9)
    assert (derived["COMFY_TIMEOUT"], derived["COMFY_VIDEO_TIMEOUT"], derived["COMFY_DEFAULT_MODEL"]) == (5, 6, "x")
    assert derived["COMFY_WORKFLOWS_DIR"] == Path("/w/i") and derived["COMFY_VIDEO_WORKFLOWS_DIR"] == Path("/w/v")


def test_the_defaults_file_is_checked_by_its_schema_and_holds_no_address():
    schema = json.loads(engine_record_defaults.SCHEMA_PATH.read_text(encoding="utf-8"))
    document = yaml.safe_load(engine_record_defaults.BUNDLED_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(document)
    for address_key in ("host", "port", "base_url", "address"):
        broken = {"defaults": {"audio.speech": {address_key: "x" if address_key != "port" else 8880}}}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(broken)


def test_a_missing_default_is_an_error_not_a_guess():
    assert "audio.speech" in engine_record_defaults.release_defaults()
    with pytest.raises(KeyError) as missing:
        engine_record_defaults.record_value("audio.speech", {}, "default_model")
    assert missing.value.args == ("default_model",)


@pytest.mark.parametrize("defaults", [
    {"audio.speach": {"timeout": 7}},
    {"tts": {"timeout": 7}},
    {"audio.speech": {"video_timeout": 7}},
    {"videos": {"timeout": 7}},
])
def test_the_schema_refuses_a_bucket_or_key_the_gateway_does_not_read(defaults):
    schema = json.loads(engine_record_defaults.SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate({"defaults": defaults})


def test_a_record_that_serves_two_roles_takes_each_roles_default(monkeypatch):
    defaults = {capability: dict(values) for capability, values in engine_record_defaults.release_defaults().items()}
    defaults["audio.speech"]["timeout"], defaults["audio.transcriptions"]["timeout"] = 11, 22
    monkeypatch.setattr(engine_record_defaults, "release_defaults", lambda: defaults)
    both = {"host": "127.0.0.1", "port": 18101, "capabilities": ["audio.speech", "audio.transcriptions"]}
    derived = proxy._derive_backend_constants({"backends": {"voice-and-ears": both}})
    assert (derived["TTS_TIMEOUT"], derived["STT_TIMEOUT"]) == (11, 22)
