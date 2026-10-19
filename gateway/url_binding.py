"""Where the credentials of a stored URL may be sent (decision D39).

A URL field that holds credentials (``<field>_entries``) is bound to the *effective origin* of the address it stands
for: its own origin when it is an absolute http(s) URL, the origin of the base it is joined against when it is a path.
A field that has no such origin (a protocol-relative ``//host/path``, a scheme that is not http(s), an unreadable host)
cannot hold credentials: the write is refused, and a stored one is never put back.

This module is pure. It decides origins and reads no secret, so management code (the write rules, the credential
fingerprint, the migration) and the runtime (``gateway.runtime_secrets``) share one definition.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterator, Mapping

from gateway import secret_store

# A reader's way of putting a path on a base: ``join(base, path) -> url``. Each reader has its own and keeps it; the
# runtime asks the reader's own function for the address it will dial, then compares origins.
Join = Callable[[str, str], str]

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_HOST = re.compile(r"^(?:[A-Za-z0-9_](?:[A-Za-z0-9_\-]*[A-Za-z0-9_])?)(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_\-]*[A-Za-z0-9_])?)*\.?$")
_ENTRIES = "_entries"


def strict_origin(url: Any) -> str | None:
    """The origin (scheme, host, port with the scheme's default filled in, lower case) of an absolute http(s) URL.

    ``None`` for anything a client could read differently from this function: a control character, a space, a backslash,
    a scheme other than http(s), a host with a percent sign or characters a host name does not have, an invalid port.
    """
    if not isinstance(url, str) or not url:
        return None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url) or "\\" in url:
        return None
    scheme, separator, rest = url.partition("://")
    if not separator or scheme.lower() not in ("http", "https"):
        return None
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    host = authority.rpartition("@")[2]
    if host.startswith("["):
        closed = host.find("]")
        literal = host[1:closed] if closed > 0 else ""
        if not re.fullmatch(r"[0-9A-Fa-f:.]+", literal) or ":" not in literal or host[closed + 1:closed + 2] not in ("", ":"):
            return None
    else:
        name = host.partition(":")[0]
        if not name or not _HOST.fullmatch(name):
            return None
    try:
        return secret_store.credential_origin_from_url(url)
    except ValueError:
        return None


def is_absolute(public: str) -> bool:
    """True when the text names a scheme (``http:``, ``HTTP://``, ``mailto:``): it is not a path, whatever the scheme is."""
    return bool(_SCHEME.match(public))


def path_shaped(public: Any) -> bool:
    """True for the text of a field that may hold credentials: an absolute http(s) URL, or a path with one leading slash.

    Nothing else (``//host/path``, ``@host/x``, ``host/x``, a backslash, another scheme) is a field no reader can be sure to
    join the way another reads it; its credentials are never put back.
    """
    text = str(public or "").strip()
    if is_absolute(text):
        return strict_origin(text) is not None
    return text.startswith("/") and not text.startswith("//") and "\\" not in text


def field_origin(public: Any, base: Any = None) -> str | None:
    """The origin credentials stored with ``public`` are bound to, or ``None`` when the field cannot hold any.

    An absolute http(s) URL has its own. A path (one leading ``/``, so no reader can read a host in it) takes the origin of
    ``base``. A protocol-relative address (``//host/path``), a text with no leading slash (``@host/x``, which a reader that
    appends it to ``host:port`` reads as user info), a scheme that is not http(s), or a path with no base gives ``None``.
    """
    text = str(public or "").strip()
    if is_absolute(text):
        return strict_origin(text)
    if not text.startswith("/") or text.startswith("//") or "\\" in text:
        return None
    return strict_origin(str(base or "").strip())


def has_entries(container: Mapping[str, Any], key: str) -> bool:
    held = container.get(key + _ENTRIES)
    return isinstance(held, Mapping) and bool(held)


# -- the base a path is joined against ------------------------------------
# One answer per kind of field, used when a value is stored, when the fingerprint is made and when a migration binds an
# older record. A reader passes the base it really dials (its own join); a reader whose base differs from this one finds
# its credentials bound to another origin and sends none.


def base_of(record: Mapping[str, Any], path: str) -> str:
    """The base a relative URL field at ``path`` is joined against, from the record as stored (``""`` when it has none)."""
    from gateway import service_registry

    config = record.get("config") if isinstance(record.get("config"), Mapping) else {}
    if path.startswith("observations."):
        # A declared probe is dialed at the connection address; a record that names none is probed at its host and port.
        connection = record.get("connection") if isinstance(record.get("connection"), Mapping) else config.get("connection")
        address = str(connection.get("address") or "").strip() if isinstance(connection, Mapping) else ""
        if address:
            return address
        if config.get("port") not in (None, ""):
            return f"http://{config.get('host') or '127.0.0.1'}:{config['port']}"
        return service_registry.service_base_url(config)
    if ".driver_config." in f".{path}":
        return service_registry.lifecycle_dial_url(config)
    return service_registry.service_base_url(config)


def bound_fields(record: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any], str, str | None]]:
    """(path, mapping, key, bound origin) of every URL field of ``record`` that holds credentials.

    The origin is ``None`` for a field that cannot hold any. The whole-URL form (``whole``) has no public address; its origin
    is only known from the value, which only the migration and the runtime read.
    """
    from gateway import record_carriers

    for path, parent, key in record_carriers.url_containers(record):
        if not has_entries(parent, key):
            continue
        held = parent[key + _ENTRIES]
        origin = None if isinstance(held, Mapping) and held.get("whole") else field_origin(parent.get(key), base_of(record, path))
        yield path, parent, key, origin


def binding_marks(record: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """What the credential fingerprint holds for the URL fields of a record: each field with credentials and where they go.

    The mark is the bound origin, or the field's own text when it has none (so a change of such a field is a change too).
    A field kept whole is marked by its entry ids: its address is a secret, and replacing it is replacing the entry.
    """
    marks = []
    for path, parent, key, origin in bound_fields(record):
        held = parent[key + _ENTRIES]
        if isinstance(held, Mapping) and held.get("whole"):
            marks.append((path, "whole:" + str((held["whole"] or {}).get("id") or "")))
        else:
            marks.append((path, origin or "unbound:" + str(parent.get(key) or "")))
    return tuple(sorted(marks))


def credential_states(record: Mapping[str, Any], origin_of_ref: Callable[[str], str | None]) -> list[dict[str, str]]:
    """For each URL field of ``record`` that holds credentials, whether they are bound to the address the field has now.

    ``"confirmed"`` when every stored credential is bound to the field's effective origin, ``"needs_confirmation"`` when one
    is bound to none (stored before decision D42) or to another (the field or its base moved). A URL kept whole holds its
    address inside the secret, so it is confirmed when it is bound to any origin. The result names carriers only: no value,
    no reference and no address.
    """
    from gateway import record_carriers

    states = []
    for path, parent, key, origin in bound_fields(record):
        held = parent[key + _ENTRIES]
        refs = record_carriers.refs_in(held)
        bound = [origin_of_ref(ref) for ref in refs]
        whole = isinstance(held, Mapping) and bool(held.get("whole"))
        ok = bool(refs) and all(item is not None and (whole or item == origin) for item in bound)
        states.append({"carrier": path, "state": "confirmed" if ok else "needs_confirmation"})
    return states
