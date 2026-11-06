"""One TLS context for every short-lived HTTP client the gateway opens to check a service.

``httpx.AsyncClient()`` builds a certificate context each time it is created: 37 ms of CPU on
an idle host. A health or resource check opens a client per service per request, so a read of
``/gateway/v1/resources`` spent most of its time building contexts. A context holds no
per-connection state; one can serve every client. It is the context httpx would build itself.
"""
from __future__ import annotations

import ssl
import threading

import httpx

_LOCK = threading.Lock()
_CONTEXT: ssl.SSLContext | None = None


def tls_context() -> ssl.SSLContext:
    global _CONTEXT
    if _CONTEXT is None:
        with _LOCK:
            if _CONTEXT is None:
                _CONTEXT = httpx.create_ssl_context()
    return _CONTEXT


def is_built() -> bool:
    return _CONTEXT is not None
