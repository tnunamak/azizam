"""Rollback (docs/design/CONFIG_HOMES.md, 8.3 step 0 and 10.1): the code before the layering step boots on a migrated database.

It reads each stored patch as the release record it copies and rebuilds the record by its own backfill, so the owner's
fields survive and the substrate and the builtin frontends come back whole. The hermetic runner holds one tree and no
git history, so this runs by hand against a checkout of the previous commit::

    git worktree add --detach ~/.tmp/previous <commit before the step>
    AZIZAM_PREVIOUS_CHECKOUT=~/.tmp/previous <hermetic python> -m pytest --noconftest tests/test_registry_rollback.py

The module imports no gateway entry point, so ``--noconftest`` touches no live data (every path is a temporary file).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from gateway import admin_store, service_registry  # noqa: E402
from registry_test_helpers import seed_registry_document, write_legacy_registry_for_test  # noqa: E402

PREVIOUS_CHECKOUT = os.environ.get("AZIZAM_PREVIOUS_CHECKOUT")

_OLD_BOOT = """
import json, sys, yaml
from pathlib import Path
sys.path[:0] = [sys.argv[1]]
from gateway import admin_store, service_registry
db = Path(sys.argv[2])
raw = yaml.safe_load((Path(sys.argv[1]) / "proxy_config.yml").read_text())  # the previous checkout: its own file
admin_store.init_db(db)
reg = service_registry.ensure_service_registry(raw, path=db, actor="startup")
litellm = reg["services"]["litellm"]
print(json.dumps({"services": sorted(reg["services"]), "frontends": sorted(reg["protocol_frontends"]),
                  "litellm_name": litellm["name"], "litellm_enabled": litellm["enabled"],
                  "litellm_config": sorted(litellm["config"]), "litellm_kind": litellm["kind"]}))
"""


def _old_install(db: Path) -> None:
    document = seed_registry_document(yaml.safe_load((ROOT / "tests/fixtures/config_homes/deployment_config.yml").read_text(encoding="utf-8")))
    document["services"].pop("openrouter", None)  # the old boot never backfilled it
    document["services"]["litellm"].update({"name": "Operator LiteLLM", "enabled": False})
    document["services"]["ui-chat"] = service_registry.normalize_service_record("ui-chat", {
        "name": "ui-chat", "source": "ui", "kind": "provider", "enabled": True, "capabilities": ["chat"],
        "config": {"base_url": "http://127.0.0.1:9101/v1"}})
    document["protocol_frontends"]["acme"] = service_registry.normalize_protocol_frontend("acme", {
        "name": "Acme", "protocol": "acme", "source": "ui", "routes": ["/acme/v1"], "intents": ["acme.run"]})
    admin_store.init_db(db)
    write_legacy_registry_for_test(document, path=db)


@pytest.mark.skipif(not PREVIOUS_CHECKOUT, reason="set AZIZAM_PREVIOUS_CHECKOUT to a checkout of the commit before C4")
def test_the_code_before_the_step_boots_on_the_migrated_database_and_rebuilds_its_copies(tmp_path):
    db = tmp_path / "admin.db"
    _old_install(db)
    service_registry.ensure_service_registry(path=db, actor="test")  # the step
    migrated = tmp_path / "rollback.db"
    shutil.copy(db, migrated)

    done = subprocess.run([sys.executable, "-I", "-c", _OLD_BOOT, PREVIOUS_CHECKOUT, str(migrated)],
                          capture_output=True, text=True, timeout=120, check=False)

    assert done.returncode == 0, done.stderr[-600:]
    booted = json.loads(done.stdout.strip().splitlines()[-1])
    assert booted["litellm_name"] == "Operator LiteLLM" and booted["litellm_enabled"] is False  # the owner's patch
    assert {"execution_substrate", "default_base_url", "health_endpoint"} <= set(booted["litellm_config"])  # rebuilt
    assert booted["litellm_kind"] == "llm-substrate"
    assert {"anthropic", "openai", "acme"} <= set(booted["frontends"])
    assert {"ui-chat", "kobold"} <= set(booted["services"])
