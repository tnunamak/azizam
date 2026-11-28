"""Chat image parts that name a stored asset instead of carrying the picture.

A client that holds a large image uploads it once (``POST /v1/assets``), then sends the id where the picture would go::

    {"type": "image_url", "image_url": {"url": "img_<sha256>"}}

The gateway swaps the id for the data URI an engine already accepts, so the request body stays small and no engine changes.
This module only rewrites the messages. The caller supplies ``read``: it answers the stored bytes, or ``None`` when the
asset is unknown or the caller may not read it (the two are not told apart, so ids cannot be probed).

An ``image_url.url`` is an address (``https://``, ``data:``) unless it has no ``:`` and no ``/``. Such a value can only be an
asset id, so one that is not a well-formed id is refused here, never passed on to an engine as a broken address.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeGuard

from gateway import media_inputs

ASSET_ID = re.compile(r"img_[0-9a-f]{64}")

# One request may name at most this many stored images, and the stored bytes it swaps in may total at most this much.
# The count is per reference, not per distinct image: each reference becomes its own copy in the request an engine reads,
# so fifty references to one 20 MB picture would otherwise build a request of over a gigabyte from a small body.
MAX_REFERENCES = 16
MAX_STORED_BYTES = 64 * 1024 * 1024

Reader = Callable[[str], bytes | None]


@dataclass(frozen=True)
class RefError:
    """Why a reference was refused. ``param`` names the content part, in the OpenAI error style."""

    code: str
    message: str
    param: str


def _is_reference(url: Any) -> TypeGuard[str]:
    return isinstance(url, str) and ":" not in url and "/" not in url


def resolve(messages: Sequence[Any], read: Reader) -> tuple[list[Any], RefError | None]:
    """The messages with every asset reference replaced by a data URI, or the first refusal.

    ``messages`` is not changed; a message with no reference is returned as it came."""
    cache: dict[str, tuple[str, int]] = {}
    used = [0, 0]  # references so far, stored bytes so far
    out: list[Any] = []
    for m_index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            out.append(message)
            continue
        parts: list[Any] = []
        for p_index, part in enumerate(content):
            replaced = _resolve_part(part, f"messages[{m_index}].content[{p_index}]", read, cache, used)
            if isinstance(replaced, RefError):
                return list(messages), replaced
            parts.append(replaced)
        out.append({**message, "content": parts})
    return out, None


def _resolve_part(part: Any, path: str, read: Reader, cache: dict[str, tuple[str, int]], used: list[int]) -> Any:
    if not isinstance(part, Mapping) or part.get("type") != "image_url":
        return part
    image = part.get("image_url")
    url = image.get("url") if isinstance(image, Mapping) else image
    if not _is_reference(url):
        return part
    param = f"{path}.image_url" + (".url" if isinstance(image, Mapping) else "")
    used[0] += 1
    if used[0] > MAX_REFERENCES:
        return RefError("too_many_images", f"A request may name at most {MAX_REFERENCES} stored images.", param)
    if url not in cache:
        made = _data_uri(url, param, read)
        if isinstance(made, RefError):
            return made
        cache[url] = made
    uri, size = cache[url]
    used[1] += size
    if used[1] > MAX_STORED_BYTES:
        return RefError("request_too_large", f"The stored images in one request may total at most {MAX_STORED_BYTES // (1024 * 1024)} MiB.", param)
    return {**part, "image_url": {**image, "url": uri} if isinstance(image, Mapping) else uri}


def _data_uri(asset_id: str, param: str, read: Reader) -> tuple[str, int] | RefError:
    if not ASSET_ID.fullmatch(asset_id):
        return RefError("invalid_image_url", "image_url.url must be a URL, a data URI or the id of a stored image (img_ followed by 64 hex digits).", param)
    data = read(asset_id)
    if data is None:
        return RefError("asset_missing", "No stored image with this id is available to this API key.", param)
    media_type = media_inputs.measure(data).media_type
    if not media_type.startswith("image/"):
        return RefError("invalid_image_format", "The stored asset is not an image.", param)
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}", len(data)
