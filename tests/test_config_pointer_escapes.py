"""Stored overrides validate RFC 6901 escapes before resolving entries."""
from __future__ import annotations

import copy

import pytest

from gateway import config_layers as layers
from tests.test_config_layers import DEFAULTS, drop, make_section, put


@pytest.mark.parametrize("key, pointer", [
    ("bad~2key", "/aliases/bad~2key"),
    ("bad~", "/aliases/bad~"),
])
@pytest.mark.parametrize("operation", ["delete", "set"])
def test_a_malformed_stored_pointer_preserves_defaults_and_reports_bad_pointer(key, pointer, operation):
    defaults = copy.deepcopy(DEFAULTS)
    defaults["aliases"][key] = {"target": "svc-special"}
    before = copy.deepcopy(defaults)
    section = make_section(defaults)
    override = drop(pointer) if operation == "delete" else put(pointer, {"target": "changed"})

    faults = layers.check_override(section, override)
    assert [fault.code for fault in faults] == ["bad_pointer"]
    assert all(word in faults[0].message for word in ("~0", "~1"))
    result = layers.build_section(section, [override])

    assert result.effective == before
    assert defaults == before
    assert result.applied == ()
    assert [(item.override.pointer, [reason.code for reason in item.reasons])
            for item in result.rejected] == [(pointer, ["bad_pointer"])]
    assert [(problem.pointer, problem.code) for problem in result.problems] == [
        (pointer, "override_rejected")
    ]
    assert "bad_pointer" in result.problems[0].message


@pytest.mark.parametrize("key, pointer", [
    ("bad~2key", "/aliases/bad~02key"),
    ("bad~", "/aliases/bad~0"),
    ("slash/key", "/aliases/slash~1key"),
    ("tilde~key", "/aliases/tilde~0key"),
    ("~1", "/aliases/~01"),
])
def test_a_valid_escaped_delete_removes_exactly_its_member(key, pointer):
    defaults = copy.deepcopy(DEFAULTS)
    defaults["aliases"][key] = {"target": "svc-special"}
    before = copy.deepcopy(defaults)
    expected = copy.deepcopy(defaults)
    del expected["aliases"][key]
    section = make_section(defaults)
    override = drop(pointer)

    assert layers.check_override(section, override) == []
    result = layers.build_section(section, [override])

    assert result.effective == expected
    assert defaults == before
    assert result.applied == (override,)
    assert result.rejected == ()
    assert result.problems == ()


@pytest.mark.parametrize("pointer, expected", [
    ("/aliases/a~1b", ["aliases", "a/b"]),
    ("/aliases/a~0b", ["aliases", "a~b"]),
    ("/aliases/~01", ["aliases", "~1"]),
    ("/aliases/~00", ["aliases", "~0"]),
    ("/aliases/~0~1", ["aliases", "~/"]),
])
def test_valid_escapes_decode_once_and_round_trip(pointer, expected):
    assert layers.split_pointer(pointer) == expected
    assert layers.join_pointer(expected) == pointer


@pytest.mark.parametrize("pointer", [
    "/aliases/bad~2key", "/aliases/bad~", "/a~3/b", "/a~~0",
])
def test_split_pointer_rejects_invalid_escapes_before_decoding(pointer):
    with pytest.raises(layers.PointerError) as error:
        layers.split_pointer(pointer)
    message = str(error.value)
    assert pointer in message
    assert "~0" in message and "~1" in message
