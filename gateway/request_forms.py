"""Read a request's form and close it when the request ends.

Starlette spills each uploaded file over 1 MiB to an anonymous file in the process temporary directory (the
gateway's private directory, ``gateway.private_tmp``). Nothing closes that file unless the route declares a
``Form`` or ``File`` parameter, so a handler that calls ``request.form()`` itself must register the close. The
exit stack lives in the ASGI scope; it runs after the response is sent, on failure and on client disconnect.
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from starlette.datastructures import FormData

_SCOPE_STACK = "fastapi_middleware_astack"


async def read_form(request: Any, **limits: Any) -> FormData:
    """``await request.form(**limits)``, with its spilled files closed when the request ends."""
    form = await request.form(**limits)
    scope = getattr(request, "scope", None)
    stack = scope.get(_SCOPE_STACK) if isinstance(scope, dict) else None
    if isinstance(stack, AsyncExitStack):
        stack.push_async_callback(form.close)
    return form
