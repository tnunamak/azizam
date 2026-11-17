"""Same route and inputs through the old multipart loop and declared job runner."""
import difflib
import json
from collections import Counter

import pytest

from gateway import admin_store, declared_operation_job, multipart_adapter, receipt_store
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
            original_answer = mesh.answer
            def complete_answer(response):
                return {**original_answer(response), 'headers': mesh._mask(dict(response.headers))}
            patch.setattr(mesh, 'answer', complete_answer)
            if declared:
                patch.setattr(multipart_adapter.MultipartRunner, 'run', multipart_adapter.MultipartRunner.run_declared)
            facts = mesh.CELLS[name](harness)
            facts['receipts'] = dump_sqlite(receipt_store.DB_PATH, Normalizer([root]), skip_tables=('receipt_history',))
            observations.append(facts)
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
