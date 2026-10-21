"""Per-request media delivery context and metadata policy.

Generated assets are stored without embedded prompt or workflow metadata.
The bytes sent to a caller use the metadata choice, in this order:

1. ``extensions["com.azizam.workflow"].metadata`` on the request;
2. the caller's app profile (``media_metadata.default``);
3. ``none`` (safe default when neither is known).

This module holds the request-scoped context (a ``ContextVar``, so concurrent
requests and background tasks stay separate) and the two byte operations:
:func:`attach_provenance` (store side) and :func:`deliver_bytes` (delivery
side). It never imports ``proxy``.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from fastapi import Request
from fastapi.responses import Response

from gateway import media_metadata, provenance

# Media scratch uses anonymous memfd buffers; no prompt-bearing cache files.
DEFAULT_WORKDIR = None


@dataclass
class DeliveryContext:
    """Facts about the current request that media delivery needs."""

    plan_bound: str = provenance.METADATA_NONE
    requested_mode: str | None = None
    receipt: Any = None
    # Provenance inputs gathered while the request runs.
    parameters: dict[str, Any] = field(default_factory=dict)
    seeds: list[int | None] = field(default_factory=list)
    model_alias: str | None = None
    app_id: str | None = None
    person_id: str | None = None
    prompt_decision: Any = None
    prompt_text: str | None = None
    capture_embedded_prompt: bool = True

    @property
    def mode(self) -> str:
        if (self.prompt_decision is not None and not self.prompt_decision.keep
                and self.prompt_decision.reason != "no_person"):
            return provenance.METADATA_NONE
        return provenance.resolve_metadata_mode(self.requested_mode, self.plan_bound)


_CONTEXT: contextvars.ContextVar[DeliveryContext | None] = contextvars.ContextVar(
    "azizam_media_delivery", default=None
)


def begin(*, plan_bound: str | None, receipt: Any = None) -> DeliveryContext:
    """Start a fresh context for the current request/task and return it."""
    ctx = DeliveryContext(
        plan_bound=provenance.resolve_metadata_mode(None, plan_bound),
        receipt=receipt,
    )
    _CONTEXT.set(ctx)
    return ctx


def current() -> DeliveryContext | None:
    return _CONTEXT.get()


def current_mode() -> str:
    ctx = _CONTEXT.get()
    return ctx.mode if ctx is not None else provenance.METADATA_NONE


def note_body(body: Mapping[str, Any] | None) -> str | None:
    """Record the request's ``metadata`` control. Raises ``MetadataModeError``."""
    requested = provenance.requested_metadata_mode(body)
    ctx = _CONTEXT.get()
    if ctx is not None and requested is not None:
        ctx.requested_mode = requested
    return requested


def note_query(items: Iterable[tuple[str, Any]]) -> str | None:
    """Record the control from GET query params.

    Accepts the extension form ``extensions[com.azizam.workflow][metadata]``
    (the same bracket syntax multipart forms use) and the short ``metadata``.
    """
    from gateway.video_inputs import parse_bracket_form

    pairs = list(items)
    try:
        nested = parse_bracket_form(pairs)
    except ValueError as exc:
        raise provenance.MetadataModeError(str(exc)) from exc
    requested = provenance.requested_metadata_mode(nested)
    if requested is None:
        for key, value in pairs:
            if key == provenance.METADATA_CONTROL:
                requested = provenance.parse_metadata_mode(value)
                break
    ctx = _CONTEXT.get()
    if ctx is not None and requested is not None:
        ctx.requested_mode = requested
    return requested


def note_parameters(values: Mapping[str, Any] | None) -> None:
    ctx = _CONTEXT.get()
    if ctx is not None and values:
        ctx.parameters.update(dict(values))


# ---------------------------------------------------------------------------
# Byte operations
# ---------------------------------------------------------------------------


def attach_provenance(
    content: bytes,
    suffix: str,
    record: Mapping[str, Any] | None,
    *,
    workdir: Path | None = None,
) -> bytes:
    """Embed provenance in the in-memory output before delivery and cache strip.

    PNG gets a ``azizam_provenance`` text chunk; video and audio get a
    container tag. JPEG and WebP are handled at encode time (EXIF), so they
    pass through unchanged here. Failure to embed never fails the request:
    the original bytes come back and the cache still strips metadata.
    """
    if not record:
        return content
    kind = media_metadata.media_kind(suffix)
    if kind not in {"png", "video", "audio"}:
        return content
    payload = {provenance.PROVENANCE_KEY: provenance.provenance_json(record)}
    try:
        if kind == "png":
            return media_metadata.embed_png_text(content, payload)
        return media_metadata.embed_media_tags(
            content, suffix, payload, workdir=workdir or DEFAULT_WORKDIR
        )
    except media_metadata.MediaMetadataError as exc:
        print(f"[proxy] provenance embed skipped ({kind}): {exc}", flush=True)
        return content


def image_exif_entries(
    png_bytes: bytes, record: Mapping[str, Any] | None
) -> dict[str, str]:
    """Entries to carry from a ComfyUI PNG into JPEG/WebP EXIF."""
    entries: dict[str, str] = {}
    try:
        chunks = media_metadata.png_text_chunks(png_bytes)
    except media_metadata.MediaMetadataError:
        chunks = {}
    for key in ("prompt", "workflow"):
        if key in chunks:
            entries[key] = chunks[key]
    if record:
        entries[provenance.PROVENANCE_KEY] = provenance.provenance_json(record)
    return entries


def deliver_bytes(
    content: bytes,
    suffix: str,
    mode: str,
    *,
    workdir: Path | None = None,
) -> bytes:
    """Return the bytes to send to the client for ``mode``.

    ``full`` returns the stored bytes untouched. ``none`` returns a lossless
    metadata-free rewrite. If the rewrite fails, raise: a ``none`` request
    must never fall back to leaking the full copy.
    """
    if mode != provenance.METADATA_NONE:
        return content
    return media_metadata.strip_metadata(
        content, suffix, workdir=workdir or DEFAULT_WORKDIR
    )


def ranged_delivery_response(
    request: Request,
    content: bytes,
    *,
    media_type: str,
    cache_control: str = "private, max-age=3600",
) -> Response:
    """Return bytes with HTTP Range support for browser media players.

    The caller passes already-prepared delivery bytes. This keeps metadata
    stripping and access checks outside the byte-range concern.
    """
    size = len(content)
    headers = {
        "accept-ranges": "bytes",
        "cache-control": cache_control,
    }
    range_header = request.headers.get("range")
    if not range_header:
        headers["content-length"] = str(size)
        return Response(content=content, status_code=200, media_type=media_type, headers=headers)

    parsed = _parse_single_byte_range(range_header, size)
    if parsed is None:
        return Response(
            status_code=416,
            media_type=media_type,
            headers={**headers, "content-range": f"bytes */{size}"},
        )
    start, end = parsed
    body = content[start : end + 1]
    return Response(
        content=body,
        status_code=206,
        media_type=media_type,
        headers={
            **headers,
            "content-length": str(len(body)),
            "content-range": f"bytes {start}-{end}/{size}",
        },
    )


def _parse_single_byte_range(value: str, size: int) -> tuple[int, int] | None:
    if size <= 0 or not value.startswith("bytes=") or "," in value:
        return None
    raw_start, sep, raw_end = value[6:].partition("-")
    if sep != "-":
        return None
    try:
        if raw_start == "":
            suffix = int(raw_end)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(raw_start)
            end = int(raw_end) if raw_end else size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)
