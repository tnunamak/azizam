import asyncio
from tests.support.video_rig import *
def test_probe(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "running")
        w.engine.history_mode = "wrong_shape"
        await drive(w, vid, "missing", wait=False)
        for _ in range(100):
            await asyncio.sleep(0.01)
        assert 0, (str(w.view(vid)), len(w.engine.bad_history), [r.url.path for r in w.engine.requests][-6:])
    run(tmp_path, exercise)
