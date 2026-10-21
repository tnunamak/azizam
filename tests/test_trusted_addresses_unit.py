"""Pure trusted-address matcher and request-context regression tests."""

import asyncio
from contextvars import ContextVar, copy_context
from ipaddress import ip_address
from itertools import permutations

import pytest

from gateway import request_source, trusted_addresses


def _entry(address, created_at, app_id):
    return {"address": address, "created_at": created_at, "app_id": app_id}


@pytest.mark.parametrize(
    ("client", "broad", "narrow"),
    [
        ("192.0.2.7", "192.0.0.0/16", "192.0.2.0/24"),
        ("2001:db8:1234::7", "2001:db8::/32", "2001:db8:1234::/48"),
    ],
)
def test_narrowest_entry_wins_independently_of_row_order(client, broad, narrow):
    """Mutant: rank = (network.prefixlen, int(entry.get("created_at") or 0))."""
    entries = [
        _entry(broad, 1, "broad"),
        _entry(narrow, 2, "narrow"),
        _entry(client, 3, "host"),
    ]
    for order in permutations(entries):
        assert trusted_addresses.best_match(ip_address(client), order) is entries[2]
    for order in permutations(entries[:2]):
        assert trusted_addresses.best_match(ip_address(client), order) is entries[1]


def test_oldest_equal_range_wins_independently_of_row_order():
    """Mutant: rank = (-network.prefixlen, 0)."""
    newer = _entry("192.0.2.0/24", 20, "newer")
    older = _entry("192.0.2.0/24", 10, "older")
    for order in permutations([newer, older]):
        assert trusted_addresses.best_match(ip_address("192.0.2.7"), order) is older


def test_ipv4_mapped_ipv6_matches_its_ipv4_entry():
    """Mutant: replace best_match's client = unwrap_mapped(client) with pass."""
    ipv4 = _entry("192.0.2.0/24", 2, "ipv4")
    ipv6 = _entry("2001:db8::/32", 1, "ipv6")
    entries = [ipv6, ipv4]
    for client in ("192.0.2.7", "::ffff:192.0.2.7"):
        assert trusted_addresses.best_match(ip_address(client), entries) is ipv4
    assert trusted_addresses.best_match(ip_address("::ffff:198.51.100.7"), entries) is None


@pytest.mark.parametrize(
    ("raw", "sentence"),
    [
        (None, "Enter an IP address or a range such as 192.168.1.0/24."),
        ("", "Enter an IP address or a range such as 192.168.1.0/24."),
        ("  ", "Enter an IP address or a range such as 192.168.1.0/24."),
        ("192.0.2.1 192.0.2.2", "Enter one address or range at a time."),
        ("192.0.2.1,192.0.2.2", "Enter one address or range at a time."),
        (
            "192.0.2.7/24",
            "The range has host bits set. Write it from its first address, "
            "such as 192.168.1.0/24.",
        ),
        (
            "2001:db8::1/64",
            "The range has host bits set. Write it from its first address, "
            "such as 192.168.1.0/24.",
        ),
        ("0.0.0.0/0", "A range of every address would trust the whole internet."),
        ("::/0", "A range of every address would trust the whole internet."),
        ("::ffff:192.0.2.7", "Write this as the IPv4 address 192.0.2.7."),
        ("::ffff:192.0.2.0/120", "Write this as the IPv4 address 192.0.2.0."),
        ("not-an-address", "This is not an IP address or a range such as 192.168.1.0/24."),
        ("256.0.0.1", "This is not an IP address or a range such as 192.168.1.0/24."),
        ("192.0.2.1/33", "This is not an IP address or a range such as 192.168.1.0/24."),
    ],
)
def test_invalid_entry_is_rejected_with_a_sentence(raw, sentence):
    """Mutant: replace the applicable raise AddressError(...) with raise AddressError("")."""
    with pytest.raises(trusted_addresses.AddressError) as caught:
        trusted_addresses.parse_entry(raw)
    assert str(caught.value) == sentence


def test_unreadable_entries_do_not_prevent_a_valid_match():
    """Mutant: replace best_match's except-block continue with raise."""
    valid = _entry("192.0.2.0/24", 2, "valid")
    entries = [{}, {"address": "not-an-address"}, valid]
    assert trusted_addresses.best_match(ip_address("192.0.2.7"), entries) is valid


def test_removal_takes_effect_on_the_next_matcher_lookup():
    """Mutant: return winner -> return best_match.__dict__.setdefault("_cached", winner)."""
    broad = _entry("192.0.2.0/24", 1, "broad")
    host = _entry("192.0.2.7", 2, "host")
    entries = [broad, host]
    client = ip_address("192.0.2.7")

    assert trusted_addresses.best_match(client, entries) is host
    entries.remove(host)
    assert trusted_addresses.best_match(client, entries) is broad
    entries.clear()
    assert trusted_addresses.best_match(client, entries) is None


@pytest.fixture
def isolated_trusted_context(monkeypatch):
    holder = ContextVar("test_trusted_address", default=None)
    monkeypatch.setattr(request_source, "_trusted_address", holder)
    return holder


def test_copied_auth_context_updates_the_request_holder(isolated_trusted_context):
    """Mutant: holder.update(...) -> _trusted_address.set(dict(app_id=app_id, address=address))."""
    token = isolated_trusted_context.set({})
    try:
        copy_context().run(
            request_source.note_trusted_address, "app-a", "192.0.2.7"
        )
        assert request_source.trusted_address_for("app-a") == "192.0.2.7"
    finally:
        isolated_trusted_context.reset(token)


def test_trusted_address_is_visible_only_to_its_app(isolated_trusted_context):
    """Mutant: replace trusted_address_for's if condition with if app_id and holder."""
    token = isolated_trusted_context.set({})
    try:
        request_source.note_trusted_address("app-a", "192.0.2.7")
        assert request_source.trusted_address_for("app-a") == "192.0.2.7"
        assert request_source.trusted_address_for("app-b") is None
        assert request_source.trusted_address_for(None) is None
        assert request_source.trusted_address_for("") is None
    finally:
        isolated_trusted_context.reset(token)


def test_note_outside_a_request_is_a_noop(isolated_trusted_context):
    """Mutant: replace note_trusted_address's if holder is not None with if True."""
    request_source.note_trusted_address("app-a", "192.0.2.7")
    assert isolated_trusted_context.get() is None
    assert request_source.trusted_address_for("app-a") is None


async def _unexpected_transport_call(*args):
    raise AssertionError("Pure context tests must not perform transport I/O.")


@pytest.mark.parametrize("fail_request", [False, True])
def test_middleware_restores_context_after_success_or_error(
    isolated_trusted_context, fail_request
):
    """Mutant: replace _trusted_address.reset(address_token) with pass."""

    async def app(scope, receive, send):
        assert request_source.trusted_address_for("outer") is None
        request_source.note_trusted_address("inner", "192.0.2.7")
        assert request_source.trusted_address_for("inner") == "192.0.2.7"
        if fail_request:
            raise RuntimeError("request failed")

    middleware = request_source.RequestSourceMiddleware(app)
    outer = {"app_id": "outer", "address": "198.51.100.7"}

    async def exercise():
        token = isolated_trusted_context.set(outer)
        try:
            call = middleware(
                {"type": "http", "headers": []},
                _unexpected_transport_call,
                _unexpected_transport_call,
            )
            if fail_request:
                with pytest.raises(RuntimeError, match="^request failed$"):
                    await call
            else:
                await call
            assert isolated_trusted_context.get() is outer
            assert request_source.trusted_address_for("outer") == "198.51.100.7"
            assert request_source.trusted_address_for("inner") is None
        finally:
            isolated_trusted_context.reset(token)

    asyncio.run(exercise())


def test_next_request_starts_without_trusted_attribution(isolated_trusted_context):
    """Mutant: address_token = _trusted_address.set(getattr(self, "_holder", {})); self._holder = _trusted_address.get()."""
    holders = []

    async def app(scope, receive, send):
        holders.append(isolated_trusted_context.get())
        assert request_source.trusted_address_for("app-a") is None
        if len(holders) == 1:
            request_source.note_trusted_address("app-a", "192.0.2.7")

    middleware = request_source.RequestSourceMiddleware(app)

    async def exercise():
        for _ in range(2):
            await middleware(
                {"type": "http", "headers": []},
                _unexpected_transport_call,
                _unexpected_transport_call,
            )
            assert isolated_trusted_context.get() is None
        assert holders[0] is not holders[1]

    asyncio.run(exercise())
