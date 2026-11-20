"""Bounded, in-memory header contracts. No external uploads or providers."""
from __future__ import annotations

import io
import json
import struct
from unittest.mock import patch

import pytest

from gateway import adapter_upload


class RecordedReads(io.BytesIO):
    def __init__(self, data: bytes, budget: int) -> None:
        super().__init__(data)
        self.budget = budget
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        assert 0 <= size <= self.budget - self.read_bytes
        chunk = super().read(size)
        self.read_bytes += len(chunk)
        return chunk


def ordinary_file() -> tuple[bytes, int]:
    header = json.dumps({"tensor": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}}).encode()
    return struct.pack("<Q", len(header)) + header + b"\x00\x00", len(header)


def test_valid_file_at_exact_header_budget_does_not_read_tensor_body() -> None:
    data, length = ordinary_file()
    stream = RecordedReads(data, 8 + length)
    result = adapter_upload.read_header(stream, len(data), max_header_bytes=length)
    assert isinstance(result, adapter_upload.Header)
    assert result.tensor_count == 1
    assert stream.read_bytes == 8 + length


def test_decoder_capacity_error_is_reported_as_refused() -> None:
    data, length = ordinary_file()
    stream = RecordedReads(data, 8 + length)
    with patch.object(adapter_upload.json, "loads", side_effect=RecursionError("fixture decoder limit")):
        with pytest.raises(adapter_upload.Refused) as caught:
            adapter_upload.read_header(stream, len(data), max_header_bytes=length)
    assert caught.value.code == "adapter_header_invalid"
    assert stream.read_bytes == 8 + length
