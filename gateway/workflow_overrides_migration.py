"""Named step ``workflow-overrides-off``: a stored override of a bundled workflow becomes run control only.

An owner used to replace a bundled workflow's contract under its bundled id (a stored record with ``overrides: <id>``).
That shadowed every later release of the workflow. This step rewrites each such record once:

- the id is still bundled, and the stored contract and facts equal the release's: the record becomes the run-control
  patch the store writes when the owner turns a bundled workflow on or off (``patch_of``, state, published, revision),
  so the release's contract serves again with the owner's run control;
- the id is still bundled and the stored contract or facts differ: the record stays exactly as stored and is named in
  the audit entry and the log, never rewritten by guesswork. Such a record never serves (the release serves the id),
  so when its state or publication differs from the release's (``kept-run-control-differs``) the owner's run control
  is not what serves. The owner settles it through the owner API: delete the kept record, then set run control;
- the release no longer ships the id: the record keeps its contract and loses only ``overrides``, a plain owner record.

The step never stops a start. The caller records it by name only when no ``kept-run-control-differs`` record
remains (``settled``), so it runs again on each start until the owner has settled every one; a run rewrites only
records that still carry ``overrides``, so a rerun changes nothing it changed before.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from gateway import registry_layers

STEP_NAME = "workflow-overrides-off"
KEPT_RUN_CONTROL_DIFFERS = "kept-run-control-differs"

_log = logging.getLogger(__name__)


def settled(outcomes: dict[str, str]) -> bool:
    """True when the step may be recorded: no kept record's run control differs from the release's."""
    return KEPT_RUN_CONTROL_DIFFERS not in outcomes.values()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _release_view() -> tuple[dict[str, Any], dict[str, str], dict[str, tuple[str, bool]]]:
    """The release's contracts by id, each bundled workflow's facts as stored-record JSON, and its served
    (state, published)."""
    from gateway import registry_snapshot, service_registry
    from gateway.workflow_registry_store import facts_record

    snapshot = registry_snapshot.build({}, registry_snapshot.bundled_release(), generation=0)
    facts: dict[str, str] = {}
    control: dict[str, tuple[str, bool]] = {}
    for workflow_id in service_registry.bundled_workflow_ids():
        entry = snapshot.get(workflow_id)
        if entry is not None:
            control[workflow_id] = (entry.state, bool(entry.published))
            if entry.facts is not None:
                facts[workflow_id] = _canonical(facts_record(entry.facts))
    return service_registry.bundled_workflow_manifests(), facts, control


def _run_control(record: dict[str, Any]) -> tuple[str, bool]:
    """State and publication as the snapshot reads them from a stored record (registry_snapshot._stored_entry)."""
    return (record.get("state") or ("active" if record.get("enabled", True) else "disabled"),
            bool(record.get("published", False)))


def _rewrite(workflow_id: str, stored: dict[str, Any], contracts: dict[str, Any],
             facts: dict[str, str]) -> tuple[dict[str, Any] | None, str]:
    """The new stored value for one override (None: keep it as stored) and what happened."""
    record = stored.get(registry_layers.REPLACE_KEY, stored)
    if workflow_id not in contracts:
        plain = {key: value for key, value in record.items() if key != "overrides"}
        plain["record_revision"] = int(record.get("record_revision") or 1) + 1
        return plain, "owner-record"
    same_contract = _canonical(record.get("manifest")) == _canonical(contracts[workflow_id])
    # A stored record with no policy serves the release's facts (registry_snapshot._stored_entry).
    same_facts = record.get("policy") is None or (workflow_id in facts and _canonical(record["policy"]) == facts[workflow_id])
    if not (same_contract and same_facts):
        return None, "kept-differs"
    state, published = _run_control(record)
    patch = {registry_layers.RUN_CONTROL_PATCH_KEY: workflow_id, "state": state, "published": published,
             "record_revision": int(record.get("record_revision") or 1) + 1}
    return {registry_layers.REPLACE_KEY: patch}, "run-control"


def migrate_registry(conn: sqlite3.Connection) -> dict[str, str]:
    """Rewrite the stored service registry in the caller's transaction; return {workflow_id: outcome}."""
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
    if row is None:
        return {}
    registry = json.loads(row[0])
    workflows = registry.get("workflows") if isinstance(registry, dict) else None
    if not isinstance(workflows, dict):
        return {}
    overrides = {wid: value for wid, value in workflows.items() if isinstance(value, dict)
                 and isinstance(value.get(registry_layers.REPLACE_KEY, value), dict)
                 and value.get(registry_layers.REPLACE_KEY, value).get("overrides")}
    if not overrides:
        return {}
    contracts, facts, control = _release_view()
    outcomes: dict[str, str] = {}
    changed = False
    for wid, stored in overrides.items():
        workflow_id = str(wid)
        new, outcome = _rewrite(workflow_id, stored, contracts, facts)
        if new is not None:
            workflows[wid] = new
            changed = True
        elif outcome == "kept-differs":
            kept = _run_control(stored.get(registry_layers.REPLACE_KEY, stored))
            release = control.get(workflow_id)
            if release is not None and kept != release:
                outcome = KEPT_RUN_CONTROL_DIFFERS
            _log_kept(workflow_id, outcome, kept, release)
        outcomes[workflow_id] = outcome
    if changed:
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(registry),))
        conn.execute(
            "UPDATE operator_settings_generations SET operator_settings_generation=operator_settings_generation+1 "
            "WHERE setting_key='service_registry'"
        )
    from gateway import admin_store

    admin_store._record_audit_in_transaction(
        conn, "operator.workflow_overrides.migrate", actor_kind="system", actor_id="migration",
        actor_label="Workflow overrides migration", target_kind="operator_setting", target_id="service_registry",
        metadata={"workflows": outcomes}, now=int(time.time()),
    )
    return outcomes


def _log_kept(workflow_id: str, outcome: str, kept: tuple[str, bool], release: tuple[str, bool] | None) -> None:
    """One full line per kept record, on every run, so the owner can find each id the step left as stored."""
    stored = f"stored state={kept[0]!r} published={kept[1]!r}"
    served = f"release state={release[0]!r} published={release[1]!r}" if release else "release state=None published=None"
    if outcome == KEPT_RUN_CONTROL_DIFFERS:
        _log.error("%s: kept workflow %r under a bundled id: %s, %s. The release serves this id, so the stored run "
                   "control is not what serves. Delete the kept record, then set run control through the owner API; "
                   "the step runs again on the next start", STEP_NAME, workflow_id, stored, served)
    else:
        _log.warning("%s: kept workflow %r under a bundled id: %s, %s. The release serves this id",
                     STEP_NAME, workflow_id, stored, served)
