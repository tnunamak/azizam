"""Literal current presentation answers, independent of future data extraction."""

import pytest

from gateway.capabilities import Capability
from gateway.model_presentation import (
    RECOMMENDED_ALIASES,
    capability_presentation,
    service_capability_labels,
)


@pytest.mark.parametrize(
    "intent,declared,expected_kind,expected_title,expected_icon,expected_alias,expected_summary",
    [
        pytest.param(
            "chat.completions", None, "chat", "Chat", "chat", "default",
            "A chat model.", id="chat",
        ),
        pytest.param(
            "image.generate", None, "image", "Images", "image", "image",
            "Generate or edit images from your prompts.", id="image",
        ),
        pytest.param(
            "video.generate", None, "video", "Video", "video", "video",
            "Video from a prompt or image.", id="video",
        ),
        pytest.param(
            "audio.generate", "music", "music", "Music", "music", "music",
            "Songs and instrumentals from a prompt.", id="declared-music",
        ),
        pytest.param(
            "audio.generate", "sfx", "sfx", "Sound effects", "sfx", "sfx",
            "Sound effects from a description.", id="declared-sfx",
        ),
        pytest.param(
            "speech.synthesis", None, "speech", "Voice", "speech", "tts",
            "Turn text into spoken audio.", id="speech",
        ),
        pytest.param(
            "speech.transcription", None, "transcribe", "Transcription", "transcribe", "stt",
            "Turn recorded speech into text.", id="transcription",
        ),
        pytest.param(
            "decision.systemone", None, "decide", "Decisions", "decide", "decide",
            "Choose an outcome from your options.", id="decision",
        ),
        pytest.param(
            "text.embeddings", None, "embed", "Document search", "embed", "embed",
            "Create vectors for search and similarity.", id="embeddings",
        ),
        pytest.param(
            "text.rerank", None, "rerank", "Reranking", "rerank", "rerank",
            "Order search results by relevance.", id="reranking",
        ),
        pytest.param(
            "audio.generate", None, "audio", "Audio", "sfx", None,
            "Generate audio from your prompts.", id="generic-audio",
        ),
        pytest.param(
            "mesh.generate", "mesh", "mesh", "3D models", "mesh", None,
            "A 3D model from one picture.", id="declared-mesh",
        ),
        pytest.param(
            "smell.infer", None, "other", "AI", "chat", None,
            "Use this configured AI capability.", id="unclassified",
        ),
    ],
)
def test_capability_words_pin_taxonomy_label_icon_alias_and_summary(
    intent, declared, expected_kind, expected_title, expected_icon, expected_alias, expected_summary,
):
    row = capability_presentation(
        Capability(intent=intent, backend="test", model="demo", capability=declared)
    )

    assert row["capability"] == expected_kind
    assert row["capability_title"] == expected_title
    assert row["icon_key"] == expected_icon
    assert RECOMMENDED_ALIASES.get(row["capability"]) == expected_alias
    assert row["summary"] == expected_summary


@pytest.mark.parametrize(
    "capabilities,expected",
    [
        pytest.param(["audio", "speech"], ["Voice"], id="subtype-hides-generic-audio"),
        pytest.param(
            ["speech", "audio", "transcriptions"], ["Voice", "Transcription"],
            id="recognized-labels-keep-order",
        ),
        pytest.param(
            ["SPEECH", " speech ", "smell"], [], id="unrecognized-case-space-and-word",
        ),
    ],
)
def test_service_capability_words_pin_subtype_filtering_order_and_exact_matching(capabilities, expected):
    assert service_capability_labels({"capabilities": capabilities}) == expected


def test_audio_metadata_subtype_wins_over_declared_subtype_and_tag():
    capability = Capability(
        intent="audio.generate", backend="test", model="demo", capability="sfx",
        tags=("capability:speech",),
    )

    row = capability_presentation(capability, {"capability": "music"})

    assert row["capability"] == "music"


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("default", "Thinks through questions before answering."),
        ("chat", "Thinks through questions before answering."),
        ("fast", "Answers quickly for everyday prompts."),
        ("quick", "Answers quickly for everyday prompts."),
        ("coder", "Helps with coding tasks and technical questions."),
        ("code", "Helps with coding tasks and technical questions."),
        ("decoder", "Helps with coding tasks and technical questions."),
        ("rp", "Uses a roleplay-tuned chat profile."),
        ("roleplay", "Uses a roleplay-tuned chat profile."),
        ("demo", "A chat model."),
    ],
)
def test_chat_profile_words_pin_default_fast_coding_roleplay_and_generic_summaries(profile, expected):
    row = capability_presentation(Capability(intent="chat.completions", backend="test", model=profile))

    assert row["summary"] == expected


@pytest.mark.parametrize("profile,expected", [("fast", False), ("quick", True)])
def test_fast_and_quick_pin_distinct_secondary_profile_flags(profile, expected):
    row = capability_presentation(Capability(intent="chat.completions", backend="test", model=profile))

    assert row["secondary_profile"] is expected
