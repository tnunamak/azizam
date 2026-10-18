"""JSON pointers (RFC 6901) with one extension for selecting: a ``*`` segment walks every item of a list.

Pointers are parsed once, when a description loads, into tuples of keys. Reading
never raises: an absent place is ``MISSING``. Writing creates objects, never
lists: a numeric segment must name an item of a list that already exists, so a
constant skeleton in the description is what gives a body its array shape.
"""

from __future__ import annotations

from typing import Any

from gateway.engine_protocol.errors import BudgetExceeded, MappingError


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Any = _Missing()
Segments = tuple[str, ...]


def parse_pointer(text: str, *, wildcard: bool = False) -> Segments:
    """The keys of a pointer. Raises ``ValueError`` with the reason; empty segments are refused."""
    if not isinstance(text, str) or not text.startswith("/"):
        raise ValueError("a pointer starts with /")
    keys: list[str] = []
    for raw in text[1:].split("/"):
        if raw == "":
            raise ValueError("a pointer has no empty segment")
        if "~" in raw:
            index = raw.find("~")
            while index != -1:
                if raw[index + 1:index + 2] not in ("0", "1"):
                    raise ValueError("~ is written ~0 and / is written ~1")
                index = raw.find("~", index + 2)
            raw = raw.replace("~1", "/").replace("~0", "~")
        if raw == "*" and not wildcard:
            raise ValueError("* is only allowed in a selecting pointer")
        keys.append(raw)
    return tuple(keys)


def get(doc: Any, segments: Segments) -> Any:
    """The value at ``segments`` or ``MISSING``."""
    cur = doc
    for key in segments:
        kind = type(cur)
        if kind is dict:
            cur = cur.get(key, MISSING)
            if cur is MISSING:
                return MISSING
        elif kind is list and key.isascii() and key.isdigit():
            index = int(key)
            if index >= len(cur):
                return MISSING
            cur = cur[index]
        else:
            return MISSING
    return cur


def select_all(doc: Any, segments: Segments, *, limit: int, budget: str = "max_selected") -> list[Any]:
    """Every value a pointer with ``*`` segments reaches, in document order. More than ``limit`` is a budget failure."""
    found: list[Any] = []

    def walk(cur: Any, position: int) -> None:
        while position < len(segments):
            key = segments[position]
            if key == "*":
                if type(cur) is not list:
                    return
                for item in cur:
                    walk(item, position + 1)
                return
            cur = get(cur, (key,))
            if cur is MISSING:
                return
            position += 1
        if len(found) >= limit:
            raise BudgetExceeded(budget, f"A selecting pointer reached more than {limit} values.")
        found.append(cur)

    walk(doc, 0)
    return found


def set_value(doc: Any, segments: Segments, value: Any) -> None:
    """Write ``value`` at ``segments``, creating missing objects on the way. Numeric segments index existing lists."""
    cur = doc
    for position, key in enumerate(segments[:-1], 1):
        kind = type(cur)
        if kind is dict:
            nxt = cur.get(key, MISSING)
            if nxt is MISSING:
                following = segments[position]
                if following.isascii() and following.isdigit():
                    raise MappingError(
                        f"Cannot write at /{'/'.join(segments)}: /{following} is a list index, and /{key} is not a list that exists."
                    )
                nxt = cur[key] = {}
            cur = nxt
        elif kind is list and key.isascii() and key.isdigit() and int(key) < len(cur):
            cur = cur[int(key)]
        else:
            raise MappingError(f"Cannot write at /{'/'.join(segments)}: /{key} is not a place that can hold it.")
    last = segments[-1]
    kind = type(cur)
    if kind is dict:
        cur[last] = value
    elif kind is list and last.isascii() and last.isdigit() and int(last) < len(cur):
        cur[int(last)] = value
    else:
        raise MappingError(f"Cannot write at /{'/'.join(segments)}: the parent is not an object or a list with that item.")


def remove(doc: Any, segments: Segments) -> None:
    """Delete the place if it exists (an absent place is not an error)."""
    parent = get(doc, segments[:-1]) if len(segments) > 1 else doc
    last = segments[-1]
    if type(parent) is dict:
        parent.pop(last, None)
    elif type(parent) is list and last.isascii() and last.isdigit() and int(last) < len(parent):
        del parent[int(last)]


def clone(value: Any) -> Any:
    """A copy of a JSON value (containers copied, scalars shared)."""
    kind = type(value)
    if kind is dict:
        return {key: clone(item) for key, item in value.items()}
    if kind is list:
        return [clone(item) for item in value]
    return value


def depth_exceeds(value: Any, limit: int) -> bool:
    """True when ``value`` nests deeper than ``limit`` containers. Iterative, so a deep value cannot overflow the stack."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        kind = type(item)
        if kind is dict:
            children: Any = item.values()
        elif kind is list:
            children = item
        else:
            continue
        if depth > limit:
            return True
        for child in children:
            if type(child) in (dict, list):
                stack.append((child, depth + 1))
    return False
