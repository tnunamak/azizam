from tests.test_workflow_run_contracts import *  # noqa
def test_probe(world):
    b = proxy.config["backends"]
    assert 0, (sorted(b["comfyui"]), b["comfyui"].get("host"), b["comfyui"].get("port"))
