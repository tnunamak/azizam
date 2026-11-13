"""Golden behavioral pins for the media format tables and prompt restore dispatch.

Expected mappings are copied intentionally, not generated from production tables.
"""
from __future__ import annotations

import pytest

from gateway import media_metadata as mm
from gateway import prompt_memory as pm


EXPECTED_SUFFIX_KIND = {
    ".png": "png",
    ".jpg": "jpeg", ".jpeg": "jpeg", ".jpe": "jpeg", ".jfif": "jpeg",
    ".webp": "webp",
    ".mp4": "video", ".m4v": "video", ".mov": "video", ".webm": "video",
    ".mkv": "video", ".avi": "video", ".gif": "gif", ".glb": "glb", ".pcm": "content",
    ".wav": "audio", ".flac": "audio", ".mp3": "audio", ".ogg": "audio",
    ".oga": "audio", ".opus": "audio", ".m4a": "audio", ".aac": "audio",
    ".mka": "audio",
}

EXPECTED_MIME_KIND = {
    "image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg",
    "image/webp": "webp", "image/gif": "gif", "model/gltf-binary": "glb", "audio/pcm": "content",
    "video/mp4": "video", "video/webm": "video",
    "video/quicktime": "video", "video/x-matroska": "video",
    "video/x-msvideo": "video",
    "audio/wav": "audio", "audio/x-wav": "audio", "audio/wave": "audio",
    "audio/vnd.wave": "audio", "audio/flac": "audio", "audio/x-flac": "audio",
    "audio/mpeg": "audio", "audio/mp3": "audio", "audio/ogg": "audio",
    "audio/opus": "audio", "audio/mp4": "audio", "audio/x-m4a": "audio",
    "audio/m4a": "audio", "audio/aac": "audio", "audio/x-matroska": "audio",
}

EXPECTED_MUXER = {
    ".mp4": "mp4", ".m4v": "mp4", ".m4a": "mp4", ".mov": "mov",
    ".webm": "webm", ".mkv": "matroska", ".mka": "matroska", ".avi": "avi",
    ".wav": "wav", ".flac": "flac", ".mp3": "mp3", ".ogg": "ogg",
    ".oga": "ogg", ".opus": "ogg", ".aac": "adts",
}


def test_media_kind_suffix_and_mime_mappings_are_pinned():
    # Completeness matters: an added or removed table key should need review.
    assert set(mm._SUFFIX_KIND) == set(EXPECTED_SUFFIX_KIND)
    assert set(mm._MIME_KIND) == set(EXPECTED_MIME_KIND)
    for key, expected in EXPECTED_SUFFIX_KIND.items():
        assert mm.media_kind(key) == expected, key
        assert mm.media_kind(key.lstrip(".")) == expected, key
    for key, expected in EXPECTED_MIME_KIND.items():
        assert mm.media_kind(key) == expected, key
    assert mm.media_kind("  .JpG  ") == "jpeg"
    assert mm.media_kind("IMAGE/PNG") == "png"
    assert mm.media_kind("audio/mpeg; charset=x") == "audio"
    assert mm.media_kind(".not-a-format") is None


def test_suffix_muxer_table_is_pinned():
    assert mm._MUXER == EXPECTED_MUXER
    for suffix, expected in EXPECTED_MUXER.items():
        assert mm._MUXER[suffix] == expected, suffix


@pytest.mark.parametrize("suffix", [".not-a-format", ".known-without-muxer"])
def test_unhandled_strip_refused_without_ffmpeg(monkeypatch, suffix):
    if suffix == ".known-without-muxer":
        # A synthetic recognized video kind without any matching muxer.
        monkeypatch.setitem(mm._SUFFIX_KIND, suffix, "video")
        assert mm.media_kind(suffix) == "video"
        assert suffix not in mm._MUXER
    else:
        assert mm.media_kind(suffix) is None

    def forbidden(*args, **kwargs):
        raise AssertionError("an unhandled format must never use ffmpeg")

    monkeypatch.setattr(mm, "_ffmpeg_rewrite", forbidden)
    original = b"opaque media bytes"
    if suffix == ".not-a-format":
        # A File that is not media passes unchanged, and still never reaches ffmpeg.
        assert mm.strip_metadata(original, suffix) is original
    else:
        with pytest.raises(mm.MediaMetadataError):
            mm.strip_metadata(original, suffix)
    assert mm.strip_audio(original, suffix) is original
    assert mm.embed_media_tags(original, suffix, {"prompt": "private"}) is original


@pytest.mark.parametrize("suffix,writer", [
    (".png", "embed_png_text"),
    (".jpg", "embed_image_metadata"),
    (".webp", "embed_image_metadata"),
    (".mp4", "embed_media_tags"),
    (".mp3", "embed_media_tags"),
    (".not-a-format", None),
])
def test_prompt_restore_uses_the_writer_for_each_kind(monkeypatch, suffix, writer):
    called = []
    def fake(name):
        def record(*args, **kwargs):
            called.append((name, args, kwargs))
            return b"restored"
        return record
    for name in ("embed_png_text", "embed_image_metadata", "embed_media_tags"):
        monkeypatch.setattr(mm, name, fake(name))

    source = b"original"
    tags = {"prompt": "remembered"}
    result = pm.restore_metadata(source, suffix, tags)
    if writer is None:
        assert result is source
        assert called == []
    else:
        assert result == b"restored"
        assert len(called) == 1
        name, args, kwargs = called[0]
        assert name == writer and kwargs == {}
        assert args == ((source, tags) if writer == "embed_png_text"
                        else (source, suffix, tags))
    assert pm.restore_metadata(source, suffix, {}) is source
