"""Tell search engines not to index anything this gateway serves as a web page.

Azizam is a private service. ``/robots.txt`` (a root file of the web export, ``ui_pages.json``) asks
crawlers to stay away, and every HTML page also carries ``noindex, nofollow``: the app's pages, its
HTML 404 page, sign-in pages and any error page, so a link from outside does not put one in a search.
"""
from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ROBOTS_TAG = "noindex, nofollow"


class NoIndexHeaderMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_tag(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if headers.get("content-type", "").lower().startswith("text/html"):
                    headers["x-robots-tag"] = ROBOTS_TAG
            await send(message)

        await self.app(scope, receive, send_with_tag)
