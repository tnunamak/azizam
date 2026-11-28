"""Independent base pins and a data-only extension for media metadata formats."""

import importlib.util
from pathlib import Path

import pytest
import yaml

from gateway import media_metadata as mm


ROWS = Path(__file__).resolve().parents[1] / "registry" / "media_metadata_formats.yml"

# Reviewed literal pins, including the fail-closed GIF/GLB and PCM rows.
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
    "video/mp4": "video", "video/webm": "video", "video/quicktime": "video",
    "video/x-matroska": "video", "video/x-msvideo": "video",
    "audio/wav": "audio", "audio/x-wav": "audio", "audio/wave": "audio",
    "audio/vnd.wave": "audio", "audio/flac": "audio", "audio/x-flac": "audio",
    "audio/mpeg": "audio", "audio/mp3": "audio", "audio/ogg": "audio",
    "audio/opus": "audio", "audio/mp4": "audio", "audio/x-m4a": "audio",
    "audio/m4a": "audio", "audio/aac": "audio", "audio/x-matroska": "audio",
}
# suffix -> ffmpeg muxer
EXPECTED_MUXER = {
    ".mp4": "mp4", ".m4v": "mp4", ".m4a": "mp4", ".mov": "mov",
    ".webm": "webm", ".mkv": "matroska", ".mka": "matroska", ".avi": "avi",
    ".wav": "wav", ".flac": "flac", ".mp3": "mp3", ".ogg": "ogg",
    ".oga": "ogg", ".opus": "ogg", ".aac": "adts",
}
EXPECTED_MIME_SUFFIX = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "video/x-msvideo": ".avi",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/vnd.wave": ".wav", "audio/flac": ".flac", "audio/x-flac": ".flac",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/ogg": ".ogg",
    "audio/opus": ".ogg", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a", "audio/aac": ".aac", "audio/x-matroska": ".mka",
}
EXPECTED_TABLES = (
    EXPECTED_SUFFIX_KIND, EXPECTED_MIME_KIND, EXPECTED_MUXER, EXPECTED_MIME_SUFFIX,
)


def write_rows(tmp_path, value):
    path = tmp_path / "media-formats.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_shipped_tables_equal_complete_literal_base():
    assert mm.load_media_metadata_formats(ROWS) == EXPECTED_TABLES
    assert (mm._SUFFIX_KIND, mm._MIME_KIND, mm._MUXER, mm._MIME_SUFFIX) == EXPECTED_TABLES
    assert ".gif" in mm._SUFFIX_KIND and mm._SUFFIX_KIND[".gif"] == "gif"


def test_speech_response_formats_and_suffix_aliases_match_public_contract():
    assert mm.load_speech_formats(ROWS) == (
        {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac",
         "flac": "audio/flac", "wav": "audio/wav", "pcm": "audio/pcm"},
        {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/ogg": ".ogg",
         "audio/aac": ".aac", "audio/flac": ".flac", "audio/x-flac": ".flac",
         "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav"},
        "mp3", ("wav", "mp3", "opus", "aac", "flac", "pcm"),
    )


def test_speech_table_data_override_reaches_runtime_tables(tmp_path):
    value = yaml.safe_load(ROWS.read_text(encoding="utf-8"))
    value["speech_formats"][0]["mime_type"] = "audio/x-mp3-test"
    value["speech_formats"][0]["suffix"] = ".test"
    formats, suffixes, default, error_order = mm.load_speech_formats(write_rows(tmp_path, value))
    assert formats["mp3"] == "audio/x-mp3-test"
    assert suffixes["audio/mp3"] == ".test"
    assert default == "mp3"
    assert error_order == ("wav", "mp3", "opus", "aac", "flac", "pcm")


def test_extra_container_row_reaches_exactly_the_four_expected_tables(tmp_path):
    value = yaml.safe_load(ROWS.read_text(encoding="utf-8"))
    value["formats"].append({
        "handler": "audio", "suffixes": [".wave"],
        "mime_types": ["audio/x-wave-alias"], "muxer": "wav",
    })
    assert mm.load_media_metadata_formats(write_rows(tmp_path, value)) == (
        {**EXPECTED_SUFFIX_KIND, ".wave": "audio"},
        {**EXPECTED_MIME_KIND, "audio/x-wave-alias": "audio"},
        {**EXPECTED_MUXER, ".wave": "wav"},
        {**EXPECTED_MIME_SUFFIX, "audio/x-wave-alias": ".wave"},
    )
    # An explicit test-only load must not replace the once-loaded module tables.
    assert (mm._SUFFIX_KIND, mm._MIME_KIND, mm._MUXER, mm._MIME_SUFFIX) == EXPECTED_TABLES


@pytest.mark.parametrize("case, where, message", [
    ("document-key", "document", "unknown fields"),
    ("row-key", "formats row 1", "unknown fields"),
    ("duplicate-suffix", "formats row 2", "duplicate suffix"),
    ("duplicate-mime", "formats row 2", "duplicate MIME type"),
    ("muxer-without-suffix", "formats row 1", "muxer requires at least one suffix"),
    ("unknown-handler", "formats row 1", "handler must be null or one of audio, content, gif, glb, jpeg, png, video, webp: 'pgn'"),
])
def test_invalid_rows_name_file_and_location(tmp_path, case, where, message):
    value = {"schema_version": 1, "formats": [{
        "handler": "audio", "suffixes": [".wave"],
        "mime_types": ["audio/x-wave-alias"], "muxer": "wav",
    }]}
    row = value["formats"][0]
    if case == "document-key":
        value["extra"] = True
    elif case == "row-key":
        row["extra"] = True
    elif case == "duplicate-suffix":
        value["formats"].append({"handler": None, "suffixes": [".wave"], "mime_types": []})
    elif case == "duplicate-mime":
        value["formats"].append({
            "handler": None, "suffixes": [], "mime_types": ["audio/x-wave-alias"],
        })
    elif case == "unknown-handler":
        row["handler"] = "pgn"
    else:
        row["suffixes"] = []
    path = write_rows(tmp_path, value)
    with pytest.raises(ValueError) as error:
        mm.load_media_metadata_formats(path)
    text = str(error.value)
    assert str(path) in text
    assert where in text
    assert message in text
    assert (mm._SUFFIX_KIND, mm._MIME_KIND, mm._MUXER, mm._MIME_SUFFIX) == EXPECTED_TABLES


def test_a_repeated_key_is_refused_not_last_wins(tmp_path):
    path = tmp_path / "media-formats.yml"
    path.write_text(
        "schema_version: 1\nformats:\n"
        "  - handler: png\n    handler: audio\n    suffixes: [.wv]\n    mime_types: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as error:
        mm.load_media_metadata_formats(path)
    assert "could not read format rows" in str(error.value)
    assert "duplicate key 'handler'" in str(error.value)


def test_every_handler_has_strip_code():
    """Each handler a row may name reaches a strip branch, so no shipped kind returns its bytes unchanged."""
    import inspect
    # strip_metadata takes its refusals and pass-throughs from the one decision it shares with the label.
    body = inspect.getsource(mm.strip_metadata) + inspect.getsource(mm._strip_decision)
    for handler in mm._HANDLERS:
        assert f'"{handler}"' in body, handler


@pytest.mark.parametrize("handler", ["audio", "video"])
def test_stream_handler_requires_muxer(tmp_path, handler):
    path = write_rows(tmp_path, {"schema_version": 1, "formats": [
        {"handler": handler, "suffixes": [".bad"], "mime_types": []},
    ]})
    with pytest.raises(ValueError, match="formats row 1.*muxer"):
        mm.load_media_metadata_formats(path)


def test_unknown_muxer_names_row_and_muxer(tmp_path):
    path = write_rows(tmp_path, {"schema_version": 1, "formats": [
        {"handler": "audio", "suffixes": [".asf"], "mime_types": [], "muxer": "asf"},
    ]})
    with pytest.raises(ValueError, match="formats row 1.*unsupported muxer.*asf"):
        mm.load_media_metadata_formats(path)


def test_shipped_bmff_suffixes_are_literal():
    assert mm._MP4_LIKE == {".mp4", ".m4v", ".m4a", ".mov"}


def test_added_bmff_suffix_gets_movflags(tmp_path, monkeypatch):
    value = yaml.safe_load(ROWS.read_text(encoding="utf-8"))
    value["formats"].append({
        "handler": "video", "suffixes": [".3gp"], "mime_types": [], "muxer": "mp4",
    })
    synthetic_rows = write_rows(tmp_path, value)
    original_read_text = Path.read_text
    reads = []

    def read_text(path, *args, **kwargs):
        if path == ROWS:
            reads.append(path)
            path = synthetic_rows
        return original_read_text(path, *args, **kwargs)

    spec = importlib.util.spec_from_file_location("_media_metadata_formats_probe", mm.__file__)
    assert spec is not None and spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as registry_patch:
        registry_patch.setattr(Path, "read_text", read_text)
        spec.loader.exec_module(isolated)
    # Module import loads the container and speech tables from the shared registry.
    assert reads == [ROWS, ROWS]
    assert isolated._MP4_LIKE == {".mp4", ".m4v", ".m4a", ".mov", ".3gp"}
    assert mm._MP4_LIKE == {".mp4", ".m4v", ".m4a", ".mov"}
    calls = []

    def rewrite(content, suffix, workdir, extra):
        calls.append((content, suffix, workdir, extra))
        return b"rewritten"

    monkeypatch.setattr(isolated, "_ffmpeg_rewrite", rewrite)
    assert isolated.embed_media_tags(b"synthetic", ".3gp", {"title": "sample"}) == b"rewritten"
    assert calls == [(b"synthetic", ".3gp", None, [
        "-metadata", "title=sample", "-movflags", "+use_metadata_tags",
    ])]


def test_bmff_suffixes_ignore_unknown_muxer():
    assert mm._bmff_suffixes({".3gp": "mp4", ".bad": "unknown"}) == {".3gp"}
