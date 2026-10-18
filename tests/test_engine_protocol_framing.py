"""Framing: SSE and NDJSON decoded chunk by chunk, multipart bodies encoded byte for byte."""

from __future__ import annotations

import pytest

from gateway.engine_protocol import framing
from gateway.engine_protocol.errors import BudgetExceeded


def sse_events(chunks, **kwargs):
    decoder = framing.SseDecoder(**kwargs)
    out = []
    for chunk in chunks:
        out.extend(decoder.feed(chunk))
    return out, decoder


def test_sse_events_split_at_any_byte_are_the_same_events() -> None:
    wire = b'event: a\ndata: {"x":1}\n\ndata: line1\ndata: line2\n\n: comment\n\ndata: [DONE]\n\n'
    whole, _ = sse_events([wire])
    assert [(e.event, e.data) for e in whole] == [("a", b'{"x":1}'), (None, b"line1\nline2"), (None, b"[DONE]")]
    for size in (1, 2, 3, 5, 7):
        pieces = [wire[i:i + size] for i in range(0, len(wire), size)]
        split, _ = sse_events(pieces)
        assert split == whole, size


def test_sse_accepts_crlf_and_lone_cr_even_when_the_pair_is_split_across_chunks() -> None:
    wire = b"data: a\r\n\r\ndata: b\r\rdata: c\n\n"
    whole, _ = sse_events([wire])
    assert [e.data for e in whole] == [b"a", b"b", b"c"]
    pieces = [wire[i:i + 1] for i in range(len(wire))]
    split, _ = sse_events(pieces)
    assert [e.data for e in split] == [b"a", b"b", b"c"]


def test_sse_strips_one_leading_space_and_ignores_unknown_fields() -> None:
    events, _ = sse_events([b"id: 7\nretry: 100\ndata:  two spaces\nfoo: bar\n\ndata:nospace\n\n"])
    assert [e.data for e in events] == [b" two spaces", b"nospace"]


def test_sse_incomplete_event_is_held_not_emitted() -> None:
    events, decoder = sse_events([b"data: a\n\ndata: partial"])
    assert [e.data for e in events] == [b"a"]
    assert decoder.pending()


def test_sse_event_larger_than_the_budget_is_refused() -> None:
    with pytest.raises(BudgetExceeded) as raised:
        sse_events([b"data: " + b"x" * 200], max_event_bytes=100)
    assert raised.value.budget == "max_event_bytes"
    with pytest.raises(BudgetExceeded):
        sse_events([b"data: " + b"x" * 90 + b"\n\n", b"data: " + b"y" * 200 + b"\n\n"], max_event_bytes=100)


def test_ndjson_lines_split_across_chunks_and_blank_lines_skipped() -> None:
    decoder = framing.NdjsonDecoder(max_line_bytes=1000)
    out = []
    for chunk in (b'{"a":', b'1}\n\n{"b"', b':2}\r\n', b'{"c":3}'):
        out.extend(decoder.feed(chunk))
    assert out == [b'{"a":1}', b'{"b":2}']
    assert decoder.finish() == [b'{"c":3}']
    assert decoder.finish() == []


def test_ndjson_line_larger_than_the_budget_is_refused() -> None:
    decoder = framing.NdjsonDecoder(max_line_bytes=10)
    with pytest.raises(BudgetExceeded):
        decoder.feed(b"x" * 11)
    decoder = framing.NdjsonDecoder(max_line_bytes=10)
    with pytest.raises(BudgetExceeded):
        decoder.feed(b"y" * 11 + b"\n")


def test_multipart_bytes_are_exactly_the_rfc_7578_shape() -> None:
    body, content_type = framing.encode_multipart(
        [
            framing.Part("model", b"m-1"),
            framing.Part("file", b"\x00\x01RIFF", filename="a.wav", content_type="audio/wav"),
            framing.Part("meta", b'{"k":1}', content_type="application/json"),
        ],
        boundary="BOUNDARY",
    )
    assert content_type == "multipart/form-data; boundary=BOUNDARY"
    assert body == (
        b'--BOUNDARY\r\nContent-Disposition: form-data; name="model"\r\n\r\nm-1\r\n'
        b'--BOUNDARY\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n\x00\x01RIFF\r\n'
        b'--BOUNDARY\r\nContent-Disposition: form-data; name="meta"\r\nContent-Type: application/json\r\n\r\n{"k":1}\r\n'
        b"--BOUNDARY--\r\n"
    )


def test_multipart_escapes_quotes_and_line_breaks_in_names() -> None:
    body, _ = framing.encode_multipart([framing.Part('a"b', b"1", filename='x\r\ny".wav', content_type="audio/wav")], boundary="B")
    assert b'name="a%22b"; filename="x%0D%0Ay%22.wav"' in body


def test_multipart_boundary_must_not_appear_in_a_part() -> None:
    with pytest.raises(ValueError):
        framing.encode_multipart([framing.Part("f", b"xx--BOUNDARYxx")], boundary="BOUNDARY")


def test_a_random_boundary_is_used_when_none_is_given() -> None:
    first, ct1 = framing.encode_multipart([framing.Part("a", b"1")])
    second, ct2 = framing.encode_multipart([framing.Part("a", b"1")])
    assert ct1 != ct2 and ct1.startswith("multipart/form-data; boundary=")
