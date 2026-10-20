"""The egress of management responses (decision D31).

Three parts make "a management response holds only what a projection admitted" something the code enforces, not a
convention:

1. **Approved responses.** ``projected_response`` (registered projection types, ``gateway/projection_types.py``),
   ``plain_json_response`` (JSON that carries no record content) and ``safe_error_response`` (a fixed error shape) are
   the only ways a management handler makes a response. Each stamps the request scope with the hash of the bytes it
   rendered. A handler that returns anything else (a bare ``JSONResponse``, a dict, a ``StreamingResponse``) is
   refused.
2. **``ManagementRoute``.** A router built with ``route_class=ManagementRoute`` refuses, per request, a response that is
   not an approved one, and answers 404 for a route registered after startup froze the table.
3. **``EgressGate``.** An ASGI middleware, outermost of the application's own, that buffers a management JSON response
   until it is admitted: the bytes must match the hash an approved response stamped. It gives every framework-made
   404, 405, 422 and 500 a fixed envelope that never echoes the request body or an exception, and turns an exception
   that escaped a handler into the fixed 500. ``verify_and_freeze`` fails startup for an admin route that is neither a
   ``ManagementRoute`` nor in ``UNMANAGED_ADMIN_MODULES`` (an inventory with its reason).

Nothing here decides what is secret: that is the projection and the data model (decisions D26 and D28).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Awaitable, Callable, Iterable, Mapping

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway import projection_types
from gateway.projection import ProjectionViolation, scrub_text

RECEIPT_KEY = "azizam.egress.approved"
MANAGEMENT_KEY = "azizam.egress.management"
MANAGEMENT_PREFIXES = ("/admin/api",)
# The paths the gate holds. ``/auth/api`` (sign-in, invitations) is not a management plane, so its routes need no inventory,
# but the framework's own 404, 405, 422 and 500 answers there get the fixed envelopes too: the default 422 quotes the
# request body (an invitation's fields) back to whoever sent it, or to whoever a proxy shows the answer to.
GATED_PREFIXES = (*MANAGEMENT_PREFIXES, "/auth/api")
MAX_BODY = 16 * 1024 * 1024

# Admin routers that have not moved to ``ManagementRoute`` yet, each with its reason. The gate still gives their
# framework errors the fixed envelopes; the canary sweeps still read and write every one of their routes.
UNMANAGED_ADMIN_MODULES: dict[str, str] = {
    "gateway.settings_api": "operator settings pages: no service record content (phase 2: move to ManagementRoute)",
    "gateway.signin_api": "sign-in rules and people: no service record content (phase 2)",
    "gateway.category_api": "categories: no service record content (phase 2)",
    "gateway.accounts_api": "accounts: no service record content (phase 2)",
    "gateway.home_api": "the home page data: no service record content (phase 2)",
    "gateway.activity_api": "activity: no service record content (phase 2)",
}

# The admin routes the gateway module defines itself (``proxy.py``), one entry per route and method, each with its
# reason. A route added there without an entry fails ``verify_and_freeze``: the exemption is for these routes, not for
# the module.
UNMANAGED_ADMIN_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/admin/api/config/reload"): "reloads the runtime config and answers with fixed text and public host addresses (phase 2)",
    ("GET", "/admin/api/drain"): "the draining flag and its reason: no service record content (phase 2)",
    ("POST", "/admin/api/drain"): "sets the draining flag: no service record content (phase 2)",
    ("GET", "/admin/api/config/export"): "owner-only YAML of the registry as the projection shows it, never a secret value "
                                         "(tests/test_config_export_owner_only.py; phase 2: move to ManagementRoute)",
}

# The signed-in person's own routes (``/me/api``). They are not behind the gate yet (phase 2); each is listed with why it
# holds no service record content, and a route added without an entry fails ``verify_and_freeze``.
ME_API_PREFIX = "/me/api"
UNMANAGED_ME_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/me/api/access-request"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("GET", "/me/api/activity"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("POST", "/me/api/apps"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("DELETE", "/me/api/apps/{app_id}"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("PATCH", "/me/api/apps/{app_id}"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("POST", "/me/api/apps/{app_id}/keys"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("POST", "/me/api/connection-test"): "the signed-in person's home page and connection test: no service record content",
    ("DELETE", "/me/api/credentials/{credential_id}"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("PATCH", "/me/api/credentials/{credential_id}"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("GET", "/me/api/credentials/{credential_id}/access"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("POST", "/me/api/credentials/{credential_id}/revoke"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("GET", "/me/api/home"): "the signed-in person's home page and connection test: no service record content",
    ("GET", "/me/api/jobs"): "the person's own media, jobs and usage: no service record content",
    ("GET", "/me/api/jobs/{job_id}"): "the person's own media, jobs and usage: no service record content",
    ("GET", "/me/api/make/jobs"): "the person's own make jobs: no service record content",
    ("POST", "/me/api/make/jobs"): "the person's own make jobs: no service record content",
    ("GET", "/me/api/make/jobs/{job_id}"): "the person's own make jobs: no service record content",
    ("POST", "/me/api/make/jobs/{job_id}/cancel"): "the person's own make jobs: no service record content",
    ("GET", "/me/api/mcp/connections"): "the person's own MCP connections and consent requests: no service record content",
    ("POST", "/me/api/mcp/connections/{grant_id}/revoke"): "the person's own MCP connections and consent requests: no service record content",
    ("GET", "/me/api/mcp/requests/{request_id}"): "the person's own MCP connections and consent requests: no service record content",
    ("POST", "/me/api/mcp/requests/{request_id}"): "the person's own MCP connections and consent requests: no service record content",
    ("GET", "/me/api/media"): "the person's own media, jobs and usage: no service record content",
    ("POST", "/me/api/media/confirm-folder"): "the person's own media, jobs and usage: no service record content",
    ("POST", "/me/api/media/remove-missing"): "the person's own media, jobs and usage: no service record content",
    ("DELETE", "/me/api/media/{asset_id}"): "the person's own media, jobs and usage: no service record content",
    ("GET", "/me/api/media/{asset_id}"): "the person's own media, jobs and usage: no service record content",
    ("GET", "/me/api/media/{asset_id}/download"): "the person's own media, jobs and usage: no service record content",
    ("GET", "/me/api/models/catalog"): "the model names the person may use: no service record content",
    ("GET", "/me/api/profile/avatar"): "the signed-in person's own avatar choice: no service record content",
    ("PUT", "/me/api/profile/avatar"): "the signed-in person's own avatar choice: no service record content",
    ("GET", "/me/api/profile/avatar/picture"): "the sign-in picture the person's identity provider shared, fetched from that provider and served as a small image: no service record content",
    ("GET", "/me/api/profile/avatars"): "the avatar choices the viewer may see: no service record content",
    ("GET", "/me/api/prompt-memory"): "the person's own prompt memory: no service record content",
    ("PUT", "/me/api/prompt-memory"): "the person's own prompt memory: no service record content",
    ("DELETE", "/me/api/prompt-memory/prompts"): "the person's own prompt memory: no service record content",
    ("GET", "/me/api/receipts/{request_id}"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("GET", "/me/api/session"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("GET", "/me/api/status"): "the signed-in person's home page and connection test: no service record content",
    ("POST", "/me/api/try-key"): "the signed-in person's own apps, keys, receipts and session: no service record content",
    ("GET", "/me/api/usage/summary"): "the person's own media, jobs and usage: no service record content",
    ("GET", "/me/api/workflows"): "the workflow names the person may use: no service record content",
}

_FIXED = {
    404: ("not_found", "There is nothing at this address."),
    405: ("method_not_allowed", "This address does not accept that method."),
    422: ("invalid_request", "The request could not be read. Check its shape and try again."),
    500: ("internal_error", "The gateway could not answer this request."),
}
_BLOCKED = ("egress_blocked", "The gateway could not answer this request.")


def _safe_text(text: str) -> str:
    return scrub_text("".join(ch for ch in str(text)[:1000] if ch >= " " or ch in "\n\t"))


class ApprovedResponse(JSONResponse):
    """A JSON response a management handler may return. It stamps the request scope with the hash of its bytes."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope[RECEIPT_KEY] = hashlib.sha256(self.body).hexdigest()
        await super().__call__(scope, receive, send)


class ProjectedResponse(ApprovedResponse):
    pass


class PlainResponse(ApprovedResponse):
    pass


class SafeErrorResponse(ApprovedResponse):
    pass


# The headers a projected response may carry besides its body: the revision of the registry it shows (``ETag``) and the
# instruction that nothing in it is kept (``Cache-Control``), and the file name of an export. They are named here so the fresh tree the projector builds
# cannot lose them and a handler cannot attach another header to a record answer by this constructor.
PROJECTED_HEADERS = frozenset({"etag", "cache-control", "content-disposition"})


def projected_response(content: Mapping[str, Any], *, status_code: int = 200, headers: Mapping[str, str] | None = None) -> ProjectedResponse:
    """A response whose every top-level key is a registered projection type (``projection_types.BODY_KEYS``)."""
    if any(str(name).lower() not in PROJECTED_HEADERS for name in (headers or {})):
        raise ProjectionViolation("a projected response may carry only the headers registered for it")
    return ProjectedResponse(content=projection_types.project_body(dict(content)).tree, status_code=status_code,
                             headers=dict(headers or {}))


# The classes of response that carry no record content, and the top-level keys each may hold. A route names its class;
# it is the class that decides, so a new key in a plain body (``providers``, once a list of records) is refused until
# someone says what it holds, rather than allowed because its name was not on a list of record keys. A class whose
# value is None is open (its keys are the route family's own, such as an access-plan editor's choices) and still
# refuses every key that names record content. A record-bearing class is not registered here: it has a projection
# type (``projection_types.BODY_KEYS``) and goes through ``projected_response``.
PLAIN_RESPONSE_CLASSES: dict[str, frozenset[str] | None] = {
    "session": frozenset({"actor"}),
    "overview": frozenset({"actor", "access_model", "counts", "attention", "current_health", "frontends", "registry_apply",
                           "registry_excluded"}),
    "status": frozenset({"status"}),
    "people": frozenset({"users"}),
    "apps": frozenset({"apps", "app"}),
    "trusted_addresses": frozenset({"trusted_addresses", "trusted_address"}),
    "access_plans": frozenset({"access_plans", "access_plan"}),
    "access_plan_choices": None,
    "plan_preview": None,
    "credentials": frozenset({"credential", "credentials"}),
    "overrides": frozenset({"service_availability", "media_job_admission", "revision"}),
    "switch": frozenset({"enabled", "revision", "label", "running_processes", "note"}),
    "probe": frozenset({"probe", "detected"}),
    "keys": frozenset({"keys", "key", "deleted"}),
    "activity": frozenset({"records", "next_cursor", "automated_count", "event"}),
    "error": frozenset({"error", "check"}),
    "engine_definitions": frozenset({"format_version", "definitions", "real_tests"}),
    "stt_measurement": frozenset({"job", "policy", "samples", "settings", "settings_note", "revision", "commands", "measurement_ready", "known_limit", "trial_lengths", "trial_lengths_note"}),
    "engine_protocols": frozenset({"protocols", "problems"}),
    "engine_check": frozenset({"check", "test", "access"}),
    "provider_catalog": frozenset({"providers"}),
    "provider_reconciliation": frozenset({"reconciliations", "reconciliation"}),
    "provider_probe": frozenset({"ok", "reachable", "checked_at", "models", "partial", "message"}),
    "acknowledgement": frozenset({"ok", "count", "deleted", "note", "revision"}),
    # Dated operator-authored evidence and derived destination metadata; no raw service record.
    "retention_ledger": frozenset({"ledger"}),
    "retention_facts": frozenset({"retention_facts", "overridden"}),
}


def plain_json_response(content: Any, *, response_class: str, status_code: int = 200,
                        headers: Mapping[str, str] | None = None) -> PlainResponse:
    """JSON of a registered plain class: a body that carries no record content (an app list, a count, an acknowledgement).

    ``response_class`` names the class (``PLAIN_RESPONSE_CLASSES``). A class that is not registered, a top-level key the
    class does not hold, or (for an open class) a key that names record content is refused at once: a body that holds
    records must be built with ``projected_response``.
    """
    if response_class not in PLAIN_RESPONSE_CLASSES:
        raise ProjectionViolation("a plain response must name a registered response class")
    allowed = PLAIN_RESPONSE_CLASSES[response_class]
    keys = list(content) if isinstance(content, Mapping) else []
    if allowed is not None and not isinstance(content, Mapping):
        raise ProjectionViolation("a plain response of this class is a mapping")
    if allowed is not None and any(key not in allowed for key in keys):
        raise ProjectionViolation("a plain response holds a key its class does not")
    if allowed is None and any(key in projection_types.BODY_KEYS for key in keys):
        raise ProjectionViolation("a body that carries record content must be built with projected_response")
    return PlainResponse(content=content, status_code=status_code, headers=dict(headers or {}))


def safe_error_response(status_code: int, code: str, message: str, *, extra: Mapping[str, Any] | None = None) -> SafeErrorResponse:
    """An error in the one fixed shape. The message is the gateway's own sentence, cleaned of control characters
    and URL user info; it is never an exception's text or a request body."""
    error: dict[str, Any] = {"code": str(code)[:100], "message": _safe_text(message)}
    for key, value in (extra or {}).items():
        if isinstance(value, (str, int, bool)) or value is None:
            error[key] = _safe_text(value) if isinstance(value, str) else value
    return SafeErrorResponse(content={"error": error}, status_code=status_code)


def fixed_error_response(status_code: int) -> SafeErrorResponse:
    code, message = _FIXED.get(status_code, _BLOCKED)
    return safe_error_response(status_code, code, message)


# --- routes ------------------------------------------------------------------------------------------------------------

FROZEN_ATTRIBUTE = "egress_frozen"


class ManagementRoute(APIRoute):
    """A route whose handler may answer only with an approved response, and only while the route table is the one
    startup froze."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Any]]:
        original = super().get_route_handler()
        path = self.path

        async def handler(request: Request) -> Any:
            request.scope[MANAGEMENT_KEY] = True
            frozen = getattr(request.app.state, FROZEN_ATTRIBUTE, None)
            if frozen is not None and (request.method, path) not in frozen:
                return fixed_error_response(404)
            try:
                response = await original(request)
            except RequestValidationError:
                return fixed_error_response(422)  # its text quotes the body it could not read
            except StarletteHTTPException as exc:
                return fixed_error_response(exc.status_code) if exc.status_code in _FIXED else safe_error_response(
                    exc.status_code, "request_refused", "The request was refused.")
            if not isinstance(response, ApprovedResponse):
                return safe_error_response(500, *_BLOCKED)
            return response

        return handler


def _routes(app: Any) -> Iterable[tuple[str, Any]]:
    def walk(routes: Iterable[Any], prefix: str) -> Iterable[tuple[str, Any]]:
        for route in routes:
            path = prefix + getattr(route, "path", "")
            children = getattr(route, "routes", None)
            if children is not None and not hasattr(route, "endpoint"):
                yield from walk(children, path)
            else:
                yield path, route

    return walk(getattr(app, "routes", ()), "")


def verify_and_freeze(app: Any) -> frozenset[tuple[str, str]]:
    """Fail for an admin route that is not a ``ManagementRoute`` and not an inventoried exception, and for a ``/me/api`` route
    that is not inventoried; freeze the table.

    The error names the route and module only. Returns the frozen set of management ``(method, path)`` pairs.
    """
    managed: set[tuple[str, str]] = set()
    unregistered: list[str] = []
    for path, route in _routes(app):
        if not path.startswith(MANAGEMENT_PREFIXES):
            continue
        methods = sorted(getattr(route, "methods", None) or ())
        if isinstance(route, ManagementRoute):
            managed.update((method, path) for method in methods)
            continue
        endpoint = getattr(route, "endpoint", None)
        module = getattr(endpoint, "__module__", "?")
        # The gateway module may be loaded under another name (a test loads proxy.py as ``proxy_under_test``): the file decides.
        if module not in UNMANAGED_ADMIN_MODULES and getattr(getattr(endpoint, "__code__", None), "co_filename", "").endswith("/proxy.py"):
            module = "proxy"
        if module == "proxy":
            if not methods or any((method, path) not in UNMANAGED_ADMIN_ROUTES for method in methods if method != "HEAD"):
                unregistered.append(f"{','.join(methods) or 'WS'} {path} ({module})")
        elif module not in UNMANAGED_ADMIN_MODULES:
            unregistered.append(f"{','.join(methods) or 'WS'} {path} ({module})")
    for path, route in _routes(app):
        if path == ME_API_PREFIX or path.startswith(ME_API_PREFIX + "/"):
            methods = sorted(getattr(route, "methods", None) or ())
            if not methods or any((method, path) not in UNMANAGED_ME_ROUTES for method in methods if method != "HEAD"):
                unregistered.append(f"{','.join(methods) or 'WS'} {path} (/me/api route without an inventory entry)")
    if unregistered:
        raise RuntimeError("admin routes that are neither management routes nor inventoried exceptions: " + "; ".join(unregistered))
    frozen = frozenset(managed)
    app.state.egress_frozen = frozen
    return frozen


# --- the gate -----------------------------------------------------------------------------------------------------------


def _log_failure(scope: Scope, exc: BaseException) -> None:
    """One log line for a management request that raised: the exception's class and where it was raised, never its
    message (it may quote what the request carried)."""
    import traceback

    frames = traceback.extract_tb(exc.__traceback__)
    where = f"{frames[-1].filename.rsplit('/', 1)[-1]}:{frames[-1].lineno}" if frames else "?"
    print(f"[egress] management request failed: {type(exc).__name__} at {where}", flush=True)


def _framework_shaped(start: Message, body: bytes) -> bool:
    """True for an error answer the framework wrote (``{"detail": ...}``, or plain text), which can quote a request body.

    A handler of a route outside the managed list answers with its own ``{"error": {...}}``; that is its own contract
    (a code a page reads) and is left alone.
    """
    content_type = next((value for key, value in start.get("headers", ()) if key.lower() == b"content-type"), b"").decode("latin-1")
    if not content_type.startswith("application/json"):
        return True  # Starlette's plain-text 404, 405 and 500
    try:
        decoded = json.loads(body or b"null")
    except ValueError:
        return True
    return not (isinstance(decoded, dict) and "error" in decoded and "detail" not in decoded)


class EgressGate:
    """Buffer a management response until its bytes are admitted; give framework errors a fixed envelope."""

    def __init__(self, app: ASGIApp, *, prefixes: tuple[str, ...] = GATED_PREFIXES, max_body: int = MAX_BODY) -> None:
        self.app, self.prefixes, self.max_body = app, prefixes, max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(self.prefixes):
            await self.app(scope, receive, send)
            return
        start: Message | None = None
        body = bytearray()
        too_big = False

        async def capture(message: Message) -> None:
            nonlocal start, too_big
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body" and not too_big:
                body.extend(message.get("body", b""))
                if len(body) > self.max_body:
                    too_big = True

        failed = False
        try:
            await self.app(scope, receive, capture)
        except Exception as exc:  # noqa: BLE001 - the framework's own 500 would quote it; the fixed one does not
            failed = True
            _log_failure(scope, exc)
        if failed or start is None or too_big:
            await self._send(send, fixed_error_response(500))
            return
        status = int(start["status"])
        approved = scope.get(RECEIPT_KEY) == hashlib.sha256(bytes(body)).hexdigest()
        if approved:
            await send(start)
            await send({"type": "http.response.body", "body": bytes(body)})
            return
        if scope.get(MANAGEMENT_KEY):
            await self._send(send, safe_error_response(500, *_BLOCKED))
        elif status in _FIXED and _framework_shaped(start, bytes(body)):
            # The fixed body replaces what the framework wrote; the ``Allow`` header of a 405 is routing data and stays.
            allow = [(key, value) for key, value in start.get("headers", ()) if key.lower() == b"allow"]
            await self._send(send, fixed_error_response(status), extra=allow)
        else:
            await send(start)
            await send({"type": "http.response.body", "body": bytes(body)})

    @staticmethod
    async def _send(send: Send, response: ApprovedResponse, extra: Iterable[tuple[bytes, bytes]] = ()) -> None:
        headers = [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in response.headers.items()] + list(extra)
        await send({"type": "http.response.start", "status": response.status_code, "headers": headers})
        await send({"type": "http.response.body", "body": response.body})


__all__ = [
    "ApprovedResponse", "EgressGate", "ManagementRoute", "PlainResponse", "ProjectedResponse", "SafeErrorResponse",
    "fixed_error_response", "plain_json_response", "projected_response", "safe_error_response", "verify_and_freeze",
]
