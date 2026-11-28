"""A deep copy of parsed documents (YAML and JSON), about four times faster than ``copy.deepcopy``.

The reload path hands each caller its own copy of release documents (workflow manifests, registry records), so no
caller can change what the next one reads. ``copy.deepcopy`` dispatches on type and records every object in Python;
on these documents it was most of an owner write once parsing and checking were cached. ``marshal`` copies the core
types (dict, list, tuple, set, str, numbers, bool, None) in C, and keeps shared and cyclic containers shared and cyclic,
as ``copy.deepcopy`` does (a YAML anchor stays one object). It refuses any other type (a date, a dict subclass); such a
value goes to ``copy.deepcopy`` whole. The bytes never leave this process.

Use it only on documents a YAML or JSON parser made, or built from them. marshal writes any buffer (a ``bytearray``, a
``memoryview``) as ``bytes``, which ``copy.deepcopy`` would not; no parser makes one.
"""
from __future__ import annotations

import copy
import marshal
from typing import TypeVar

T = TypeVar("T")


def copy_document(value: T) -> T:
    """An independent copy of the parsed document ``value``, equal to ``copy.deepcopy(value)``."""
    try:
        return marshal.loads(marshal.dumps(value))
    except ValueError:   # a type marshal does not carry
        return copy.deepcopy(value)
