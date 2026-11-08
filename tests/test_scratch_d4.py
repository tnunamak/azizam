import asyncio, json
from tests.test_adapter_differential_s7_mesh_rows import *  # noqa
from tests.test_adapter_differential_s7_mesh_rows import rig  # noqa

def test_x(rig):
    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        print("CREATED", created.status_code, created.text)
        jid = job_id_of(created)
        for _ in range(10):
            await asyncio.sleep(0.3)
            r = await c.get(f"{JOBS}/{jid}")
            print("JOB", r.status_code, r.text[:600])
        return {}
    run(rig, go)
