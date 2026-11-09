import asyncio
from tests.support.video_rig import *
def test_probe(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.engine.view_error = True
        await drive(w, vid, "completed", wait=False)
        await settled(w, vid)
        assert 0, str(w.view(vid).failure)
    run(tmp_path, exercise)
