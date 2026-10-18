"""Deny-default public projections (decision D28).

A management response is built from a registered *projection type*, never from the stored value or from a list
of fields in a route. Every field of a type is classified once, in ``gateway/projection_types.py``:

``Scalar`` / ``Json``   public data, checked for its exact type and size
``Configured``          a reference to the secret store: shows only whether it is set
``Headers`` ``Env`` ``Argv`` ``UrlText`` ``UrlEntries`` ``ScalarSecret``  the carriers of ``gateway/record_carriers.py``,
                        shown as entries with an id and a configured flag, never a value
``Omit``                accepted and never shown
anything not named      forbidden: it is dropped, and counted so a test can see it

The projector builds a fresh tree from the registered types only. It never calls ``model_dump``, ``__dict__``,
``repr`` or ``str`` on a stored value, accepts only the exact JSON types (a subclass is refused), and keeps to
a depth and size budget. A value that is not the shape its type declares is left out and reported
(``Projection.shape_errors``): the rest of the record is still shown, and a record that cannot be parsed into its
type is told apart from one that can (see ``projection_types.excluded_view``).

Text is also scanned for URL user info as a second guard. That scan is never what makes a response safe: no
secret value is in a stored record to begin with (decision D26).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from gateway import secret_refs

MAX_DEPTH = 12
MAX_NODES = 200_000
MAX_TEXT = 8192
MAX_KEY = 256


class ProjectionViolation(RuntimeError):
    """The projector refused to build a tree. The message never holds a value, a key or a path."""


OMITTED = object()

# --- policies ----------------------------------------------------------------------------------------------------


class Policy:
    """Base of every field classification."""


@dataclass(frozen=True)
class Scalar(Policy):
    types: tuple[type, ...]
    max_len: int = MAX_TEXT


@dataclass(frozen=True)
class Json(Policy):
    """Public JSON of any shape inside the size budget: for data that is public by contract (a model list)."""


@dataclass(frozen=True)
class Obj(Policy):
    name: str
    fields: Mapping[str, Policy]
    extras: Policy | None = None  # a policy for every key the type does not name; None drops them


@dataclass(frozen=True)
class Seq(Policy):
    item: Policy
    max_items: int = 5000


@dataclass(frozen=True)
class Dict(Policy):
    value: Policy
    max_items: int = 5000


@dataclass(frozen=True)
class Configured(Policy):
    """A reference to the secret store (an id): only whether something is set under it is shown."""


@dataclass(frozen=True)
class RefMap(Policy):
    """A mapping from a name to a reference or placeholder: each name is shown as configured."""


@dataclass(frozen=True)
class Headers(Policy):
    pass


@dataclass(frozen=True)
class Env(Policy):
    pass


@dataclass(frozen=True)
class Argv(Policy):
    pass


@dataclass(frozen=True)
class UrlText(Policy):
    """A URL, address or path stored in public form (no user info, no query)."""


@dataclass(frozen=True)
class UrlEntries(Policy):
    """The credentials of a URL field, stored beside it as entries."""


@dataclass(frozen=True)
class ScalarSecret(Policy):
    """One secret value held as a single entry (a driver's admin key)."""


@dataclass(frozen=True)
class InlineSecret(Policy):
    """A secret written inline in a file the operator keeps (a YAML config key): only whether it is set is shown."""


@dataclass(frozen=True)
class Omit(Policy):
    pass


STR = Scalar((str,))
SHORT = Scalar((str,), 256)
INT = Scalar((int,))
NUM = Scalar((int, float))
BOOL = Scalar((bool,))
ANY_SCALAR = Scalar((str, int, float, bool, type(None)))
OMIT = Omit()
INLINE_SECRET = InlineSecret()
JSON = Json()
CONFIGURED = Configured()
HEADERS = Headers()
ENV = Env()
ARGV = Argv()
URL = UrlText()
URL_ENTRIES = UrlEntries()
SCALAR_SECRET = ScalarSecret()
REF_MAP = RefMap()


def seq(item: Policy, max_items: int = 5000) -> Seq:
    return Seq(item, max_items)


def mapping(value: Policy, max_items: int = 5000) -> Dict:
    return Dict(value, max_items)


# --- the registry --------------------------------------------------------------------------------------------------

TYPES: dict[str, Obj] = {}


def register(obj: Obj) -> Obj:
    if obj.name in TYPES:
        raise ValueError(f"projection type {obj.name!r} is registered twice")
    TYPES[obj.name] = obj
    return obj


# --- the walk --------------------------------------------------------------------------------------------------------


@dataclass
class Projection:
    """The result: the public tree, what the type did not name, and what was not the shape it declared."""

    tree: Any
    dropped: list[str] = field(default_factory=list)
    shape_errors: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)  # paths of the secret slots that hold something


@dataclass
class _Walk:
    configured: Callable[[str], bool]
    nodes: int = 0
    dropped: list[str] = field(default_factory=list)
    shape_errors: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)

    def spend(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ProjectionViolation("a response is larger than the projection budget")


def public_url(text: str) -> str:
    """A URL, address or path in its public form: no user info, no query, no fragment.

    A record stores a URL field this way already (decision D29); a value that still has them (a hand edit) is cut
    down here by structure, not by what it looks like. Text that is not a URL passes through ``scrub_userinfo``.
    """
    text = text[:MAX_TEXT]
    if "://" in text:
        scheme, _, rest = text.partition("://")
        rest = rest.split("#", 1)[0].split("?", 1)[0]
        authority, slash, path = rest.partition("/")
        authority = authority.rpartition("@")[2]
        return f"{scheme}://{authority}{slash}{path}"
    if "@" in text.split("/", 1)[0] and " " not in text.split("/", 1)[0] and not text.startswith("/"):
        text = text.rpartition("@")[2]
    if text.startswith("/") or "?" in text:
        text = text.split("#", 1)[0].split("?", 1)[0]
    return scrub_userinfo(text)


_USERINFO = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@'\"<>]+@")


def scrub_userinfo(text: str) -> str:
    """A second guard: user info in a URL inside text is removed. It is never what makes a response safe."""
    return _USERINFO.sub(r"\1", text) if "://" in text else text


def _exact(value: Any, types: tuple[type, ...]) -> bool:
    return type(value) in types and not (type(value) is float and not math.isfinite(value))


def _path(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else f"{key}"


def _shape(walk: _Walk, path: str) -> Any:
    walk.shape_errors.append(path)
    return OMITTED


def _configured_entry(walk: _Walk, item: Mapping[str, Any], ref: str, path: str, **public: Any) -> dict[str, Any]:
    walk.entries.append(path)
    out: dict[str, Any] = {}
    if isinstance(item.get("id"), str):
        out["id"] = item["id"]
    out.update({key: value for key, value in public.items() if value is not None})
    out["configured"] = bool(walk.configured(ref))
    return out


def _headers(walk: _Walk, value: Any, path: str) -> Any:
    if isinstance(value, Mapping):
        # A header map written inline in a file the operator keeps: names are shown, every value is hidden.
        out_map: dict[str, Any] = {}
        for name, item in value.items():
            walk.spend()
            if isinstance(name, str) and len(name) <= MAX_KEY:
                walk.entries.append(_path(path, name))
                out_map[name] = {"configured": item not in (None, "")}
        return out_map
    if not isinstance(value, list):
        return _shape(walk, path)
    out: list[Any] = []
    for index, row in enumerate(value):
        here = f"{path}[{index}]"
        walk.spend()
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            walk.shape_errors.append(here)
            continue
        name = row["name"][:MAX_KEY]
        if secret_refs.is_secret_entry(row):
            out.append(_configured_entry(walk, row, row["secret_ref"], here, name=name))
        elif row.get("public") is True and _exact(row.get("value"), (str,)):
            out.append({**({"id": row["id"]} if isinstance(row.get("id"), str) else {}), "name": name,
                        "value": scrub_userinfo(row["value"][:MAX_TEXT]), "public": True})
        else:
            # a row written before decision D26 that no migration has moved: its value is never shown
            walk.entries.append(here)
            out.append({"name": name, "configured": False, "legacy": True})
    return out


def _env(walk: _Walk, value: Any, path: str) -> Any:
    if not isinstance(value, Mapping):
        return _shape(walk, path)
    out: dict[str, Any] = {}
    for name, item in value.items():
        here = _path(path, name)
        walk.spend()
        if not isinstance(name, str) or len(name) > MAX_KEY:
            walk.shape_errors.append(here)
            continue
        if secret_refs.is_secret_entry(item):
            out[name] = _configured_entry(walk, item, item["secret_ref"], here)
        elif isinstance(item, Mapping) and item.get("public") is True and _exact(item.get("value"), (str,)):
            out[name] = {**({"id": item["id"]} if isinstance(item.get("id"), str) else {}),
                         "value": scrub_userinfo(item["value"][:MAX_TEXT]), "public": True}
        else:
            walk.entries.append(here)
            out[name] = {"configured": False, "legacy": True}
    return out


def _argv(walk: _Walk, value: Any, path: str) -> Any:
    if isinstance(value, str):
        return scrub_userinfo(value[:MAX_TEXT])
    if not isinstance(value, list):
        return _shape(walk, path)
    out: list[Any] = []
    for index, item in enumerate(value):
        here = f"{path}[{index}]"
        walk.spend()
        if secret_refs.is_secret_entry(item):
            prefix = item.get("prefix")
            out.append(_configured_entry(walk, item, item["secret_ref"], here,
                                         prefix=prefix[:MAX_TEXT] if isinstance(prefix, str) and prefix else None))
        elif _exact(item, (str,)):
            out.append(scrub_userinfo(item[:MAX_TEXT]))
        else:
            walk.shape_errors.append(here)
    return out


def _url_entries(walk: _Walk, value: Any, path: str) -> Any:
    if not isinstance(value, Mapping):
        return _shape(walk, path)
    out: dict[str, Any] = {}
    for key in ("userinfo", "fragment", "whole"):
        item = value.get(key)
        if secret_refs.is_secret_entry(item):
            out[key] = _configured_entry(walk, item, item["secret_ref"], _path(path, key))
    query = value.get("query")
    if isinstance(query, list):
        out["query"] = [
            _configured_entry(walk, item, item["secret_ref"], f"{path}.query[{index}]",
                              name=item["name"][:MAX_KEY] if isinstance(item.get("name"), str) else None)
            for index, item in enumerate(query) if secret_refs.is_secret_entry(item)]
    return out


def _json(walk: _Walk, value: Any, path: str, depth: int) -> Any:
    walk.spend()
    if depth > MAX_DEPTH:
        return _shape(walk, path)
    if value is None or type(value) is bool:
        return value
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    if type(value) is str:
        return scrub_userinfo(value[:MAX_TEXT])
    if type(value) in (list, tuple):
        out = []
        for index, item in enumerate(value):
            projected = _json(walk, item, f"{path}[{index}]", depth + 1)
            if projected is not OMITTED:
                out.append(projected)
        return out
    if isinstance(value, Mapping):
        out_map: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or len(key) > MAX_KEY:
                walk.shape_errors.append(path)
                continue
            projected = _json(walk, item, _path(path, key), depth + 1)
            if projected is not OMITTED:
                out_map[key] = projected
        return out_map
    return _shape(walk, path)


def _project(policy: Policy, value: Any, walk: _Walk, path: str, depth: int) -> Any:
    walk.spend()
    if depth > MAX_DEPTH:
        return _shape(walk, path)
    if isinstance(policy, Omit):
        return OMITTED
    if value is None and not isinstance(policy, Scalar):
        return None
    if isinstance(policy, Scalar):
        if not _exact(value, policy.types):
            return _shape(walk, path)
        return scrub_userinfo(value[: policy.max_len]) if type(value) is str else value
    if isinstance(policy, Json):
        return _json(walk, value, path, depth)
    if isinstance(policy, Configured):
        if type(value) is not str:
            return _shape(walk, path)
        if value:
            walk.entries.append(path)
        return {"configured": bool(value) and bool(walk.configured(value))}
    if isinstance(policy, RefMap):
        if not isinstance(value, Mapping):
            return _shape(walk, path)
        names = [name for name in value if type(name) is str and len(name) <= MAX_KEY]
        for name in names:
            walk.entries.append(_path(path, name))
        return {name: {"configured": True} for name in names}
    if isinstance(policy, Headers):
        return _headers(walk, value, path)
    if isinstance(policy, Env):
        return _env(walk, value, path)
    if isinstance(policy, Argv):
        return _argv(walk, value, path)
    if isinstance(policy, UrlText):
        if type(value) is not str:
            return _shape(walk, path)
        return public_url(value)
    if isinstance(policy, UrlEntries):
        return _url_entries(walk, value, path)
    if isinstance(policy, InlineSecret):
        if value in (None, ""):
            return value
        walk.entries.append(path)
        return {"configured": True}
    if isinstance(policy, ScalarSecret):
        if secret_refs.is_secret_entry(value):
            return _configured_entry(walk, value, value["secret_ref"], path)
        walk.entries.append(path)
        return {"configured": False, "legacy": True} if value not in (None, "") else OMITTED
    if isinstance(policy, Obj):
        if not isinstance(value, Mapping):
            return _shape(walk, path)
        out: dict[str, Any] = {}
        for key, item in value.items():
            here = _path(path, key)
            if type(key) is not str or len(key) > MAX_KEY:
                walk.shape_errors.append(path)
                continue
            field_policy = policy.fields.get(key)
            if field_policy is None:
                if policy.extras is None:
                    walk.dropped.append(here)
                    continue
                field_policy = policy.extras
            projected = _project(field_policy, item, walk, here, depth + 1)
            if projected is not OMITTED:
                out[key] = projected
        return out
    if isinstance(policy, Seq):
        if not isinstance(value, (list, tuple)) or len(value) > policy.max_items:
            return _shape(walk, path)
        items = []
        for index, item in enumerate(value):
            projected = _project(policy.item, item, walk, f"{path}[{index}]", depth + 1)
            if projected is not OMITTED:
                items.append(projected)
        return items
    if isinstance(policy, Dict):
        if not isinstance(value, Mapping) or len(value) > policy.max_items:
            return _shape(walk, path)
        out_dict: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or len(key) > MAX_KEY:
                walk.shape_errors.append(path)
                continue
            projected = _project(policy.value, item, walk, _path(path, key), depth + 1)
            if projected is not OMITTED:
                out_dict[key] = projected
        return out_dict
    raise ProjectionViolation("a field has a classification the projector does not know")


def url_secret_parts(address: str) -> list[str]:
    """The parts of an address a caller typed that must not come back in a report: user name, password, query values."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(address)
    except ValueError:
        return []
    found = {item for item in (parts.username, parts.password, parts.fragment) if item}
    found.update(pair.partition("=")[2] for pair in parts.query.split("&") if pair.partition("=")[2])
    return sorted((item for item in found if len(item) >= 4), key=len, reverse=True)


def scrub_value(value: Any, secrets: Any = ()) -> Any:
    """``value`` with each text in ``secrets`` replaced, in every string at any depth (a check that ran against an
    address a caller typed reports that address in its own words)."""
    texts = [secret for secret in secrets if secret]
    if not texts:
        return value
    if isinstance(value, str):
        return scrub_text(value, texts)
    if isinstance(value, Mapping):
        return {key: scrub_value(item, texts) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(item, texts) for item in value]
    return value


def public_process(process: Mapping[str, Any], *, projected: bool = False) -> dict[str, Any]:
    """The process fields the v2 record view may show: an allow-list.

    A stored process block is projected like the rest of a record. A block taken from a record that was already
    projected (``projected=True``) is only narrowed: projecting a view again would read its entries as plain values.
    """
    from gateway import projection_types

    tree = process if projected else (project_policy(projection_types.PROCESS, process).tree or {})
    return {key: tree[key] for key in projection_types.PUBLIC_PROCESS_FIELDS if key in tree}


def public_json(value: Any) -> Any:
    """Public JSON inside the size budget, for data that is public by contract (a model alias table)."""
    return project_policy(JSON, value).tree


def scrub_text(text: str, secrets: Any = ()) -> str:
    """An error message without the secret texts a record still holds inline, and without URL user info."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return scrub_userinfo(text)


def project(type_name: str, value: Any, *, configured: Callable[[str], bool] | None = None) -> Projection:
    """``value`` projected through the registered type. Raises ``ProjectionViolation`` for an unregistered type."""
    from gateway import projection_types  # noqa: F401 - importing it registers the types

    policy = TYPES.get(type_name)
    if policy is None:
        raise ProjectionViolation("an unregistered projection type was asked for")
    walk = _Walk(configured=configured or secret_refs.configured)
    tree = _project(policy, value, walk, "", 0)
    return Projection(None if tree is OMITTED else tree, walk.dropped, walk.shape_errors, walk.entries)


def project_policy(policy: Policy, value: Any, *, configured: Callable[[str], bool] | None = None) -> Projection:
    walk = _Walk(configured=configured or secret_refs.configured)
    tree = _project(policy, value, walk, "", 0)
    return Projection(None if tree is OMITTED else tree, walk.dropped, walk.shape_errors, walk.entries)
