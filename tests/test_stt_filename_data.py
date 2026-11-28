"""Freeze multipart bytes before moving fallback filenames into description data."""
from pathlib import Path

import httpx
import pytest
import yaml

from gateway import stt_chunking
from gateway.engine_protocol import description
from gateway.engine_protocol.chunked_stt import ChunkedSttAdapter, _RunWire
from tests.support.fake_upstream import fake_upstreams  # noqa: F401
from tests.test_engine_protocol_adapter import _Upload, _serve_stt, _stt_run, STT_CONN, run

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {"unnamed": "audio", "chunk_stem": "audio", "normalized": "audio.wav"}
CUSTOM = {"unnamed": "unlabelled.bin", "chunk_stem": "segment-source", "normalized": "converted.wav"}


def document():
    return yaml.safe_load((ROOT / "registry/protocols/chunked-stt.yml").read_text())


def expected_body(filename, content_type, stream):
    fields = [("model", "m-1")] + ([("stream", "true")] if stream else [])
    chunks = []
    for key, value in fields:
        chunks.append(f'--fixed-boundary\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f'--fixed-boundary\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
    return b"".join(chunks) + b"sample-bytes\r\n--fixed-boundary--\r\n"


@pytest.mark.parametrize("custom", [False, True], ids=["shipped-golden", "replacement-data"])
@pytest.mark.parametrize("scenario", ["direct", "named", "stream", "wav-direct", "wav-stream", "chunk", "empty-stem"])
def test_filename_defaults_reach_exact_multipart_bytes(fake_upstreams, tmp_path, monkeypatch, custom, scenario):
    defaults = CUSTOM if custom else DEFAULTS
    doc = document()
    if custom:
        doc["multipart_filenames"] = defaults
    adapter = ChunkedSttAdapter(description.parse_description(doc, source="fixture.yml")) if custom else ChunkedSttAdapter()
    # Fix only multipart framing; leave all other random sources alone.
    original = httpx._multipart.MultipartStream.__init__
    def fixed(self, data, files, boundary=None):
        original(self, data, files, boundary=b"fixed-boundary")
    monkeypatch.setattr(httpx._multipart.MultipartStream, "__init__", fixed)
    seen = _serve_stt(fake_upstreams, lambda request: httpx.Response(200, json={"text": "words"}))
    source = tmp_path / "source.bin"
    source.write_bytes(b"sample-bytes")
    stream = scenario in ("stream", "wav-stream")
    normalized = scenario.startswith("wav-")
    filename = "explicit.ogg" if scenario == "named" else "." if scenario == "empty-stem" else None
    upload = _Upload(source, filename, "audio/ogg")
    stems = []
    async def plan(**kwargs):
        stems.append(kwargs["source_stem"])
        if scenario in ("chunk", "empty-stem"):
            await kwargs["post_file"](source, kwargs["source_stem"] + "-000.wav")
            return stt_chunking.ChunkingOutcome("chunked", 1, "fixture", merged_payload={"text": "words"})
        return stt_chunking.ChunkingOutcome("passthrough", 0, "fixture", verified_duration_seconds=1)
    async def prepared(self, upload):
        return source
    monkeypatch.setattr(stt_chunking, "execute_chunking", plan)
    monkeypatch.setattr(_RunWire, "prepared_wav", prepared)
    async def go():
        call, _ = _stt_run(tmp_path, [("file", upload)], stream=stream)
        call.wav16k_input = normalized
        try:
            reply = await adapter.invoke(STT_CONN, "stt.transcribe", call)
            if stream:
                await reply.aclose()
        finally:
            await call.cleanup.aclose()
            upload.file.close()
    run(go())
    stem = Path(filename or defaults["chunk_stem"]).stem or defaults["chunk_stem"]
    assert stems == [stem]
    expected = (stem + "-000.wav" if scenario in ("chunk", "empty-stem") else defaults["normalized"] if normalized
                else filename or defaults["unnamed"])
    media_type = "audio/wav" if normalized or scenario in ("chunk", "empty-stem") else "application/octet-stream" if stream else "audio/ogg"
    assert len(seen) == 1
    assert seen[0].content == expected_body(expected, media_type, stream)
    assert seen[0].headers["content-type"] == "multipart/form-data; boundary=fixed-boundary"


@pytest.mark.parametrize("value", [None, {}, [], "invalid", 1, {**DEFAULTS, "unknown": "x"},
    *[{k: v for k, v in DEFAULTS.items() if k != missing} for missing in DEFAULTS],
    *[{**DEFAULTS, key: bad} for key in DEFAULTS for bad in (None, 1, True, "", " ", "../x", "a/b", "a\\b", ".", "..", "a\n", "a\x7f", "a\x00")]])
def test_malformed_filename_defaults_refuse_description_load(value):
    doc = document()
    if value is None:
        doc.pop("multipart_filenames", None)
    else:
        doc["multipart_filenames"] = value
    with pytest.raises(description.DescriptionError, match="multipart_filenames"):
        description.parse_description(doc, source="fixture.yml")
