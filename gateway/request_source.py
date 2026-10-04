"""Safe client hints attached to receipts across all HTTP protocols.

Keep only a known client product name, never the raw user-agent or headers.
The context is inherited by request tasks, including streamed responses, and
reset at the ASGI boundary so concurrent clients cannot share attribution.
"""
from contextvars import ContextVar

_client_hint: ContextVar[str | None] = ContextVar("receipt_client_hint", default=None)
_CLIENT_HINTS = (
    (("sillytavern",), "SillyTavern"),
    (("open-webui", "open webui", "openwebui"), "Open WebUI"),
    (("daisy", "raspberry"), "Daisy/pi"),
)


def current_client_hint() -> str | None:
    return _client_hint.get()


class RequestSourceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        agent = next((value.decode("latin-1").lower() for key, value in scope.get("headers", [])
                      if key.lower() == b"user-agent"), "")
        hint = next((label for needles, label in _CLIENT_HINTS
                     if any(needle in agent for needle in needles)), None)
        token = _client_hint.set(hint)
        try:
            await self.app(scope, receive, send)
        finally:
            _client_hint.reset(token)
