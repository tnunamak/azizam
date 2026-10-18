"""An in-process stand-in for the model router sidecar that routes with the real LiteLLM Router.

The gateway sends chat requests to its LiteLLM sidecar, a separate process
that tests do not start. This ASGI app does the sidecar's part with the same
library: it compiles the gateway's service registry exactly as the gateway
writes the sidecar config (litellm_runtime.compile_executable_config, which resolves the references a record holds) and routes
the request with ``litellm.Router``. So a test can send a request through
the gateway and watch what reaches a fake engine behind the router.

What it does not reproduce: the sidecar's own HTTP server, its auth and
logging callbacks, and its config file reload. The gateway's request body is
forwarded with its model and messages only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import litellm
from litellm import Router

from gateway import litellm_runtime


def router_sidecar(registry: Callable[[], Mapping[str, Any]]):
    """An ASGI app answering ``POST /v1/chat/completions`` by routing it through the compiled registry."""

    async def app(scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        payload = json.loads(body or b"{}")
        compiled = litellm_runtime.compile_executable_config(registry())
        router = Router(model_list=compiled["model_list"], num_retries=0)
        try:
            reply = await router.acompletion(model=payload["model"], messages=payload["messages"],
                                             num_retries=0, max_retries=0)
            status, answer = 200, reply.model_dump()
        except litellm.Timeout as exc:
            status, answer = 408, {"error": {"message": str(exc), "type": "timeout", "code": "timeout"}}
        except Exception as exc:  # noqa: BLE001 - the sidecar answers what LiteLLM raised
            status = int(getattr(exc, "status_code", 500) or 500)
            answer = {"error": {"message": str(exc), "type": type(exc).__name__}}
        data = json.dumps(answer).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
        await send({"type": "http.response.body", "body": data})

    return app
