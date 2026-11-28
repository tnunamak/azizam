"""Literal pins for catalog operation words, request examples and paths."""

from __future__ import annotations

import pytest

from gateway.capabilities import Capability
from gateway import models_catalog as catalog


def _cap(intent):
    return Capability(intent=intent, model="catalog-model", backend="local")


# Literal answers from the pre-data implementation, not production-derived oracles.
@pytest.mark.parametrize(("operation", "expected"), (
    ("chat", ("chat.",)),
    ("chat.completions", ("chat.",)),
    ("completions", ("text.completions",)),
    ("completion", ("text.completions",)),
    ("text", ("text.completions",)),
    ("text.completions", ("text.completions",)),
    ("embeddings", ("text.embeddings",)),
    ("embedding", ("text.embeddings",)),
    ("text.embeddings", ("text.embeddings",)),
    ("images", ("image.generate",)),
    ("image", ("image.generate",)),
    ("image.generate", ("image.generate",)),
    ("image.generations", ("image.generate",)),
    ("image.edit", ("image.edit",)),
    ("image.edits", ("image.edit",)),
    ("images.edit", ("image.edit",)),
    ("edits", ("image.edit",)),
    ("audio", ("audio.generate",)),
    ("audio.generate", ("audio.generate",)),
    ("speech", ("speech.synthesis",)),
    ("speech.synthesis", ("speech.synthesis",)),
    ("tts", ("speech.synthesis",)),
    ("transcription", ("speech.transcription",)),
    ("transcriptions", ("speech.transcription",)),
    ("speech.transcription", ("speech.transcription",)),
    ("stt", ("speech.transcription",)),
    ("translation", ("speech.translation",)),
    ("translations", ("speech.translation",)),
    ("speech.translation", ("speech.translation",)),
    ("video", ("video.generate", "video.edit")),
    ("videos", ("video.generate", "video.edit")),
    ("video.generate", ("video.generate",)),
    ("video.edit", ("video.edit",)),
    ("videos.edit", ("video.edit",)),
    ("rerank", ("text.rerank",)),
    ("reranking", ("text.rerank",)),
    ("text.rerank", ("text.rerank",)),
    ("decision", ("decision.",)),
    ("decisions", ("decision.",)),
))
def test_every_operation_synonym_has_its_literal_ordered_prefixes(operation, expected):
    assert catalog._intent_prefixes_for_operation(operation) == expected


@pytest.mark.parametrize(("operation", "expected"), (
    (" \tTTS\n ", ("speech.synthesis",)),
    (" IMAGE_EDITS ", ("image.edit",)),
    (" VIDEOS ", ("video.generate", "video.edit")),
    ("Chat_Completions", ("chat.",)),
), ids=("trim-and-lower-tts", "trim-lower-underscore-edit", "trim-lower-videos", "mixed-case-chat-underscore"))
def test_operation_words_strip_lowercase_and_replace_underscores(operation, expected):
    assert catalog._intent_prefixes_for_operation(operation) == expected


@pytest.mark.parametrize("operation", (
    " ", "audio.speech", "audio.transcriptions", "audio.translations",
    "images.generate", "images.generations", "images.edits", "videos.generate",
    "video.remix", "decision.systemone",
))
def test_unrecognized_operation_words_have_no_prefixes(operation):
    assert catalog._intent_prefixes_for_operation(operation) == ()


def test_chat_filter_keeps_chat_prefix_matches_in_input_order():
    caps = [_cap("chat.responses"), _cap("chat.completions"), _cap("text.completions")]
    selected = catalog._capabilities_for_operation(caps, "chat")
    assert [cap.intent for cap in selected] == ["chat.responses", "chat.completions"]
    assert selected[0] is caps[0]
    assert selected[1] is caps[1]


def test_image_filter_matches_longer_generation_intents_but_not_edits():
    caps = [_cap("image.generate.extra"), _cap("image.edit")]
    selected = catalog._capabilities_for_operation(caps, "images")
    assert [cap.intent for cap in selected] == ["image.generate.extra"]
    assert selected[0] is caps[0]


def test_unknown_operation_keeps_the_whole_capability_list_in_order():
    caps = [_cap("chat.responses"), _cap("image.edit"), _cap("text.completions")]
    selected = catalog._capabilities_for_operation(caps, "unknown-operation")
    assert [cap.intent for cap in selected] == ["chat.responses", "image.edit", "text.completions"]
    assert selected[0] is caps[0]
    assert selected[1] is caps[1]
    assert selected[2] is caps[2]


@pytest.mark.parametrize(("intent", "expected", "path"), (
    ("chat.completions", [{"kind": "chat", "body": {
        "model": "row-id", "messages": [{"role": "user", "content": "Hello"}],
    }}], "/v1/chat/completions"),
    ("text.completions", [{"kind": "completion", "body": {
        "model": "row-id", "prompt": "Hello",
    }}], "/v1/completions"),
    ("text.embeddings", [{"kind": "embeddings", "body": {
        "model": "row-id", "input": "Hello",
    }}], "/v1/embeddings"),
    ("image.generate", [{"kind": "image", "body": {
        "model": "row-id", "prompt": "A small red cabin",
    }}], "/v1/images/generations"),
    ("image.edit", [{"kind": "image_edit", "body": {
        "model": "row-id", "prompt": "Replace the sky",
    }, "files": ["image"]}], "/v1/images/edits"),
    ("speech.synthesis", [{"kind": "speech", "body": {
        "model": "row-id", "input": "Hello", "voice": "alloy",
    }}], "/v1/audio/speech"),
    ("speech.transcription", [{"kind": "transcription", "body": {
        "model": "row-id",
    }, "files": ["file"]}], "/v1/audio/transcriptions"),
    ("speech.translation", [{"kind": "translation", "body": {
        "model": "row-id",
    }, "files": ["file"]}], "/v1/audio/translations"),
    ("audio.generate", [{"kind": "audio", "body": {
        "model": "row-id", "prompt": "Gentle piano loop",
    }}], "/v1/audio/generate"),
    ("video.generate", [{"kind": "video", "body": {
        "model": "row-id", "prompt": "A kite above a beach",
    }}], "/v1/videos"),
    ("text.rerank", [{"kind": "rerank", "body": {
        "model": "row-id", "query": "capital of France",
        "documents": ["Paris is the capital of France.", "Berlin is in Germany."],
    }}], "/v1/rerank"),
))
def test_each_path_bearing_intent_has_its_exact_example_and_path(intent, expected, path):
    capability = _cap(intent)
    assert catalog._examples("row-id", [capability]) == expected
    assert catalog._path_for_capability(capability) == path


@pytest.mark.parametrize(("intent", "expected"), (
    ("video.edit", [{"kind": "video.edit", "body": None}]),
    ("decision.systemone", [{"kind": "decision.systemone", "body": None}]),
    ("mesh.generate", [{"kind": "mesh.generate", "body": None}]),
    ("CHAT.COMPLETIONS", [{"kind": "CHAT.COMPLETIONS", "body": None}]),
    (" chat.completions ", [{"kind": " chat.completions ", "body": None}]),
    ("", [{"kind": "", "body": None}]),
))
def test_unmapped_or_nonexact_intents_keep_the_literal_null_body_and_no_path(intent, expected):
    capability = _cap(intent)
    assert catalog._examples("row-id", [capability]) == expected
    assert catalog._path_for_capability(capability) is None


@pytest.mark.parametrize(("intents", "expected"), (
    (("image.edit", "chat.completions"), [{
        "kind": "image_edit", "body": {"model": "row-id", "prompt": "Replace the sky"},
        "files": ["image"],
    }]),
    (("chat.completions", "image.edit"), [{
        "kind": "chat", "body": {"model": "row-id", "messages": [{"role": "user", "content": "Hello"}]},
    }]),
    (("video.edit", "chat.completions"), [{"kind": "video.edit", "body": None}]),
))
def test_only_the_first_capability_decides_examples_even_when_it_has_no_body(intents, expected):
    assert catalog._examples("row-id", [_cap(intent) for intent in intents]) == expected


def test_no_capabilities_produce_no_examples():
    assert catalog._examples("row-id", []) == []
