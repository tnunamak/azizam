import json
from gateway.discovery import BUNDLED_WORKFLOWS_DIR, WorkflowRegistry
from tests.workflow_reader_outputs import CONSUMERS, read_one


def test_regen():
    registry = WorkflowRegistry.from_directory(BUNDLED_WORKFLOWS_DIR)
    rec = [r for r in registry.records() if r.id == "video.edit.video-mux"][0]
    entry = {name: read_one(rec, name) for name in CONSUMERS}
    raise AssertionError("ENTRY=" + json.dumps(entry, sort_keys=True) + "=END")
