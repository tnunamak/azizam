"""S7 goldens for the mesh route (the ``mr`` cells of the S7 matrix), recorded on today's ``POST /v1/meshes/generate``.

The route is a frontend over its own pool lease and engine call (``gateway/mesh_api.py``), not over the run core, so the
existing mesh drivers (``tests/test_mesh_route.py``, ``tests/test_mesh_async_job.py``) are the rig. Each cell here is one
route outcome of the S7 matrix, recorded as the wire answer, the receipts written, what the engine received, the job rows
and the lease state. Nothing masks a status, a code, a header, an engine request or a byte; only the random multipart
boundary, request ids that the harness numbers itself, and job ids are replaced.

The replay cells record what today's route does with an ``Idempotency-Key``: nothing. It has no replay, so the golden is
the two runs it makes. The ``ml`` cells (LocalAI's ``/v1/3d/generations``) have no route today and are not recorded.

``tests/fixtures/adapter_differential/s7_mr_cells.json`` is the record. To re-record, run the module with
``--junit-xml-out`` and ``-o junit_logging=all`` and take the JSON between the ``S7-MR-RECORD`` markers.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.mesh_api import MESH_PATH
from tests.test_mesh_async_job import AsyncHarness, dribbling_engine, job_id_of, run
from tests.test_mesh_route import PNG, Harness, _call, _Denied, _pool_held

GOLDEN = Path(__file__).parent / "fixtures" / "adapter_differential" / "s7_mr_cells.json"
KEY = {"Idempotency-Key": "golden-key-1"}
MARK = "S7-MR-RECORD"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _engine_requests(harness: Harness) -> list[dict[str, Any]]:
    out = []
    for request in harness.downstream:
        content_type = request.headers["content-type"]
        boundary = content_type.split("boundary=", 1)[1]
        out.append({"method": request.method, "url": str(request.url), "content_type": content_type.replace(boundary, "BOUNDARY"),
                    "body": _b64(request.content.replace(boundary.encode(), b"BOUNDARY"))})
    return out


def _answer(response: Any) -> dict[str, Any]:
    """Status, headers and body. A JSON body is kept as parsed JSON so a masked job id can sit in it; any other body
    (the GLB) is kept as its exact bytes."""
    content_type = response.headers.get("content-type")
    is_json = bool(content_type and content_type.startswith("application/json"))
    return {"status": response.status_code, "content_type": content_type,
            "request_id": response.headers.get("x-request-id"), "retry_after": response.headers.get("retry-after"),
            "location": response.headers.get("location"),
            "json": json.loads(response.content) if is_json else None,
            "body": None if is_json else _b64(response.content)}


def _state(harness: Harness) -> dict[str, Any]:
    return {"receipts": [{"request_id": r["request_id"], "outcome": list(r["outcome"]) if r["outcome"] else None, "exc": r["exc"]}
                         for r in harness.receipts],
            "engine_requests": _engine_requests(harness), "claims_left": harness.leases.active_claims()}


def _sync(harness: Harness, **kwargs: Any) -> dict[str, Any]:
    response = TestClient(harness.app()).post(MESH_PATH, **kwargs)
    return {"answer": _answer(response), **_state(harness)}


def _image(**fields: str) -> dict[str, Any]:
    return {"data": fields, "files": {"image": ("photo.png", PNG, "image/png")}}


def _job(harness: AsyncHarness, job_id: str) -> dict[str, Any]:
    job = harness.job(job_id)
    metadata = job.get("metadata") or {}
    return {"status": job["status"], "error": job.get("error"), "assets": len(job.get("assets") or []),
            "cancel_requested": bool(metadata.get("cancel_requested_at"))}


def _async_state(harness: AsyncHarness) -> dict[str, Any]:
    return {**_state(harness.route), "stored": [_b64(item) for item in harness.stored]}


def _cancel(harness: AsyncHarness, job_id: str) -> dict[str, Any]:
    import proxy
    saved = proxy.media_jobs.DB_PATH
    proxy.media_jobs.DB_PATH = harness.jobs
    try:
        done = proxy._cancel_media_job(job_id, audience="client")  # the cancel route's own function
    except ValueError as error:                                    # a finished job refuses the cancel
        return {"refused": str(error)}
    finally:
        proxy.media_jobs.DB_PATH = saved
    return {"status": done["status"], "cancel_requested": done.get("cancel_requested")}


# ------------------------------------------------------------------------------------------------ the cells
def cell_success() -> dict[str, Any]:
    return _sync(Harness(), **_image(seed="42"))


def cell_upstream_failure() -> dict[str, Any]:
    server, down = Harness(), Harness()
    server.downstream_mode = 502
    down.downstream_mode = httpx.ConnectError("golden engine unreachable")
    return {"engine_502": _sync(server, **_image()), "engine_unreachable": _sync(down, **_image())}


def cell_validation_refusal() -> dict[str, Any]:
    out = {}
    for name, kwargs in (
        ("seed_negative", _image(seed="-1")),
        ("missing_image", {"data": {"seed": "1"}}),
        ("not_multipart", {"content": b"not a multipart body", "headers": {"content-type": "multipart/form-data; boundary=zz"}}),
    ):
        out[name] = _sync(Harness(), **kwargs)
    return out


def cell_policy_refusal() -> dict[str, Any]:
    harness = Harness()
    harness.route_policy = lambda _auth: _Denied()
    return _sync(harness, **_image(seed="42"))


def cell_admission_refusal() -> dict[str, Any]:
    """A run holds the pool; a second run is refused at once with the busy answer and Retry-After."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        harness = AsyncHarness(Path(tmp))

        async def scenario() -> dict[str, Any]:
            first = await harness.submit()
            await harness.engine_started()
            second = await harness.submit(prefer=False)
            harness.let_engine_return()
            await harness.idle()
            return {"first": _answer(first), "second": _answer(second), "job": _job(harness, job_id_of(first))}

        return {**run(scenario()), **_async_state(harness)}


def cell_replay_same() -> dict[str, Any]:
    harness = Harness()
    client = TestClient(harness.app())
    first = client.post(MESH_PATH, headers=KEY, **_image(seed="42"))
    second = client.post(MESH_PATH, headers=KEY, **_image(seed="42"))
    return {"first": _answer(first), "second": _answer(second), **_state(harness)}


def cell_replay_conflict() -> dict[str, Any]:
    harness = Harness()
    client = TestClient(harness.app())
    first = client.post(MESH_PATH, headers=KEY, **_image(seed="42"))
    second = client.post(MESH_PATH, headers=KEY, **_image(seed="43"))
    return {"first": _answer(first), "second": _answer(second), **_state(harness)}


def cell_wait_timeout() -> dict[str, Any]:
    """The engine dribbles bytes past its configured time: the sync form answers its timeout, the job form fails the job."""
    import tempfile
    out: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, prefer in (("sync", False), ("job", True)):
            (Path(tmp) / name).mkdir()
            harness = AsyncHarness(Path(tmp) / name)
            dribbling_engine(harness, timeout=0.3)

            async def scenario() -> dict[str, Any]:
                response = await harness.submit(prefer=prefer)
                await harness.idle()
                body = {"answer": _answer(response)}
                if prefer:
                    body["job"] = _job(harness, job_id_of(response))
                return body

            out[name] = {**run(scenario()), **_async_state(harness)}
    return out


def cell_cancel_queued() -> dict[str, Any]:
    """Cancel while the engine works (the job is admitted at once, so there is no earlier state to cancel)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        harness = AsyncHarness(Path(tmp))

        async def scenario() -> dict[str, Any]:
            submit = await harness.submit()
            job_id = job_id_of(submit)
            await harness.engine_started()
            cancel = _cancel(harness, job_id)
            again = _cancel(harness, job_id)
            during = _job(harness, job_id)
            harness.let_engine_return()
            await harness.idle()
            return {"submit": _answer(submit), "cancel": cancel, "cancel_again": again, "during": during,
                    "after": _job(harness, job_id), "pool_held_after": _pool_held(harness.route)}

        return {**run(scenario()), **_async_state(harness)}


def cell_cancel_finished() -> dict[str, Any]:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        harness = AsyncHarness(Path(tmp))
        harness.hold_engine = False

        async def scenario() -> dict[str, Any]:
            submit = await harness.submit()
            job_id = job_id_of(submit)
            await harness.idle()
            before = _job(harness, job_id)
            cancel = _cancel(harness, job_id)
            again = _cancel(harness, job_id)
            return {"submit": _answer(submit), "before": before, "cancel": cancel, "cancel_again": again,
                    "after": _job(harness, job_id)}

        return {**run(scenario()), **_async_state(harness)}


def cell_disconnect() -> dict[str, Any]:
    """The client leaves after the 202: the job is not cancelled. A sync client that leaves while the engine works closes
    its receipt as cancelled and the pool frees when the engine returns."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        harness = AsyncHarness(Path(tmp))

        async def scenario() -> dict[str, Any]:
            sends = await _call(harness.app, harness.body, harness.boundary, extra_headers=[(b"prefer", b"respond-async")],
                                disconnect_when=lambda: "engine-start" in harness.engine.events)
            start = next(m for m in sends if m["type"] == "http.response.start")
            body = json.loads(b"".join(m.get("body", b"") for m in sends if m["type"] == "http.response.body"))
            await asyncio.sleep(0.1)
            while_running = {"job": _job(harness, body["id"]), "pool_held": _pool_held(harness.route),
                             "receipts": len(harness.route.receipts)}
            harness.let_engine_return()
            await harness.idle()
            return {"status": start["status"], "while_running": while_running, "after": _job(harness, body["id"])}

        return {**run(scenario()), **_async_state(harness)}


CELLS = {
    "success": cell_success, "upstream_failure": cell_upstream_failure, "validation_refusal": cell_validation_refusal,
    "policy_refusal": cell_policy_refusal, "admission_refusal": cell_admission_refusal, "replay_same": cell_replay_same,
    "replay_conflict": cell_replay_conflict, "wait_timeout": cell_wait_timeout, "cancel_queued": cell_cancel_queued,
    "cancel_finished": cell_cancel_finished, "disconnect": cell_disconnect,
}


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


def _masked(value: Any) -> Any:
    """Job ids are random; the Location header and the 202 body carry one. Nothing else is masked."""
    text = json.dumps(value, sort_keys=True)
    text = re.sub(r"mj_[0-9a-f]{32}", "JOB", text)
    return json.loads(text)


@pytest.mark.parametrize("name", sorted(CELLS))
def test_the_mesh_cell_matches_its_recording(name: str) -> None:
    first, second = _masked(CELLS[name]()), _masked(CELLS[name]())
    print(f"{MARK} {name} {json.dumps(first, sort_keys=True)}")
    assert first == second, f"{name} is not deterministic"
    assert name in _golden(), f"{name} has no recording; take it from the {MARK} line"
    assert first == _golden()[name]


def test_the_recording_holds_exactly_the_cells() -> None:
    assert set(_golden()) == set(CELLS)
