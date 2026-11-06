"""Trusted addresses: an owner-made mapping from a network address to one App.

A request with no key whose direct client address is inside an entry's range
runs as that App, under that App's access plan. There are no entries on a
fresh install, so no address is trusted until the owner adds one.

This module holds the pure rules: how an entry is written and which entry
wins. Finding the client address is the proxy's job (it knows the trusted
proxy settings); storing entries is admin_store's.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Iterable, Mapping

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_ENTRIES_PER_APP = 32


class AddressError(ValueError):
    """The owner's text is not an address range the gateway can trust."""


def unwrap_mapped(addr: IPAddress) -> IPAddress:
    """``::ffff:1.2.3.4`` is the IPv4 address 1.2.3.4."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def parse_entry(text: Any) -> IPNetwork:
    """One IP address or one CIDR range, as written by the owner.

    A single address is a range of one. A range must start on its network
    boundary (``192.168.1.0/24``, not ``192.168.1.7/24``) so what the owner
    typed is what is trusted. A range of every address is refused.
    """
    raw = str(text or "").strip()
    if not raw:
        raise AddressError("Enter an IP address or a range such as 192.168.1.0/24.")
    if any(ch.isspace() for ch in raw) or "," in raw:
        raise AddressError("Enter one address or range at a time.")
    address_part = raw.split("/", 1)[0]
    try:
        mapped = unwrap_mapped(ipaddress.ip_address(address_part))
    except ValueError:
        mapped = None
    if mapped is not None and mapped != ipaddress.ip_address(address_part):
        # ::ffff:10.0.0.1 or ::ffff:10.0.0.0/120: write the IPv4 form.
        raise AddressError(f"Write this as the IPv4 address {mapped}.")
    try:
        network = ipaddress.ip_network(raw, strict=True)
    except ValueError as exc:
        text_ = str(exc)
        if "host bits" in text_:
            raise AddressError("The range has host bits set. Write it from its first address, such as 192.168.1.0/24.") from exc
        raise AddressError("This is not an IP address or a range such as 192.168.1.0/24.") from exc
    if network.prefixlen == 0:
        raise AddressError("A range of every address would trust the whole internet.")
    return network


def normalize_entry(text: Any) -> str:
    """The stored spelling: a lone host is its bare address, a range is CIDR."""
    network = parse_entry(text)
    return str(network.network_address) if network.num_addresses == 1 else str(network)


def best_match(client: IPAddress, entries: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The entry with the narrowest range that holds ``client``.

    A narrower range is the more deliberate choice. Equal ranges go to the
    older entry, so the winner never depends on row order.
    """
    client = unwrap_mapped(client)
    best: tuple[int, int] | None = None
    winner: Mapping[str, Any] | None = None
    for entry in entries:
        try:
            network = parse_entry(entry["address"])
        except (AddressError, KeyError):
            continue  # A row nobody can read trusts nobody.
        if network.version != client.version or client not in network:
            continue
        rank = (-network.prefixlen, int(entry.get("created_at") or 0))
        if best is None or rank < best:
            best, winner = rank, entry
    return winner
