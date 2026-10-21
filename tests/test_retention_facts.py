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
            validate_facts(facts["facts"] if group is contract["system_facts"] else facts)
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


def test_reader_rules_have_plain_explanations() -> None:
    kinds = {
        "library_prompt": {"purpose": "Find it again", "readers": "maker"},
        "conversation": {"purpose": "Continue it", "readers": "scope"},
    }
    view = ledger({"services": {}}, kinds=kinds)
    assert "personal App keys" in view["kinds"][0]["reader_explanation"]
    assert "app that saved it" in view["kinds"][1]["reader_explanation"]


def test_ledger_drops_url_credentials_and_ignores_non_data_ids() -> None:
    registry = {"services": {"p": {"kind": "provider", "config": {
        "remote_provider": True,
        "preset_id": "../../private",
        "base_url": "https://user:password@example.test/v1?api_key=hidden#secret",
    }}}}
    destination = ledger(registry, kinds={})["destinations"][-1]
    assert destination["address"] == "https://example.test/v1"
    assert destination["facts"] == []


def test_fact_ids_reject_trailing_newline() -> None:
    with pytest.raises(ValueError, match="retention_facts"):
        validate_facts([{**_fact(), "id": "valid-id\n"}])


def test_future_checked_is_rejected_only_on_write() -> None:
    fact = {**_fact(), "checked": "9999-01-01"}
    assert validate_facts([fact])[0] == fact
    assert revision(fact)
    with pytest.raises(ValueError, match="future"):
        validate_facts([fact], for_write=True)


@pytest.mark.parametrize("facts", [None, {}, [{"id": "incomplete"}]])
def test_service_save_rejects_invalid_retention_facts(facts) -> None:
    from gateway.service_registry import upsert_service_in_registry
    with pytest.raises(ValueError, match="retention_facts"):
        upsert_service_in_registry({"services": {}}, service_id="test", raw={
            "retention_facts": facts,
        })


def test_service_save_rejects_future_retention_facts() -> None:
    from gateway.service_registry import upsert_service_in_registry
    with pytest.raises(ValueError, match="future"):
        upsert_service_in_registry({"services": {}}, service_id="test", raw={
            "retention_facts": [{**_fact(), "checked": "9999-01-01"}],
        })


def test_ledger_reuses_parsed_bundled_data_and_isolates_results(monkeypatch) -> None:
    from pathlib import Path
    import gateway.retention_facts as facts_module
    facts_module._file_data.cache_clear()
    reads = []
    original = Path.read_text
    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    registry = {"services": {"p": {"config": {
        "remote_provider": True, "preset_id": "openai",
    }}, "e": {"config": {"connection": {"protocol": "node-graph"}}}}}
    first = ledger(registry)
    first["destinations"][0]["facts"][0]["holds"] = "changed"
    second = ledger(registry)
    assert second["destinations"][0]["facts"][0]["holds"] != "changed"
    assert len(reads) == len(set(reads)) == 4
    facts_module._file_data.cache_clear()


def test_system_names_and_never_save_paragraphs_come_from_data() -> None:
    view = ledger({"services": {}})
    assert next(row for row in view["destinations"] if row["id"] == "litellm")["name"] == "LiteLLM"
    paragraphs = view["never_save_text"].split("\n\n")
    assert len(paragraphs) == 1 + sum(len(row["facts"]) for row in view["destinations"])


def test_provider_evidence_covers_files_unpaid_review_and_zdr_conflict() -> None:
    presets = load_provider_catalog()
    google = {row["id"]: row for row in presets["google-openai"].retention_facts}
    assert google["feature-state"]["evidence_url"] == "https://ai.google.dev/gemini-api/docs/files"
    assert "48 hours" in google["feature-state"]["how_long"]
    unpaid = google["unpaid-improvement"]
    assert unpaid["evidence_url"] == "https://ai.google.dev/gemini-api/terms"
    assert "human" in unpaid["reach"]
    assert "EEA" in unpaid["condition"] and "Switzerland" in unpaid["condition"] and "UK" in unpaid["condition"]
    router = {row["id"]: row for row in presets["openrouter"].retention_facts}
    assert router["provider-routing"]["evidence_url"] == "https://openrouter.ai/docs/guides/features/zdr"
    conflict = router["retention-routing-contradiction"]
    assert conflict["evidence_url"] == "https://openrouter.ai/docs/guides/privacy/provider-logging"
    assert "conflict" in conflict["control"].lower()


def test_provider_record_facts_override_the_bundled_seed() -> None:
    registry = {"services": {"p": {"config": {
        "remote_provider": True, "preset_id": "openai", "retention_facts": [],
    }}}}
    assert ledger(registry)["destinations"][-1]["facts"] == []


def test_tool_name_metadata_is_disclosed_in_ledger_and_never_save():
    view = ledger({'services': {}}, kinds={}, now=date(2026, 10, 21))
    destination = next(item for item in view['destinations'] if item['id'] == 'litellm')
    fact = next(item for item in destination['facts'] if item['id'] == 'litellm-tool-names')
    assert fact['default'] == 'on'
    assert 'LiteLLM_ToolTable' in fact['where']
    assert 'Azizam does not read' in fact['condition']
    assert 'delete' in fact['control']
    assert 'LiteLLM_ToolTable' in view['never_save_text']
    assert 'never tool arguments, descriptions, schemas or results' in view['never_save_text']
