"""A fake gateway for the transition proof harness: the owner transition routes and a chat route with a cache.

It is served with ``LoopbackServer`` so the harness runs against a real socket. Every behaviour the proof must
tell apart is an attribute the test sets before the run:

- ``park`` / ``wake``: ``succeed``, ``fail`` (ends ``failed_closed``), ``reconcile`` (ends ``reconciling``),
  ``hang`` (stays ``running``), ``conflict`` (409 at once);
- ``cache``: ``kept`` (a park leaves the prefix cached) or ``lost`` (a park drops it);
- ``slow_when_cached``: a cached reply is as slow as a cold one;
- ``usage_shape``: ``standard`` or ``alternate`` (the same two counts at other JSON paths);
- ``echo_auth``: an error body repeats the Authorization header, to prove the harness redacts it.

A reply's prompt size is its message text length divided by four; the cached count is the length of the longest
earlier request whose messages are a prefix of this one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .loopback import LoopbackServer

KEY = "owner-key-for-tests-0123456789abcdef"
DOMAIN = "proof-domain"


def _tokens(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(m["content"])) for m in messages) // 4


class FakeTransitionGateway:
    def __init__(self) -> None:
        self.key = KEY
        self.domain = DOMAIN
        self.park, self.wake, self.cache = "succeed", "succeed", "kept"
        self.slow_when_cached, self.usage_shape, self.echo_auth = False, "standard", False
        self.polls_to_finish = 2
        self.cold_delay_s, self.warm_delay_s = 0.25, 0.01
        self.requests: list[dict[str, Any]] = []
        self.gate, self.parked = "open", False
        self.ops: dict[str, dict[str, Any]] = {}
        self._cached: list[list[dict[str, Any]]] = []
        self._server = LoopbackServer(self._app)

    def start(self) -> "FakeTransitionGateway":
        self._server.start()
        return self

    def stop(self) -> None:
        self._server.stop()

    @property
    def base_url(self) -> str:
        return self._server.base_url

    def paths(self) -> list[str]:
        return [f"{r['method']} {r['path']}" for r in self.requests]

    # -- ASGI ---------------------------------------------------------------------------------------------

    async def _app(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        raw = (await receive()).get("body", b"")
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        body = json.loads(raw) if raw else None
        self.requests.append({"method": scope["method"], "path": scope["path"], "body": body,
                              "authorization": headers.get("authorization")})
        status, payload = await self._route(scope["method"], scope["path"], body, headers.get("authorization"))
        data = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
        await send({"type": "http.response.body", "body": data})

    async def _route(self, method: str, path: str, body: Any, auth: Any) -> tuple[int, Any]:
        if auth != f"Bearer {self.key}":
            return 401, {"error": "bad key"}
        prefix = f"/admin/engines/domains/{self.domain}/"
        if path == "/v1/chat/completions":
            return await self._chat(body)
        if not path.startswith(prefix):
            return 404, {"error": "unknown domain"}
        rest = path[len(prefix):]
        if rest == "gate":
            return 200, {"gate": self.gate, "op_id": None}
        if method == "POST" and rest == "transitions":
            return self._begin(body)
        if method == "GET" and rest.startswith("transitions/"):
            return self._read(rest.split("/", 1)[1])
        return 404, {"error": "no route"}

    # -- transitions --------------------------------------------------------------------------------------

    def _begin(self, body: dict[str, Any]) -> tuple[int, Any]:
        mode = getattr(self, body["kind"])
        for op_id, op in self.ops.items():
            if op["state"] in ("running", "failed_closed", "reconciling"):
                if op["key"] == body["idempotency_key"]:
                    return 202, {"op_id": op_id, "state": op["state"], "gate": self.gate}
                return 409, {"op_id": op_id, "state": op["state"], "echo": self._echo()}
        if mode == "conflict":
            return 409, {"op_id": "op-other", "state": "running", "echo": self._echo()}
        op_id = f"op-{len(self.ops) + 1}"
        self.ops[op_id] = {"kind": body["kind"], "key": body["idempotency_key"], "state": "running",
                           "polls_left": self.polls_to_finish}
        self.gate = "closed"
        return 202, {"op_id": op_id, "state": "running", "gate": "closed"}

    def _echo(self) -> str:
        return f"Authorization: Bearer {self.key}" if self.echo_auth else ""

    def _read(self, op_id: str) -> tuple[int, Any]:
        op = self.ops.get(op_id)
        if op is None:
            return 404, {}
        if op["state"] == "running" and getattr(self, op["kind"]) != "hang":
            op["polls_left"] -= 1
            if op["polls_left"] <= 0:
                self._settle(op)
        return 200, {"op_id": op_id, "domain": self.domain, "kind": op["kind"], "state": op["state"]}

    def _settle(self, op: dict[str, Any]) -> None:
        mode = getattr(self, op["kind"])
        op["state"] = {"succeed": "succeeded", "fail": "failed_closed", "reconcile": "reconciling"}[mode]
        self.gate = {"succeeded": "open", "failed_closed": "failed_closed", "reconciling": "reconciling"}[op["state"]]
        if op["state"] == "succeeded":
            self.parked = op["kind"] == "park"
            if self.parked and self.cache == "lost":
                self._cached.clear()

    # -- chat ---------------------------------------------------------------------------------------------

    async def _chat(self, body: dict[str, Any]) -> tuple[int, Any]:
        if self.parked:
            op_status, op = self._begin({"kind": "wake", "idempotency_key": "wake-auto"})
            if op_status == 202 and self.wake != "hang":
                self._settle(self.ops[op["op_id"]])
            if op_status != 202 or self.gate != "open":
                return 503, {"error": "the domain is not open", "echo": self._echo()}
        messages = body["messages"]
        cached = max((_tokens(seen) for seen in self._cached if messages[:len(seen)] == seen), default=0)
        self._cached.append(messages)
        await asyncio.sleep(self.warm_delay_s if cached and not self.slow_when_cached else self.cold_delay_s)
        prompt = _tokens(messages)
        usage: dict[str, Any] = {"completion_tokens": 3}
        extra: dict[str, Any] = {}
        if self.usage_shape == "standard":
            usage.update(prompt_tokens=prompt, prompt_tokens_details={"cached_tokens": cached})
        else:
            extra["timings"] = {"prompt_n": prompt, "cache_n": cached}
        return 200, {"choices": [{"message": {"role": "assistant", "content": "answer"}}], "usage": usage, **extra}
