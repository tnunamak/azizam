"""Where a cookie-authenticated write came from (the browser CSRF defence, beside the CSRF token).

A browser sends the sign-in cookie with every request to this host, including one a page on another site started.
The CSRF token stops that page reading the token. This check is the second rule, and it needs no secret: an unsafe
request that carries a sign-in cookie must say, with ``Origin`` (or ``Referer`` when ``Origin`` is absent), that it
came from this gateway, or carry ``Sec-Fetch-Site: same-origin``, which a page cannot set. Prior art: Django's CSRF
middleware, Rails ``forgery_protection_origin_check``, and the OWASP Fetch Metadata guidance.

Trusted origins are the request's own Host and the instance's pinned ``base_url``. A forwarded host header is not
evidence. A request with neither header is refused: every browser sends ``Origin`` on a same-origin POST, PUT, PATCH
and DELETE, and a script with no browser behind it uses a bearer key (which this check does not touch).
"""
from __future__ import annotations

from urllib.parse import urlsplit

from starlette.requests import Request


def _netloc(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"}:
        return ""
    return parts.netloc.lower()


def sent_from_this_gateway(request: Request, pinned_base_url: str = "") -> bool:
    """True when a browser says this request came from the gateway's own pages."""
    if request.headers.get("sec-fetch-site", "").strip().lower() == "same-origin":
        return True
    supplied = request.headers.get("origin") or request.headers.get("referer")
    if not supplied:
        return False
    origin = _netloc(supplied)
    if not origin:
        return False
    trusted = {request.url.netloc.lower()}
    pinned = _netloc(pinned_base_url) if pinned_base_url else ""
    if pinned:
        trusted.add(pinned)
    return origin in trusted
