"""Image output-shape helpers: ``output_format``, ``output_compression``, ``quality``.

OpenAI's gpt-image-1 image routes (`/v1/images/generations`, `/v1/images/edits`)
accept ``output_format`` (png/jpeg/webp), ``output_compression`` (0-100,
jpeg/webp only), and ``quality`` (low/medium/high/auto) at the request top
level. Before this module, all three were *accepted-but-inert*: recognized by
``STANDARD_IMAGE_GENERATE_FIELDS``/``STANDARD_IMAGE_EDIT_FIELDS`` so no
"unknown parameter" error was raised, but never read anywhere downstream — a
silent no-op the module's own contract (see ``gateway/image_extensions.py``
docstring) exists to prevent.

This module is pure (no I/O, no FastAPI types) so it is cheap to unit test.
``proxy.py`` wires the parsed values into Pillow re-encoding and the response
echo fields.
"""

from __future__ import annotations

import io
from typing import Any, Mapping

VALID_OUTPUT_FORMATS: tuple[str, ...] = ("png", "jpeg", "webp")
VALID_QUALITY: tuple[str, ...] = ("low", "medium", "high", "auto")

# dall-e-2/dall-e-3 quality values. These predate gpt-image-1's low/medium/
# high/auto enum; a client that hasn't migrated its `quality` value (or one
# still targeting a dall-e-* alias) must keep working, not regress to a 400.
_LEGACY_QUALITY_ALIASES: dict[str, str] = {"standard": "medium", "hd": "high"}

_OUTPUT_FORMAT_ERROR = "output_format must be one of 'png', 'jpeg', or 'webp'"
_OUTPUT_COMPRESSION_ERROR = "output_compression must be an integer between 0 and 100"
_QUALITY_ERROR = "quality must be one of 'low', 'medium', 'high', or 'auto'"

_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def parse_output_format(value: Any) -> tuple[str, str | None]:
    """Parse OpenAI's ``output_format`` option. Returns (format, error).

    None/empty string means OpenAI's default: png.
    """
    if value is None or value == "":
        return "png", None
    if not isinstance(value, str):
        return "png", _OUTPUT_FORMAT_ERROR
    normalized = value.strip().lower()
    if normalized == "jpg":
        normalized = "jpeg"
    if normalized in VALID_OUTPUT_FORMATS:
        return normalized, None
    return "png", _OUTPUT_FORMAT_ERROR


def parse_output_compression(value: Any, output_format: str) -> tuple[int | None, str | None]:
    """Parse OpenAI's ``output_compression`` option. Returns (compression, error).

    OpenAI documents this as applying only to jpeg/webp output; a value
    supplied alongside ``output_format=png`` is accepted (not rejected — png
    is inherently lossless) but has no effect, which callers should record on
    the receipt rather than silently drop.
    """
    if value is None or value == "":
        return (None if output_format == "png" else 100), None
    try:
        # Reject bool explicitly: `isinstance(True, int)` is True in Python.
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, float):
            # A JSON body delivers `output_compression` as a Python float
            # (e.g. 100.9, -0.9). int(100.9) == 100 with no error -- silently
            # truncating a fractional value is not the same as validating an
            # integer. Multipart string values ("100.9") already raise via
            # int() below without this check.
            if not value.is_integer():
                raise ValueError
        compression = int(value)
    except (TypeError, ValueError):
        return None, _OUTPUT_COMPRESSION_ERROR
    if compression < 0 or compression > 100:
        return None, _OUTPUT_COMPRESSION_ERROR
    if output_format == "png":
        return None, None
    return compression, None


def parse_quality(value: Any) -> tuple[str, str | None]:
    """Parse OpenAI's ``quality`` option. Returns (quality, error).

    Accepts both the gpt-image-1 enum (low/medium/high/auto) and the older
    dall-e-2/dall-e-3 values (standard/hd), mapped onto it
    (standard->medium, hd->high) so a client/alias that hasn't migrated
    keeps working.
    """
    if value is None or value == "":
        return "auto", None
    if not isinstance(value, str):
        return "auto", _QUALITY_ERROR
    normalized = value.strip().lower()
    normalized = _LEGACY_QUALITY_ALIASES.get(normalized, normalized)
    if normalized in VALID_QUALITY:
        return normalized, None
    return "auto", _QUALITY_ERROR


def background_format_conflict(background: str, output_format: str) -> str | None:
    """OpenAI rejects a transparent background combined with jpeg output
    (jpeg has no alpha channel). Returns an error message, or None."""
    if background == "transparent" and output_format == "jpeg":
        return (
            "background: 'transparent' requires output_format 'png' or "
            "'webp'; 'jpeg' has no alpha channel."
        )
    return None


def quality_steps_from_manifest(
    manifest: Mapping[str, Any] | None,
    quality: str,
) -> int | None:
    """Look up a manifest-declared ``quality`` -> sampler-steps mapping.

    Manifests may optionally declare a top-level ``quality_steps`` mapping,
    e.g. ``{low: 4, medium: 8, high: 16}``. Returns None (record-only, no
    lie) when the manifest doesn't declare this mapping or ``quality`` is
    ``"auto"`` — callers must not fabricate a step count a manifest never
    asked for.
    """
    if quality == "auto" or not isinstance(manifest, Mapping):
        return None
    table = manifest.get("quality_steps")
    if not isinstance(table, Mapping):
        return None
    steps = table.get(quality)
    if isinstance(steps, bool) or not isinstance(steps, int):
        return None
    return steps


def encode_image_bytes(
    raw: bytes,
    output_format: str,
    compression: int | None,
    *,
    exif: bytes | None = None,
) -> tuple[bytes, str]:
    """Re-encode PNG bytes (ComfyUI's native ``SaveImage`` output) to
    ``output_format``. Returns ``(encoded_bytes, mime_type)``.

    ``png`` passes through unchanged — lossless, so there is nothing to gain
    from a decode/encode round-trip, and it avoids any incidental pixel
    drift for the common case (png is also the default).

    ``exif`` (jpeg/webp only) is written into the encoded file so a stored
    copy can keep the ComfyUI prompt/workflow and gateway provenance.
    """
    if output_format not in VALID_OUTPUT_FORMATS:
        raise ValueError(f"unsupported output_format: {output_format}")
    if output_format == "png":
        return raw, _MEDIA_TYPES["png"]

    from PIL import Image

    image = Image.open(io.BytesIO(raw))
    buf = io.BytesIO()
    quality = compression if compression is not None else 100
    if output_format == "jpeg":
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGB")
        image.save(buf, format="JPEG", quality=quality, **_exif_kwargs(exif))
    else:  # webp
        image.save(
            buf, format="WEBP", quality=quality, lossless=quality >= 100, **_exif_kwargs(exif)
        )
    return buf.getvalue(), _MEDIA_TYPES[output_format]


def _exif_kwargs(exif: bytes | None) -> dict[str, bytes]:
    return {"exif": exif} if exif else {}
