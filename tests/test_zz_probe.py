import traceback
from tests.test_workflow_run_contracts import *  # noqa
from gateway import workflow_run as wr

def test_probe(world):
    publish(world.db, owner_workflow(True))
    orig = wr.WorkflowRunCore.submit_run
    seen = []
    async def wrapped(self, *a, **k):
        try:
            return await orig(self, *a, **k)
        except BaseException:
            seen.append(traceback.format_exc()); raise
    world.monkeypatch.setattr(wr.WorkflowRunCore, "submit_run", wrapped)
    serve_engine(world, FakeGraphEngine(manifest_of(IMAGE_ID), png_bytes((9, 9, 9)), "image/png", "out.png"))
    call("post", f"/gateway/v1/workflows/{OWNER_ID}/run", json={"inputs": {"prompt": "x"}}, headers={"Prefer": "wait=20"})
    assert not seen, seen[0][-1800:]
