from __future__ import annotations

import ipaddress

from starlette.requests import Request

import proxy


def _request(peer: str, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/v1/models",
        "headers": raw_headers,
        "client": (peer, 43210),
        "server": ("testserver", 80),
        "scheme": "http",
    })


def _set_trust(monkeypatch):
    monkeypatch.setattr(proxy, "LAN_BYPASS_ENABLED", True)
    monkeypatch.setattr(proxy, "LAN_BYPASS_CIDRS", [ipaddress.ip_network("192.168.0.0/16")])
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [ipaddress.ip_network("192.168.1.4/32")])
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", False)
    monkeypatch.setattr(proxy, "_CF_IP_NETWORKS", None)


def test_trusted_proxy_without_xff_fails_closed_for_lan_bypass(monkeypatch):
    _set_trust(monkeypatch)

    request = _request("192.168.1.4")

    assert proxy.resolve_real_client_ip(request) is None
    assert proxy._client_is_lan(request) is False


def test_trusted_proxy_empty_xff_fails_closed_for_lan_bypass(monkeypatch):
    _set_trust(monkeypatch)

    request = _request("192.168.1.4", {"x-forwarded-for": "   "})

    assert proxy.resolve_real_client_ip(request) is None
    assert proxy._client_is_lan(request) is False


def test_trusted_proxy_malformed_xff_fails_closed_for_lan_bypass(monkeypatch):
    _set_trust(monkeypatch)

    request = _request("192.168.1.4", {"x-forwarded-for": "192.168.1.20, garbage"})

    assert proxy.resolve_real_client_ip(request) is None
    assert proxy._client_is_lan(request) is False


def test_trusted_proxy_valid_xff_preserves_rightmost_non_proxy_resolution(monkeypatch):
    _set_trust(monkeypatch)
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [
        ipaddress.ip_network("192.168.1.4/32"),
        ipaddress.ip_network("10.10.0.0/16"),
    ])

    request = _request(
        "192.168.1.4",
        {"x-forwarded-for": "203.0.113.9, 10.10.0.12, 192.168.1.25"},
    )

    assert proxy.resolve_real_client_ip(request) == ipaddress.ip_address("192.168.1.25")
    assert proxy._client_is_lan(request) is True


def test_trusted_proxy_ipv4_mapped_xff_is_normalized(monkeypatch):
    _set_trust(monkeypatch)

    request = _request("192.168.1.4", {"x-forwarded-for": "::ffff:192.168.1.25"})

    assert proxy.resolve_real_client_ip(request) == ipaddress.ip_address("192.168.1.25")
    assert proxy._client_is_lan(request) is True


def test_cloudflare_mode_missing_header_fails_closed_for_lan_bypass(monkeypatch):
    _set_trust(monkeypatch)
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", True)
    monkeypatch.setattr(proxy, "_CF_IP_NETWORKS", [ipaddress.ip_network("203.0.113.0/24")])
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [])

    request = _request("203.0.113.10")

    assert proxy.resolve_real_client_ip(request) is None
    assert proxy._client_is_lan(request) is False


def test_cloudflare_mode_malformed_header_fails_closed_for_lan_bypass(monkeypatch):
    _set_trust(monkeypatch)
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", True)
    monkeypatch.setattr(proxy, "_CF_IP_NETWORKS", [ipaddress.ip_network("203.0.113.0/24")])
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [])

    request = _request("203.0.113.10", {"cf-connecting-ip": "garbage"})

    assert proxy.resolve_real_client_ip(request) is None
    assert proxy._client_is_lan(request) is False


def test_cloudflare_mode_valid_header_is_accepted(monkeypatch):
    _set_trust(monkeypatch)
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", True)
    monkeypatch.setattr(proxy, "_CF_IP_NETWORKS", [ipaddress.ip_network("203.0.113.0/24")])
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [])

    request = _request("203.0.113.10", {"cf-connecting-ip": "192.168.1.25"})

    assert proxy.resolve_real_client_ip(request) == ipaddress.ip_address("192.168.1.25")
    assert proxy._client_is_lan(request) is True
