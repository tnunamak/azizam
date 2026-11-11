"""A lower priority number goes first; a record that names no priority sorts at 100, and 0 is 0."""
from __future__ import annotations

import pytest

import proxy
from gateway.service_priority import selected_service, service_priority


@pytest.mark.parametrize("record, expected", [
    ({"priority": 0}, 0), ({"priority": 10}, 10), ({}, 100), ({"priority": None}, 100), ({"priority": ""}, 100),
    ({"priority": "5"}, 5), ({"priority": 5.0}, 5), (None, 100),
])
def test_a_records_priority(record, expected):
    assert service_priority(record) == expected
    assert type(service_priority(record)) is int


@pytest.mark.parametrize("value", ["first", False, True, 5.7, [5], {"rank": 5}, (5,)])
def test_a_priority_that_is_not_a_whole_number_is_refused(value):
    with pytest.raises(ValueError):
        service_priority({"priority": value})


def test_selected_service_sorts_an_integral_float_as_an_integer():
    backends = {
        "z-integer": {"capabilities": ["custom.capability"], "priority": 5},
        "a-float": {"capabilities": ["custom.capability"], "priority": 5.0},
        "higher": {"capabilities": ["custom.capability"], "priority": 6},
    }
    assert selected_service(backends, "custom.capability") == ("a-float", backends["a-float"])


@pytest.mark.parametrize("priority", [False, True, 5.7, float("inf"), float("-inf"), float("nan"), [5], {"rank": 5}])
def test_selected_service_names_the_record_with_an_invalid_priority(priority):
    backends = {"bad-priority": {"capabilities": ["custom.capability"], "priority": priority}}
    with pytest.raises(ValueError) as exc:
        selected_service(backends, "custom.capability")
    assert str(exc.value) == f"service 'bad-priority': a service priority is a whole number, not {priority!r}"


@pytest.mark.parametrize("capabilities", ["custom.capability", "", {"custom.capability": True}, {}, 7, 0, False])
def test_selected_service_names_the_record_with_invalid_capabilities(capabilities):
    backends = {"bad-capabilities": {"capabilities": capabilities}}
    with pytest.raises(ValueError) as exc:
        selected_service(backends, "custom.capability")
    assert str(exc.value) == f"service 'bad-capabilities': capabilities are a list or tuple, not {capabilities!r}"


@pytest.mark.parametrize("record", [{"capabilities": None}, {}, {"capabilities": []}, {"capabilities": ()}])
def test_selected_service_accepts_absent_or_empty_capabilities(record):
    assert selected_service({"empty": record}, "custom.capability") is None


def test_selected_service_accepts_tuple_capabilities_and_compound_matches():
    backends = {"tuple": {"capabilities": (" Custom ", " CAPABILITY ")}}
    assert selected_service(backends, "custom.capability") == ("tuple", backends["tuple"])


def test_a_service_at_priority_zero_is_selected_over_one_at_ten():
    backends = {
        "voice-a": {"host": "127.0.0.1", "port": 18101, "capabilities": ["audio.speech"], "priority": 10},
        "voice-b": {"host": "127.0.0.1", "port": 18102, "capabilities": ["audio.speech"], "priority": 0},
        "voice-c": {"host": "127.0.0.1", "port": 18103, "capabilities": ["audio.speech"]},
        "voice-d": {"host": "127.0.0.1", "port": 18104, "capabilities": ["audio.speech"], "priority": 50},
    }
    assert proxy.backends_for_capability("audio.speech", backends=backends) == ["voice-b", "voice-a", "voice-d", "voice-c"]
    assert proxy._selected_service(backends, "audio.speech")[0] == "voice-b"
    assert selected_service(backends, "audio.speech") == proxy._selected_service(backends, "audio.speech")


def test_selected_service_orders_by_priority_without_mutating_records():
    backends = {
        "default": {"capabilities": ["images"]},
        "ten": {"capabilities": ["images"], "priority": 10},
        "zero": {"capabilities": ["images"], "priority": 0},
        "unmatched": {"capabilities": ["videos"], "priority": -1},
    }
    before = {key: dict(record) for key, record in backends.items()}
    service_id, record = selected_service(backends, "images")
    assert service_id == "zero"
    assert record is backends["zero"]
    assert backends == before


def test_selected_service_breaks_priority_ties_by_id():
    backends = {
        "z-last": {"capabilities": ["images"], "priority": "5"},
        "a-first": {"capabilities": ["images"], "priority": 5},
    }
    assert selected_service(backends, "images") == ("a-first", backends["a-first"])


@pytest.mark.parametrize("winner", [
    {"enabled": False, "host": "127.0.0.1", "port": 18101},
    {},
], ids=["disabled", "addressless"])
def test_selected_service_keeps_an_off_winner(winner):
    backends = {
        "runner-up": {"capabilities": ["images"], "priority": 10, "enabled": True,
                      "host": "127.0.0.1", "port": 18102},
        "winner": {**winner, "capabilities": ["images"], "priority": 0},
    }
    assert selected_service(backends, "images") == ("winner", backends["winner"])
    assert selected_service(backends, "images") == proxy._selected_service(backends, "images")


@pytest.mark.parametrize("capability, capabilities, matched", [
    (" audio.generate ", [" AUDIO.GENERATE "], True),
    ("audio.generate", [" Audio ", " GENERATE "], True),
    ("audio.generate", ["audio"], False),
    ("audio", ["audio.generate"], False),
    (" IMAGES ", ["", " ", None, 7, " Images "], True),
])
def test_selected_service_normalizes_and_matches_compound_capabilities(capability, capabilities, matched):
    backends = {"graph-one": {"capabilities": capabilities}}
    expected = ("graph-one", backends["graph-one"]) if matched else None
    assert selected_service(backends, capability) == expected
    assert selected_service(backends, capability) == proxy._selected_service(backends, capability)


def test_selected_service_skips_non_mapping_records_and_returns_none_without_a_match():
    backends = {"null": None, "text": "images", "list": ["images"], "number": 7,
                "empty": {}, "no-caps": {"capabilities": None}}
    assert selected_service(backends, "images") is None
    backends["graph-one"] = {"capabilities": ["images"]}
    assert selected_service(backends, "images") == ("graph-one", backends["graph-one"])


def test_selected_service_refuses_non_iterable_capabilities_and_root_keeps_its_type_error():
    backends = {"malformed": {"capabilities": 7}}
    with pytest.raises(ValueError, match="malformed.*capabilities are a list or tuple"):
        selected_service(backends, "images")
    # The root selector keeps its own contract (tests/test_service_role_contract.py pins its handling of other shapes).
    with pytest.raises(TypeError):
        proxy._selected_service(backends, "images")
