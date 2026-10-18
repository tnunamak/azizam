"""Contract section 10.2: a second engine added as data only.

``tests/fixtures/engine_protocol/acme`` holds a made-up API unlike the OpenAI shape (its own paths, a nested answer,
an NDJSON stream, a header key, a multipart upload and a submit-poll-fetch job) as a protocol description plus an engine
record. It is served here by a fake upstream and driven through the generic adapter: no gateway code names it. Golden
request and response bytes are checked in; ``AZIZAM_UPDATE_GOLDEN=1`` rewrites them for review.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import pytest

from gateway.engine_protocol import adapter, description
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.budgets import Budgets
from gateway.engine_protocol.errors import BudgetExceeded, DescriptionError, RedirectRefused, UpstreamError
from gateway.engine_protocol.executor import HttpV1Executor
from gateway.engine_protocol.http_adapter import HttpV1Adapter
from gateway.engine_protocol.transport import HttpxTransport
from gateway.engine_protocol.types import CanonicalRequest, JobState, Reply
from tests.support.fake_upstream.loopback import LoopbackServer
from tests.support.fake_upstream.transport import StreamingASGITransport
from tests.support.second_engine_fake import KEY, AcmeFake

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "engine_protocol"
ACME = FIXTURES / "acme"
GOLDEN = ACME / "golden"
ROOT = Path(__file__).resolve().parent.parent
MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Say hello"}]


def run(coro):
    return asyncio.run(coro)


def golden(name: str, actual: bytes) -> None:
    path = GOLDEN / name
    if os.environ.get("AZIZAM_UPDATE_GOLDEN") == "1":
        path.write_bytes(actual)
    assert path.exists(), f"golden file {name} is missing; run with AZIZAM_UPDATE_GOLDEN=1 and review it"
    assert actual == path.read_bytes(), f"{name} differs from its golden file"


def pretty(doc) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def head(recorded: dict) -> bytes:
    """The request line and the headers a reader needs to judge, the key masked."""
    keep = ("content-type", "accept-encoding", "x-acme-key")
    lines = [f'{recorded["method"]} {recorded["target"]}']
    for name in keep:
        if name in recorded["headers"]:
            value = "<key>" if name == "x-acme-key" else recorded["headers"][name]
            lines.append(f"{name}: {value}")
    return ("\n".join(lines) + "\n").encode()


class Rig:
    def __init__(self, fake: AcmeFake | None = None, *, real_socket: bool = False) -> None:
        self.fake = fake or AcmeFake()
        self.assets = MemoryAssetStore()
        self.description = description.parse_description_text((ACME / "acme.yml").read_text(), source="acme.yml")
        self.registry = adapter.AdapterRegistry({"acme": self.description}, assets=self.assets)
        record = json.loads((ACME / "engine.json").read_text())
        self.conn = adapter.connection_from_record(record["connection"], {"ACME_KEY": KEY}.get)
        if real_socket:
            self.server = LoopbackServer(self.fake).start()
            self.conn = adapter.connection_from_record({**record["connection"], "address": self.server.base_url}, {"ACME_KEY": KEY}.get)
            transport = HttpxTransport(gateway_ports=())
        else:
            self.server = None
            transport = HttpxTransport.for_tests(StreamingASGITransport(self.fake))
        self.registry = adapter.AdapterRegistry({"acme": self.description}, transport=transport, assets=self.assets)
        self.adapter: HttpV1Adapter = self.registry.adapter_for("acme")  # type: ignore[assignment]

    def close(self) -> None:
        if self.server is not None:
            self.server.stop()


@pytest.fixture()
def rig():
    made = Rig()
    yield made
    made.close()


# --- no code change ------------------------------------------------------------------------------


def test_adding_the_second_engine_changed_no_gateway_code() -> None:
    assert isinstance(Rig().adapter, HttpV1Adapter)
    named = [str(p.relative_to(ROOT)) for p in [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py"))] if re.search("acme", p.read_text(), re.I)]
    assert named == []
    record = json.loads((ACME / "engine.json").read_text())
    assert record["connection"]["protocol"] == "acme"
    assert sorted(p.name for p in ACME.iterdir() if p.is_file()) == ["acme.yml", "chat.upstream.json", "engine.json", "stream.upstream.ndjson"]


# --- chat: nested response, enum table, conditionals ---------------------------------------------


def test_chat_request_and_reply_bytes(rig: Rig) -> None:
    request = CanonicalRequest(op="chat", body={"model": "acme-1", "messages": MESSAGES, "temperature": 3.5, "max_tokens": 64, "ignored": True})
    reply = run(rig.adapter.invoke(rig.conn, "chat", request))
    assert isinstance(reply, Reply) and reply.status == 200
    sent = rig.fake.requests[0]
    golden("chat.request.head.txt", head(sent))
    golden("chat.request.body.json", sent["body"] + b"\n")
    golden("chat.reply.json", pretty(reply.body))
    assert reply.body["choices"][0]["message"]["content"] == "Hello"
    assert KEY.encode() not in sent["body"]
    assert sent["headers"]["x-acme-key"] == KEY


def test_chat_without_optional_fields_sends_the_defaults_only(rig: Rig) -> None:
    run(rig.adapter.invoke(rig.conn, "chat", CanonicalRequest(op="chat", body={"model": "acme-1", "messages": MESSAGES})))
    golden("chat.minimal.request.body.json", rig.fake.requests[0]["body"] + b"\n")


def test_chat_stream_turns_ndjson_lines_cut_across_chunks_into_chunk_objects(rig: Rig) -> None:
    async def go():
        reply = await rig.adapter.invoke(rig.conn, "chat", CanonicalRequest(op="chat", body={"model": "acme-1", "messages": MESSAGES, "stream": True}))
        return [event async for event in reply]
    events = run(go())
    golden("stream.request.body.json", rig.fake.requests[0]["body"] + b"\n")
    golden("stream.events.jsonl", b"".join(json.dumps(e, sort_keys=True).encode() + b"\n" for e in events))
    assert [e["choices"][0]["delta"]["content"] for e in events] == ["Hel", "lo", ""]
    assert events[-1]["choices"][0]["finish_reason"] == "stop" and events[-1]["usage"] == {"prompt_tokens": 5, "completion_tokens": 2}


def test_inventory_health_and_a_wrong_key(rig: Rig) -> None:
    inventory = run(rig.adapter.inventory(rig.conn))
    assert inventory.ids == ("acme-1", "acme-2")
    assert rig.fake.requests[0]["target"] == "/api/v3/catalog?kind=text"
    assert run(rig.adapter.health(rig.conn)).state == "ok"
    bad = adapter.connection_from_record({**json.loads((ACME / "engine.json").read_text())["connection"]}, {"ACME_KEY": "wrong"}.get)
    health = run(rig.adapter.health(bad))
    assert health.state == "failing" and "Acme rejected the X-Acme-Key header." in health.detail
    with pytest.raises(UpstreamError) as raised:
        run(rig.adapter.invoke(bad, "inventory", CanonicalRequest(op="inventory")))
    assert raised.value.advice_code == "acme_key_rejected"


def test_multipart_upload_from_a_canonical_asset(rig: Rig) -> None:
    clip = rig.assets.put(b"RIFF-clip", "audio/wav", name="clip.wav")
    rig.registry  # the same registry serves it
    ex = HttpV1Executor(rig.description, transport=HttpxTransport.for_tests(StreamingASGITransport(rig.fake)), assets=rig.assets)
    ex.boundary_factory = lambda: "ACMEBOUNDARY"
    reply = run(ex.invoke(rig.conn, CanonicalRequest(op="transcribe", body={"language": "en"}, files={"file": clip})))
    assert reply.body == {"text": "hello from audio"}
    sent = rig.fake.requests[0]
    golden("transcribe.request.head.txt", head(sent))
    golden("transcribe.request.body.bin", sent["body"])


# --- the render job --------------------------------------------------------------------------------


def test_render_job_submit_poll_fetch_and_the_bytes_of_each_step(rig: Rig) -> None:
    async def go():
        ref = await rig.adapter.submit(rig.conn, "render", CanonicalRequest(op="render", body={"prompt": "a red fox", "size": "256x256"}))
        states = []
        while True:
            status = await rig.adapter.poll(rig.conn, ref)
            states.append(status.state)
            if status.state is JobState.succeeded:
                break
        return ref, status, states, await rig.adapter.fetch_assets(rig.conn, ref, status)
    ref, status, states, refs = run(go())
    assert ref.id == "r-7" and states == [JobState.pending, JobState.running, JobState.succeeded]
    assert status.assets == ("o1", "o 2")
    assert [rig.assets.get(r) for r in refs] == [b"\x89PNG-one", b"\x89PNG-two"]
    assert all(r.content_type == "image/png" for r in refs)
    submit = rig.fake.requests[0]
    golden("render.submit.request.head.txt", head(submit))
    golden("render.submit.request.body.json", submit["body"] + b"\n")
    golden("render.requests.txt", "".join(f'{r["method"]} {r["target"]}\n' for r in rig.fake.requests).encode())


def test_render_defaults_and_cancel_and_recover(rig: Rig) -> None:
    async def go():
        ref = await rig.adapter.submit(rig.conn, "render", CanonicalRequest(op="render", body={"prompt": "p"}))
        await rig.adapter.cancel(rig.conn, ref)
        return ref
    ref = run(go())
    assert json.loads(rig.fake.requests[0]["body"]) == {"spec": {"text": "p", "dims": "512x512"}}
    assert rig.fake.cancelled and rig.fake.requests[-1]["method"] == "DELETE"
    assert run(rig.adapter.recover(rig.conn, ref)).state is JobState.pending


def test_a_failed_render_carries_acmes_own_reason() -> None:
    class Failing(AcmeFake):
        async def _json(self, send, status, doc):
            if doc.get("phase") == "ERRORED":
                doc = {**doc, "problem": {"text": "model not loaded"}}
            await super()._json(send, status, doc)
    rig = Rig(Failing(phases=("ERRORED",)))
    status = run(_one_poll(rig))
    assert status.state is JobState.failed and status.detail == "model not loaded"


async def _one_poll(rig: Rig):
    ref = await rig.adapter.submit(rig.conn, "render", CanonicalRequest(op="render", body={"prompt": "p"}))
    return await rig.adapter.poll(rig.conn, ref)


# --- the same engine over a real socket, through the address-checked transport ---------------------


def test_the_whole_path_over_a_real_loopback_socket() -> None:
    rig = Rig(real_socket=True)
    try:
        reply = run(rig.adapter.invoke(rig.conn, "chat", CanonicalRequest(op="chat", body={"model": "acme-1", "messages": MESSAGES})))
        assert reply.body["choices"][0]["message"]["content"] == "Hello"
        assert run(rig.adapter.inventory(rig.conn)).ids == ("acme-1", "acme-2")

        async def stream():
            r = await rig.adapter.invoke(rig.conn, "chat", CanonicalRequest(op="chat", body={"model": "acme-1", "messages": MESSAGES, "stream": True}))
            return [e["choices"][0]["delta"]["content"] async for e in r]
        assert run(stream()) == ["Hel", "lo", ""]
        assert rig.fake.requests[0]["body"] == (GOLDEN / "chat.minimal.request.body.json").read_bytes().rstrip(b"\n")
    finally:
        rig.close()


# --- negative fixtures -----------------------------------------------------------------------------

NEGATIVE = FIXTURES / "negative"
EXPECTATIONS = json.loads((NEGATIVE / "expectations.json").read_text())


@pytest.mark.parametrize("name", sorted(EXPECTATIONS))
def test_invalid_descriptions_are_refused_with_a_clear_message(name: str) -> None:
    with pytest.raises(DescriptionError) as raised:
        description.parse_description_text((NEGATIVE / name).read_text(), source=name)
    message = str(raised.value)
    assert name in message and EXPECTATIONS[name] in message, message


def test_every_negative_fixture_has_an_expectation() -> None:
    on_disk = {p.name for p in NEGATIVE.glob("*.yml") if p.name != "runtime.yml"}
    assert on_disk == set(EXPECTATIONS)


class _Hostile:
    """Serves what the runtime negatives need."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []

    async def __call__(self, scope, receive, send) -> None:
        self.seen.append((scope["path"], {k.decode(): v.decode() for k, v in scope["headers"]}))
        path = scope["path"]
        if path == "/huge":
            body = b'{"x":"' + b"a" * 5000 + b'"}'
        elif path == "/deep":
            body = b"[" * 50 + b"]" * 50
        elif path == "/hop":
            await send({"type": "http.response.start", "status": 302, "headers": [(b"location", b"http://evil.example/steal")]})
            return await send({"type": "http.response.body", "body": b""})
        else:
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
            if path == "/fat":
                await send({"type": "http.response.body", "body": b"data: " + b"z" * 5000 + b"\n\n"})
            else:
                for _ in range(100):
                    await send({"type": "http.response.body", "body": b"data: " + b"y" * 40 + b"\n\n", "more_body": True})
                await send({"type": "http.response.body", "body": b""})
            return
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


def _runtime(budgets: Budgets):
    hostile = _Hostile()
    desc = description.parse_description_text((NEGATIVE / "runtime.yml").read_text(), source="runtime.yml", budgets=budgets)
    ex = HttpV1Executor(desc, transport=HttpxTransport.for_tests(StreamingASGITransport(hostile)), budgets=budgets)
    conn = adapter.connection_from_record({"address": "http://acme.test:9100", "credentials": {"inference": {"secret_ref": "K"}}}, {"K": KEY}.get)
    return ex, conn, hostile


@pytest.mark.parametrize("op, budgets, budget_name", [
    ("huge", Budgets(max_response_bytes=1000), "max_response_bytes"),
    ("deep", Budgets(max_json_depth=32), "max_json_depth"),
])
def test_runtime_budgets_stop_a_hostile_answer(op: str, budgets: Budgets, budget_name: str) -> None:
    ex, conn, _ = _runtime(budgets)
    with pytest.raises(BudgetExceeded) as raised:
        run(ex.invoke(conn, CanonicalRequest(op=op)))
    assert raised.value.budget == budget_name


def test_runtime_stream_budgets() -> None:
    ex, conn, _ = _runtime(Budgets(max_event_bytes=1000))

    async def drain(op):
        async for _ in await ex.invoke(conn, CanonicalRequest(op=op)):
            pass
    with pytest.raises(BudgetExceeded) as raised:
        run(drain("fat_event"))
    assert raised.value.budget == "max_event_bytes"
    ex, conn, _ = _runtime(Budgets(max_stream_bytes=1000))
    with pytest.raises(BudgetExceeded) as raised:
        run(drain("endless"))
    assert raised.value.budget == "max_stream_bytes"


def test_a_cross_origin_redirect_is_refused_and_the_key_stays_home() -> None:
    ex, conn, hostile = _runtime(Budgets())
    with pytest.raises(RedirectRefused) as raised:
        run(ex.invoke(conn, CanonicalRequest(op="hop")))
    assert [path for path, _ in hostile.seen] == ["/hop"]
    assert KEY not in str(raised.value) and "evil.example" in str(raised.value)
