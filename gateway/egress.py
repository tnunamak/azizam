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
    "proxy": "the status and drain routes defined in the gateway module: no service record content (phase 2)",
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


def projected_response(content: Mapping[str, Any], *, status_code: int = 200, headers: Mapping[str, str] | None = None) -> ProjectedResponse:
    """A response whose every top-level key is a registered projection type (``projection_types.BODY_KEYS``)."""
    return ProjectedResponse(content=projection_types.project_body(dict(content)).tree, status_code=status_code,
                             headers=dict(headers or {}))


def plain_json_response(content: Any, *, status_code: int = 200, headers: Mapping[str, str] | None = None) -> PlainResponse:
    """JSON that carries no record content (an app list, a count, an acknowledgement).

    A body with a key that names record content is refused: it must go through ``projected_response``.
    """
    if isinstance(content, Mapping) and any(key in projection_types.BODY_KEYS for key in content):
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
    """Fail for an admin route that is not a ``ManagementRoute`` and not an inventoried exception; freeze the table.

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
        module = getattr(getattr(route, "endpoint", None), "__module__", "?")
        if module not in UNMANAGED_ADMIN_MODULES:
            unregistered.append(f"{','.join(methods) or 'WS'} {path} ({module})")
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


class EgressGate:
    """Buffer a management response until its bytes are admitted; give framework errors a fixed envelope."""

    def __init__(self, app: ASGIApp, *, prefixes: tuple[str, ...] = MANAGEMENT_PREFIXES, max_body: int = MAX_BODY) -> None:
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
        elif status in _FIXED:
            await self._send(send, fixed_error_response(status))
        else:
            await send(start)
            await send({"type": "http.response.body", "body": bytes(body)})

    @staticmethod
    async def _send(send: Send, response: ApprovedResponse) -> None:
        headers = [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in response.headers.items()]
        await send({"type": "http.response.start", "status": response.status_code, "headers": headers})
        await send({"type": "http.response.body", "body": response.body})


__all__ = [
    "ApprovedResponse", "EgressGate", "ManagementRoute", "PlainResponse", "ProjectedResponse", "SafeErrorResponse",
    "fixed_error_response", "plain_json_response", "projected_response", "safe_error_response", "verify_and_freeze",
]
