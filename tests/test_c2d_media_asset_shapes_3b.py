"""Golden shapes and data-driven compatibility for bounded media shapers.

No backend calls or root gateway imports are needed by these tests.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml
from pydantic import ValidationError

from gateway import audio_media_jobs as audio_jobs
from gateway import compatibility
from gateway import video_media_jobs as video_jobs


ROOT = Path(__file__).resolve().parent.parent
COMPATIBILITY_PATH = ROOT / "registry" / "compatibility.yml"
LEGACY_KEYS = ["audio", "b64_json", "data"]


def _store(artifact):
    store = Mock(return_value="aud_fixture")
    assets = audio_jobs.store_audio_artifacts(
        [artifact], store_asset=store, owner_key_hash="owner-fixture",
    )
    return assets, store


def _with_keys(tmp_path, keys):
    document = yaml.safe_load(COMPATIBILITY_PATH.read_text(encoding="utf-8"))
    document["legacy_audio_result_keys"] = keys
    path = tmp_path / "compatibility.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return compatibility.load_compatibility(path)


def test_supported_audio_container_serialization_matches_inline_golden():
    cases = [
        (b"RIFF0000WAVEpayload", ".wav", "audio/wav"),
        (b"ID3payload", ".mp3", "audio/mpeg"),
        (b"\xff\xfbpayload", ".mp3", "audio/mpeg"),
        (b"\xff\xf3payload", ".mp3", "audio/mpeg"),
        (b"\xff\xf2payload", ".mp3", "audio/mpeg"),
        (b"fLaCpayload", ".flac", "audio/flac"),
        (b"OggSpayload", ".ogg", "audio/ogg"),
        (b"0000ftypM4Apayload", ".m4a", "audio/mp4"),
    ]
    for content, suffix, media_type in cases:
        artifacts = audio_jobs.audio_artifacts_from_result(content, response_format="wav")
        assert artifacts == [audio_jobs.AudioArtifact(content, media_type, suffix)]
        assets, store = _store(artifacts[0])
        assert json.loads(json.dumps(assets)) == [{
            "asset_id": "aud_fixture", "kind": "audio", "media_type": media_type,
            "size_bytes": len(content), "index": 0,
        }]
        store.assert_called_once_with(
            content, kind="aud", suffix=suffix, owner_key_hash="owner-fixture",
        )


def test_supported_video_container_serialization_matches_inline_golden():
    for suffix, media_type in [
        (".mp4", "video/mp4"), (".webm", "video/webm"),
        (".mov", "video/quicktime"), (".mkv", "video/x-matroska"),
    ]:
        asset = video_jobs.normalize_video_asset(
            filename=f"/outputs/clip{suffix}", asset_id="vid_fixture",
            url="https://example.test/view", content_url="/v1/assets/fixture/content",
            source={"node_id": 3, "output_key": "videos", "type": "output",
                    "subfolder": "", "private": "omit", "unused": None},
        )
        assert json.loads(json.dumps(asset)) == {
            "kind": "video", "filename": f"clip{suffix}", "media_type": media_type,
            "asset_id": "vid_fixture", "url": "https://example.test/view",
            "content_url": "/v1/assets/fixture/content",
            "source": {"node_id": "3", "output_key": "videos",
                       "type": "output", "subfolder": ""},
        }


def test_audio_mime_parameters_unknown_types_and_suffix_fallback_stay_safe():
    cases = [
        (" AUDIO/MPEG; codecs=MP3 ", "mp3", "audio/mpeg; codecs=mp3", ".mp3"),
        ("audio/x-unfamiliar; version=1", ".ogg", "audio/x-unfamiliar; version=1", ".ogg"),
        ("application/octet-stream", ".flac", "audio/flac", ".flac"),
        ("not-a-mime", ".unknown", "audio/wav", ".wav"),
    ]
    for supplied, suffix, media_type, stored_suffix in cases:
        assets, store = _store(audio_jobs.AudioArtifact(b"payload", supplied, suffix))
        assert assets == [{
            "asset_id": "aud_fixture", "kind": "audio", "media_type": media_type,
            "size_bytes": 7, "index": 0,
        }]
        assert store.call_args.kwargs["suffix"] == stored_suffix


def test_audio_presentation_family_uses_the_normalized_artifact_mime():
    # Isolate the assignment from the existing audio-only MIME fallback policy.
    with patch.object(audio_jobs, "_normalize_media_type", return_value="x-novel/framed"):
        assets, store = _store(audio_jobs.AudioArtifact(b"payload"))
    assert assets[0]["kind"] == "x-novel"
    assert assets[0]["media_type"] == "x-novel/framed"
    assert store.call_args.kwargs["kind"] == "aud"


def test_video_asset_keeps_the_supplied_media_type_and_video_kind():
    # Train 43 behaviour, captured from the review's inputs: the stored media type is kept byte for byte.
    for supplied in (" VIDEO/MP4; codecs=AVC1 ", "video/mp4; codecs=avc1", "audio/mpeg; codecs=x",
                     "application/x-framed; version=1", "x-novel/framed", "not-a-mime", "video/", "video/mp4/extra"):
        assert video_jobs.normalize_video_asset(filename="clip.mp4", media_type=supplied) == {
            "kind": "video", "filename": "clip.mp4", "media_type": supplied,
        }


def test_unknown_video_filename_falls_back_to_octet_stream_as_before():
    for media_type in (None, ""):
        assert video_jobs.normalize_video_asset(filename="clip.unknown", media_type=media_type) == {
            "kind": "video", "filename": "clip.unknown", "media_type": "application/octet-stream",
        }
    assert video_jobs.normalize_video_asset(filename="clip.mp4") == {
        "kind": "video", "filename": "clip.mp4", "media_type": "video/mp4",
    }


def test_legacy_result_keys_cannot_be_changed_after_validation():
    keys = compatibility.COMPATIBILITY.legacy_audio_result_keys
    assert isinstance(keys, tuple)
    with pytest.raises(AttributeError):
        keys.clear()


def test_raw_binary_sniffing_and_fallback_order_are_unchanged():
    for factory in (bytes, bytearray, memoryview):
        assert audio_jobs.audio_artifacts_from_result(
            factory(b"fLaCpayload"), response_format="mp3", media_type="audio/ogg",
        ) == [audio_jobs.AudioArtifact(b"fLaCpayload", "audio/flac", ".flac")]
    assert audio_jobs.audio_artifacts_from_result(
        b"opaque", response_format="mp3", media_type="audio/ogg; codecs=opus",
    ) == [audio_jobs.AudioArtifact(b"opaque", "audio/ogg", ".ogg")]
    assert audio_jobs.audio_artifacts_from_result(
        b"opaque", response_format="mp3", media_type="application/octet-stream",
    ) == [audio_jobs.AudioArtifact(b"opaque", "audio/mpeg", ".mp3")]
    assert audio_jobs.audio_artifacts_from_result(
        b"opaque", response_format="unknown",
    ) == [audio_jobs.AudioArtifact(b"opaque", "audio/wav", ".wav")]


def test_all_declared_keys_accept_base64_and_byte_payloads():
    keys = compatibility.COMPATIBILITY.legacy_audio_result_keys
    assert list(keys) == list(LEGACY_KEYS)
    content = b"legacy payload"
    for key in keys:
        for value in (base64.b64encode(content).decode("ascii"),
                      content, bytearray(content), memoryview(content)):
            for wrapper in ({key: value}, {"data": [{key: value}]}):
                assert audio_jobs.audio_artifacts_from_result(
                    wrapper, response_format="mp3",
                ) == [audio_jobs.AudioArtifact(content, "audio/mpeg", ".mp3")]


def test_legacy_key_precedence_and_falsy_fallback_match_the_base():
    for index, key in enumerate(LEGACY_KEYS):
        wrapper = {earlier: "" for earlier in LEGACY_KEYS[:index]}
        wrapper.update({later: b"later" for later in LEGACY_KEYS[index + 1:]})
        wrapper[key] = b"winner"
        assert audio_jobs.audio_artifacts_from_result(wrapper) == [
            audio_jobs.AudioArtifact(b"winner"),
        ]
    assert audio_jobs.audio_artifacts_from_result({"data": b""}) == [
        audio_jobs.AudioArtifact(b""),
    ]
    assert audio_jobs.audio_artifacts_from_result({"audio": b""}) == []

    class UntestedTruth:
        def __bool__(self):
            raise AssertionError("the final key must not be tested for truth")

    assert audio_jobs.audio_artifacts_from_result({"data": UntestedTruth()}) == []


def test_invalid_first_truthy_value_never_falls_through_to_a_later_key():
    for invalid in ("not valid base64!", {"nested": "no"}, 123, True):
        assert audio_jobs.audio_artifacts_from_result(
            {"audio": invalid, "b64_json": base64.b64encode(b"later").decode("ascii")},
        ) == []


def test_legacy_mapping_metadata_and_outer_data_list_precedence_are_unchanged():
    assert audio_jobs.audio_artifacts_from_result({
        "audio": b"outside", "data": [None, 1, {
            "audio": base64.b64encode(b"fLaCpayload").decode("ascii"),
            "media_type": "audio/ogg; codecs=opus", "content_type": "audio/mp4",
        }],
    }, response_format="mp3") == [
        audio_jobs.AudioArtifact(b"fLaCpayload", "audio/ogg; codecs=opus", ".ogg"),
    ]
    assert audio_jobs.audio_artifacts_from_result({
        "b64_json": b"opaque", "content_type": "audio/mp4",
    }) == [audio_jobs.AudioArtifact(b"opaque", "audio/mp4", ".m4a")]
    assert audio_jobs.audio_artifacts_from_result({"data": []}) == []


def test_unfamiliar_wrapper_key_and_reordering_need_only_changed_data(tmp_path, monkeypatch):
    contract = _with_keys(tmp_path, ["novel_payload", "data", "audio"])
    monkeypatch.setattr(compatibility, "COMPATIBILITY", contract)
    content = b"new envelope"
    for value in (base64.b64encode(content).decode("ascii"), content):
        assert audio_jobs.audio_artifacts_from_result({
            "novel_payload": value, "audio": b"old",
        }) == [audio_jobs.AudioArtifact(content)]
    assert audio_jobs.audio_artifacts_from_result({
        "data": b"second", "audio": b"third",
    }) == [audio_jobs.AudioArtifact(b"second")]
    assert audio_jobs.audio_artifacts_from_result({"b64_json": b"removed"}) == []


def test_legacy_key_declaration_rejects_non_lists_empty_duplicates_and_non_words(tmp_path):
    for invalid in (None, "audio", {}, [], ["audio", "audio"],
                    [1], [True], [""], [" bad"], ["bad key"], ["bad\n"]):
        with pytest.raises(ValidationError):
            _with_keys(tmp_path, invalid)


def test_missing_legacy_key_declaration_is_rejected(tmp_path):
    document = yaml.safe_load(COMPATIBILITY_PATH.read_text(encoding="utf-8"))
    document.pop("legacy_audio_result_keys", None)
    path = tmp_path / "missing.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValidationError):
        compatibility.load_compatibility(path)


def test_typed_artifacts_do_not_consult_legacy_envelope_keys(monkeypatch):
    monkeypatch.setattr(compatibility, "COMPATIBILITY", None)
    assets, store = _store(audio_jobs.AudioArtifact(b"typed", "audio/flac", ".flac"))
    assert assets == [{
        "asset_id": "aud_fixture", "kind": "audio", "media_type": "audio/flac",
        "size_bytes": 5, "index": 0,
    }]
    store.assert_called_once_with(
        b"typed", kind="aud", suffix=".flac", owner_key_hash="owner-fixture",
    )
    assert audio_jobs.audio_artifacts_from_result(b"fLaCtyped") == [
        audio_jobs.AudioArtifact(b"fLaCtyped", "audio/flac", ".flac"),
    ]


def test_empty_artifacts_preserve_original_indices_and_accounting_prefix():
    store = Mock(return_value="aud_fixture")
    assert audio_jobs.store_audio_artifacts(
        [audio_jobs.AudioArtifact(b""), audio_jobs.AudioArtifact(b"kept")],
        store_asset=store,
    ) == [{
        "asset_id": "aud_fixture", "kind": "audio", "media_type": "audio/wav",
        "size_bytes": 4, "index": 1,
    }]
    store.assert_called_once_with(b"kept", kind="aud", suffix=".wav", owner_key_hash=None)
