from tests.c2d_policy_support import release_values

def test_shared_ceilings(monkeypatch):
    from gateway import workflow_run as run, workflow_registry_store as store
    assert run.RunLimits().max_nodes == store.Bounds().max_nodes == 2000
    with release_values(monkeypatch, "workflow_execution", {"bounds": {"max_nodes": 17, "max_depth": 7, "max_template_bytes": 1000}, "history_depth": 3}, "gateway.workflow_registry_store", "gateway.workflow_run") as (store, run):
        assert run.run_limits_from_config({}).max_nodes == store.bounds_from_config({}).max_nodes == 17
        assert run.run_limits_from_config({"workflow_execution":{"bounds":{"max_nodes":100}}}).max_nodes == store.bounds_from_config({"workflow_execution":{"bounds":{"max_nodes":100}}}).max_nodes == 17
