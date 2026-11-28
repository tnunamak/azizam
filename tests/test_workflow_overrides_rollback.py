"""Rollback for the named step ``workflow-overrides-off``: the code before r6 boots on a database the step migrated.

The step removes ``overrides`` from a stored record whose contract equals the release's and leaves the run-control patch
the store already wrote before r6 (``patch_of``, state, published). The previous code must read that patch, serve the
release's contract with the owner's run control, and accept the owner's next state change. The hermetic runner holds
one tree and no git history, so this runs by hand against a checkout of the commit before the step::

    git worktree add --detach ~/.tmp/previous <commit before the step>
    AZIZAM_PREVIOUS_CHECKOUT=~/.tmp/previous <hermetic python> -m pytest --noconftest tests/test_workflow_overrides_rollback.py

Every path is a temporary file; no live data is read.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT)]

from gateway import admin_store, registry_layers  # noqa: E402
from tests.test_workflow_overrides_migration import SAME, _registry, _store_with  # noqa: E402

PREVIOUS_CHECKOUT = os.environ.get("AZIZAM_PREVIOUS_CHECKOUT")

_OLD_BOOT = """
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1]]
from gateway import admin_store, registry_snapshot
from gateway.definition_store import DefinitionStore
from gateway.workflow_registry_store import WorkflowRegistryStore
from gateway.workflow_runtime_types import Actor
db, wid = Path(sys.argv[2]), sys.argv[3]
admin_store.init_db(db)
def build(document, _release, *, generation):
    return registry_snapshot.build(document, registry_snapshot.bundled_release(), generation=generation)
store = WorkflowRegistryStore(path=db, definitions=DefinitionStore(db.parent / "jobs.db"), build_snapshot=build)
release = registry_snapshot.build({}, registry_snapshot.bundled_release(), generation=0).get(wid)
served = store.snapshot().get(wid)
generation = int(admin_store.get_service_registry_json_with_generation(path=db)["operator_settings_generation"])
store.set_state(wid, "disabled", if_match=generation, actor=Actor("owner", "owner-1", "Owner"))
after = store.snapshot().get(wid)
print(json.dumps({"state": served.state, "published": served.published,
                  "same_contract": served.contract_digest == release.contract_digest,
                  "same_facts": served.facts == release.facts, "state_after_write": after.state,
                  "same_contract_after_write": after.contract_digest == release.contract_digest}))
"""


@pytest.mark.skipif(not PREVIOUS_CHECKOUT, reason="set AZIZAM_PREVIOUS_CHECKOUT to a checkout of the commit before the step")
def test_the_code_before_the_step_serves_the_migrated_run_control_patch_and_writes_over_it(tmp_path):
    assert PREVIOUS_CHECKOUT
    db = _store_with(tmp_path, _registry())
    admin_store.init_db(db)   # the step
    document = admin_store.get_service_registry_with_generation(path=db)
    assert document is not None
    stored = document["value"]["workflows"][SAME]
    patch = stored.get(registry_layers.REPLACE_KEY, stored)
    assert patch.get(registry_layers.RUN_CONTROL_PATCH_KEY) == SAME and "overrides" not in patch, stored

    done = subprocess.run([sys.executable, "-I", "-c", _OLD_BOOT, PREVIOUS_CHECKOUT, str(db), SAME],
                          capture_output=True, text=True, timeout=120, check=False)

    assert done.returncode == 0, done.stderr[-1500:]
    booted = json.loads(done.stdout.strip().splitlines()[-1])
    assert booted == {"state": "active", "published": False, "same_contract": True, "same_facts": True,
                      "state_after_write": "disabled", "same_contract_after_write": True}
