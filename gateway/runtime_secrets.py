"""Runtime-private resolution of secret references (decisions D26 and D30).

This is the one module that reads a secret value back. It is called at the moment something needs the value:
an engine request that sends a header, a process or command start that needs an argument or an environment, a
rendered provider config, a package clone. Management code (the admin routes, the projection, the migration,
the commands) never imports it; an architecture test fails if one does.

Resolution is bound to the origin the secret was stored for. A header secret is read with the origin of the address
it is about to be sent to. The credentials of a URL are read for the effective origin of the field (``url_binding``)
and are put back only when the address the reader dials has that origin (decision D39), so a record whose address
moved, or whose public field was re-aimed, cannot send them somewhere else.
"""

from __future__ import annotations

import collections
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, quote_plus

from cryptography.exceptions import InvalidTag

from gateway import admin_store, record_carriers, secret_refs, secret_store, url_binding

configure = secret_refs.configure
use_database_of = secret_refs.use_database_of
database = secret_refs.database


class SecretUnavailable(RuntimeError):
    """A reference could not be resolved (missing, or bound to another origin). Carries no value and no reference."""


class OriginRefused(SecretUnavailable):
    """The stored credentials of a URL were not put back: the address it is about to be sent to is not the one they were
    stored for (decision D39). The text is fixed; it holds no value, no reference and no address."""

    def __init__(self) -> None:
        super().__init__("the stored credentials of this URL belong to another address; nothing was sent")


class StructuredEnvValue(SecretUnavailable):
    """An environment value that was a mapping or a list when it was stored (decision D39). The text names the variable only."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"the environment value {name[:64]!r} was saved as a mapping or a list, which the gateway never passed to a "
            "process; set it again as text. The process was not started.")
        self.name = name


# Every secret value this process has resolved or read, by where it came from (a secret reference, or ``env:NAME``). The log
# lines that can carry an upstream's text pass through ``scrub``, which removes these (a server may echo a credential it
# received, in a body, a header or an error).
#
# A value is kept as long as its source exists. A source is only dropped when more than ``_SOURCE_LIMIT`` of them are held
# at once (a store with that many secrets in use is not a store this was written for), oldest-used first, and every use
# refreshes it; so a value still in use is never pushed out by newer ones. A source whose value changes (an environment
# variable set again, a secret replaced under the same reference) keeps its last ``_VALUES_PER_SOURCE`` values, because an
# upstream may still echo the one before.
_RESOLVED: "collections.OrderedDict[str, tuple[str, ...]]" = collections.OrderedDict()
_RESOLVED_LOCK = threading.Lock()
_SOURCE_LIMIT = 65536
_VALUES_PER_SOURCE = 3


def remember(value: Any, source: str | None = None) -> None:
    """Register a secret value for ``scrub``. ``source`` names where it came from (default: the value itself)."""
    if not isinstance(value, str) or not value:
        return
    key = source or "value:" + value
    with _RESOLVED_LOCK:
        held = _RESOLVED.get(key, ())
        if value not in held:
            held = (value, *held)[:_VALUES_PER_SOURCE]
        _RESOLVED[key] = held
        _RESOLVED.move_to_end(key)
        while len(_RESOLVED) > _SOURCE_LIMIT:
            _RESOLVED.popitem(last=False)


def env_secret(name: str, default: str = "") -> str:
    """The value of an environment variable that holds a credential (empty is unset), registered for ``scrub``."""
    value = os.environ.get(name) or default
    remember(value, f"env:{name}")
    return value


def scrub(text: Any, *, limit: int = 300) -> str:
    """``text`` for a log line: every secret value this process resolved is removed (as stored and URL-encoded), and the
    result is cut to ``limit`` characters.

    A value of four characters or more is removed wherever it appears. A shorter one is removed only where it stands alone
    (between characters that are not letters or digits): anywhere else it would take letters out of every line.
    """
    out = str(text)
    with _RESOLVED_LOCK:
        values = sorted({value for held in _RESOLVED.values() for value in held}, key=len, reverse=True)
    for value in values:
        for form in dict.fromkeys((value, quote(value, safe=""), quote_plus(value), quote(value))):
            if not form:
                continue
            if len(form) >= 4:
                out = out.replace(form, "[removed]")
            else:
                out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", "[removed]", out)
    return out if len(out) <= limit else out[:limit] + "...[cut]"


_REFUSALS: dict[str, float] = {}


def _refuse(label: str) -> "OriginRefused":
    """The refusal of a URL whose credentials belong to another address; logged once a minute per field, without values."""
    now = time.monotonic()
    if now - _REFUSALS.get(label, -3600.0) >= 60.0:
        _REFUSALS[label] = now
        print(f"[secrets] the stored credentials of {label!r} were not sent: the address it would reach is not the one they were stored for", flush=True)
    return OriginRefused()


def resolve(ref: Any, *, origin: str | None = None) -> str:
    """The secret stored under ``ref`` for ``origin``. Every way a lookup can fail raises ``SecretUnavailable``, with no value."""
    if not secret_refs.is_ref(ref):
        raise SecretUnavailable("a secret reference is invalid")
    try:
        value = secret_store.get_provider_secret(str(ref), db_path=database(), expected_origin=origin)
    except PermissionError:
        raise OriginRefused() from None
    except (KeyError, ValueError, OSError, sqlite3.Error, InvalidTag):
        raise SecretUnavailable("a secret could not be read") from None
    remember(value, str(ref))
    return value


def _ref_of(entry: Any) -> Any:
    """The reference an entry holds, or None for anything that is not a mapping with one (a view, a stray value)."""
    return entry.get("secret_ref") if isinstance(entry, Mapping) else None


def header_pairs(headers: Any, *, origin: str | None) -> dict[str, str]:
    """The request headers a record lists: public rows as written, secret rows resolved for ``origin``.

    A row that cannot be resolved is left out, never sent empty or half-built.
    """
    out: dict[str, str] = {}
    for row in headers if isinstance(headers, list) else ():
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            continue
        name = row["name"].strip()
        if secret_refs.is_secret_entry(row):
            try:
                out[name] = resolve(row["secret_ref"], origin=origin)
            except SecretUnavailable:
                continue
        elif row.get("public") and isinstance(row.get("value"), str):
            out[name] = row["value"]
    return out


def env_mapping(env: Any, *, strict: bool = False) -> dict[str, str]:
    """The environment a record declares: public values as written, secret values resolved.

    A plain text value is one no migration has seen yet (a record written by hand): it is used as written.
    With ``strict`` any value that is not text, a secret entry or a public entry raises ``ValueError``.
    """
    out: dict[str, str] = {}
    if not isinstance(env, Mapping):
        return out
    for name, item in env.items():
        if not isinstance(name, str):
            if strict:
                raise ValueError("an environment name must be text")
            continue
        if secret_refs.is_secret_entry(item):
            if item.get("structured"):
                print(f"[secrets] {StructuredEnvValue(name)}", flush=True)
                raise StructuredEnvValue(name)
            out[name] = resolve(item["secret_ref"])
        elif isinstance(item, Mapping) and item.get("public") and isinstance(item.get("value"), str):
            out[name] = item["value"]
        elif isinstance(item, str):
            out[name] = item
        elif strict:
            raise ValueError("an environment value must be text")
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            out[name] = str(item)
    return out


def argv(command: Any, *, strict: bool = False) -> list[str]:
    """A command as an argument list: public literals as written, secret entries resolved (``prefix`` + value).

    A line is split as a shell would (no shell is run). With ``strict`` an item that is neither text nor a secret
    entry raises ``ValueError`` instead of being turned into text.
    """
    if isinstance(command, str):
        import shlex

        return shlex.split(command)
    out: list[str] = []
    for item in command if isinstance(command, (list, tuple)) else ():
        if secret_refs.is_secret_entry(item) and item.get("whole"):
            import shlex

            out.extend(shlex.split(resolve(item["secret_ref"])))  # a line kept whole fails here as it always did
        elif secret_refs.is_secret_entry(item):
            out.append(str(item.get("prefix") or "") + resolve(item["secret_ref"]))
        elif isinstance(item, str) or not strict:
            out.append(str(item))
        else:
            raise ValueError("a command argument must be text or a secret entry")
    return out


def scalar(container: Mapping[str, Any], key: str) -> str:
    """One secret value of a mapping (a driver's admin key): an entry resolved, a plain text as written, else empty."""
    item = container.get(key) if isinstance(container, Mapping) else None
    if secret_refs.is_secret_entry(item):
        return resolve(item["secret_ref"])
    return item if isinstance(item, str) else ""


def first_url(config: Mapping[str, Any], keys: tuple[str, ...] = ("base_url", "api_base", "default_base_url")) -> str:
    """The first of ``keys`` that a record sets, with its stored credentials put back, without a trailing slash.

    Empty when none is set. A URL kept whole (``<key>_entries.whole``) leaves its public field empty and counts as set.
    """
    for key in keys:
        raw = str(config.get(key) or "").strip()
        held = config.get(key + record_carriers.SECRETS_SUFFIX)
        if raw or (isinstance(held, Mapping) and isinstance(held.get("whole"), Mapping)):
            return url({key: raw, key + record_carriers.SECRETS_SUFFIX: held}, key).rstrip("/")
    return ""


def service_url(config: Mapping[str, Any], keys: tuple[str, ...] = ("base_url", "api_base", "default_base_url")) -> str:
    """A record's endpoint with its stored credentials: the first of ``keys`` that holds a URL, joined, without a
    trailing slash; the record's host and port when none does."""
    from gateway.service_registry import service_base_url

    return first_url(config, keys) or service_base_url(config)


def url(container: Mapping[str, Any], key: str, *, base: str | None = None, join: url_binding.Join | None = None,
        dial: Callable[[str], str] | None = None) -> str:
    """The address a record's URL field stands for, with its stored credentials put back (decisions D39 and D42).

    ``dial(text)`` is the reader's own way of turning the field's text into the address it will send to (``base`` and
    ``join`` are a short form: ``dial(text) = join(base, text)``). It is called once, on the field's public text, and the
    credentials are attached to the address it returns: that address is checked and is the one returned, so a reader that
    sends the result sends to exactly what was checked, and nothing is read from the record or the configuration between
    the check and the send. The credentials are put back only for the origin of that address, and only when the store
    bound them to that origin: an address with no origin, a secret bound to another origin, or a secret bound to none
    (stored before decision D42, and not yet confirmed by the owner) raises ``OriginRefused``.

    A field without credentials is its public text, or ``dial(text)`` with a ``dial``. A path needs a ``dial``.
    """
    if dial is None and join is not None:
        dial = lambda text: join(str(base or ""), text)  # noqa: E731
    public = container.get(key)
    held = container.get(key + record_carriers.SECRETS_SUFFIX)
    if not isinstance(held, Mapping) or not held:
        text = str(public or "")
        return dial(text) if dial is not None and text else text
    whole = held.get("whole")
    if whole:
        bound = _stored_origin(_ref_of(whole))
        if bound is None:
            raise _refuse(key)  # before anything is read
        try:
            value = resolve(_ref_of(whole), origin=bound)
        except OriginRefused:
            raise _refuse(key) from None
        dialed = dial(value) if dial is not None else value
        if url_binding.strict_origin(dialed) != bound:
            raise _refuse(key)
        return dialed
    text = str(public or "")
    if not url_binding.path_shaped(text):
        raise _refuse(key)  # before anything is read
    address = dial(text) if dial is not None else text
    origin = url_binding.strict_origin(address)
    # An absolute field names its own origin; a reader that does not treat it as absolute (it appends it to a base) would
    # send somewhere else than the field says, so the origin it reaches must be the field's own.
    if origin is None or (url_binding.is_absolute(text) and url_binding.strict_origin(text) != origin):
        raise _refuse(key)  # before anything is read
    try:
        dialed = restore(address, held, origin)
    except OriginRefused:
        raise _refuse(key) from None
    if url_binding.strict_origin(dialed) != origin:
        raise _refuse(key)
    return dialed


def _stored_origin(ref: Any) -> str | None:
    """The origin the store bound a secret to (metadata only); None when it has none or cannot be read."""
    if not secret_refs.is_ref(ref):
        return None
    try:
        return secret_store.provider_secret_origin(str(ref), db_path=database())
    except (ValueError, OSError, sqlite3.Error):
        return None


def restore(public: str, held: Mapping[str, Any], origin: str) -> str:
    """``public`` (an address with no credentials) with the credential parts of ``held`` put back, each read for ``origin``."""
    userinfo = None
    if held.get("userinfo"):
        marker = held["userinfo"]
        if secret_refs.is_secret_entry(marker):
            userinfo = resolve(marker["secret_ref"], origin=origin)
        elif isinstance(marker, Mapping) and marker.get("empty") is True:
            userinfo = ""  # the address was written with an empty user info (``http://@host/``)
        else:
            resolve(None)  # neither an entry nor an empty marker: it cannot be rebuilt
    params = record_carriers.query_chunks(held.get("params"), lambda ref: resolve(ref, origin=origin)) if "params" in held else None
    query = record_carriers.query_chunks(held.get("query"), lambda ref: resolve(ref, origin=origin)) if "query" in held else None
    fragment = None
    if isinstance(held.get("fragment"), Mapping):
        marker = held["fragment"]
        if secret_refs.is_secret_entry(marker):
            fragment = resolve(marker["secret_ref"], origin=origin)
        elif marker.get("empty") is True:
            fragment = ""
        else:
            resolve(None)  # a fragment that is neither an entry nor an empty marker cannot be rebuilt
    if params and record_carriers.path_param_count(public) != len(params):
        raise SecretUnavailable("a stored URL part cannot be placed in this address")
    return record_carriers.join_url(public, userinfo=userinfo, query=query, fragment=fragment, params=params)


