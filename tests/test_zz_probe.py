import traceback
import proxy
from tests.test_workflow_run_contracts import *  # noqa


def test_probe(world, monkeypatch):
    seen = []
    monkeypatch.setattr(proxy, "_log_media_job_crash", lambda job_id, exc: seen.append("".join(traceback.format_exception(exc))))
    manifest = manifest_of(IMAGE_ID)
    serve_engine(world, FakeGraphEngine(manifest, png_bytes((9, 9, 9)), "image/png", "out.png"))
    call("post", f"/gateway/v1/workflows/{IMAGE_ID}/run", json={"inputs": {"prompt": "x"}}, headers={"Prefer": "wait=20"})
    assert not seen, seen[0][-2500:]
