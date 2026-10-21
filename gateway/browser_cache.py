"""Browser identity and hand-off responses must reach the server every time."""
from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BrowserCacheMiddleware:
    """Apply outside the egress gate, which can replace framework error responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        handoff = path in {"/logout", "/auth/api/sign-out-everywhere"}
        private_api = any(path == prefix or path.startswith(prefix + "/")
                          for prefix in ("/me/api", "/admin/api", "/auth/api"))
        if scope["type"] != "http" or not (handoff or private_api):
            return await self.app(scope, receive, send)

        started = False

        async def send_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                headers["cache-control"] = "no-store"
                if handoff:
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
