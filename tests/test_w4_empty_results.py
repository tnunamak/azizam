"""An empty 3D engine reply keeps the original refusal, ahead of generic defaults."""
import asyncio
from pathlib import Path

import httpx
import pytest

import proxy
from gateway import media_jobs, multipart_adapter, receipt_store, workflow_adapters
from tests import test_adapter_differential_s7_mesh_rows as mesh

SOURCE = Path(__file__).parent / 'fixtures/w4_multipart_fd30cbfca.py'


def observe(root, old):
    with pytest.MonkeyPatch.context() as patch:
        root.mkdir()
        harness = mesh.rig.__wrapped__(patch, root)
        if old:
            namespace = dict(vars(multipart_adapter))
            exec(compile(SOURCE.read_text(), str(SOURCE), 'exec'), namespace)
            current = next(runner for runner in workflow_adapters._RUNNERS.values()
                           if isinstance(runner, multipart_adapter.MultipartRunner))
            runner = namespace['MultipartRunner'](current._host, current._description)
            patch.setitem(workflow_adapters._RUNNERS, runner.binding_key, runner)
        handler = harness.handler

        def empty(request):
            if request.url.path == '/generate':
                harness.downstream.append(request)
                return httpx.Response(200, content=b'', headers={'content-type': 'model/gltf-binary'})
            return handler(request)

        patch.setattr(harness, 'handler', empty)

        async def drive(client):
            response = await client.post(mesh.RAW, **mesh.form())
            tasks = tuple(proxy._workflow_run_tasks)
            if tasks:
                await asyncio.gather(*tasks)
            return {'answer': mesh.answer(response)}

        facts = mesh.run(harness, drive)
        job, = media_jobs.recent(path=media_jobs.DB_PATH)
        receipt, = [row for row in receipt_store.recent(audience='internal', path=receipt_store.DB_PATH)
                    if row['path'] == mesh.RAW]
        facts.update(job_status=job['status'], receipt_status=receipt['status'])
        return facts


def test_empty_3d_reply_matches_fd30cbfca(tmp_path):
    old = observe(tmp_path / 'old', True)
    new = observe(tmp_path / 'new', False)
    assert old['answer']['status'] == 500
    assert old['answer']['json']['error']['code'] == 'result_not_kept'
    assert old['job_status'] == 'failed'
    assert old['receipt_status'] == 'failed'
    assert new == old
