#!/usr/bin/env python3
"""Overhead of the http-v1 mapping against a hand-written mapping of the same request.

Measures, with best-of-N timing (the minimum is the least noisy estimate on a loaded host):
- request mapping per call: compiled Program.apply vs a hand-written function (and the JSON write that follows both)
- response mapping per call
- per streamed event: SSE decode + JSON parse + map, vs hand-written; and the unmapped relay path (decode only)
- one whole executor.invoke on an in-memory transport vs the same steps written by hand

Run:  ./venv/bin/python scripts/bench_engine_protocol.py [--json]
The numbers recorded in gateway/engine_protocol/executor.py come from this script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import timeit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.engine_protocol import description, framing, mapping, pointer  # noqa: E402
from gateway.engine_protocol.executor import HttpV1Executor  # noqa: E402
from gateway.engine_protocol.transport import WireRequest  # noqa: E402
from gateway.engine_protocol.types import CanonicalRequest, Connection  # noqa: E402

ACME = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "engine_protocol" / "acme" / "acme.yml"
MESSAGES = [{"role": "user" if i % 2 == 0 else "assistant", "content": "Some text of a typical message, about sixty characters long."} for i in range(20)]
BODY = {"model": "acme-1", "messages": MESSAGES, "temperature": 0.7, "max_tokens": 256, "stream": False, "ignored": True}
UPSTREAM = {"result": {"id": "cv-1", "model": "acme-1", "output": {"text": "Hello there, this is the answer."}, "why": "eos"}, "meter": {"in": 120, "out": 9}}
FINISH = {"eos": "stop", "cap": "length", "filter": "content_filter"}


def hand_request(body: dict) -> dict:
    out = {"engine": {"model": body["model"]}, "input": {"turns": body["messages"]}, "delivery": "live" if body.get("stream") is True else "once"}
    if "temperature" in body:
        t = body["temperature"]
        out["sampling"] = {"t": 0 if t < 0 else 2 if t > 2 else t}
    if "max_tokens" in body:
        out.setdefault("sampling", {})["max_new"] = body["max_tokens"]
    return out


SMALL_RULES = [
    {"copy": {"from": "/body/model", "to": "/model"}},
    {"rename": {"from": "/max_tokens", "to": "/max_new"}},
    {"default": {"to": "/stream", "value": False}},
    {"drop": {"path": "/user"}},
    {"clamp": {"path": "/temperature", "min": 0, "max": 1}},
]
SMALL_BODY = {"model": "acme-1", "max_tokens": 64, "temperature": 1.7, "user": "u-1", "messages": [{"role": "user", "content": "hi"}]}


def hand_small(body: dict, deep: bool = False) -> dict:
    """The same five rules, written by hand over a passthrough copy (``deep``: copy every container, as the executor does)."""
    out = pointer.clone(body) if deep else dict(body)
    out["model"] = body["model"]
    if "max_tokens" in out:
        out["max_new"] = out.pop("max_tokens")
    if "stream" not in out:
        out["stream"] = False
    out.pop("user", None)
    t = out.get("temperature")
    if t is not None:
        out["temperature"] = 0 if t < 0 else 1 if t > 1 else t
    return out


def hand_response(up: dict) -> dict:
    r = up["result"]
    out = {"object": "chat.completion", "id": r["id"], "model": r["model"],
           "choices": [{"index": 0, "message": {"role": "assistant", "content": r["output"]["text"]}, "finish_reason": FINISH[r["why"]]}]}
    out["usage"] = {"prompt_tokens": up["meter"]["in"], "completion_tokens": up["meter"]["out"]}
    return out


def hand_event(line: bytes) -> dict:
    d = json.loads(line)
    out = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": d["delta"]}, "finish_reason": None}]}
    if d.get("done") is True:
        out["choices"][0]["finish_reason"] = FINISH[d["why"]]
        out["usage"] = {"prompt_tokens": d["meter"]["in"], "completion_tokens": d["meter"]["out"]}
    return out


def best(fn, number: int, repeat: int = 9, scale: float = 1.0) -> float:
    """Microseconds per call, best of ``repeat`` runs of ``number`` calls (``scale`` shortens the runs)."""
    number = max(1, int(number * scale))
    return min(timeit.repeat(fn, number=number, repeat=repeat)) / number * 1e6


class NullTransport:
    def __init__(self, answer: bytes) -> None:
        self.answer = answer

    async def send(self, request: WireRequest, timeouts):
        answer = self.answer

        class Response:
            status = 200
            headers = {"content-type": "application/json"}

            async def chunks(self_inner):
                yield answer

            async def aclose(self_inner):
                return None
        return Response()


def measure(scale: float = 1.0) -> dict[str, dict[str, float]]:
    """Time every case; ``scale`` below 1 shortens the runs (the tests use it), at the cost of noisier numbers."""
    desc = description.parse_description_text(ACME.read_text(), source="acme.yml")
    chat = desc.operations["chat"]
    req_program = chat.request.body.program
    resp_program = chat.response.program
    stream_program = chat.response.stream.program
    source = {"body": BODY, "params": {}, "files": {}}
    up_source = {"body": UPSTREAM, "status": 200, "headers": {}, "params": {}, "event": None}
    dumps = lambda doc: json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()  # noqa: E731
    lines = [b'{"delta":"Hel","done":false}', b'{"delta":"lo","done":false}', b'{"delta":"","done":true,"why":"eos","meter":{"in":5,"out":2}}']
    wire = b"".join(b"data: " + line + b"\n\n" for line in lines)
    event_source = {"body": None, "status": 200, "headers": {}, "params": {}, "event": None}

    def mapped_events() -> None:
        decoder = framing.SseDecoder(1 << 20)
        for ev in decoder.feed(wire):
            event_source["body"] = json.loads(ev.data)
            stream_program.apply(event_source)

    def hand_events() -> None:
        for chunk in wire.split(b"\n\n")[:-1]:
            hand_event(chunk[6:])

    def relay_events() -> None:
        for ev in framing.SseDecoder(1 << 20).feed(wire):
            _ = ev.data

    def relay_hand() -> None:
        for chunk in wire.split(b"\n\n")[:-1]:
            chunk[6:]

    small = mapping.build(SMALL_RULES, base="passthrough", roots=mapping.REQUEST_ROOTS)
    small_source = {"body": SMALL_BODY, "params": {}, "files": {}}
    bare = mapping.build([], base="passthrough", roots=mapping.REQUEST_ROOTS)
    n_events = len(lines)
    conn = Connection(address="http://engine.test:9100")
    executor = HttpV1Executor(desc, transport=NullTransport(json.dumps(UPSTREAM).encode()))

    def whole_executor() -> None:
        asyncio.run(executor.invoke(conn, CanonicalRequest(op="chat", body=BODY)))

    async def hand_whole_async() -> None:
        request = dumps(hand_request(BODY))
        reply = hand_response(json.loads(json.dumps(UPSTREAM).encode()))
        del request, reply

    def hand_whole() -> None:
        asyncio.run(hand_whole_async())

    from gateway.engine_protocol.dispatch import Runtime, Target
    offering = {"id": "benchmark-chat", "upstream": BODY["model"]}
    service = {"config": {"connection": {"address": conn.address, "protocol": desc.id}, "offerings": [offering]}}
    target = Target("benchmark", service, offering, desc)

    def whole_record_runtime() -> None:
        runtime = Runtime(target, transport=NullTransport(json.dumps(UPSTREAM).encode()))
        asyncio.run(runtime.invoke("chat", BODY))

    rows = {
        "passthrough, no rules": (best(lambda: bare.apply(small_source), 100000, scale=scale), best(lambda: SMALL_BODY, 100000, scale=scale)),
        "5 rules, hand-written shares containers": (best(lambda: small.apply(small_source), 50000, scale=scale), best(lambda: hand_small(SMALL_BODY), 50000, scale=scale)),
        "5 rules, hand-written copies as it does": (best(lambda: small.apply(small_source), 50000, scale=scale), best(lambda: hand_small(SMALL_BODY, True), 50000, scale=scale)),
        "request map (20 messages)": (best(lambda: req_program.apply(source), 20000, scale=scale), best(lambda: hand_request(BODY), 20000, scale=scale)),
        "request map + JSON write": (best(lambda: dumps(req_program.apply(source)), 10000, scale=scale), best(lambda: dumps(hand_request(BODY)), 10000, scale=scale)),
        "response map": (best(lambda: resp_program.apply(up_source), 50000, scale=scale), best(lambda: hand_response(UPSTREAM), 50000, scale=scale)),
        "SSE event, parsed and mapped": (best(mapped_events, 20000, scale=scale) / n_events, best(hand_events, 20000, scale=scale) / n_events),
        "SSE event, relayed unparsed": (best(relay_events, 50000, scale=scale) / n_events, best(relay_hand, 50000, scale=scale) / n_events),
        "whole invoke (loop + stub transport)": (best(whole_executor, 500, 7, scale=scale), best(hand_whole, 500, 7, scale=scale)),
        "record connection + whole invoke": (best(whole_record_runtime, 500, 7, scale=scale), best(hand_whole, 500, 7, scale=scale)),
    }
    return {name: {"executor_us": round(a, 3), "hand_written_us": round(b, 3), "ratio": round(a / b, 2)} for name, (a, b) in rows.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    out = measure()
    if args.json:
        print(json.dumps(out, indent=2))
        return
    print(f"{'case':44} {'executor µs':>12} {'hand µs':>10} {'ratio':>7}")
    for name, row in out.items():
        print(f"{name:44} {row['executor_us']:12.3f} {row['hand_written_us']:10.3f} {row['ratio']:7.2f}")


if __name__ == "__main__":
    main()
