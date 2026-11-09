import gzip, json, os
from gateway.discovery import BUNDLED_WORKFLOWS_DIR, WorkflowRegistry
from tests.workflow_reader_outputs import CONSUMERS, read_one
from tests.test_workflow_readers_v2 import OLD_OUTPUT


def test_regen():
    old = json.load(gzip.open(OLD_OUTPUT, "rt", encoding="utf-8"))
    registry = WorkflowRegistry.from_directory(BUNDLED_WORKFLOWS_DIR)
    rec = [r for r in registry.records() if r.id == "video.edit.video-mux"][0]
    old[rec.id] = {name: read_one(rec, name) for name in CONSUMERS}
    open(os.path.expanduser("~/.tmp/c2d-run/reader_outputs_new.json"), "w").write(json.dumps(old, sort_keys=True))
