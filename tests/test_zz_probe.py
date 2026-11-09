import asyncio
from tests.support.video_rig import *
def test_probe(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.engine.view_error = True
        await drive(w, vid, "completed", wait=False)
        await settled(w, vid)
        assert 0, ((await w.status(vid)).json(), (await w.content(vid)).json(), w.view(vid))
    run(tmp_path, exercise)
