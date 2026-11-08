import asyncio, traceback
import tests.test_adapter_differential_s7_mesh_rows as m
from tests.test_adapter_differential_s7_mesh_rows import rig  # noqa
from gateway.frontend_shapes import _wire

def test_x(rig, monkeypatch):
    orig = _wire.split_form
    async def spy(request):
        try:
            return await orig(request)
        except BaseException as e:
            print("SPLITFORM RAISED", type(e).__mro__, e)
            traceback.print_exc()
            raise
    monkeypatch.setattr(_wire, "split_form", spy)
    async def go(c):
        r = await c.post(m.RAW, content=b"not a multipart body", headers={"content-type": "multipart/form-data; boundary=zz"})
        print("RESULT", r.status_code, r.text[:200])
        return {}
    m.run(rig, go)
