"""Same route and inputs through the old multipart loop and declared job runner."""
import asyncio
import difflib
import json
import threading
from collections import Counter

import pytest
import proxy

from gateway import request_history_capture, admin_store, declared_operation_job, multipart_adapter, receipt_store
from tests.support.adapter_differential import Normalizer, dump_sqlite
from tests.support.adapter_differential_rig import reset_store
from tests import test_adapter_differential_s7_mesh_rows as mesh


@pytest.mark.parametrize('name', sorted(mesh.CELLS))
def test_mesh_old_and_declared_wire(tmp_path, name):
    observations = []
    for declared in (False, True):
        root = tmp_path / str(declared)
        root.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            identities = Counter()
            def stable_id(prefix):
                identities[prefix] += 1
                return f'{prefix}_{identities[prefix]:016x}'
            patch.setattr(admin_store, '_gen_id', stable_id)
            harness = mesh.rig.__wrapped__(patch, root)
            reset_store(receipt_store.DB_PATH)
            # Async workers must finalize after the request's capture write. Without this
            # ordering the existing two writers can overwrite each other's retention fact.
            captured = {}
            original_finish = request_history_capture._Capture.finish
            def finish(capture, receipt_id, slot):
                try:
                    return original_finish(capture, receipt_id, slot)
                finally:
                    captured.setdefault(receipt_id, threading.Event()).set()
                    print('W4-CAPTURE', receipt_id)
            patch.setattr(request_history_capture._Capture, 'finish', finish)
            original_close = proxy._WORKFLOW_RUN_PORT.close_receipt
            async def close(request_id, status, reason):
                print('W4-CLOSE', request_id, list(captured))
                if request_id in proxy._RUN_RECEIPT_OWNERS:
                    event = captured.setdefault(request_id, threading.Event())
                    assert await asyncio.to_thread(event.wait, 5), 'capture did not finish'
                await original_close(request_id, status, reason)
            patch.setattr(proxy._WORKFLOW_RUN_PORT, 'close_receipt', close)
            original_run = mesh.run
            def settled_run(harness, scenario):
                async def settled(client):
                    facts = await scenario(client)
                    tasks = tuple(proxy._workflow_run_tasks)
                    if tasks:
                        await asyncio.gather(*tasks)
                    return facts
                return original_run(harness, settled)
            patch.setattr(mesh, 'run', settled_run)
            original_answer = mesh.answer
            def complete_answer(response):
                return {**original_answer(response), 'headers': mesh._mask(dict(response.headers))}
            patch.setattr(mesh, 'answer', complete_answer)
            if declared:
                patch.setattr(multipart_adapter.MultipartRunner, 'run', multipart_adapter.MultipartRunner.run_declared)
            facts = mesh.CELLS[name](harness)
            facts['receipts'] = dump_sqlite(receipt_store.DB_PATH, Normalizer([root]), skip_tables=('receipt_history',))
            observations.append(facts)
    print('W4-WIRE ' + name + ' ' + json.dumps(observations[0], sort_keys=True))
    assert observations[0] == observations[1], '\n'.join(difflib.unified_diff(
        json.dumps(observations[0], indent=2, sort_keys=True).splitlines(),
        json.dumps(observations[1], indent=2, sort_keys=True).splitlines()))


def test_mesh_default_runs_declared_job(rig, monkeypatch):
    calls = []
    original = declared_operation_job.run

    async def traced(*args, **kwargs):
        calls.append(kwargs['job_id'])
        return await original(*args, **kwargs)

    monkeypatch.setattr(declared_operation_job, 'run', traced)
    facts = mesh.cell_success_raw(rig)
    assert facts['answer']['status'] == 200
    assert len(calls) == 1


rig = mesh.rig
