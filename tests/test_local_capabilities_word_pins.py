"""Literal behavior pins before local capability vocabulary moves into data."""

from collections import Counter
from copy import deepcopy
from pathlib import Path
import sys

# Import the existing fixture, including when this file is run on its own.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_local_capabilities import REPRESENTATIVE_CONFIG  # noqa: E402

from gateway.local_capabilities import (  # noqa: E402
    build_alias_capabilities,
    build_local_capabilities,
)


# Expectations are literals, never imported intent constants or production tables.
EXPECTED_COUNTS = {
    "chat.completions": 7,
    "text.completions": 2,
    "text.embeddings": 1,
    "speech.synthesis": 1,
    "speech.transcription": 1,
    "image.generate": 1,
    "image.edit": 1,
    "video.generate": 2,
    "audio.generate": 2,
}


def _local_capabilities(**options):
    return build_local_capabilities(deepcopy(REPRESENTATIVE_CONFIG), **options)


def _only(caps, intent, **fields):
    matches = [
        cap for cap in caps
        if cap.intent == intent
        and all(getattr(cap, name) == value for name, value in fields.items())
    ]
    assert len(matches) == 1, (intent, fields, matches)
    return matches[0]


def _signature(cap):
    return (
        cap.input_modalities, cap.output_modalities, cap.execution, cap.streaming,
    )


def test_representative_families_keep_literal_intent_counts():
    assert Counter(cap.intent for cap in _local_capabilities()) == EXPECTED_COUNTS


def test_local_families_keep_literal_modalities_execution_and_streaming():
    caps = _local_capabilities()
    cases = [
        ("chat.completions", {"model": "role:text-only-role"},
         (("text",), ("text",), "sync", True)),
        ("text.completions", {"backend": "kobold"},
         (("text",), ("text",), "sync", True)),
        ("text.embeddings", {},
         (("text",), ("text",), "sync", False)),
        ("speech.synthesis", {},
         (("text",), ("audio",), "sync", True)),
        ("speech.transcription", {},
         (("audio",), ("text",), "sync", False)),
        ("image.generate", {},
         (("text",), ("image",), "job", False)),
        ("image.edit", {},
         (("text", "image"), ("image",), "job", False)),
        ("video.generate", {"workflow": "video-ltx-2.5-t2v"},
         (("text",), ("video",), "job", False)),
        ("audio.generate", {"workflow": "ace-step-1.5-xl-turbo"},
         (("text",), ("audio",), "job", False)),
    ]
    for intent, selector, expected in cases:
        assert _signature(_only(caps, intent, **selector)) == expected, intent


def test_disabling_each_family_removes_only_its_literal_intents():
    suppressed = {
        "llm": ("chat.completions", "text.completions"),
        "embeddings": ("text.embeddings",),
        "tts": ("speech.synthesis",),
        "stt": ("speech.transcription",),
        "images": ("image.generate", "image.edit"),
        "videos": ("video.generate",),
        "audio": ("audio.generate",),
    }
    for flag, intents in suppressed.items():
        expected = EXPECTED_COUNTS.copy()
        for intent in intents:
            del expected[intent]
        actual = _local_capabilities(enabled_flags={flag: False})
        assert Counter(cap.intent for cap in actual) == expected, flag


def test_disabling_all_literal_families_advertises_nothing():
    assert _local_capabilities(enabled_flags={
        "llm": False,
        "embeddings": False,
        "tts": False,
        "stt": False,
        "images": False,
        "videos": False,
        "audio": False,
    }) == []


def test_translation_requires_its_declared_trigger_and_keeps_recording_labels():
    caps = _local_capabilities()
    assert [cap for cap in caps if cap.intent == "speech.translation"] == []
    transcribe = _only(caps, "speech.transcription")
    assert (transcribe.action_label, transcribe.recordable_inputs) == (
        "Transcribe", ("audio",),
    )
    for trigger in ("translations", "translation"):
        config = deepcopy(REPRESENTATIVE_CONFIG)
        config["backends"]["stt"]["capabilities"].append(trigger)
        caps = build_local_capabilities(config)
        translate = _only(caps, "speech.translation")
        assert (
            translate.intent, translate.input_modalities,
            translate.output_modalities, translate.action_label,
            translate.recordable_inputs, translate.execution, translate.streaming,
        ) == (
            "speech.translation", ("audio",), ("text",), "Translate",
            ("audio",), "sync", False,
        ), trigger
        transcribe = _only(caps, "speech.transcription")
        assert (transcribe.action_label, transcribe.recordable_inputs) == (
            "Transcribe", ("audio",),
        )
        disabled = build_local_capabilities(config, enabled_flags={"stt": False})
        assert not [
            cap for cap in disabled
            if cap.intent in ("speech.transcription", "speech.translation")
        ]


def test_alias_operations_keep_literal_intents_modalities_and_execution():
    cases = [
        ("chat", "chat.completions", ("text",), ("text",), "sync"),
        ("embeddings", "text.embeddings", ("text",), ("embedding",), "sync"),
        ("images", "image.generate", ("text",), ("image",), "job"),
        ("image_edits", "image.edit", ("text", "image"), ("image",), "job"),
        ("videos", "video.generate", ("text",), ("video",), "job"),
        ("speech", "speech.synthesis", ("text",), ("audio",), "sync"),
        ("transcriptions", "speech.transcription", ("audio",), ("text",), "sync"),
        ("audio_generate", "audio.generate", ("text",), ("audio",), "job"),
    ]
    for operation, intent, inputs, outputs, execution in cases:
        caps = build_alias_capabilities({
            operation: {"stable": [{"alias": "a", "resolves_to": "b"}]},
        })
        assert len(caps) == 1, operation
        cap = caps[0]
        assert (
            cap.intent, cap.input_modalities, cap.output_modalities, cap.execution,
        ) == (intent, inputs, outputs, execution), operation
        assert (cap.backend, cap.model, cap.tags) == (
            "gateway", "a", ("alias", "resolves_to:b"),
        ), operation


def test_unknown_or_unresolved_aliases_are_not_advertised():
    assert build_alias_capabilities({
        "unknown": {"stable": [{"alias": "a", "resolves_to": "b"}]},
    }) == []
    assert build_alias_capabilities({
        "chat": {"stable": [{"alias": "a", "resolves_to": None}]},
    }) == []


def test_speech_keeps_literal_response_format_order():
    speech = _only(_local_capabilities(), "speech.synthesis")
    assert speech.tags == (
        "response_format:wav",
        "response_format:mp3",
        "response_format:opus",
        "response_format:aac",
        "response_format:flac",
        "response_format:pcm",
    )


def test_workflow_families_keep_default_backend_models_and_resource_tiers():
    caps = _local_capabilities()
    assert [
        (cap.intent, cap.backend, cap.model or cap.workflow, cap.resource_tier,
         cap.resources.gpu_required)
        for cap in caps
        if cap.intent in ("image.generate", "image.edit", "video.generate", "audio.generate")
    ] == [
        ("image.generate", "comfyui", "flux2-klein-4b", "batch", True),
        ("image.edit", "comfyui", "flux2-klein-4b-edit", "batch", True),
        ("video.generate", "comfyui", "video-ltx-2.5-t2v", "expensive", True),
        ("video.generate", "comfyui", "video-ltx-2.5-i2v", "expensive", True),
        ("audio.generate", "comfyui", "ace-step-1.5-xl-turbo", "batch", True),
        ("audio.generate", "comfyui", "tangoflux", "batch", True),
    ]


def test_vision_role_projector_and_backend_words_independently_add_image_input():
    cases = [
        ("vision", {}, [], ("text", "image")),
        ("ordinary", {"mmproj": "projector.gguf"}, [], ("text", "image")),
        ("ordinary", {}, ["vision"], ("text", "image")),
        ("ordinary", {}, [], ("text",)),
    ]
    for role, extra, backend_words, expected in cases:
        caps = build_local_capabilities({
            "backends": {"x": {"capabilities": backend_words}},
            "roles": {role: {"backend": "x", **extra}},
        })
        assert _only(
            caps, "chat.completions", model="role:" + role,
        ).input_modalities == expected, (role, extra, backend_words)
    caps = build_local_capabilities({
        "backends": {"x": {"capabilities": ["chat", "vision"]}},
    })
    assert _only(caps, "chat.completions").input_modalities == ("text", "image")
    caps = build_local_capabilities({
        "backends": {"x": {"capabilities": []}},
        "models": [{"id": "vision", "backend": "x"}],
    })
    assert _only(caps, "chat.completions", model="vision").input_modalities == ("text",)


def test_embeddings_keep_cpu_resources_and_text_endpoint_output():
    embedding = _only(_local_capabilities(), "text.embeddings")
    assert embedding.resources.gpu_required is False
    assert embedding.resources.vram_gb_estimate == 0
    # The alias output is deliberately pinned as ("embedding",) above.
    assert embedding.output_modalities == ("text",)


def test_backend_trigger_synonyms_keep_literal_intents():
    cases = [
        ("chat", "chat.completions"),
        ("completions", "text.completions"),
        ("embeddings", "text.embeddings"),
        ("speech", "speech.synthesis"),
        ("tts", "speech.synthesis"),
        ("transcriptions", "speech.transcription"),
        ("stt", "speech.transcription"),
        ("translations", "speech.translation"),
        ("translation", "speech.translation"),
    ]
    for word, expected_intent in cases:
        caps = build_local_capabilities({
            "backends": {"x": {"capabilities": [word]}},
        })
        assert [cap.intent for cap in caps] == [expected_intent], word


def test_unknown_and_canonical_only_backend_words_advertise_nothing():
    for word in ("unknown", "speech.synthesis"):
        assert build_local_capabilities({
            "backends": {"x": {"capabilities": [word]}},
        }) == [], word
