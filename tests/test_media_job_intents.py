"""The media job intent table (registry/media_job_intents.yml) reproduces every literal it replaced."""

from __future__ import annotations

import copy

import pytest

import proxy
from gateway import audio_media_jobs, media_job_intents

# The oracle: each literal as it was hand-written at its site before the table existed.
ORACLE_ENUM = ["image.generate", "audio.generate", "music.generate", "sfx.generate", "video.generate"]
ORACLE_SUPPORTED_INTENTS = ["image.generate", "audio.generate", "music.generate", "sfx.generate", "video.generate"]
ORACLE_AUDIO = frozenset({"audio.generate", "music.generate", "sfx.generate"})
ORACLE_RESOURCE_CLASS = {
    "image.generate": "image",
    "image.edit": "image",
    "image.identity": "image",
    "audio.generate": "audio",
    "music.generate": "audio",
    "sfx.generate": "audio",
    "video.generate": "video",
}
ORACLE_PROSE = (
    "The body you would send to the matching sync route: "
    "`/v1/images/generations` (image.generate), `/v1/audio/generate` "
    "(audio.generate, music.generate, sfx.generate) or `/v1/videos` "
    "(video.generate)."
)
ORACLE_COMPONENTS = [
    ("ImageGenerationRequest", "/v1/images/generations"),
    ("AudioGenerationRequest", "/v1/audio/generate"),
    ("VideoGenerationRequest", "/v1/videos"),
]


def test_submit_enum_and_supported_intents_keep_their_order():
    assert list(media_job_intents.SUBMITTABLE_INTENTS) == ORACLE_ENUM
    body = proxy._unsupported_gateway_media_job_response("nope").body
    import json
    assert json.loads(body)["error"]["supported_intents"] == ORACLE_SUPPORTED_INTENTS
    assert "Currently supported: " + ", ".join(ORACLE_SUPPORTED_INTENTS) + "." in json.loads(body)["error"]["message"]


def test_audio_sets_match():
    assert media_job_intents.AUDIO_INTENTS == ORACLE_AUDIO
    assert proxy._AUDIO_MEDIA_JOB_INTENTS == ORACLE_AUDIO
    assert audio_media_jobs._AUDIO_INTENTS == ORACLE_AUDIO


def test_resource_class_map_matches():
    assert proxy._MEDIA_JOB_RESOURCE_CLASS_BY_INTENT == ORACLE_RESOURCE_CLASS
    assert proxy._IDENTITY_JOB_INTENT == "image.identity"


def test_prose_and_components_match():
    assert media_job_intents.request_description() == ORACLE_PROSE
    assert [(c, r) for c, r, _ in media_job_intents.submit_routes()] == ORACLE_COMPONENTS


def test_non_submittable_rows_are_not_published():
    names = {r.intent for r in media_job_intents.ROWS if not r.submittable}
    assert names == {"image.edit", "image.identity"}


def _doc():
    return {"intents": [
        {"intent": "image.generate", "modality": "image", "submittable": True,
         "route": "/v1/images/generations", "component": "ImageGenerationRequest"},
        {"intent": "image.edit", "modality": "image", "submittable": False},
    ]}


def test_a_good_document_parses():
    assert [r.intent for r in media_job_intents.parse_rows(_doc())] == ["image.generate", "image.edit"]


def test_unknown_modality_is_refused():
    doc = _doc()
    doc["intents"][1]["modality"] = "smell"
    with pytest.raises(ValueError):
        media_job_intents.parse_rows(doc)


def test_duplicate_intent_is_refused():
    doc = _doc()
    doc["intents"].append(copy.deepcopy(doc["intents"][1]))
    with pytest.raises(ValueError, match="duplicate"):
        media_job_intents.parse_rows(doc)


def test_submittable_row_without_route_is_refused():
    doc = _doc()
    del doc["intents"][0]["route"]
    with pytest.raises(ValueError):
        media_job_intents.parse_rows(doc)


def test_row_that_is_not_submittable_with_a_route_is_refused():
    doc = _doc()
    doc["intents"][1]["route"] = "/v1/videos"
    with pytest.raises(ValueError):
        media_job_intents.parse_rows(doc)


def test_a_table_with_no_submittable_intent_is_refused():
    doc = {"intents": [{"intent": "image.edit", "modality": "image", "submittable": False}]}
    with pytest.raises(ValueError, match="no submittable"):
        media_job_intents.parse_rows(doc)


def test_one_component_naming_two_routes_is_refused():
    doc = _doc()
    doc["intents"].append({"intent": "audio.generate", "modality": "audio", "submittable": True,
                           "route": "/v1/audio/generate", "component": "ImageGenerationRequest"})
    with pytest.raises(ValueError, match="names two routes"):
        media_job_intents.parse_rows(doc)


def test_one_route_naming_two_components_is_refused():
    doc = _doc()
    doc["intents"].append({"intent": "image.identity", "modality": "image", "submittable": True,
                           "route": "/v1/images/generations", "component": "OtherRequest"})
    with pytest.raises(ValueError, match="names two components"):
        media_job_intents.parse_rows(doc)
