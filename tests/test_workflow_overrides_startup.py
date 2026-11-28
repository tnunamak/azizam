"""The workflow-overrides-off step never stops the gateway's start.

A kept override under a bundled id whose run control differs from the release's used to raise inside the lifespan's
store start-up, which awaits admin_store.init_db with no guard, so the server did not start and the owner API the
message pointed to was unreachable. The start now goes on: the ordinary override is rewritten and committed, the
kept record stays as stored and is logged, and the step stays unrecorded so the next start runs it again.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import proxy
from gateway import providers, workflow_overrides_migration
from tests.test_workflow_overrides_migration import (
    DIFFERS, PATCH_OF, REPLACE, RETIRED, SAME, _registry, _step_recorded, _store_with, _stored,
)


def _start(db) -> None:
    """The lifespan's store start-up against ``db``; the other stores are not under test."""
    def nothing(*_args, **_kwargs) -> None:
        return None

    with patch.object(proxy.ADMIN_STORE, "path", db), patch.object(proxy.responses_store, "init_db", nothing), \
            patch.object(proxy.receipt_store, "init_db", nothing), patch.object(proxy.media_jobs, "init_db", nothing), \
            patch.object(providers, "recover_provider_spend", nothing):
        asyncio.run(proxy._initialize_stores())


def test_a_kept_override_with_differing_run_control_does_not_stop_the_start(tmp_path, caplog):
    registry = _registry()
    del registry["workflows"][RETIRED]
    registry["workflows"][DIFFERS][REPLACE]["published"] = False   # the release publishes it
    db = _store_with(tmp_path, registry)

    with caplog.at_level("WARNING", logger=workflow_overrides_migration.__name__):
        _start(db)   # no exception

    workflows = _stored(db)["workflows"]   # a fresh connection: only committed rows
    assert workflows[SAME] == {REPLACE: {PATCH_OF: SAME, "state": "active", "published": False, "record_revision": 4}}
    assert workflows[DIFFERS] == registry["workflows"][DIFFERS]
    assert not _step_recorded(db)
    assert any(DIFFERS in r.getMessage() and "published=False" in r.getMessage() for r in caplog.records)

    _start(db)   # the next start runs the step again and still changes nothing it should not
    assert _stored(db)["workflows"] == workflows and not _step_recorded(db)

    settled = {wid: record for wid, record in workflows.items() if wid != DIFFERS}   # the owner deleted the kept record
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'",
                     (json.dumps({"workflows": settled}),))
        conn.commit()
    _start(db)
    assert _step_recorded(db)
