"""A saved prompt is harvested once without losing the rest of its requested batch."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from gateway import media_jobs
from gateway.engine_protocol import graph_round
from gateway.engine_protocol import jobs as protocol_jobs
from gateway.node_graph_runner import GraphHost, NodeGraphRunner, Plan


@pytest.fixture
def harvest(monkeypatch):
    def run(*, n, returned, resume, media_type="image/png", batched=True, per_prompt=None):
        stored, calls, metadata, commits = [], [], [], []
        limits = object()
        outputs = {"out": {"media_type": media_type}}
        ctx = SimpleNamespace(
            job_id="mj_resume", inputs={"batch_size": per_prompt} if per_prompt else {}, generated={}, media={},
            # The image shape sends the count as ``options.n``; it is never a workflow input (shapes.py, the image options).
            payload={"body": {"options": {"n": n} if batched else {}}},
            pin=SimpleNamespace(contract={"inputs": {}, "outputs": outputs}, binding={},
                                envelope=SimpleNamespace(binding_key="b")),
            record_result_metadata=metadata.append, record_progress=lambda _percent: None,
        )

        class Batching:
            async def plan(self, session, ctx, inputs, options):
                assert resume is None, "recovery must not rerun engine batch sizing"
                total = options["n"] * inputs.get("batch_size", 1)
                return Plan(total=total, max_rounds=total)

            async def target(self, plan, session, round_no, remaining):
                return remaining

            def probe(self, plan, session, round_no):
                return None

            def round_inputs(self, plan, target):
                return {"n": target}

            async def measured(self, plan, session, round_no, target, probe):
                pass

        @asynccontextmanager
        async def opened(conn):
            yield object()

        adapter = SimpleNamespace(
            label="Engine", poll_for=lambda op: None, open=opened,
            compile=lambda op, request, edit=None: ({}, {}),
        )
        runtime = SimpleNamespace(
            request=lambda op, body: SimpleNamespace(params={}), connection_for=lambda op: None,
        )
        host = GraphHost(
            engine=None, identity=None, problem=None, adapter=None, connection=None,
            limits=lambda: limits, max_wait_s=lambda: 100, control_timeout=lambda: 1,
            headers=lambda ctx: {}, batching=lambda ctx, adapter: Batching() if batched else None, stages={},
        )
        runner = NodeGraphRunner("b", "a", host)

        async def round_result(*args, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(prompt_id=resume or "fresh", files={"out": tuple(returned)}, names=())

        async def store(ctx, adapter, files, name, result_limits):
            assert result_limits is limits
            assert name == "out"
            stored.extend(files)
            return SimpleNamespace(results={})

        monkeypatch.setattr(graph_round, "run_round", round_result)
        monkeypatch.setattr(media_jobs, "get", lambda *args, **kwargs: {})
        monkeypatch.setattr(media_jobs, "update_status", lambda *args, **kwargs: None)
        monkeypatch.setattr(protocol_jobs, "_commit_bundle", lambda *args, **kwargs: commits.append(kwargs))
        monkeypatch.setattr(runner, "_store", store)

        asyncio.run(runner._rounds(ctx, adapter, runtime, "generate", {}, resume=resume))

        assert len(calls) == 1
        assert calls[0]["resume_prompt_id"] == resume
        assert calls[0]["limits"] is limits
        assert calls[0]["contract_outputs"] is outputs
        assert len(commits) == 1
        assert commits[0]["clear_marker"] is True
        assert metadata[-1]["rounds"] == 1
        assert metadata[-1]["returned"] == len(stored)
        return stored
    return run


def test_resumed_three_image_batch_keeps_every_file(harvest):
    assert harvest(n=3, returned=[b"A", b"B", b"C"], resume="saved") == [b"A", b"B", b"C"]


def test_fresh_three_image_batch_still_keeps_every_file(harvest):
    assert harvest(n=3, returned=[b"A", b"B", b"C"], resume=None) == [b"A", b"B", b"C"]


def test_resumed_single_video_keeps_one_file(harvest):
    assert harvest(n=1, returned=[b"video", b"extra-video"], resume="saved-video",
                   media_type="video/mp4", batched=False) == [b"video"]


def test_resumed_batch_caps_extra_files_at_requested_count(harvest):
    assert harvest(n=3, returned=[b"A", b"B", b"C", b"extra"], resume="saved") == [b"A", b"B", b"C"]


def test_resumed_batch_keeps_every_file_of_a_batch_size_input(harvest):
    assert harvest(n=2, per_prompt=2, returned=[b"A", b"B", b"C", b"D", b"extra"], resume="saved") == [b"A", b"B", b"C", b"D"]
