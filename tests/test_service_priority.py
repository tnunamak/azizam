"""A lower priority number goes first; a record that names no priority sorts at 100, and 0 is 0."""
from __future__ import annotations

import pytest

import proxy
from gateway.service_priority import service_priority


@pytest.mark.parametrize("record, expected", [
    ({"priority": 0}, 0), ({"priority": 10}, 10), ({}, 100), ({"priority": None}, 100), ({"priority": ""}, 100),
    ({"priority": "5"}, 5), (None, 100),
])
def test_a_records_priority(record, expected):
    assert service_priority(record) == expected


@pytest.mark.parametrize("value", ["first", False, True])
def test_a_priority_that_is_not_a_whole_number_is_refused(value):
    with pytest.raises(ValueError):
        service_priority({"priority": value})


def test_a_service_at_priority_zero_is_selected_over_one_at_ten():
    backends = {
        "voice-a": {"host": "127.0.0.1", "port": 18101, "capabilities": ["audio.speech"], "priority": 10},
        "voice-b": {"host": "127.0.0.1", "port": 18102, "capabilities": ["audio.speech"], "priority": 0},
        "voice-c": {"host": "127.0.0.1", "port": 18103, "capabilities": ["audio.speech"]},
        "voice-d": {"host": "127.0.0.1", "port": 18104, "capabilities": ["audio.speech"], "priority": 50},
    }
    assert proxy.backends_for_capability("audio.speech", backends=backends) == ["voice-b", "voice-a", "voice-d", "voice-c"]
    assert proxy._selected_service(backends, "audio.speech")[0] == "voice-b"
