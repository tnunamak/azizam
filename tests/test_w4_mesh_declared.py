"""Same route and inputs through the old multipart loop and declared job runner."""
import pytest

from gateway import declared_operation_job, multipart_adapter
from tests import test_adapter_differential_s7_mesh_rows as mesh


@pytest.mark.parametrize('name', ['success_raw', 'success_sync', 'async_job', 'engine_failure', 'health_503', 'observer_refusal'])
def test_mesh_old_and_declared_wire(tmp_path, name):
    observations = []
    for declared in (False, True):
        root = tmp_path / str(declared)
        root.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            harness = mesh.rig.__wrapped__(patch, root)
            if declared:
                patch.setattr(multipart_adapter.MultipartRunner, 'run', multipart_adapter.MultipartRunner.run_declared)
            observations.append(mesh.CELLS[name](harness))
    assert observations[0] == observations[1]


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
