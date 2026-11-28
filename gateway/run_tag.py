"""The optional run tag: a short label a caller puts on a request so its requests can be grouped and totalled.

A tag travels as the ``X-Azizam-Tag`` header (the MCP ``run`` tool sets it from its ``tag`` argument). It is kept on the
request's receipt and on its kept request-history record, and ``requests`` can filter by it. A tag is a label chosen by
the caller, never a secret, so a bad one is refused with a message instead of being cleaned up.
"""
from __future__ import annotations

import re

TAG_HEADER = "x-azizam-tag"
TAG_MIN, TAG_MAX = 1, 64
TAG_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,64}")
TAG_RULE = (f"tag must be {TAG_MIN} to {TAG_MAX} characters long and use only letters, digits and the "
            "characters . _ : -")


class InvalidTag(ValueError):
    """The tag breaks ``TAG_RULE``; the message says how."""


def validate(value: object) -> str:
    """The tag, or ``InvalidTag``. A non-string, an empty string and a space all fail."""
    if not isinstance(value, str) or TAG_PATTERN.fullmatch(value) is None:
        raise InvalidTag(TAG_RULE)
    return value


def valid_or_none(value: object) -> str | None:
    """The tag when it is one, else ``None``: for a reader that must not trust a header it did not validate."""
    return value if isinstance(value, str) and TAG_PATTERN.fullmatch(value) else None
