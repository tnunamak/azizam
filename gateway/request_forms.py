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
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser, parse_options_header
from starlette.requests import Request

_SCOPE_STACK = "fastapi_middleware_astack"


async def read_form(request: Any, **limits: Any) -> FormData:
    """``await request.form(**limits)``, with its spilled files closed when the request ends."""
    if (isinstance(request, Request) and request._form is None
            and parse_options_header(request.headers.get("content-type", ""))[0] == b"multipart/form-data"):
        # Own the parser before it awaits the body. Starlette 0.50 closes this list only for
        # MultiPartException, leaving spills open on disconnect/cancellation with a retained traceback.
        parser = MultiPartParser(request.headers, request.stream(), **limits)
        try:
            request._form = await parser.parse()
        except BaseException as error:
            for handle in parser._files_to_close_on_error:
                handle.close()  # synchronous: cleanup must also finish under cancellation
            if isinstance(error, MultiPartException) and "app" in request.scope:
                raise HTTPException(status_code=400, detail=error.message) from error
            raise
        form = request._form
    else:
        form = await request.form(**limits)
    scope = getattr(request, "scope", None)
    stack = scope.get(_SCOPE_STACK) if isinstance(scope, dict) else None
    if isinstance(stack, AsyncExitStack):
        stack.push_async_callback(form.close)
    return form
