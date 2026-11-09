import asyncio
from tests.support.video_rig import *
def test_probe(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.engine.view_started, w.engine.view_release = asyncio.Event(), asyncio.Event()
        await drive(w, vid, "completed", wait=False)
        await asyncio.wait_for(w.engine.view_started.wait(), timeout=5)
        pending = asyncio.ensure_future(w.delete(vid))
        await asyncio.sleep(0.05)
        j = media_jobs.get(job_id_of(vid), audience="internal", path=media_jobs.DB_PATH)
        info = (j["status"], list((j.get("metadata") or {}).keys()), (j.get("metadata") or {}).get("cancel_requested_at"))
        w.engine.view_release.set()
        r = await asyncio.wait_for(pending, timeout=5)
        j = media_jobs.get(job_id_of(vid), audience="internal", path=media_jobs.DB_PATH)
        assert 0, (info, r.status_code, j and j["status"], list(w.video_files()))
    run(tmp_path, exercise)
