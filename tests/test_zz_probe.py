from tests.test_workflow_run_contracts import *  # noqa
def test_probe(world):
    b = proxy.config["backends"]
    assert 0, (b.get("comfyui"), proxy.DERIVED.COMFY_HOST)
