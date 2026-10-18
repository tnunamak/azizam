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

This module is management-safe: it builds and walks structure and never decrypts. A single secret value (``lifecycle.admin_key``) is one entry. The legacy shapes (an inline
header value, a plain environment value, a command argument that follows a credential flag, a URL with user info)
are read here once, by the migration and by ingestion, and turned into entries; nothing else reads them.
"""

from __future__ import annotations

import copy
import re
import shlex
from typing import Any, Iterator, Mapping
from urllib.parse import quote, unquote, urlsplit

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
URL_FIELD_PATHS = (
    "config.host", "config.url", "config.address", "config.base_url", "config.api_base", "config.default_base_url",
    "config.health_endpoint", "config.key_url", "config.docs_url", "config.gateway_lifecycle_base_url",
    "config.context_window_probe.endpoint",
    "process.health_endpoint", "process.legacy_lifecycle.driver_config.health_endpoint",
    "lifecycle.driver_config.health_endpoint", "config.lifecycle.driver_config.health_endpoint",
    "config.process.health_endpoint",
)
# A single secret value in a mapping (the admin key of an http load/unload driver).
SCALAR_SECRET_PATHS = (
    "lifecycle.admin_key", "process.legacy_lifecycle.admin_key", "config.lifecycle.admin_key",
)
HEADER_PATH = "config.headers"
HEADER_ROW_KEYS = ("id", "name", "secret_ref", "value", "public")
SECRETS_SUFFIX = "_entries"


class AmbiguousUrl(ValueError):
    """A URL whose parts cannot be told apart with confidence."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _get(record: Mapping[str, Any], path: str) -> Any:
    node: Any = record
    for part in path.split("."):
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
    """(path of the field, the mapping that holds it, its key) for every URL field a record can hold."""
    for path in URL_FIELD_PATHS:
        parent_path, _, key = path.rpartition(".")
        parent = _container(record, parent_path)
        if parent is not None and key in parent:
            yield path, parent, key
    for base, http in observation_http_blocks(record):
        for key in ("path", "url"):
            if key in http:
                yield f"{base}.{key}", http, key


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def split_url(value: str) -> tuple[str, dict[str, Any]]:
    """``(public form, credential parts)`` of a URL, an address with user info, or a path with a query.

    The public form has no user info, no query and no fragment. The credential parts are
    ``{"userinfo": "user:password", "query": [(name, value), ...], "fragment": text}``, each present only when
    the URL had it. Raises ``AmbiguousUrl`` for text that cannot be read with confidence (an invalid port, a
    bracket that does not close, a control character).
    """
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise AmbiguousUrl("a URL holds a control character")
    parts: dict[str, Any] = {}
    if "://" in value:
        scheme, _, rest = value.partition("://")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", scheme):
            raise AmbiguousUrl("a URL has an invalid scheme")
        prefix = f"{scheme}://"
    else:
        rest, prefix = value, ""
    rest, _, fragment = rest.partition("#")
    rest, question, query = rest.partition("?")
    authority, slash, path = rest.partition("/")
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
    if question and query:
        pairs: list[tuple[str, str]] = []
        for chunk in query.split("&"):
            if not chunk:
                continue
            name, equals, item = chunk.partition("=")
            pairs.append((unquote(name), unquote(item) if equals else ""))
        if pairs:
            parts["query"] = pairs
    if fragment:
        parts["fragment"] = fragment
    return public, parts


def _looks_like_authority(text: str) -> bool:
    """A bare ``user:password@host`` (a ``host`` field): the part before the first slash has user info in it."""
    return "@" in text and " " not in text


def join_url(public: str, *, userinfo: str | None = None, query: list[tuple[str, str]] | None = None,
             fragment: str | None = None) -> str:
    """The URL that ``split_url`` took apart: the public form with its credential parts put back."""
    out = public
    if userinfo:
        if "://" in out:
            scheme, _, rest = out.partition("://")
            out = f"{scheme}://{userinfo}@{rest}"
        elif not out.startswith("/"):
            out = f"{userinfo}@{out}"
    if query:
        out += ("&" if "?" in out else "?") + "&".join(
            f"{quote(name, safe='')}={quote(item, safe='')}" if item != "" else quote(name, safe="") for name, item in query)
    if fragment:
        out += f"#{fragment}"
    return out


def url_entries(public: str, parts: Mapping[str, Any], put: Put) -> dict[str, Any]:
    """The entries that hold the credential ``parts`` of a URL whose public form is ``public`` (bound to its origin)."""
    origin = secret_refs.origin_or_none(public)
    held: dict[str, Any] = {}
    if "userinfo" in parts:
        held["userinfo"] = secret_refs.entry(put(parts["userinfo"], origin))
    if "query" in parts:
        held["query"] = [secret_refs.entry(put(item, origin), name=name) for name, item in parts["query"]]
    if "fragment" in parts:
        held["fragment"] = secret_refs.entry(put(parts["fragment"], origin))
    return held


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


def externalize_url(container: dict[str, Any], key: str, put: Put) -> bool:
    """Move the credentials out of ``container[key]`` into the store. True when it changed anything. Idempotent."""
    value = container.get(key)
    if not isinstance(value, str) or not value:
        return False
    try:
        public, parts = split_url(value)
    except AmbiguousUrl:
        container[key] = ""
        container[key + SECRETS_SUFFIX] = {"whole": secret_refs.entry(put(value, None))}
        return True
    if not parts:
        return False
    held = url_entries(public, parts, put)
    existing = container.get(key + SECRETS_SUFFIX)
    container[key] = public
    container[key + SECRETS_SUFFIX] = {**(existing if isinstance(existing, Mapping) else {}), **held}
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _legacy_command_items(command: Any, put: Put) -> list[Any] | None:
    """A legacy command (an argument list or one line) as items, or None when it holds no secret to move."""
    from gateway.legacy_credential_rules import argv_secret_slots

    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None
    elif isinstance(command, list):
        tokens = list(command)
    else:
        return None
    if not all(isinstance(token, str) for token in tokens):
        return None
    slots = argv_secret_slots(tokens)
    if not slots:
        return None
    items: list[Any] = []
    for index, token in enumerate(tokens):
        visible = slots.get(index)
        if visible is None:
            items.append(token)
        else:
            entry = secret_refs.entry(put(token[visible:], None), **({"prefix": token[:visible]} if visible else {}))
            items.append(entry)
    return items


# ---------------------------------------------------------------------------
# One record
# ---------------------------------------------------------------------------


def externalize_service(record: Mapping[str, Any], put: Put) -> tuple[dict[str, Any], list[str]]:
    """``record`` with every inline secret moved into the store, and the paths that were moved. Idempotent.

    Headers: a legacy row with a value becomes a secret row. Environment: a legacy plain value becomes a secret
    value. Commands: an argument that follows a credential flag (or sits after ``=`` in one) becomes an entry, and
    a command line that holds one becomes an argument list. URLs: user info and query values move beside the
    field. A record with none of these comes back equal to the one that went in.
    """
    out = copy.deepcopy(dict(record))
    moved: list[str] = []
    origin = endpoint_origin(out.get("config"))

    headers = _get(out, HEADER_PATH)
    if isinstance(headers, list):
        rows: list[Any] = []
        for row in headers:
            if isinstance(row, Mapping):
                # A row has the fields of its type and no others: an extra field inside a carrier is a place for a
                # value that no view or migration looks at, so it is not kept.
                row = {key: row[key] for key in HEADER_ROW_KEYS if key in row}
            if (isinstance(row, Mapping) and "value" in row and not row.get("public") and isinstance(row.get("value"), str)
                    and not secret_refs.is_secret_entry(row)):
                if row["value"]:
                    rows.append(secret_refs.entry(put(row["value"], origin), name=row.get("name")))
                    moved.append(HEADER_PATH)
                else:
                    rows.append(dict(row))  # an empty value holds nothing to move
            else:
                rows.append(row)
        out["config"]["headers"] = rows
    elif isinstance(headers, dict):
        # A name-to-value mapping is not the shape the gateway sends from, but it is still a place a value can be
        # written: every value moves to the store and the mapping keeps one entry per name, so nothing stays inline.
        for name, value in list(headers.items()):
            if isinstance(value, str) and value and not secret_refs.is_secret_entry(value):
                headers[name] = secret_refs.entry(put(value, origin))
                moved.append(HEADER_PATH)

    for path in ENV_CONTAINERS:
        container = _container(out, path)
        env = container.get("env") if container is not None else None
        if isinstance(env, dict):
            for name, value in list(env.items()):
                if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value) != "":
                    env[name] = secret_refs.entry(put(str(value), None))
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
        if isinstance(value, str) and value:
            parent[key] = secret_refs.entry(put(value, None))
            moved.append(path)

    for path, parent, key in list(url_containers(out)):
        if externalize_url(parent, key, put):
            moved.append(path)
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


def externalize_package_index(index: Mapping[str, Any], put: Put) -> tuple[dict[str, Any], list[str]]:
    """``index`` with the credentials of every git source URL moved into the store. Idempotent."""
    out = copy.deepcopy(dict(index))
    moved: list[str] = []
    for source in package_sources(out):
        for key in PACKAGE_SOURCE_URL_KEYS:
            if key in source and externalize_url(source, key, put):
                moved.append(f"source.{key}")
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
    return externalize_service(record, put)
