"""Streaming helpers for the speech routes (no gateway state, no config).

Three small jobs live here so ``proxy.py`` only wires them together:

* ``peek_multipart_field`` reads the head of a multipart upload to find one
  small text field (the transcription ``model``) before the route decides
  which STT service owns the request, then hands back a request whose body
  replays what was read.
* ``sse_event`` / ``pcm_to_sse`` frame OpenAI-style server-sent events.
* ``transcode_pcm_stream`` turns an engine's raw PCM stream into the encoding
  the client asked for (OpenAI ``pcm`` is 24 kHz signed 16-bit mono) with one
  ``ffmpeg`` pipe, so audio is forwarded while the engine is still speaking.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any, AsyncIterator, Mapping

from starlette.requests import Request

OPENAI_PCM_RATE = 24000
# The model field must arrive within this many body bytes or the route falls
# back to the default STT service. OpenAI SDKs send form fields before the file.
PEEK_LIMIT_BYTES = 8 * 1024 * 1024
MAX_FIELD_VALUE_BYTES = 4096

_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)

# ffmpeg output options per OpenAI response_format for a streamed encode.
# wav keeps the engine's own rate; pcm is pinned to the OpenAI rate.
_OUTPUT_OPTIONS: dict[str, list[str]] = {
    "pcm": ["-f", "s16le", "-ar", str(OPENAI_PCM_RATE), "-ac", "1"],
    "wav": ["-f", "wav", "-c:a", "pcm_s16le"],
    "mp3": ["-f", "mp3", "-c:a", "libmp3lame"],
    "opus": ["-f", "ogg", "-c:a", "libopus"],
    "aac": ["-f", "adts", "-c:a", "aac"],
    "flac": ["-f", "flac", "-c:a", "flac"],
}


def multipart_boundary(content_type: str | None) -> bytes | None:
    match = _BOUNDARY_RE.search(content_type or "")
    return match.group(1).encode("latin-1", "ignore") if match else None


def find_multipart_field(head: bytes, boundary: bytes, name: str) -> str | None:
    """Return the complete text value of form field ``name`` in ``head``, else None.

    Only a field whose part header and closing boundary are both inside
    ``head`` counts, so a half-read value is never returned.
    """
    delimiter = b"--" + boundary
    start = 0
    while True:
        at = head.find(delimiter + b"\r\n", start)
        if at < 0:
            return None
        header_start = at + len(delimiter) + 2
        header_end = head.find(b"\r\n\r\n", header_start)
        if header_end < 0:
            return None
        header = head[header_start:header_end]
        value_start = header_end + 4
        end = head.find(b"\r\n" + delimiter, value_start)
        if re.search(rb'name="' + re.escape(name.encode()) + rb'"', header, re.IGNORECASE) and b"filename=" not in header.lower():
            if end < 0 or end - value_start > MAX_FIELD_VALUE_BYTES:
                return None
            return head[value_start:end].decode("utf-8", "replace")
        if end < 0:
            return None
        start = end + 2


async def peek_multipart_field(
    request: Request, name: str, *, limit: int | None = None
) -> tuple[str | None, Request]:
    """Read ASGI body messages until field ``name`` is complete, then replay them.

    Returns ``(value, request)``. ``value`` is None when the field is absent
    from the first ``limit`` bytes (or the body is not multipart); the returned
    request always yields the full original body, so nothing is lost.
    """
    limit = PEEK_LIMIT_BYTES if limit is None else limit
    boundary = multipart_boundary(request.headers.get("content-type"))
    original_receive = request.receive
    if boundary is None:
        return None, request
    messages: list[Mapping[str, Any]] = []
    head = b""
    value: str | None = None
    while len(head) < limit:
        message = await original_receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        head += message.get("body", b"")
        value = find_multipart_field(head, boundary, name)
        if value is not None or not message.get("more_body", False):
            break

    async def replay() -> Mapping[str, Any]:
        if messages:
            return messages.pop(0)
        return await original_receive()

    return value, Request(request.scope, replay)


def sse_event(event: Mapping[str, Any]) -> bytes:
    return b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"


async def pcm_to_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Frame raw audio chunks as OpenAI ``speech.audio.delta`` / ``.done`` events."""
    async for chunk in chunks:
        if chunk:
            yield sse_event({"type": "speech.audio.delta", "audio": base64.b64encode(chunk).decode()})
    yield sse_event({"type": "speech.audio.done"})


async def align_samples(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Re-cut a PCM16 byte stream so no chunk ends in the middle of a sample."""
    carry = b""
    async for chunk in chunks:
        data = carry + chunk
        cut = len(data) - (len(data) % 2)
        carry = data[cut:]
        if cut:
            yield data[:cut]


def ffmpeg_pcm_command(src_rate: int, response_format: str) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(int(src_rate)), "-ac", "1", "-i", "pipe:0",
        *_OUTPUT_OPTIONS[response_format],
        "-flush_packets", "1", "pipe:1",
    ]


def needs_transcode(src_rate: int, response_format: str) -> bool:
    return not (response_format == "pcm" and int(src_rate) == OPENAI_PCM_RATE)


async def transcode_pcm_stream(
    chunks: AsyncIterator[bytes], *, src_rate: int, response_format: str
) -> AsyncIterator[bytes]:
    """Pipe raw s16le mono PCM at ``src_rate`` through ffmpeg into ``response_format``.

    The input is fed from a task while output is yielded as soon as ffmpeg
    emits it, so the first encoded bytes leave before synthesis finishes.
    """
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_pcm_command(src_rate, response_format),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # Both pipes exist because they were requested above; bind them so the
    # type checker and a reader can see that.
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:
        raise RuntimeError("ffmpeg pipes were not created")

    async def feed() -> None:
        try:
            async for chunk in chunks:
                if chunk:
                    stdin.write(chunk)
                    await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                stdin.close()
            except Exception:  # noqa: BLE001
                pass

    feeder = asyncio.create_task(feed())
    try:
        while True:
            data = await stdout.read(16384)
            if not data:
                break
            yield data
        await feeder
    finally:
        feeder.cancel()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.shield(process.wait())


def transcript_json_to_sse(payload: Mapping[str, Any]) -> list[bytes]:
    """Frame a finished transcription as one delta plus the done event.

    Used for engines that cannot stream, so ``stream=true`` clients still get
    the OpenAI event shape (all text arrives in the single delta).
    """
    text = str(payload.get("text") or "")
    events = []
    if text:
        events.append(sse_event({"type": "transcript.text.delta", "delta": text}))
    done: dict[str, Any] = {"type": "transcript.text.done", "text": text}
    if isinstance(payload.get("usage"), Mapping):
        done["usage"] = payload["usage"]
    events.append(sse_event(done))
    return events
