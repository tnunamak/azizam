import asyncio, json
from gateway import api, media_jobs
import tests.test_adapter_differential_s7_mesh_rows as m
from tests.test_adapter_differential_s7_mesh_rows import rig  # noqa
SYNC,ASYNC,JOBS,form,job_id_of,run=m.SYNC,m.ASYNC,m.JOBS,m.form,m.job_id_of,m.run

def test_x(rig):
    jobs = media_jobs.DB_PATH
    _, getter, lister, ev, _, canceler = api._build_media_job_providers(
        media_job_creator=None, media_job_getter=None, media_job_lister=None, media_job_event_lister=None,
        media_job_event_appender=None, media_job_canceler=None, media_job_store_path=jobs)
    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        jid = job_id_of(created)
        await asyncio.sleep(1.5)
        print("JOB", json.dumps(getter(jid, audience="client"))[:1500])
        try:
            print("CANCEL", canceler(jid, audience="client"))
        except Exception as e:
            print("CANCELERR", type(e), e)
        print("EVENTS", json.dumps(ev(jid, audience="client", limit=20))[:800])
        h = rig
        h.downstream_mode = 502
        c2 = await c.post(SYNC, headers=ASYNC, **form())
        await asyncio.sleep(1.5)
        print("JOB502", json.dumps(getter(job_id_of(c2), audience="client"))[:1500])
        r = await c.post(m.RAW, **form())
        print("SYNC502", r.status_code, dict(r.headers), r.text)
        return {}
    run(rig, go)
