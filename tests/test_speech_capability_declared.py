"""Speech producers declare their product subtype, independent of intent spelling."""

from copy import deepcopy
from dataclasses import replace

import pytest

from gateway.local_capabilities import build_alias_capabilities, build_local_capabilities
from gateway.model_presentation import product_capability
from tests.test_local_capabilities import REPRESENTATIVE_CONFIG


def _local_rows(config=REPRESENTATIVE_CONFIG):
    # Speech and media do not need the unrelated LLM model-file probes.
    return build_local_capabilities(config, enabled_flags={"llm": False, "embeddings": False})


def _speech_rows(producer):
    if producer == "tts":
        rows = [row for row in _local_rows() if row.backend == "tts"]
        assert len(rows) == 1
    else:
        rows = build_alias_capabilities({
            "speech": {"stable": [
                {"alias": "voice-default", "resolves_to": "voice-model", "service": "tts"},
                {"alias": "voice-alt", "resolves_to": "voice-other"},
                {"alias": "unresolved", "resolves_to": None},
            ]},
        })
        assert {row.model for row in rows} == {"voice-default", "voice-alt"}
        assert len(rows) == 2
    for row in rows:
        assert row.intent == "speech.synthesis"
        assert row.input_modalities == ("text",)
        assert row.output_modalities == ("audio",)
    return rows


@pytest.mark.parametrize("producer", ["tts", "aliases"])
def test_speech_producers_declare_subtype(producer):
    for row in _speech_rows(producer):
        assert row.capability == "speech"


@pytest.mark.parametrize("producer", ["tts", "aliases"])
def test_speech_product_does_not_need_speech_intent_spelling(producer):
    for row in _speech_rows(producer):
        assert product_capability(row) == "speech"
        # Keep the audio contract, but bypass both speech intent prefixes.
        unrelated = replace(row, intent="audio.generate")
        assert product_capability(unrelated) == "speech"


@pytest.mark.parametrize("advertised", ["speech", "tts"])
def test_both_tts_backend_capability_words_declare_speech(advertised):
    config = deepcopy(REPRESENTATIVE_CONFIG)
    config["backends"]["tts"]["capabilities"] = ["audio", advertised]
    rows = [row for row in _local_rows(config) if row.backend == "tts"]
    assert len(rows) == 1
    assert rows[0].capability == "speech"


def test_music_and_sfx_workflows_are_not_declared_speech():
    workflows = {
        row.workflow: row for row in _local_rows()
        if row.workflow in {"ace-step-1.5-xl-turbo", "tangoflux"}
    }
    assert set(workflows) == {"ace-step-1.5-xl-turbo", "tangoflux"}
    for row in workflows.values():
        assert row.input_modalities == ("text",)
        assert row.output_modalities == ("audio",)
        assert row.capability != "speech"
        assert product_capability(row) == "audio"


def test_non_speech_aliases_keep_declared_audio_subtypes():
    rows = build_alias_capabilities({
        "audio_generate": {"stable": [
            {"alias": "music", "resolves_to": "ace-step-1.5-xl-turbo", "capability": "music"},
            {"alias": "sfx", "resolves_to": "tangoflux", "capability": "sfx"},
            {"alias": "generic-audio", "resolves_to": "other-audio"},
        ]},
    })
    by_model = {row.model: row for row in rows}
    assert set(by_model) == {"music", "sfx", "generic-audio"}
    for name in ("music", "sfx"):
        assert by_model[name].capability == name
        assert product_capability(by_model[name]) == name
    assert by_model["generic-audio"].capability is None
    assert product_capability(by_model["generic-audio"]) == "audio"
