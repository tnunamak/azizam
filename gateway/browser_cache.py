"""Browser identity and hand-off responses must reach the server every time."""
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The route whose only work is the header below. The sign-out page calls it once the sign-out has answered.
CLEAR_BROWSER_CACHE_PATH = "/auth/api/clear-browser-cache"
_SIGN_OUT_PATHS = frozenset({"/logout", "/auth/api/sign-out-everywhere"})


def _clears_cache(path: str, scope: Scope) -> bool:
    """Whether the answer tells the browser to drop its cached copies of this site's files.

    A browser holds an answer that carries the header until its cache is cleared, which can take many seconds. The
    sign-out page waits a bounded time for its answer, so a held answer read as a failed sign-out. That page's answers
    (JSON) leave the header out; the page then calls CLEAR_BROWSER_CACHE_PATH, whose answer it does not wait for. A
    form sign-out with no page script (a redirect answer) still carries it, as nothing else would clear the cache.
    """
    if path == CLEAR_BROWSER_CACHE_PATH:
        return True
    return path in _SIGN_OUT_PATHS and "application/json" not in Headers(scope=scope).get("accept", "")


class BrowserCacheMiddleware:
    """Apply outside the egress gate, which can replace framework error responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        handoff = path in _SIGN_OUT_PATHS
        private_api = any(path == prefix or path.startswith(prefix + "/")
                          for prefix in ("/me/api", "/admin/api", "/auth/api"))
        if scope["type"] != "http" or not (handoff or private_api):
            return await self.app(scope, receive, send)
        clears = _clears_cache(path, scope)

        started = False

        async def send_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                headers["cache-control"] = "no-store"
                # The route clears only when it answers: a refusal (another site's request, a wrong method) must not
                # let a page elsewhere clear a visitor's cache for this site.
                if clears and (path != CLEAR_BROWSER_CACHE_PATH or message["status"] < 400):
                    headers["clear-site-data"] = '"cache"'
            await send(message)

        try:
            await self.app(scope, receive, send_headers)
        except Exception:
            # Starlette's outer error handler sends outside user middleware.
            # Match its default 500 here so even an unhandled /me/api error gets
            # the rule above, then re-raise for the server's normal error logging.
            if not started:
                response = PlainTextResponse("Internal Server Error", status_code=500)
                await response(scope, receive, send_headers)
            raise
