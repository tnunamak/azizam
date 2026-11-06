"""Criterion 7: a second offering (molecule smell inference) runs through the same runner with no core change.

The offering is a test-only http-v1 description (tests/fixtures/engine_protocol/d0/smell.yml) served by an in-process
fake. It is driven by the runner the audio offering uses (``jobs.run``); the only difference is the description file.
The word "smell" must not appear in the code of ``gateway/`` (comments and docstrings are allowed to mention it).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tokenize
from pathlib import Path
from typing import Callable

import httpx
import pytest

from gateway import media_jobs
from gateway.engine_protocol import description, jobs
from gateway.engine_protocol.adapter import AdapterRegistry
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.errors import ProtocolError
from gateway.engine_protocol.transport import HttpxTransport
from gateway.engine_protocol.types import Connection, Timeouts
from tests.support.fake_upstream import HandlerTransport
from tests.test_operation_runner import Runtime, _new_job, jobs_db  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parent.parent
SMELL = ROOT / "tests" / "fixtures" / "engine_protocol" / "d0" / "smell.yml"
CONN = Connection(address="http://smell.test:9000", timeouts=Timeouts(connect_s=10, first_byte_s=30, total_s=30))
VECTOR = [0.25, -1.5, 3.0, 0.0]
MOLECULE = json.dumps({"smiles": "CCO", "odour": ["alcohol", "sweet"]}, sort_keys=True).encode("utf-8")


def _runtime(answer: Callable[[httpx.Request], httpx.Response]) -> tuple[Runtime, MemoryAssetStore]:
    doc = description.parse_description_text(SMELL.read_text(encoding="utf-8"), source="smell.yml")
    store = MemoryAssetStore()
    transport = HttpxTransport.for_tests(HandlerTransport(answer))
    adapter = AdapterRegistry({doc.id: doc}, transport=transport, assets=store).adapter_for(doc.id)
    return Runtime(adapter, CONN, {}, (0.5, 5.0)), store


def _engine(states: list[str], posts: list) -> Callable[[httpx.Request], httpx.Response]:
    remaining = list(states)

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/infer":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"job": {"id": "smell-1"}})
        if request.method == "GET" and path == "/v1/infer/smell-1":
            state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return httpx.Response(200, json={"state": state})
        if request.method == "GET" and path == "/v1/infer/smell-1/molecule":
            return httpx.Response(200, content=MOLECULE, headers={"content-type": "application/octet-stream"})
        return httpx.Response(404)

    return answer


def test_the_smell_offering_completes_through_the_same_runner(jobs_db):  # noqa: F811 - fixture
    posts: list = []
    runtime, store = _runtime(_engine(["queued", "working", "ready"], posts))
    job_id = _new_job(jobs_db)
    refs = asyncio.run(jobs.run(runtime, "smell.infer", {"vector": VECTOR}, job_id=job_id))

    assert posts == [{"input": {"vector": VECTOR}}], "the vector did not reach the engine's input"
    assert len(refs) == 1
    ref = refs[0]
    assert ref.content_type == "application/octet-stream"
    assert ref.size == len(MOLECULE)
    data = asyncio.run(store.read(ref, max_bytes=10_000))
    assert hashlib.sha256(data).hexdigest() == hashlib.sha256(MOLECULE).hexdigest()
    stored = media_jobs.get(job_id, audience="internal", path=jobs_db) or {}
    assert (stored.get("metadata") or {}).get("protocol_state") == "succeeded"


def test_the_smell_offering_never_resubmits_after_a_lost_reply(jobs_db):  # noqa: F811 - fixture
    posts: list = []

    def lost(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        raise httpx.ReadError("the reply to the submission was lost", request=request)

    lost_runtime, _ = _runtime(lost)
    healthy_runtime, _ = _runtime(_engine(["ready"], posts))
    job_id = _new_job(jobs_db)

    with pytest.raises(ProtocolError) as first:
        asyncio.run(jobs.run(lost_runtime, "smell.infer", {"vector": VECTOR}, job_id=job_id))
    assert first.value.code == "submission_unknown"

    # The engine is healthy now, but the job's submit may have started: the later run must not send it again.
    with pytest.raises(ProtocolError) as second:
        asyncio.run(jobs.run(healthy_runtime, "smell.infer", {"vector": VECTOR}, job_id=job_id))
    assert second.value.code == "submission_unknown"
    assert len(posts) == 1, "the submit was sent again after its reply was lost"


def test_no_core_module_names_the_smell_offering():
    """The offering lives in its description and tests; no code token in gateway/ mentions it."""
    hits = []
    for path in sorted((ROOT / "gateway").rglob("*.py")):
        with path.open("rb") as handle:
            for token in tokenize.tokenize(handle.readline):
                if token.type == tokenize.COMMENT or (token.type == tokenize.STRING and token.string.startswith(('"""', "'''"))):
                    continue  # comments and docstrings may describe anything
                if "smell" in token.string.lower():
                    hits.append(f"{path.relative_to(ROOT)}:{token.start[0]}: {token.string[:60]!r}")
    assert hits == []
