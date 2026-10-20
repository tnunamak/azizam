"""Regression checks for owner fact lifetime and independent destination evidence."""
from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

import pytest
import yaml

from gateway import admin_store
from gateway.provider_catalog import load_provider_catalog
from gateway.retention_facts import ledger, validate_facts


def _fact():
    return copy.deepcopy(load_provider_catalog()["openai"].retention_facts[0])


@pytest.mark.parametrize("target_type", ["engine", "provider"])
def test_service_removal_clears_its_overrides_in_the_registry_transaction(tmp_path, target_type):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry({"services": {"removed": {}, "kept": {}}}, expected_generation=0, path=path)
    for kind, target in [(target_type, "removed"), (target_type, "kept"), ("system", "removed")]:
        admin_store.put_retention_fact_override(kind, target, [], path=path)
    with sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        admin_store.write_service_registry_in_transaction(
            conn, {"services": {"kept": {}}}, updated_by=None, expected_generation=1, now=1,
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM retention_fact_overrides WHERE target_type=? AND target_id='removed'",
            (target_type,),
        ).fetchone()[0] == 0
        conn.rollback()
    assert (target_type, "removed") in admin_store.retention_fact_overrides(path)
    admin_store.write_service_registry({"services": {"kept": {}}}, expected_generation=1, path=path)
    assert admin_store.retention_fact_overrides(path) == {(target_type, "kept"): [], ("system", "removed"): []}


@pytest.mark.parametrize("source", ["record", "override", "system"])
def test_invalid_legacy_fact_list_only_hides_its_destination(source):
    registry = {"services": {name: {"config": {"retention_facts": [_fact()]}} for name in ("broken", "healthy")}}
    overrides = {}
    if source == "record":
        registry["services"]["broken"]["config"]["retention_facts"] = [{"invalid": True}]
    elif source == "override":
        overrides[("engine", "broken")] = [{"invalid": True}]
    else:
        overrides[("system", "litellm")] = [{"invalid": True}]
    view = ledger(registry, overrides=overrides)
    destinations = {row["id"]: row for row in view["destinations"]}
    assert destinations["healthy"]["facts"]
    assert destinations["litellm" if source == "system" else "broken"]["facts"] == []
    assert "Some destination facts are unavailable" in view["fact_basis"]


def test_evidence_uri_rejects_trailing_newline():
    with pytest.raises(ValueError, match="retention_facts"):
        validate_facts([{**_fact(), "evidence_url": "https://example.test/evidence\n"}])


def test_seed_evidence_covers_each_distinct_claim():
    root = Path(__file__).resolve().parent.parent
    graph = yaml.safe_load((root / "registry/protocols/node-graph.yml").read_text())["retention_facts"]
    graph_urls = {row["evidence_url"] for row in graph}
    prefix = "https://github.com/Comfy-Org/ComfyUI/blob/8f04fd6ecf4a548937971e840af2a985529fab76/"
    assert prefix + "execution.py#L1303-L1359" in graph_urls
    assert prefix + "server.py#L1299-L1310" in graph_urls
    catalog = yaml.safe_load((root / "registry/retention.yml").read_text())["provider_fact_catalog"]
    assert {row["evidence_url"] for row in catalog["bedrock"]} >= {
        "https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html",
        "https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html#SettingLogRetention",
        "https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html",
    }
    assert {row["evidence_url"] for row in catalog["langsmith"]} >= {
        "https://docs.langchain.com/langsmith/annotate-code",
        "https://docs.langchain.com/langsmith/usage-and-billing#data-retention",
    }
    fact = next(row for row in load_provider_catalog()["anthropic"].retention_facts if row["id"] == "standard-api-retention")
    assert fact["confidence"] == "inferred"


def test_orphan_sweep_protects_pending_receipt_by_source_age(tmp_path, monkeypatch):
    from gateway import media_jobs, receipt_store
    clock = [1_800_000_000]
    monkeypatch.setattr(media_jobs.time, "time", lambda: clock[0])
    monkeypatch.setattr(receipt_store.time, "time", lambda: clock[0])
    admin = tmp_path / "admin.db"
    receipts = tmp_path / "receipts.db"
    facts = tmp_path / "facts.db"
    admin_store.init_db(admin)
    receipt_store.init_db(receipts)
    media_jobs.init_db(facts)
    # A shared revision's first_seen can be old even though this source is new.
    destination = [{"id": "remote", "facts": [_fact()]}]
    media_jobs.save_fact_snapshot(destination, source_kind="kept_record", source_id="old", path=facts, now=clock[0] - 1000)
    hashes = media_jobs.save_fact_snapshot(destination, source_kind="receipt", source_id="pending", path=facts)
    # Interleave maintenance after the fact commit but before the receipt commit.
    receipt_store.maintain(admin, path=receipts, fact_store_path=facts)
    assert media_jobs.fact_revisions_for_source("receipt", "pending", path=facts)
    receipt_store.record({"request_id": "pending", "retention_facts": hashes}, path=receipts)
    clock[0] += 301
    receipt_store.maintain(admin, path=receipts, fact_store_path=facts)
    assert media_jobs.fact_revisions_for_source("receipt", "pending", path=facts)
    with sqlite3.connect(receipts) as conn:
        conn.execute("DELETE FROM receipts WHERE request_id='pending'")
    receipt_store.maintain(admin, path=receipts, fact_store_path=facts)
    assert media_jobs.fact_revisions_for_source("receipt", "pending", path=facts) == {}


def test_source_timestamp_migrates_existing_fact_destinations(tmp_path):
    from gateway import media_jobs
    path = tmp_path / "facts.db"
    media_jobs.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE retention_fact_destinations")
        conn.execute("CREATE TABLE retention_fact_destinations (source_kind TEXT, source_id TEXT, destination_id TEXT, metadata_json TEXT, PRIMARY KEY(source_kind,source_id,destination_id))")
        conn.execute("INSERT INTO retention_fact_destinations VALUES ('receipt','legacy','remote','{}')")
        conn.execute("UPDATE media_jobs_schema_version SET version=3")
    media_jobs.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT source_id,created_at FROM retention_fact_destinations").fetchone() == ("legacy", 0)
    assert media_jobs.schema_version(path) == 4
