from __future__ import annotations

import copy
import socket
from datetime import date

import pytest

from gateway.engine_definitions import load_engine_catalog
from gateway.engine_protocol.description import parse_description_text
from gateway.provider_catalog import load_provider_catalog
from gateway.retention import load_contract
from gateway.retention_facts import assemble_never_save, ledger, revision, validate_facts


def _fact() -> dict:
    return copy.deepcopy(load_provider_catalog()["openai"].retention_facts[0])


def test_fact_schema_rejects_bad_or_duplicate_rows() -> None:
    fact = _fact()
    assert validate_facts([fact])[0]["id"] == fact["id"]
    with pytest.raises(ValueError, match="duplicated"):
        validate_facts([fact, fact])
    with pytest.raises(ValueError, match="Additional properties"):
        validate_facts([{**fact, "secret": "should not be accepted"}])
    with pytest.raises(ValueError, match="date"):
        validate_facts([{**fact, "checked": "2026-02-30"}])


def test_revision_changes_with_content_and_ignores_key_order() -> None:
    fact = _fact()
    assert revision(fact) == revision(dict(reversed(list(fact.items()))))
    assert revision(fact) != revision({**fact, "how_long": "changed"})


def test_bundled_facts_are_valid_at_every_level() -> None:
    contract = load_contract().data
    assert contract["stale_after_days"] == 180
    for group in (contract["system_facts"], contract["provider_fact_catalog"]):
        for facts in group.values():
            validate_facts(facts)
    for preset in load_provider_catalog().values():
        validate_facts(list(preset.retention_facts))
    for definition in load_engine_catalog().data["definitions"]:
        validate_facts(definition.get("retention_facts", []))
    from pathlib import Path
    source = Path(__file__).resolve().parent.parent / "registry/protocols/node-graph.yml"
    assert parse_description_text(source.read_text(), source=str(source)).retention_facts


def test_removed_provider_seed_stays_removed_and_old_revision_stays_identifiable() -> None:
    registry = {"services": {"p": {"kind": "provider", "name": "Provider", "config": {
        "remote_provider": True, "preset_id": "openai", "base_url": "https://example.test/v1",
    }}}}
    before = ledger(registry, kinds={}, now=date(2026, 10, 20))
    old_revision = before["destinations"][-1]["facts"][0]["revision"]
    assert old_revision == revision(_fact())
    after = ledger(registry, kinds={}, overrides={("provider", "p"): []}, now=date(2026, 10, 20))
    assert after["destinations"][-1]["facts"] == []
    assert old_revision == revision(_fact())


def test_removed_system_seed_stays_removed() -> None:
    view = ledger({"services": {}}, kinds={}, overrides={("system", "litellm"): []})
    assert next(item for item in view["destinations"] if item["id"] == "litellm")["facts"] == []


def test_engine_precedence_reach_and_stale_label() -> None:
    registry = {"services": {"e": {"kind": "provider", "name": "Local graph", "config": {
        "connection": {"protocol": "node-graph", "base_url": "http://127.0.0.1:18188"},
    }}}}
    view = ledger(registry, kinds={}, now=date(2027, 10, 20))
    destination = view["destinations"][-1]
    assert destination["kind"] == "engine"
    assert destination["facts"][0]["id"] == "graph-history"
    assert destination["facts"][0]["stale_label"] == "This was last checked on 2026-10-19."
    assert destination["reach"] == "Azizam reaches it on this computer"
    assert "graph-history" not in assemble_never_save(view)  # prose is assembled from fact content
    assert "submitted graphs" in view["never_save_text"]
    registry["services"]["e"]["config"]["retention_facts"] = []
    assert ledger(registry, kinds={})["destinations"][-1]["facts"] == []


def test_definition_fact_is_replaced_by_record_fact() -> None:
    config = {"definition": "own-model-list", "address": "http://127.0.0.1:11434"}
    registry = {"services": {"e": {"kind": "provider", "config": config}}}
    seeded = ledger(registry, kinds={})["destinations"][-1]["facts"]
    assert [row["id"] for row in seeded] == ["request-recorder"]
    config["retention_facts"] = [_fact()]
    replaced = ledger(registry, kinds={})["destinations"][-1]["facts"]
    assert [row["id"] for row in replaced] == ["abuse-monitoring"]
    assert replaced[0]["source"] == "set by the owner"


def test_ledger_read_does_not_use_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_args, **_kwargs):
        raise AssertionError("ledger attempted a network call")
    monkeypatch.setattr(socket, "create_connection", deny)
    view = ledger({"services": {}}, kinds={})
    assert view["destinations"]
    assert view["never_save_text"]


def test_ledger_drops_url_credentials_and_ignores_non_data_ids() -> None:
    registry = {"services": {"p": {"kind": "provider", "config": {
        "remote_provider": True,
        "preset_id": "../../private",
        "base_url": "https://user:password@example.test/v1?api_key=hidden#secret",
    }}}}
    destination = ledger(registry, kinds={})["destinations"][-1]
    assert destination["address"] == "https://example.test/v1"
    assert destination["facts"] == []
