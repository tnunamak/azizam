"""A record's host and port reach the network only when they form a valid address.

``host_port_origin`` builds the address a lifecycle driver sends its keys to (``lifecycle_dial_url``) and the address
probes and dispatch dial. A host such as ``user@evil.example.com`` or ``a/b`` would build a URL whose real host is
not the one the record shows. A record that names an invalid host or port names no address: no caller falls back to
its base URL or connection address, because the record did name one.
"""

from __future__ import annotations

import pytest

from gateway import service_registry

BAD = [
    {"host": "user@evil.example.com", "port": 8080},
    {"host": "203.0.113.5/x", "port": 8080},
    {"host": "203.0.113.5#x", "port": 8080},
    {"host": "203.0.113.5", "port": 0},
    {"host": "203.0.113.5", "port": 70000},
    {"host": "203.0.113.5", "port": "80a"},
    {"host": "203.0.113.5", "port": True},
    {"host": "203.0.113.5", "port": 8080, "scheme": "javascript"},
    {"host": "203.0.113.5", "port": 8080, "scheme": "http://evil.example.com/?"},
]
FALLBACK = {"base_url": "http://198.51.100.7:9000/v1", "connection": {"address": "http://198.51.100.8:9001"}}


@pytest.mark.parametrize("record", BAD, ids=[f"{r['host']}|{r['port']}|{r.get('scheme', '')}" for r in BAD])
def test_an_invalid_host_or_port_names_no_address_and_nothing_falls_back(record):
    config = {**FALLBACK, **record}
    assert service_registry.host_port_origin(config) == ""
    assert service_registry.lifecycle_dial_url(config) == ""
    assert service_registry.engine_origin(config) == ""
    assert service_registry.engine_base_address(config) == ""
    with pytest.raises(service_registry.EngineAddressError):
        service_registry.checked_engine_base_address(config)


@pytest.mark.parametrize("record, origin", [
    ({"host": "203.0.113.5", "port": 8080}, "http://203.0.113.5:8080"),
    ({"host": "engine.example.com", "port": "8443", "scheme": "https"}, "https://engine.example.com:8443"),
    ({"host": " 203.0.113.5 ", "port": " 8080 "}, "http://203.0.113.5:8080"),
    ({"host": "::1", "port": 8080}, "http://[::1]:8080"),
    ({"host": "[2001:db8::1]", "port": 8080}, "http://[2001:db8::1]:8080"),
])
def test_a_valid_host_and_port_keep_their_address(record, origin):
    config = {**FALLBACK, **record}
    assert service_registry.host_port_origin(config) == origin
    assert service_registry.lifecycle_dial_url(config) == origin
    assert service_registry.engine_origin(config) == origin
    assert service_registry.checked_engine_base_address(config) == origin


def test_a_record_without_host_and_port_still_reads_its_base_url():
    assert service_registry.host_port_origin(FALLBACK) == ""
    assert service_registry.lifecycle_dial_url(FALLBACK) == "http://198.51.100.7:9000"


def test_a_probe_base_for_an_invalid_host_port_record_is_empty():
    from gateway import url_binding

    record = {"config": {"base_url": "http://198.51.100.7:9000/v1", "host": "user@evil.example.com", "port": 8080}}
    assert url_binding.base_of(record, "observations.health.path") == ""
