from tests.c2d_policy_support import release_values

def test_deleted_job_release_lifetime(monkeypatch):
    from gateway import workflow_run
    assert workflow_run.TOMBSTONE_RETENTION_S == 86400
    with release_values(monkeypatch, 'workflow_retention', {'deleted_job_s': 12}, 'gateway.workflow_run') as (run,):
        assert run.TOMBSTONE_RETENTION_S == 12
        service = run.WorkflowJobService(port=None)
        assert service._retention_s == 12
