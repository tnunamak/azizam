"""Wire expectations proved against both multipart loops before the old loop was deleted.

The transition comparison remains in commit b6244db06; only clocks and generated
identities are normalized. Receipts are read after workers finish retention writes.
"""
import asyncio
import difflib
import json
from pathlib import Path
from collections import Counter

import pytest
import proxy

from gateway import admin_store, declared_operation_job, multipart_adapter, receipt_store
from tests.support.adapter_differential import Normalizer, dump_sqlite
from tests.support.adapter_differential_rig import reset_store
from tests import test_adapter_differential_s7_mesh_rows as mesh


@pytest.mark.parametrize('name', sorted(mesh.CELLS))
def test_mesh_declared_wire(tmp_path, name):
    with pytest.MonkeyPatch.context() as patch:
        identities = Counter()
        def stable_id(prefix):
            identities[prefix] += 1
            return f'{prefix}_{identities[prefix]:016x}'
        patch.setattr(admin_store, '_gen_id', stable_id)
        harness = mesh.rig.__wrapped__(patch, tmp_path)
        reset_store(receipt_store.DB_PATH)
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
        facts = mesh.CELLS[name](harness)
        facts['receipts'] = dump_sqlite(receipt_store.DB_PATH, Normalizer([tmp_path]), skip_tables=('receipt_history',))
    expected = json.loads((Path(__file__).parent / 'fixtures/w4_mesh_wire.json').read_text())[name]
    assert facts == expected, '\n'.join(difflib.unified_diff(
        json.dumps(expected, indent=2, sort_keys=True).splitlines(),
        json.dumps(facts, indent=2, sort_keys=True).splitlines()))


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
