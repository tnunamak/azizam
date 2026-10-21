"""Errors to an API caller are built from our own words only (decision D44).

An upstream engine echoes what it was sent: a rejected upload comes back as "bad key sk-..." and an HTTP client error
names the URL it dialed. Relaying such text to the caller, in a message or a header, hands the caller a credential the
gateway holds for the engine. Scrubbing the text first is the backup (``gateway.log_scrub``); the rule is that no
error response is built from upstream text at all.

``client_error`` is the one builder. It takes the status the gateway answers with, a *key* into the fixed public reasons
below (a key, not text: nothing derived from a reply or an exception can be passed as a reason), the gateway's own error
code, and the request id. ``upstream_error_response`` is the same for a reply the gateway received from an engine and
must pass on as an error. It is deny-default (decision D45): the error ``type`` and ``code`` pass only from the fixed
lists below, ``param`` only when it names a field of the caller's own request, ``Retry-After`` only as a number the gateway
writes itself. The message is the gateway's sentence for the code (with a limit the gateway knows locally), and every
other header and field is dropped.

``tests/test_no_upstream_text_in_client_errors.py`` fails for a response, an ``HTTPException`` or an error builder that is
given text derived from an upstream reply or from an exception caught around a call to an upstream.
"""

from __future__ import annotations

import codecs
import datetime
import email.utils
import json
import math
import re
from typing import Any, Collection, Mapping

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

# Machine codes a client branches on, with the gateway's own sentence for each. An engine's wording is never relayed.
CODE_REASONS: dict[str, str] = {
    "context_length_exceeded": "The request is longer than the model's context window.",
    "model_not_found": "The model was not found.",
    "model_not_deployed": "The model is not deployed.",
    "rate_limit_exceeded": "The engine is busy or limiting requests. Try again later.",
    "insufficient_quota": "The provider quota is used up.",
    "billing_not_active": "The provider account is not active.",
    "billing_hard_limit_reached": "The provider spending limit is reached.",
    "content_policy_violation": "The request was refused by the model's content policy.",
    "content_filter": "The request was refused by the model's content policy.",
    "string_above_max_length": "A value in the request is longer than the engine accepts.",
    "string_below_min_length": "A value in the request is shorter than the engine accepts.",
    "array_above_max_length": "A list in the request has more items than the engine accepts.",
    "array_below_min_length": "A list in the request has fewer items than the engine needs.",
    "integer_above_max_value": "A number in the request is larger than the engine accepts.",
    "integer_below_min_value": "A number in the request is smaller than the engine accepts.",
    "decimal_above_max_value": "A number in the request is larger than the engine accepts.",
    "decimal_below_min_value": "A number in the request is smaller than the engine accepts.",
    "empty_string": "A value in the request is empty.",
    "invalid_api_key": "The credentials were not accepted.",
    "invalid_value": "A value in the request is not valid.",
    "invalid_type": "A value in the request has the wrong type.",
    "unsupported_value": "A value in the request is not supported.",
    "unsupported_parameter": "A field in the request is not supported.",
    "unknown_parameter": "A field in the request is not known.",
    "missing_required_parameter": "A required field is missing from the request.",
    "invalid_image": "The image could not be read.",
    "invalid_image_format": "The image format is not supported.",
    "invalid_image_url": "The image address could not be used.",
    "invalid_prompt": "The prompt was refused.",
    "invalid_json": "The request body is not valid JSON.",
    "json_validate_failed": "The model's reply did not match the requested format.",
    "tool_use_failed": "The model could not complete the tool call.",
    "request_too_large": "The request is larger than the engine accepts.",
    "payload_too_large": "The request is larger than the engine accepts.",
    "file_too_large": "The file is larger than the engine accepts.",
    "overloaded": "The engine is overloaded. Try again later.",
    "engine_overloaded": "The engine is overloaded. Try again later.",
    "model_overloaded": "The engine is overloaded. Try again later.",
    "service_unavailable": "The engine is unavailable. Try again later.",
    "backend_unavailable": "The engine is unavailable. Try again later.",
    "timeout": "The engine did not answer in time.",
    "request_timeout": "The engine did not answer in time.",
    "server_error": _SERVER_REASON,
    "internal_error": _SERVER_REASON,
    "upstream_error": _SERVER_REASON,
    "upstream_stream_interrupted": "The LLM execution stream ended before the response was complete.",
    "unsupported_country_region_territory": "The provider does not serve this region.",
}

# Codes the gateway's own routes put in an error. A reply that crosses the gateway again (a protocol frontend reading the
# chat route, the router behind it) keeps them; an engine that echoes one gains nothing, because each is a fixed word.
GATEWAY_CODES = frozenset({
    "access_plan_editor_unavailable", "account_disabled", "adapter_not_registered", "adapter_required", "adapter_version_mismatch",
    "address_invalid", "admin_action_header_required", "admin_csrf_required", "admin_forbidden", "alias_policy_unavailable",
    "ambiguous_faces", "asset_missing", "backend_error", "backend_not_found", "backend_recovery_failed", "both_methods_off",
    "builtin_disabled", "canceled", "cannot_start_model", "client_disconnected", "csrf_required", "current_model_unavailable",
    "cursor_not_found", "custom_voice_engine_unsupported", "decision_backend_error", "engine_address_needs_confirmation",
    "engine_credential_origin_mismatch", "engine_credential_unavailable", "engine_protocol_unavailable", "export_failed",
    "face_too_small", "gpu_memory_unavailable", "hosted_tool_not_supported", "idempotency_conflict", "image_client_disconnected",
    "image_generation_timeout", "image_post_processing_failed", "incomplete_rule", "invalid_client", "invalid_connection",
    "invalid_cursor", "invalid_decision", "invalid_defaults", "invalid_engine_connection", "invalid_extension",
    "invalid_gateway_extensions", "invalid_instance", "invalid_mask_dimensions", "invalid_native_path", "invalid_prompt_memory",
    "invalid_prompt_memory_settings", "invalid_prompt_mode", "invalid_request", "invalid_request_error", "invalid_retention", "invalid_sessions",
    "invalid_shots", "invalid_source", "invalid_stt_chunk_response", "invalid_stt_stream_response", "invalid_workflow_extension",
    "generation_mismatch", "job_canceled", "litellm_unconfigured", "login_required", "long_video_not_supported", "malformed_response",
    "media_job_assets_missing", "media_job_not_found", "media_job_not_ready", "media_job_outputs_missing", "media_payload_capacity", "measurement_engine_offline", "method_not_allowed",
    "missing_file", "missing_required_image", "model_policy_schema_unavailable", "model_switch_deferred", "native_ambiguous_path",
    "native_credential_origin_mismatch", "native_credential_unavailable", "native_destination_blocked", "native_not_allowed",
    "native_not_enabled", "native_owner_only", "native_remote_forbidden", "native_request_too_large", "no_access",
    "no_asset_store", "no_face", "not_found", "not_in_access_plan", "owner_needs_password", "owner_required",
    "paid_option_unsupported", "pose_rewritten", "pose_risks_likeness", "previous_response_not_found", "profile_result",
    "prompt_erasure_unavailable", "prompt_not_accessible", "prompt_retention_pending", "provider_auth_failed", "provider_error",
    "provider_model_list_invalid", "provider_native_paid_remote_forbidden", "provider_no_sign_in_time", "provider_not_found",
    "provider_passthrough_error", "provider_timeout", "provider_unavailable", "reauth_required", "receipt_retention_unavailable", "reconnect_required",
    "reference_skipped", "reload_failed", "request_expired", "resource_admission_denied", "same_face", "secret_needed",
    "selection_unavailable", "settings_conflict", "settings_revision_required", "shots_seconds_mismatch",
    "stt_audio_cannot_be_split", "stt_backend_connection_lost", "stt_backend_unavailable", "stt_cancellation_capacity", "stt_chunk_attempt_failed",
    "stt_cleanup_failed", "stt_configuration_invalid", "stt_media_setup_failed", "stt_proxy_error", "stt_request_deadline",
    "stt_request_timeout", "stt_resource_limit", "stt_response_limit", "stt_upload_limit", "test_required", "too_many_images",
    "tts_backend_error", "unauthorized", "unsupported_audio_translation", "unsupported_media_job_content",
    "unsupported_media_job_intent", "unsupported_operation", "unsupported_response_format", "unsupported_stream_format",
    "unsupported_stream_response_format", "unsupported_stt_operation", "user_action_header_required", "user_csrf_required",
    "video_cancel_failed", "video_extend_not_configured", "video_extend_not_supported", "video_fps_unsupported",
    "video_generation_failed", "video_not_ready", "video_remix_not_configured", "video_too_short", "video_total_length_exceeded",
    "vision_unsupported", "voice_not_available", "workflow_manifest_missing",
})

ERROR_CODES = frozenset(CODE_REASONS) | GATEWAY_CODES

# The error ``type`` values OpenAI, Anthropic and the gateway use. Anything else is replaced by the type for the status.
ERROR_TYPES = frozenset({
    "invalid_request_error", "authentication_error", "permission_error", "not_found_error", "rate_limit_error",
    "api_error", "server_error", "insufficient_quota", "overloaded_error", "request_too_large", "timeout_error",
    "upstream_error", "service_unavailable", "requests", "tokens",
})

MODEL_NOT_FOUND = CODE_REASONS["model_not_found"]

# A router that names no code says it in words. The words decide only that the model is unknown (a decision, as for an
# out-of-memory retry); the sentence and the model name that follow are ours, and the name is the caller's own.
_UNKNOWN_MODEL = re.compile(
    r"Invalid model name passed in model=\S+"
    r"|model [\"'`]?[^\s\"'`]+[\"'`]? (?:not found|does not exist)",
    re.IGNORECASE,
)

_RETRY_AFTER_NUMBER = re.compile(r"^[0-9]{1,9}(\.[0-9]{1,3})?$")
_RETRY_AFTER_MAX = 86400
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9.+\-]*/[a-z0-9][a-z0-9.+\-]*")
_CHARSET_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.:+\-]{0,31}")
# RFC 9110 parameter: ``;`` name ``=`` token or quoted-string (backslash escapes). Whitespace around ``;`` is allowed.
_PARAMETER = re.compile(r'\s*;\s*([!#$%&\'*+.^_`|~0-9a-z-]+)=("(?:[^"\\]|\\.)*"|[!#$%&\'*+.^_`|~0-9a-z-]*)', re.IGNORECASE)


def _charset_of(parameters: str) -> str | None:
    position = 0
    while position < len(parameters):
        match = _PARAMETER.match(parameters, position)
        if match is None:
            return None
        position = match.end()
        if match.group(1).lower() != "charset":
            continue
        value = match.group(2)
        if value.startswith('"'):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        value = value.lower()
        if _CHARSET_TOKEN.fullmatch(value):
            try:
                codecs.lookup(value)
            except LookupError:
                return None
            return value
        return None
    return None


def safe_content_type(value: str | None, default: str = "application/json") -> str:
    """The media type of an engine's reply with its charset (quoted or not, in any position), nothing else it wrote.

    The reviewer found no relayed multipart success, so no ``boundary`` is kept; a path that relays one must add it.
    """
    if not isinstance(value, str) or not value.strip():
        return default
    media, _, parameters = value.strip().lower().partition(";")
    media = media.strip()
    if _MEDIA_TYPE.fullmatch(media) is None:
        return default
    charset = _charset_of(";" + parameters) if parameters else None
    return media + (f"; charset={charset}" if charset else "")


def status_reason(status: int) -> str:
    """The fixed sentence for an engine's status."""
    return STATUS_REASONS.get(status) or (_SERVER_REASON if status >= 500 else STATUS_REASONS[400])


def _default_type(status: int) -> str:
    return "invalid_request_error" if 400 <= status < 500 else "api_error"


# The type for an engine's error that named none on the list: the one OpenAI and Anthropic give the status.
_TYPE_BY_STATUS = {401: "authentication_error", 403: "permission_error", 404: "not_found_error", 429: "rate_limit_error"}


def retry_after(value: Any) -> str | None:
    """``Retry-After`` as whole seconds the gateway writes itself, from a number or an HTTP date; anything else is dropped.

    The engine's text is never passed on: only the number it stands for, at most one day.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _RETRY_AFTER_NUMBER.fullmatch(text):
        seconds = math.ceil(float(text))
    else:
        try:
            moment = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None:
            return None
        seconds = math.ceil((moment - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    return str(max(0, min(seconds, _RETRY_AFTER_MAX)))


def error_fields(
    error: Any,
    status: int | None = None,
    *,
    request_fields: Collection[str] | None = None,
    limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """The ``error`` object a caller gets for an error an engine named: ``message``, ``type``, ``param`` and ``code``.

    Each field is the gateway's. ``code`` and ``type`` pass only when they are in ``ERROR_CODES`` and ``ERROR_TYPES``;
    ``param`` passes only when it is a top-level field of the caller's own request (``request_fields``); the message is
    our sentence for the code, else for ``status`` (an in-stream error has none). ``limits`` carries what the gateway
    knows locally (``context_window``: the model's window from its records) and is written into the sentence.
    """
    fields = error if isinstance(error, Mapping) else {}
    raw_code, raw_type, raw_param = fields.get("code"), fields.get("type"), fields.get("param")
    code = raw_code if isinstance(raw_code, str) and raw_code in ERROR_CODES else None
    words = fields.get("message")
    if code is None and isinstance(words, str) and (status is None or status in (400, 404)) and _UNKNOWN_MODEL.search(words):
        code = "model_not_found"
    kind = raw_type if isinstance(raw_type, str) and raw_type in ERROR_TYPES else None
    param = raw_param if isinstance(raw_param, str) and request_fields is not None and raw_param in request_fields else None
    message = CODE_REASONS.get(code or "") or (status_reason(status) if status is not None else _SERVER_REASON)
    window = (limits or {}).get("context_window")
    if code == "context_length_exceeded" and isinstance(window, int) and not isinstance(window, bool) and window > 0:
        message = f"The request is longer than the model's context window of {window} tokens."
    return {"message": message,
            "type": kind or (_TYPE_BY_STATUS.get(status) or _default_type(status) if status is not None else "api_error"),
            "param": param, "code": code}


def error_code(body: bytes | str | None) -> str | None:
    """The allowlisted machine code in an engine's error body, or None."""
    try:
        parsed = json.loads(body) if body else None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    error = parsed.get("error") if isinstance(parsed, Mapping) else None
    return error_fields(error)["code"]


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


_STT_SPLIT_REASONS = {
    "unknown_duration": (422, "its duration could not be determined"),
    "split_failed": (502, "could not be cut by the gateway"),
    "unsupported_request": (422, "uses request options that cannot be preserved across pieces"),
}


def stt_audio_cannot_be_split(reason: str, limit_seconds: float) -> Response:
    """A plain refusal built from the validated service limit and a fixed local reason, never media-tool text."""
    status, why = _STT_SPLIT_REASONS[reason]
    if not math.isfinite(limit_seconds) or limit_seconds <= 0:
        raise ValueError("The STT hard maximum must be finite and positive")
    if reason == "unknown_duration":
        message = (f"The gateway could not determine this audio's duration, so it could not verify the recorded or "
                   f"measured {limit_seconds:g}-second maximum. The audio was not sent to the transcription engine.")
    else:
        message = (f"This audio exceeds the recorded or measured {limit_seconds:g}-second maximum and {why}, "
                   f"so the gateway did not send it whole.")
    return _response(status, {
        "message": message,
        "type": "api_error" if status >= 500 else "invalid_request_error",
        "param": None,
        "code": "stt_audio_cannot_be_split",
    }, None, None)


def upstream_error_response(
    status: int,
    body: bytes | str | None,
    headers: Mapping[str, str] | None = None,
    *,
    request_id: str | None = None,
    own_headers: Mapping[str, str] | None = None,
    request_fields: Collection[str] | None = None,
    limits: Mapping[str, int] | None = None,
) -> Response:
    """Pass an engine's error reply on to the caller without its words (decision D45).

    The caller gets the status, the error object of ``error_fields`` and ``Retry-After`` as a number. ``headers`` are the
    engine's, read only for ``Retry-After``. ``own_headers`` are the gateway's, built by the caller from values the
    gateway produced (never from the engine's headers, not even by a name prefix). Every other header is dropped.
    """
    error: Any = None
    try:
        parsed = json.loads(body) if body else None
        error = parsed.get("error") if isinstance(parsed, dict) else None
    except (ValueError, TypeError, UnicodeDecodeError):
        error = None
    out = {str(name): str(value) for name, value in (own_headers or {}).items()}
    seconds = retry_after(next((value for name, value in (headers or {}).items() if str(name).lower() == "retry-after"), None))
    if seconds is not None:
        out["retry-after"] = seconds
    return _response(status, error_fields(error, status, request_fields=request_fields, limits=limits), request_id, out)


def stream_error(error: Any, *, request_fields: Collection[str] | None = None, limits: Mapping[str, int] | None = None) -> dict[str, Any]:
    """The error object of an in-stream error event: the same fields as ``error_fields``, with no status to speak of."""
    return error_fields(error, None, request_fields=request_fields, limits=limits)


def rewrite_stream_errors(data: bytes, *, request_fields: Collection[str] | None = None,
                          limits: Mapping[str, int] | None = None) -> bytes:
    """``data`` is whole server-sent events; an event that carries an ``error`` is rewritten without the engine's words.

    The status of a stream is sent before its body, so an engine reports a failure inside the stream. The event goes out
    with ``error_fields`` (our message, a code and type from the fixed lists, a param only from the caller's request).
    Every other event is left as it is.
    """
    if b"error" not in data:
        return data
    text = data.decode("utf-8", errors="replace")   # the bytes go out unchanged unless an error event is rewritten
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
        # Three shapes carry an error: {"error": {...}}, a flat {"type": "error", ...} and {"object": "error", ...}.
        flat = isinstance(payload, dict) and "error" not in payload and "error" in (payload.get("type"), payload.get("object"))
        if isinstance(payload, dict) and (payload.get("error") is not None or flat):
            payload = {**{key: value for key, value in payload.items() if key == "type" and value == "error"},
                       "error": stream_error(payload if flat else payload["error"], request_fields=request_fields, limits=limits)}
            newline = line[len(line.rstrip("\r\n")):]
            out.append("data: " + json.dumps(payload, separators=(",", ":")) + newline)
            changed = True
        else:
            out.append(line)
    return "".join(out).encode("utf-8") if changed else data
