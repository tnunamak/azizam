"""Safe client hints attached to receipts across all HTTP protocols.

Keep only a known client product name, never the raw user-agent or headers.
The context is inherited by request tasks, including streamed responses, and
reset at the ASGI boundary so concurrent clients cannot share attribution.
"""
from contextvars import ContextVar
from contextlib import contextmanager
from urllib.parse import urlsplit

_client_hint: ContextVar[str | None] = ContextVar("receipt_client_hint", default=None)
_gateway_ui: ContextVar[bool] = ContextVar("receipt_gateway_ui", default=False)


def current_gateway_ui() -> bool:
    return _gateway_ui.get()


@contextmanager
def gateway_ui_request():
    """Mark an in-process UI test without trusting a public request header."""
    token = _gateway_ui.set(True)
    try:
        yield
    finally:
        _gateway_ui.reset(token)


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
        headers = {key.lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        try:
            referer = urlsplit(headers.get(b"referer", ""))
            ui_origin = (referer.netloc == headers.get(b"host")
                         and referer.path.rstrip("/") in {"/ui/try", "/ui/home"})
        except ValueError:
            ui_origin = False
        ui_token = _gateway_ui.set(current_gateway_ui() or ui_origin)
        token = _client_hint.set(hint)
        try:
            await self.app(scope, receive, send)
        finally:
            _client_hint.reset(token)
            _gateway_ui.reset(ui_token)
