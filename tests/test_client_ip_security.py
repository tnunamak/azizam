from __future__ import annotations

import ipaddress

from starlette.responses import Response
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


def test_trusted_proxy_duplicate_separator_xff_fails_closed_for_lan_bypass(monkeypatch):
    _set_trust(monkeypatch)

    request = _request("192.168.1.4", {"x-forwarded-for": "192.168.1.20,, 203.0.113.9"})

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


def test_trusted_proxy_attacker_controlled_leftmost_does_not_override_rightmost_client(monkeypatch):
    _set_trust(monkeypatch)

    request = _request(
        "192.168.1.4",
        {"x-forwarded-for": "192.168.1.20, 203.0.113.9"},
    )

    assert proxy.resolve_real_client_ip(request) == ipaddress.ip_address("203.0.113.9")
    assert proxy._client_is_lan(request) is False


def test_trusted_proxy_wholly_trusted_xff_chain_fails_closed(monkeypatch):
    _set_trust(monkeypatch)
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [
        ipaddress.ip_network("192.168.1.4/32"),
        ipaddress.ip_network("10.10.0.0/16"),
    ])

    request = _request("192.168.1.4", {"x-forwarded-for": "10.10.0.12, 192.168.1.4"})

    assert proxy.resolve_real_client_ip(request) is None
    assert proxy._client_is_lan(request) is False


def test_trusted_proxy_ipv4_mapped_xff_is_normalized(monkeypatch):
    _set_trust(monkeypatch)

    request = _request("192.168.1.4", {"x-forwarded-for": "::ffff:192.168.1.25"})

    assert proxy.resolve_real_client_ip(request) == ipaddress.ip_address("192.168.1.25")
    assert proxy._client_is_lan(request) is True


def test_cloudflare_range_fetch_failure_keeps_direct_peer_behavior(monkeypatch):
    _set_trust(monkeypatch)
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", True)
    monkeypatch.setattr(proxy, "_CF_IP_NETWORKS", None)
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [])

    request = _request("198.51.100.10", {"cf-connecting-ip": "192.168.1.25"})

    assert proxy.resolve_real_client_ip(request) == ipaddress.ip_address("198.51.100.10")
    assert proxy._client_is_lan(request) is False


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


def test_lan_request_with_valid_api_key_uses_key_identity(monkeypatch):
    key_identity = proxy.RuntimeIdentity(
        name="member-key",
        key_hash="stored-key-hash",
        profile=proxy.DEFAULT_PROFILE,
    )
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "_client_is_lan", lambda _request: True)
    monkeypatch.setattr(proxy, "_resolve_client_api_key", lambda _request: "valid-key")
    monkeypatch.setattr(proxy, "hash_api_key", lambda api_key: f"hash:{api_key}")
    monkeypatch.setattr(
        proxy,
        "runtime_identity_for_app_credential_hash",
        lambda key_hash, **_kwargs: key_identity if key_hash == "hash:valid-key" else None,
    )

    auth = proxy.authenticate_request(_request("192.168.1.25"), "/v1/models")

    assert isinstance(auth, proxy.AuthContext)
    assert auth.identity is key_identity


def test_lan_request_with_invalid_api_key_is_rejected(monkeypatch):
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "_client_is_lan", lambda _request: True)
    monkeypatch.setattr(proxy, "_resolve_client_api_key", lambda _request: "revoked-key")
    monkeypatch.setattr(proxy, "hash_api_key", lambda api_key: f"hash:{api_key}")
    monkeypatch.setattr(proxy, "runtime_identity_for_app_credential_hash", lambda *_args, **_kwargs: None)

    auth = proxy.authenticate_request(_request("192.168.1.25"), "/v1/models")

    assert isinstance(auth, Response)
    assert auth.status_code == 401


def test_lan_request_without_api_key_keeps_anonymous_lan_bypass(monkeypatch):
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "_client_is_lan", lambda _request: True)
    monkeypatch.setattr(proxy, "_resolve_client_api_key", lambda _request: "")

    auth = proxy.authenticate_request(_request("192.168.1.25"), "/v1/models")

    assert isinstance(auth, proxy.AuthContext)
    assert auth.identity is not None
    assert auth.identity.name == "lan-bypass"
