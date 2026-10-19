"""Errors to an API caller are built from our own words only (decision D44).

An upstream engine echoes what it was sent: a rejected upload comes back as "bad key sk-..." and an HTTP client error
names the URL it dialed. Relaying such text to the caller, in a message or a header, hands the caller a credential the
gateway holds for the engine. Scrubbing the text first is the backup (``gateway.log_scrub``); the rule is that no
error response is built from upstream text at all.

``client_error`` is the one builder. It takes the status the gateway answers with, a *key* into the fixed public reasons
below (a key, not text: nothing derived from a reply or an exception can be passed as a reason), the gateway's own error
code, and the request id. ``upstream_error_response`` is the same for a reply the gateway received from an engine and
must pass on as an error: it keeps the status class, the machine code (a short token), the error type and ``Retry-After``,
and replaces the message and drops every other header.

``tests/test_no_upstream_text_in_client_errors.py`` fails for a response, an ``HTTPException`` or an error builder that is
given text derived from an upstream reply or from an exception caught around a call to an upstream.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from starlette.responses import Response

# Fixed public reasons. A key is chosen by the caller; the text is ours.
REASONS: dict[str, str] = {
    "upstream_unreachable": "The engine could not be reached.",
    "upstream_timeout": "The engine did not answer in time.",
    "upstream_failed": "The engine failed while handling this request.",
    "upstream_refused": "The engine refused this request.",
    "upstream_dropped": "The connection to the engine dropped.",
    "upstream_out_of_memory": "The engine ran out of memory. Try a smaller request.",
    "upstream_invalid_reply": "The engine's reply could not be read.",
    "upload_failed": "Image upload failed: the engine refused the file.",
    "mask_upload_failed": "Mask upload failed: the engine refused the file.",
    "asset_upload_failed": "Asset upload failed: the engine refused the file.",
    "proxy_error": "The gateway could not complete this request. Try again.",
    "invalid_form": "The form data could not be read.",
    "invalid_request": "The request could not be read. Check its shape and try again.",
    "invalid_json": "The request body is not valid JSON.",
    "cancel_failed": "The gateway could not cancel the job that was in progress.",
    "tts_unavailable": "The speech engine is unavailable.",
    "tts_failed": "The speech engine failed while handling this request.",
    "voice_design_failed": "Voice design failed.",
    "audio_failed": "The audio engine failed while handling this request.",
    "video_failed": "The video engine failed while handling this request.",
    "setup_failed": "The gateway could not complete this step. Try again.",
    "provider_failed": "The provider failed while handling this request.",
    "admin_failed": "The administration service could not be reached.",
    "sign_in_failed": "The identity provider refused the request.",
}

# Fixed public text for the status an engine answered with, when the gateway must say that the engine refused.
STATUS_REASONS: dict[int, str] = {
    400: "The engine rejected the request.",
    401: "The engine did not accept the credentials the gateway holds for it.",
    403: "The engine refused the request.",
    404: "The engine does not have what the request asked for.",
    408: "The engine did not answer in time.",
    409: "The engine could not accept the request in its current state.",
    413: "The request is larger than the engine accepts.",
    415: "The engine does not accept this kind of content.",
    422: "The engine could not process the request.",
    429: "The engine is busy or limiting requests. Try again later.",
}
_SERVER_REASON = "The engine failed while handling the request."

# Machine codes an engine may name that clients branch on; the text is ours.
CODE_REASONS: dict[str, str] = {
    "context_length_exceeded": "The request is longer than the model's context window.",
    "model_not_found": "The model was not found.",
    "rate_limit_exceeded": "The engine is busy or limiting requests. Try again later.",
    "insufficient_quota": "The provider quota is used up.",
    "content_policy_violation": "The request was refused by the model's content policy.",
    "string_above_max_length": "A value in the request is longer than the engine accepts.",
}

_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.\-\[\]]{0,63}$")  # ``known_speaker_names[]`` is a form field
_RETRY_AFTER = re.compile(r"^[0-9A-Za-z ,:\-]{1,64}$")


def status_reason(status: int) -> str:
    """The fixed sentence for an engine's status."""
    return STATUS_REASONS.get(status) or (_SERVER_REASON if status >= 500 else STATUS_REASONS[400])


def _default_type(status: int) -> str:
    return "invalid_request_error" if 400 <= status < 500 else "api_error"


def _response(status: int, error: dict[str, Any], request_id: str | None, headers: Mapping[str, str] | None) -> Response:
    out = {str(name): str(value) for name, value in (headers or {}).items()}
    if request_id:
        out.setdefault("x-request-id", str(request_id))
    return Response(content=json.dumps({"error": error}), status_code=status, media_type="application/json", headers=out)


def client_error(
    status: int,
    reason: str,
    *,
    code: str | None = None,
    error_type: str | None = None,
    param: str | None = None,
    request_id: str | None = None,
    subject: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """An OpenAI-shaped error response made of ``REASONS[reason]`` and the gateway's own ``code``.

    ``subject`` names the configured engine ("ComfyUI") the sentence is about: an operator's label, never a reply's text.
    ``param`` is the request field at fault (a name the gateway chose). ``reason`` must be a key of ``REASONS``.
    """
    text = REASONS[reason]
    message = f"{subject}: {text}" if subject else text
    error = {"message": message, "type": error_type or _default_type(status), "param": param, "code": code}
    return _response(status, error, request_id, headers)


def upstream_error_response(
    status: int,
    body: bytes | str | None,
    headers: Mapping[str, str] | None = None,
    *,
    request_id: str | None = None,
    own_headers: Mapping[str, str] | None = None,
) -> Response:
    """Pass an engine's error reply on to the caller without its words.

    Kept: the status, and the error ``type``, the machine ``code`` and ``param`` when each is a short lower-case token
    (``requests``, ``context_length_exceeded``, ``model``), and ``Retry-After``. Replaced: the message (by the fixed sentence for the code, else for the
    status). Dropped: every other header and every other field. ``own_headers`` are the gateway's (``X-Azizam-*``).
    """
    error: Any = None
    try:
        parsed = json.loads(body) if body else None
        error = parsed.get("error") if isinstance(parsed, dict) else None
    except (ValueError, TypeError, UnicodeDecodeError):
        error = None
    fields = error if isinstance(error, dict) else {}

    def token(name: str) -> str | None:
        value = fields.get(name)
        return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None

    code, param, kind = token("code"), token("param"), token("type")
    out = {str(name): str(value) for name, value in (own_headers or {}).items()}
    retry_after = next((value for name, value in (headers or {}).items() if str(name).lower() == "retry-after"), None)
    if isinstance(retry_after, str) and _RETRY_AFTER.fullmatch(retry_after):
        out["retry-after"] = retry_after
    message = CODE_REASONS.get(code or "") or status_reason(status)
    return _response(status, {"message": message, "type": kind or _default_type(status), "param": param, "code": code},
                     request_id, out)


def _stream_error(error: Any) -> dict[str, Any]:
    """The error object of an in-stream error event, made of the fixed message and the tokens the engine named."""
    fields = error if isinstance(error, dict) else {}

    def token(name: str) -> str | None:
        value = fields.get(name)
        return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None

    code = token("code")
    return {"message": CODE_REASONS.get(code or "") or _SERVER_REASON, "type": token("type") or "api_error",
            "param": token("param"), "code": code}


def rewrite_stream_errors(data: bytes) -> bytes:
    """``data`` is whole server-sent events; an event that carries an ``error`` is rewritten without the engine's words.

    The status of a stream is sent before its body, so an engine reports a failure inside the stream. Its message is the
    engine's text like any error reply. Every other event is left as it is.
    """
    if b"error" not in data:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    out: list[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped.startswith("data:") or '"error"' not in stripped:
            out.append(line)
            continue
        payload_text = stripped.removeprefix("data:").strip()
        try:
            payload = json.loads(payload_text)
        except ValueError:
            out.append(line)
            continue
        if isinstance(payload, dict) and "error" in payload:
            payload = {**{key: value for key, value in payload.items() if key == "type" and value == "error"},
                       "error": _stream_error(payload["error"])}
            newline = line[len(line.rstrip("\r\n")):]
            out.append("data: " + json.dumps(payload, separators=(",", ":")) + newline)
            changed = True
        else:
            out.append(line)
    return "".join(out).encode("utf-8") if changed else data
