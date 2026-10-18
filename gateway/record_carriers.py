"""The places a service record can carry a secret, as structure (decisions D26 and D29).

A record never holds a secret value. It holds, in each carrier below, an *entry* that points at the encrypted
store (``gateway/secret_refs.py``):

``config.headers``  a list of rows. A secret row is ``{"id", "name", "secret_ref"}``. A public row is
                    ``{"id", "name", "value", "public": true}`` and exists only when a write said so.
env (``env`` of ``process``, of a driver config and of ``config``)
                    a mapping of variable name to an entry. A secret value is ``{"id", "secret_ref"}``; a public
                    one is ``{"id", "value", "public": true}``.
commands (``start``, ``stop``, ``status``, ``reload``)
                    an argument list whose items are a public literal (a plain string) or a secret entry
                    ``{"id", "secret_ref", "prefix"?}``. ``prefix`` is the public text that comes before the
                    secret in the same argument (``--api-key=``), so the resolved argument is ``prefix + value``.
URL fields          the field holds the public endpoint (no user info, no query, no fragment); its credentials sit
                    beside it in ``<field>_entries``: ``{"userinfo": entry, "query": [{"id", "name", "secret_ref"}],
                    "fragment": entry}``. A URL that cannot be read with confidence is stored whole in
                    ``<field>_entries.whole`` and the field is left empty.

This module is management-safe: it builds and walks structure and never decrypts. A single secret value (``lifecycle.admin_key``, ``lifecycle.api_key``) is one entry. The legacy shapes (an inline
header value, a plain environment value, a command argument that follows a credential flag, a URL with user info)
are read here once, by the migration and by ingestion, and turned into entries; nothing else reads them.
"""

from __future__ import annotations

import copy
import json
import re
import shlex
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from gateway import secret_refs
from gateway.secret_refs import Put

COMMAND_KEYS = ("start", "stop", "status", "reload")
# Mappings that may hold commands and an ``env`` (a record's process block and every driver config).
COMMAND_CONTAINERS = (
    "process",
    "process.profile_apply",
    "process.legacy_lifecycle.driver_config",
    "process.legacy_lifecycle.driver_config.profile_apply",
    "lifecycle.driver_config",
    "lifecycle.driver_config.profile_apply",
    "config.lifecycle.driver_config",
    "config.lifecycle.driver_config.profile_apply",
    "config.process",
    "config.process.profile_apply",
)
ENV_CONTAINERS = (*COMMAND_CONTAINERS, "config")
# Every URL a record holds and the runtime reads as one (decision D29). A field the runtime reads as an address
# is listed here whatever its name, so the credentials it carries move to the store like those of ``base_url``.
_DRIVER_URL_KEYS = ("health_endpoint", "load_url", "unload_url", "verify_endpoint")
URL_FIELD_PATHS = (
    "config.host", "config.url", "config.address", "config.base_url", "config.api_base", "config.default_base_url",
    "config.health_endpoint", "config.key_url", "config.docs_url", "config.gateway_lifecycle_base_url",
    "config.endpoint", "config.upstream",
    "config.context_window_probe.endpoint",
    "process.health_endpoint", "config.process.health_endpoint",
    *(f"{base}.{key}" for base in ("process.legacy_lifecycle.driver_config", "lifecycle.driver_config",
                                   "config.lifecycle.driver_config") for key in _DRIVER_URL_KEYS),
)
# A list of mappings whose ``url`` is a URL field too (the models a record lists).
URL_LIST_PATHS = ("config.models",)
# A single secret value in a mapping (the admin key and the API key of an http load/unload driver).
SCALAR_SECRET_PATHS = (
    "lifecycle.admin_key", "process.legacy_lifecycle.admin_key", "config.lifecycle.admin_key",
    "lifecycle.api_key", "process.legacy_lifecycle.api_key", "config.lifecycle.api_key",
)
HEADER_PATH = "config.headers"
HEADER_ROW_KEYS = ("id", "name", "secret_ref", "value", "public")
SECRETS_SUFFIX = "_entries"


class AmbiguousUrl(ValueError):
    """A URL whose parts cannot be told apart with confidence."""


class UrlRefused(ValueError):
    """A write named a URL that cannot be read with confidence. The text names the field and never the value."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"{path} is not a URL the gateway can read with confidence (a backslash, a control character or an invalid "
            "port); write it as scheme://host:port/path, with any credentials sent as secret entries.")
        self.path = path


class EntryRefused(ValueError):
    """A write put a read's view of a secret entry, or a malformed entry, where an entry goes. The text names the place only."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"{path} holds what a read shows for a secret entry (or a malformed entry), not an entry. Leave it out to keep "
            "the stored one, or change it with the secret-entries commands.")
        self.path = path


class ValueRefused(ValueError):
    """A write put a mapping or a list where an environment value goes. The text names the place and never the value."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"{path} holds a mapping or a list; an environment value is text. Write it as text, or leave it out "
            "(the gateway never passed a structured value to a process).")
        self.path = path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


_INDEXED = re.compile(r"^([^\[\]]+)\[(\d{1,4})\]$")


def _get(record: Mapping[str, Any], path: str) -> Any:
    """The value at a dotted path; a part ``name[3]`` is the fourth item of the list ``name``."""
    node: Any = record
    for part in path.split("."):
        indexed = _INDEXED.match(part)
        if indexed:
            node = node.get(indexed.group(1)) if isinstance(node, Mapping) else None
            if not isinstance(node, list) or int(indexed.group(2)) >= len(node):
                return None
            node = node[int(indexed.group(2))]
            continue
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _container(record: Mapping[str, Any], path: str) -> dict[str, Any] | None:
    node = _get(record, path)
    return node if isinstance(node, dict) else None


def observation_http_blocks(record: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    observations = record.get("observations")
    if isinstance(observations, Mapping):
        for name, observation in observations.items():
            http = observation.get("http") if isinstance(observation, Mapping) else None
            if isinstance(http, dict):
                yield f"observations.{name}.http", http


def url_containers(record: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any], str]]:
    """(path of the field, the mapping that holds it, its key) for every URL field a record can hold.

    A field counts when its credentials sit beside it even if the public field itself is gone."""
    for path in URL_FIELD_PATHS:
        parent_path, _, key = path.rpartition(".")
        parent = _container(record, parent_path)
        if parent is not None and (key in parent or key + SECRETS_SUFFIX in parent):
            yield path, parent, key
    for base, http in observation_http_blocks(record):
        for key in ("path", "url"):
            if key in http or key + SECRETS_SUFFIX in http:
                yield f"{base}.{key}", http, key
    for list_path in URL_LIST_PATHS:
        rows = _get(record, list_path)
        for index, row in enumerate(rows if isinstance(rows, list) else ()):
            if isinstance(row, dict) and ("url" in row or "url" + SECRETS_SUFFIX in row):
                yield f"{list_path}[{index}].url", row, "url"


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


_PARAM = re.compile(r";([^;/]*)")


def split_path_params(path: str) -> tuple[str, list[tuple[str, str | None]]]:
    """A path without the values of its ``;name=value`` parameters, and the raw chunks it held, in order.

    A parameter is read like a query chunk: ``name=value`` keeps ``;name`` in the path (the name is public), a bare word
    (``;token123``) keeps a bare ``;`` because the word may be the credential itself, and an empty chunk (``;;``, a
    trailing ``;``) is a gap. Every ``;`` of the path is one chunk, so ``_join_params`` gives back the same bytes.
    """
    chunks: list[tuple[str, str | None]] = []

    def take(match: re.Match[str]) -> str:
        text = match.group(1)
        name, equals, value = text.partition("=")
        if equals:
            chunks.append((name, value))
            return ";" + name
        chunks.append((text, None))
        return ";"

    return _PARAM.sub(take, path), chunks


def _params_held(path: str, entries: Any) -> bool:
    """True when the ``;`` chunks of a public path are the ones ``entries`` already stands for (a second pass changes nothing)."""
    texts = [match.group(1) for match in _PARAM.finditer(path)]
    if not isinstance(entries, list) or len(entries) != len(texts):
        return False
    for text, entry in zip(texts, entries):
        if not isinstance(entry, Mapping):
            return False
        expected = "" if entry.get("bare") or entry.get("gap") else str(entry.get("name") or "")
        if text != expected:
            return False
    return True


def _join_params(public: str, chunks: list[tuple[str, str | None]] | None) -> str:
    """``public`` with the parameter chunks put back into the path, in order."""
    if not chunks:
        return public
    start = public.find("/", public.index("://") + 3) if "://" in public else public.find("/")
    if start < 0:
        return public
    remaining = iter(chunks)

    def put_back(match: re.Match[str]) -> str:
        chunk = next(remaining, None)
        if chunk is None:
            return match.group(0)
        name, value = chunk
        return f";{name}" if value is None else f";{name}={value}"

    return public[:start] + _PARAM.sub(put_back, public[start:])


def split_url(value: str, held_params: Any = None) -> tuple[str, dict[str, Any]]:
    """``(public form, credential parts)`` of a URL, an address with user info, or a path with a query.

    The public form has no user info, no query, no fragment and no path parameter values. The credential parts are
    ``{"userinfo": "user:password", "params": [(name, value or None), ...], "query": [(name, value or None), ...],
    "fragment": text}``, each present only when the URL had it; ``params`` and ``query`` are the raw chunks of the
    string, in order. ``held_params`` are the parameter entries a stored record already has for this field: a public
    path whose ``;`` chunks they stand for is not read again. Raises ``AmbiguousUrl`` for text that cannot be read with
    confidence (an invalid port, a bracket that does not close, a control character).
    """
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise AmbiguousUrl("a URL holds a control character")
    if "\\" in value:
        # Browsers and some clients read a backslash as a slash, urlsplit does not: ``http://evil.com\@good.com/`` is
        # one host to one reader and another to the other. No part of it can be told apart with confidence.
        raise AmbiguousUrl("a URL holds a backslash")
    parts: dict[str, Any] = {}
    if "://" in value:
        scheme, _, rest = value.partition("://")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", scheme):
            raise AmbiguousUrl("a URL has an invalid scheme")
        prefix = f"{scheme}://"
    else:
        rest, prefix = value, ""
    rest, hash_mark, fragment = rest.partition("#")
    rest, question, query = rest.partition("?")
    authority, slash, path = rest.partition("/")
    if slash and ";" in path and not _params_held(path, held_params):
        path, chunks = split_path_params(path)
        parts["params"] = chunks
    if prefix or _looks_like_authority(authority):
        if "@" in authority:
            userinfo, _, host = authority.rpartition("@")
            parts["userinfo"] = userinfo
        else:
            host = authority
        if prefix:
            try:
                urlsplit(f"{prefix}{host}").port  # noqa: B018 - validates the port
            except ValueError:
                raise AmbiguousUrl("a URL has an invalid port") from None
        public = f"{prefix}{host}{slash}{path}"
    else:
        public = f"{authority}{slash}{path}" if slash else authority
    if question:
        # The query string is kept as written, chunk by chunk: a chunk is ``name=value`` (``value`` may be empty) or a bare
        # word with no ``=`` (``value`` is None), and an empty chunk (``&&``, a trailing ``?``) is a chunk too, so
        # ``join_url`` gives back exactly the bytes that came in (no decoding, no re-encoding, no dropped separator).
        parts["query"] = [(chunk.partition("=")[0], chunk.partition("=")[2]) if "=" in chunk else (chunk, None)
                          for chunk in query.split("&")]
    if hash_mark:
        parts["fragment"] = fragment  # an empty fragment is kept too: ``http://h/x#`` is not ``http://h/x``
    return public, parts


def _looks_like_authority(text: str) -> bool:
    """A bare ``user:password@host`` (a ``host`` field): the part before the first slash has user info in it."""
    return "@" in text and " " not in text


def join_url(public: str, *, userinfo: str | None = None, query: list[tuple[str, str | None]] | None = None,
             fragment: str | None = None, params: list[tuple[str, str | None]] | None = None) -> str:
    """The URL that ``split_url`` took apart: the public form with its credential parts put back, byte for byte."""
    out = _join_params(public, params)
    if userinfo is not None:  # an empty user info is still an @ (``http://@host/``)
        if "://" in out:
            scheme, _, rest = out.partition("://")
            out = f"{scheme}://{userinfo}@{rest}"
        elif not out.startswith("/"):
            out = f"{userinfo}@{out}"
    if query:
        out += ("&" if "?" in out else "?") + "&".join(name if item is None else f"{name}={item}" for name, item in query)
    if fragment is not None:
        out += f"#{fragment}"
    return out


def url_entries(public: str, parts: Mapping[str, Any], put: Put) -> dict[str, Any]:
    """The entries that hold the credential ``parts`` of a URL whose public form is ``public`` (bound to its origin)."""
    origin = secret_refs.origin_or_none(public)
    held: dict[str, Any] = {}
    if "userinfo" in parts:
        # an empty user info holds nothing to store, but it is part of the bytes: it gets a marker like an empty fragment
        held["userinfo"] = (secret_refs.entry(put(parts["userinfo"], origin)) if parts["userinfo"]
                            else {"id": secret_refs.new_entry_id(), "empty": True})
    if "params" in parts:
        held["params"] = [_query_entry(name, item, put, origin) for name, item in parts["params"]]
    if "query" in parts:
        held["query"] = [_query_entry(name, item, put, origin) for name, item in parts["query"]]
    if "fragment" in parts:
        held["fragment"] = (secret_refs.entry(put(parts["fragment"], origin)) if parts["fragment"]
                            else {"id": secret_refs.new_entry_id(), "empty": True})
    return held


def _query_entry(name: str, item: str | None, put: Put, origin: str | None) -> dict[str, Any]:
    """One chunk of a query string as an entry. A value is private, a name is public.

    ``name=value`` keeps its name beside the stored value. An empty value (``name=``) and an empty chunk hold nothing to
    store, so they are entries without a reference; a bare word (``?token123``) is stored whole, since a word with no
    ``=`` may be the credential itself.
    """
    if item is None:
        if name == "":
            return {"id": secret_refs.new_entry_id(), "gap": True}
        return secret_refs.entry(put(name, origin), bare=True)
    if item == "":
        return {"id": secret_refs.new_entry_id(), "name": name, "empty": True}
    return secret_refs.entry(put(item, origin), name=name)


def query_chunks(entries: Any, resolve: Any) -> list[tuple[str, str | None]]:
    """The raw chunks a stored query holds, in order; ``resolve(ref)`` gives a stored value."""
    chunks: list[tuple[str, str | None]] = []
    for item in entries if isinstance(entries, list) else ():
        if isinstance(item, Mapping) and secret_refs.is_secret_entry(item):
            value = resolve(item["secret_ref"])
            chunks.append((value, None) if item.get("bare") else (str(item.get("name") or ""), value))
        elif isinstance(item, Mapping) and item.get("empty"):
            chunks.append((str(item.get("name") or ""), ""))
        elif isinstance(item, Mapping) and item.get("gap"):
            chunks.append(("", None))
        else:
            # A chunk with no reference (a read written back) cannot be rebuilt: the resolver refuses it, so the URL is not
            # sent with a part missing.
            resolve(item.get("secret_ref") if isinstance(item, Mapping) else None)
    return chunks


def endpoint_origin(config: Any) -> str | None:
    """The origin a record's secrets are bound to: where the record's own endpoint is, from the same fields the runtime reads."""
    if not isinstance(config, Mapping):
        return None
    for key in ("base_url", "api_base", "default_base_url", "address"):
        origin = secret_refs.origin_or_none(str(config.get(key) or "").strip())
        if origin:
            return origin
    host, port = str(config.get("host") or "").strip(), config.get("port")
    if host and port is not None:
        scheme = str(config.get("scheme") or "http").strip() or "http"
        return secret_refs.origin_or_none(f"{scheme}://{host}:{port}")
    return None


def externalize_url(container: dict[str, Any], key: str, put: Put, *, reject_ambiguous: bool = False) -> bool:
    """Move the credentials out of ``container[key]`` into the store. True when it changed anything. Idempotent.

    A URL that cannot be read with confidence is stored whole as one private value and the field is left empty
    (a record that exists already must not lose it); with ``reject_ambiguous`` (a new write) it raises
    ``AmbiguousUrl`` instead, so the caller is told to fix it.
    """
    value = container.get(key)
    if not isinstance(value, str) or not value:
        return False
    existing = container.get(key + SECRETS_SUFFIX)
    try:
        public, parts = split_url(value, existing.get("params") if isinstance(existing, Mapping) else None)
    except AmbiguousUrl:
        if reject_ambiguous:
            raise
        container[key] = ""
        container[key + SECRETS_SUFFIX] = {"whole": secret_refs.entry(put(value, None))}
        return True
    if not parts:
        return False
    held = url_entries(public, parts, put)
    container[key] = public
    container[key + SECRETS_SUFFIX] = {**(existing if isinstance(existing, Mapping) else {}), **held}
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


WHOLE_COMMAND_LABEL = "a command line that could not be split into arguments"


def _legacy_command_items(command: Any, put: Put) -> list[Any] | None:
    """A legacy command (an argument list or one line) as items, or None when it holds no secret to move.

    A line that ``shlex`` cannot split (an unbalanced quote) is not shown and not guessed at: it is stored whole as
    one secret with a public label, and ``runtime_secrets.argv`` splits it when the command runs, which fails the
    way the line always did. An argument list whose items are all text (or numbers) is read by the credential rule.
    """
    from gateway.legacy_credential_rules import argv_secret_slots

    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            return [secret_refs.entry(put(command, None), whole=True, label=WHOLE_COMMAND_LABEL)] if command.strip() else None
    elif isinstance(command, list):
        tokens = list(command)
    else:
        return None
    # a number written where an argument goes is an argument (``--pin 4242``)
    texts = [str(token) if isinstance(token, (int, float)) and not isinstance(token, bool) else token for token in tokens]
    if not all(isinstance(token, str) for token in texts):
        return None
    slots = argv_secret_slots(texts)
    if not slots:
        return None
    items: list[Any] = []
    for index, token in enumerate(texts):
        visible = slots.get(index)
        if visible is None:
            items.append(tokens[index])
        else:
            entry = secret_refs.entry(put(token[visible:], None), **({"prefix": token[:visible]} if visible else {}))
            items.append(entry)
    return items


# ---------------------------------------------------------------------------
# One record
# ---------------------------------------------------------------------------


def _value_text(value: Any) -> str:
    """The text a header value is stored as: a number a YAML file wrote (``value: 1234``) is as much a secret as a string."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, dict)) and value:
        return json.dumps(value, sort_keys=True)
    return ""


def _coerce_header_row(row: Any) -> Any:
    """A header row written in a shape other than ``{"name", "value"}``, as that shape (None for a row that holds nothing).

    ``"Name: value"``, ``["Name", "value"]`` and ``{"Name": "value"}`` are all ways a header gets written by hand; each
    has a value in it, so each becomes a row. Anything else is kept whole as the value of a row with no name.
    """
    if row is None or row == "" or row == {} or row == []:
        return None
    if isinstance(row, str):
        name, colon, value = row.partition(":")
        return {"name": name.strip(), "value": value.strip()} if colon and name.strip() else {"name": "", "value": row}
    if isinstance(row, (list, tuple)):
        if len(row) == 2 and all(isinstance(item, (str, int, float)) and not isinstance(item, bool) for item in row):
            return {"name": str(row[0]), "value": row[1]}
        return {"name": "", "value": json.dumps(list(row), default=str)}
    if isinstance(row, Mapping):
        if any(key in row for key in HEADER_ROW_KEYS):
            return {key: row[key] for key in HEADER_ROW_KEYS if key in row}
        if len(row) == 1:
            (name, value), = row.items()
            return {"name": str(name), "value": value}
        return {"name": "", "value": json.dumps(dict(row), default=str)}
    return {"name": "", "value": json.dumps(row, default=str)}


def names_credential_header(name: Any) -> bool:
    """True for a header named like one that carries a credential (``Authorization``, ``X-Api-Key``): such a header is
    never public, whatever its row says, so an administrator cannot store a key in plain sight by marking it public."""
    from gateway.litellm_substrate import CREDENTIAL_HEADER_RE

    return isinstance(name, str) and CREDENTIAL_HEADER_RE.search(name) is not None


def _entry_keeping_id(ref: str, previous: Any, **public: Any) -> dict[str, Any]:
    entry = secret_refs.entry(ref, **public)
    if isinstance(previous, Mapping) and secret_refs.is_entry_id(previous.get("id")):
        entry["id"] = previous["id"]
    return entry


def _env_is_structured(value: Any) -> bool:
    """True for an environment value that is a non-empty mapping or list once a ``{"value": ...}`` wrapper is taken off.

    The runtime never passed such a value to a process (it skipped it), so a write that names one is refused; a stored
    one is kept by the migration as the text it would have been.
    """
    if isinstance(value, Mapping) and "value" in value:
        value = value["value"]
    return isinstance(value, (list, Mapping)) and bool(value)


def _env_text(value: Any) -> str:
    """The text an environment value is stored as; nothing for a switch or an empty value."""
    if isinstance(value, Mapping) and "value" in value:
        value = value["value"]
    if isinstance(value, bool) or value is None:
        return ""
    return _value_text(value)


def externalize_service(record: Mapping[str, Any], put: Put, *, reject_ambiguous: bool = False) -> tuple[dict[str, Any], list[str]]:
    """``record`` with every inline secret moved into the store, and the paths that were moved. Idempotent.

    Headers: a legacy row with a value becomes a secret row (only a row that says ``public: true``, exactly, stays
    public). Environment: a legacy value, in whatever shape, becomes a secret value. Commands: an argument that follows
    a credential flag (or sits after ``=`` in one) becomes an entry, and a command line that holds one becomes an
    argument list. URLs: user info and query values move beside the field. A record with none of these comes back
    equal to the one that went in. With ``reject_ambiguous`` (a new write) a URL that cannot be read raises ``UrlRefused``
    and an environment value that is a mapping or a list raises ``ValueRefused``.
    """
    out = copy.deepcopy(dict(record))
    moved: list[str] = []
    origin = endpoint_origin(out.get("config"))

    headers = _get(out, HEADER_PATH)
    if isinstance(headers, str) and headers.strip():
        headers = [line for line in headers.splitlines() if line.strip()]
        out["config"]["headers"] = headers
        moved.append(HEADER_PATH)
    if isinstance(headers, list):
        rows: list[Any] = []
        for original in headers:
            row = _coerce_header_row(original)
            if row is None:
                continue
            name = row.get("name")
            if "name" in row and not isinstance(name, str):
                row["name"] = "" if name is None else str(name)
            held = secret_refs.is_secret_entry(row)
            if (row.get("public") is True and isinstance(row.get("value"), str) and not held
                    and not names_credential_header(row.get("name"))):
                rows.append(row)  # a public row says so, exactly, holds text, and is not named for a credential
                continue
            if "value" in row:
                text = _value_text(row["value"])
                if text:
                    rows.append(_entry_keeping_id(put(text, origin), row, name=row.get("name", "")))
                    moved.append(HEADER_PATH)
                    continue
                row = {key: item for key, item in row.items() if key != "value"} if held else row  # an empty value holds nothing
            rows.append(row)
        out["config"]["headers"] = rows
    elif isinstance(headers, dict):
        # A name-to-value mapping is not the shape the gateway sends from, but it is still a place a value can be
        # written: every value moves to the store and the mapping keeps one entry per name, so nothing stays inline.
        for name, value in list(headers.items()):
            text = _value_text(value) if not secret_refs.is_secret_entry(value) else ""
            if text:
                headers[name] = secret_refs.entry(put(text, origin))
                moved.append(HEADER_PATH)

    for path in ENV_CONTAINERS:
        container = _container(out, path)
        env = container.get("env") if container is not None else None
        if isinstance(env, dict):
            for name, value in list(env.items()):
                if isinstance(value, Mapping) and value.get("public") is True and isinstance(value.get("value"), str) \
                        and not secret_refs.is_secret_entry(value):
                    continue
                if secret_refs.is_secret_entry(value) and "value" not in value:
                    continue
                if reject_ambiguous and _env_is_structured(value):
                    raise ValueRefused(f"{path}.env.{str(name)[:64]}")
                text = _env_text(value)
                if text:
                    env[name] = _entry_keeping_id(put(text, None), value)
                    moved.append(f"{path}.env.{name}")

    for path in COMMAND_CONTAINERS:
        container = _container(out, path)
        if container is None:
            continue
        for key in COMMAND_KEYS:
            items = _legacy_command_items(container.get(key), put) if key in container else None
            if items is not None:
                container[key] = items
                moved.append(f"{path}.{key}")

    for path in SCALAR_SECRET_PATHS:
        parent_path, _, key = path.rpartition(".")
        parent = _container(out, parent_path)
        value = parent.get(key) if parent is not None else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)  # a key a YAML file wrote as a number is as much a secret as one written as text
        if isinstance(value, str) and value:
            parent[key] = secret_refs.entry(put(value, None))
            moved.append(path)

    for path, parent, key in list(url_containers(out)):
        try:
            if externalize_url(parent, key, put, reject_ambiguous=reject_ambiguous):
                moved.append(path)
        except AmbiguousUrl:
            raise UrlRefused(path) from None
    return out, moved


# ---------------------------------------------------------------------------
# What a record refers to
# ---------------------------------------------------------------------------


def refs_in(value: Any) -> list[str]:
    """Every secret reference held anywhere in ``value`` (a record, a section), in document order."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            ref = node.get("secret_ref")
            if secret_refs.is_ref(ref):
                found.append(ref)
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(value)
    return found


def legacy_secret_texts(record: Any) -> list[str]:
    """The secret texts a record still holds inline (what the migration would move), longest first.

    A record that went through the schema 11 migration has none. An error message about a record that did not
    (a hand edit after the migration) must not repeat them, so the caller scrubs these from the message.
    """
    if not isinstance(record, Mapping):
        return []
    found: list[str] = []

    def collect(value: str, origin: str | None = None) -> str:
        found.append(value)
        return secret_refs.new_ref()

    try:
        externalize_service(record, collect)
    except Exception:  # noqa: BLE001 - a record of a shape this walk cannot read has nothing it can name
        return []
    return sorted({text for text in found if len(text) >= 4}, key=len, reverse=True)


def typed_value_texts(record: Any, *, minimum: int = 8) -> list[str]:
    """The longer text values a record holds in its plain slots (not its id or name), longest first.

    An error about a record the registry leaves out can quote a value of a slot that is not a secret slot: text typed into
    a numeric field, say. The caller scrubs these from the message, so naming the record is enough to find it.
    """
    found: set[str] = set()

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, Mapping):
            for name, item in node.items():
                walk(item, str(name))
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and len(node) >= minimum and key not in ("id", "name"):
            found.add(node)

    walk(record)
    return sorted(found, key=len, reverse=True)


def inline_carriers(record: Mapping[str, Any]) -> list[str]:
    """Paths where a record still holds a legacy inline carrier: what the migration would move, without moving it."""
    class _Probe:
        def __init__(self) -> None:
            self.count = 0

        def __call__(self, value: str, origin: str | None = None) -> str:
            self.count += 1
            return secret_refs.new_ref()

    _, moved = externalize_service(record, _Probe())
    return moved


PACKAGE_SOURCE_URL_KEYS = ("url", "repo_url")
# The fields of a package source the gateway reads or shows: where it is, which revision, what it is called. Any other
# field a caller puts in a source (a token, a password, a header, a key) is a credential by default and moves to the
# store, held beside the source in ``extra_entries``; nothing reads it back (decision D29: unknown means secret).
PACKAGE_SOURCE_PUBLIC_KEYS = (
    "type", "kind", "id", "name", "ref", "resolved_ref", "commit", "tag", "branch", "subdir", "path_in_repo", "path",
    "directory", "repo", "repository", *PACKAGE_SOURCE_URL_KEYS,
)
SOURCE_EXTRA_ENTRIES = "extra_entries"
_SOURCE_EXTRA_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
MAX_SOURCE_EXTRAS = 16


def package_sources(index: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Every source mapping an extension package index holds: its list of sources and each package's own."""
    sources = index.get("sources")
    for source in sources if isinstance(sources, list) else ():
        if isinstance(source, dict):
            yield source
    packages = index.get("packages")
    for package in packages.values() if isinstance(packages, Mapping) else ():
        source = package.get("source") if isinstance(package, Mapping) else None
        if isinstance(source, dict):
            yield source


def source_extras(source: Mapping[str, Any]) -> dict[str, str]:
    """The fields of a package source that are not the source's own, as the text each would be stored as.

    Raises ``ValueError`` (fixed text, no value) for a name that is not a plain name, for too many of them, or for a
    value too large to store.
    """
    extras: dict[str, str] = {}
    for key, value in source.items():
        if key in PACKAGE_SOURCE_PUBLIC_KEYS or key.endswith(SECRETS_SUFFIX) or key == SOURCE_EXTRA_ENTRIES:
            continue
        if value in (None, "", [], {}):
            continue
        if not isinstance(key, str) or not _SOURCE_EXTRA_NAME.fullmatch(key):
            raise ValueError("a package source field has a name that cannot be kept")
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
        if len(text) > 8192:
            raise ValueError("a package source field is too large to keep")
        extras[key] = text
    if len(extras) > MAX_SOURCE_EXTRAS:
        raise ValueError("a package source has too many fields")
    return extras


def externalize_source_extras(source: dict[str, Any], put: Put) -> bool:
    """Move every field of a source that is not its own into the store, one entry per field name. True when it moved any."""
    extras = source_extras(source)
    if not extras:
        return False
    held = source.get(SOURCE_EXTRA_ENTRIES)
    held = dict(held) if isinstance(held, Mapping) else {}
    for key, text in extras.items():
        held[key] = secret_refs.entry(put(text, None))
        del source[key]
    source[SOURCE_EXTRA_ENTRIES] = held
    return True


def externalize_package_index(index: Mapping[str, Any], put: Put) -> tuple[dict[str, Any], list[str]]:
    """``index`` with the credentials of every package source moved into the store (URL parts and every extra field). Idempotent."""
    out = copy.deepcopy(dict(index))
    moved: list[str] = []
    for source in package_sources(out):
        for key in PACKAGE_SOURCE_URL_KEYS:
            if key in source and externalize_url(source, key, put):
                moved.append(f"source.{key}")
        try:
            if externalize_source_extras(source, put):
                moved.append("source.extra")
        except ValueError:
            # a stored source with a field that cannot be kept holds nothing the gateway reads: it is cut down to its own fields
            for key in [k for k in source if k not in PACKAGE_SOURCE_PUBLIC_KEYS and not k.endswith(SECRETS_SUFFIX) and k != SOURCE_EXTRA_ENTRIES]:
                del source[key]
            moved.append("source.extra")
    return out, moved


# ---------------------------------------------------------------------------
# Writing a record that may hold inline input (decision D27)
# ---------------------------------------------------------------------------


class CarrierLocked(ValueError):
    """A write named a carrier that holds secret entries and did not send back exactly what a read shows."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"{path} holds secret entries, and a record write cannot change them. Send them back as they were read, "
            "or change them with the secret-entries commands.")
        self.path = path


def carrier_slots(record: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any], str, str]]:
    """(path, parent, key, policy name) for every carrier a record can hold, whether or not it holds anything."""
    parent = _container(record, "config")
    if parent is not None:
        yield HEADER_PATH, parent, "headers", "headers"
    for container in ENV_CONTAINERS:
        holder = _container(record, container)
        if holder is not None:
            yield f"{container}.env", holder, "env", "env"
    for container in COMMAND_CONTAINERS:
        holder = _container(record, container)
        if holder is not None:
            for key in COMMAND_KEYS:
                yield f"{container}.{key}", holder, key, "argv"
    for path in SCALAR_SECRET_PATHS:
        parent_path, _, key = path.rpartition(".")
        holder = _container(record, parent_path)
        if holder is not None:
            yield path, holder, key, "scalar"
    for path, holder, key in url_containers(record):
        yield path + SECRETS_SUFFIX, holder, key + SECRETS_SUFFIX, "url_entries"


def _carrier_view(policy_name: str, value: Any) -> Any:
    """What a read shows for a carrier value: the comparison a write must match to keep entries it cannot change."""
    from gateway import projection

    policy = {"headers": projection.HEADERS, "env": projection.ENV, "argv": projection.ARGV,
              "scalar": projection.SCALAR_SECRET, "url_entries": projection.URL_ENTRIES}[policy_name]
    return projection.project_policy(policy, value, configured=secret_refs.configured).tree


def ingest_service(
    raw: Mapping[str, Any], stored: Mapping[str, Any] | None, put: Put,
) -> tuple[dict[str, Any], list[str]]:
    """The record to store for a write: entries a write cannot change kept, inline input turned into new entries.

    A carrier of ``stored`` that holds secret entries is locked: ``raw`` must send back what a read shows for it, or
    leave it out, and the stored carrier is kept. Everything else is public data, except inline input (a header
    value, an environment value, a command argument after a credential flag, URL user info), which becomes a new
    secret entry. Returns the record and the paths whose inline input was moved. Raises ``CarrierLocked``.
    """
    record = copy.deepcopy(dict(raw))
    if stored is not None:
        for path, _parent, _key, policy_name in carrier_slots(stored):
            parent_path, _, key = path.rpartition(".")
            held = _container(stored, parent_path)
            value = held.get(key) if held is not None else None
            if not refs_in(value):
                continue
            target = _container(record, parent_path)
            if target is None:
                raise CarrierLocked(path)  # the block that holds the entries is gone: that is a change to them
            if key in target and target[key] != value and target[key] != _carrier_view(policy_name, value):
                raise CarrierLocked(path)
            target[key] = copy.deepcopy(value)
    from gateway import projection, projection_types

    # A read hides the settings no type classifies (decision D34); a write built from a read does not carry them, so
    # the stored ones are kept. The count a read shows is never stored.
    projection.retain_hidden(projection_types.SERVICE_RECORD, record, stored)
    refuse_echoed_views(record)
    return externalize_service(record, put, reject_ambiguous=True)


def _is_view(item: Any) -> bool:
    """What a read shows for a secret entry: a mapping that says whether it is configured and holds no reference."""
    return isinstance(item, Mapping) and "configured" in item and not secret_refs.is_ref(item.get("secret_ref"))


def _walk_views(node: Any, path: str) -> None:
    if _is_view(node):
        raise EntryRefused(path)
    if isinstance(node, Mapping):
        for key, item in node.items():
            _walk_views(item, f"{path}.{str(key)[:64]}")
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _walk_views(item, f"{path}[{index}]")


def _url_entries_malformed(held: Any) -> bool:
    """True when ``<field>_entries`` is not a shape the migration and ingestion write: every part is an entry that holds
    a reference, or (for a fragment or a query chunk) an empty or gap marker."""
    if not isinstance(held, Mapping):
        return True

    def entry(item: Any) -> bool:
        return secret_refs.is_secret_entry(item)

    def marker(item: Any, key: str) -> bool:
        return isinstance(item, Mapping) and item.get(key) is True

    for part, item in held.items():
        if part == "whole":
            if not entry(item):
                return True
        elif part in ("userinfo", "fragment"):
            if not (entry(item) or marker(item, "empty")):
                return True
        elif part in ("query", "params"):
            if not isinstance(item, list) or not all(entry(chunk) or marker(chunk, "empty") or marker(chunk, "gap") for chunk in item):
                return True
        else:
            return True
    return False


def refuse_echoed_views(record: Mapping[str, Any]) -> None:
    """Raise ``EntryRefused`` when a carrier of ``record`` holds a read's view of an entry or a malformed ``_entries`` part.

    A view (``{"id", "configured"}``) written where no stored entry has that id would otherwise become a secret whose
    value is the view's text, or a URL part that cannot be rebuilt when the engine is called.
    """
    for path, parent, key, policy_name in carrier_slots(record):
        if key not in parent:
            continue
        value = parent[key]
        if policy_name == "url_entries":
            if value in (None, {}, ""):
                continue
            if _url_entries_malformed(value):
                raise EntryRefused(path)
        else:
            _walk_views(value, path)


# ---------------------------------------------------------------------------
# A read written back (decision D27)
# ---------------------------------------------------------------------------


class EchoRefused(ValueError):
    """A write put, in a reference slot, something other than what a read shows for it or a reference id."""


def _echo_view(policy_name: str, held: Any) -> Any:
    from gateway import projection

    policy = {"configured": projection.CONFIGURED, "ref_map": projection.REF_MAP}[policy_name]
    return projection.project_policy(policy, held, configured=secret_refs.configured).tree


def restore_echoed_record(record: dict[str, Any], stored: Mapping[str, Any] | None) -> None:
    """Replace, in ``record``, a reference slot that came back as the read showed it with what the store holds.

    A read shows a provider's key reference (``config.api_key_secret_id``) and the record's ``secret_refs`` as
    ``{"configured": true}`` and as names: no id, nothing to retain by. A write that sends back exactly that view
    leaves the slot as stored; it is the same rule as for the carriers (``ingest_service``), and the view is compared
    with what a read of the stored record shows now, never taken as a command. Anything else that is not an id
    (another marker, a mapping that differs from the view, a reserved text) is refused with fixed text: a slot is
    changed by sending its new reference id, or its stored key by the secret-entries commands.
    """
    from gateway.secret_commands import RESERVED_TEXTS

    config = record.get("config") if isinstance(record.get("config"), dict) else None
    stored_config = stored.get("config") if isinstance(stored, Mapping) and isinstance(stored.get("config"), Mapping) else {}
    if config is not None and "api_key_secret_id" in config:
        value, held = config["api_key_secret_id"], stored_config.get("api_key_secret_id")
        if isinstance(value, str) and value.strip() in RESERVED_TEXTS:
            raise EchoRefused("config.api_key_secret_id holds a marker text, not a reference id")
        if isinstance(value, (Mapping, list)):
            if isinstance(held, str) and held and isinstance(value, Mapping) and dict(value) == _echo_view("configured", held):
                config["api_key_secret_id"] = held
            else:
                raise EchoRefused("config.api_key_secret_id is not a reference id and is not what a read shows for the stored one")
    refs = record.get("secret_refs")
    if isinstance(refs, Mapping) and any(isinstance(item, (Mapping, list)) for item in refs.values()):
        held_refs = stored.get("secret_refs") if isinstance(stored, Mapping) else None
        if isinstance(held_refs, Mapping) and held_refs and dict(refs) == _echo_view("ref_map", held_refs):
            record["secret_refs"] = copy.deepcopy(dict(held_refs))
        else:
            raise EchoRefused("secret_refs holds something that is not a reference and is not what a read shows for the stored ones")


def restore_echoed_registry(registry: Mapping[str, Any], stored: Mapping[str, Any] | None) -> dict[str, Any]:
    """``registry`` (a write body) with ``restore_echoed_record`` applied to each service; the input is not changed."""
    out = copy.deepcopy(dict(registry))
    services = out.get("services")
    held = stored.get("services") if isinstance(stored, Mapping) and isinstance(stored.get("services"), Mapping) else {}
    for service_id, record in (services.items() if isinstance(services, dict) else ()):
        if isinstance(record, dict):
            restore_echoed_record(record, held.get(service_id))
    return out
