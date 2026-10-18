"""Runtime-private resolution of secret references (decisions D26 and D30).

This is the one module that reads a secret value back. It is called at the moment something needs the value:
an engine request that sends a header, a process or command start that needs an argument or an environment, a
rendered provider config, a package clone. Management code (the admin routes, the projection, the migration,
the commands) never imports it; an architecture test fails if one does.

Resolution is bound to the origin the secret was stored for. A header or URL secret is read with the origin of
the address it is about to be sent to, so a record whose address moved cannot send it somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from gateway import admin_store, record_carriers, secret_refs, secret_store

configure = secret_refs.configure
use_database_of = secret_refs.use_database_of
database = secret_refs.database


class SecretUnavailable(RuntimeError):
    """A reference could not be resolved (missing, or bound to another origin). Carries no value and no reference."""


def resolve(ref: Any, *, origin: str | None = None) -> str:
    if not secret_refs.is_ref(ref):
        raise SecretUnavailable("a secret reference is invalid")
    try:
        value = secret_store.get_provider_secret(str(ref), db_path=database(), expected_origin=origin)
    except (KeyError, PermissionError, ValueError, FileNotFoundError):
        raise SecretUnavailable("a secret could not be read") from None
    return value


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


def url(container: Mapping[str, Any], key: str) -> str:
    """The URL a record's field stands for: the public form with its stored credentials put back."""
    public = container.get(key)
    held = container.get(key + record_carriers.SECRETS_SUFFIX)
    if not isinstance(held, Mapping) or not held:
        return str(public or "")
    if held.get("whole"):
        return resolve(held["whole"]["secret_ref"])
    base = str(public or "")
    origin = secret_refs.origin_or_none(base)
    userinfo = resolve(held["userinfo"]["secret_ref"], origin=origin) if isinstance(held.get("userinfo"), Mapping) else None
    query = record_carriers.query_chunks(held.get("query"), lambda ref: resolve(ref, origin=origin)) if "query" in held else None
    fragment = None
    if isinstance(held.get("fragment"), Mapping):
        fragment = resolve(held["fragment"]["secret_ref"], origin=origin) if secret_refs.is_secret_entry(held["fragment"]) else ""
    return record_carriers.join_url(base, userinfo=userinfo, query=query, fragment=fragment)


