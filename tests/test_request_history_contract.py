"""Request history is a group in the retention data: on by default for new members, validated by the one validator.

docs/design/REQUEST_RETENTION.md sections 3.1-3.2 and 16.1.3 (new-group case) and 16.1.5 (widening). The default
is Tim's decision in DECISIONS.md D50, carried as data (registry/retention.yml), not as a code constant.
"""
from __future__ import annotations

import copy
import json

import pytest
import yaml

from gateway import retention

SCHEMA = json.loads(retention.SCHEMA_PATH.read_text(encoding="utf-8"))


def _bundled_document() -> dict:
    return yaml.safe_load(retention.BUNDLED_PATH.read_text(encoding="utf-8"))


def _parse(document: dict) -> retention.Contract:
    return retention.parse_contract(document, schema=SCHEMA, source="<test>")


def test_request_history_is_a_group_on_by_default_as_data():
    group = retention.bundled().data["groups"]["request_history"]
    assert group == {
        "kinds": ["request_history"],
        "default_keep": True,
        "default_retention": "7_days",
        "default_cap": "30_days",
        "choices": ["1_day", "7_days", "30_days"],
    }


def test_request_history_kind_captures_the_accepted_and_received_parts():
    kind = retention.bundled().data["kinds"]["request_history"]
    assert kind["parts"] == {"allowed": ["accepted", "received"], "default": ["accepted"]}
    assert kind["readers"] == "maker"
    assert kind["max_record_bytes"] == 1048576


def test_new_member_keeps_request_history_for_the_default_period():
    decision = retention.decide(
        retention.REQUEST_HISTORY, person_id="maker", member=None, household_policy=None,
        request_body={"model": "m"}, now=1_000,
    )
    assert decision.keep is True
    assert decision.reason == "remembered"
    assert decision.kept_until == 1_000 + 7 * 86_400
    assert decision.parts == ("accepted",)


def test_changing_the_default_in_the_file_changes_a_new_members_decision(tmp_path, monkeypatch):
    """16.1.3: the default is data. Edit the file, reload it, and the decision follows with no code change."""
    document = _bundled_document()
    document["groups"]["request_history"]["default_keep"] = False
    edited = tmp_path / "retention.yml"
    edited.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setattr(retention, "BUNDLED_PATH", edited)
    decision = retention.decide(
        retention.REQUEST_HISTORY, person_id="maker", member=None, household_policy=None,
        request_body=None, now=1_000,
    )
    assert decision.keep is False
    assert decision.reason == "member_disabled"


def test_the_bundled_prompts_group_public_contract_is_unchanged():
    public = retention.public_contract(retention.LIBRARY_PROMPT)
    assert public["choices"] == [
        {"id": "never", "seconds": None}, {"id": "1_year", "seconds": 31536000}, {"id": "3_months", "seconds": 7776000},
    ]
    assert "known_unoffered" not in public


@pytest.mark.parametrize("breaks, message", [
    (lambda d: d["groups"].__setitem__("request_history", {**d["groups"]["request_history"], "default_retention": "1_year"}),
     "default_retention"),
    (lambda d: d["groups"]["request_history"].__setitem__("choices", ["1_day", "7_days", "1_hour"]),
     "1_hour is not in retention_choices"),
    (lambda d: d["groups"]["request_history"].__setitem__("default_cap", "never"),
     "default_cap"),
    (lambda d: d["kinds"]["request_history"]["parts"].__setitem__("default", ["accepted", "sent"]),
     "parts/default: sent"),
    (lambda d: d["kinds"]["request_history"].__setitem__("readers", "owner"),
     "readers"),
    (lambda d: d["groups"]["prompts"]["kinds"].append("request_history"),
     "already in the group"),
    (lambda d: d["kinds"].__setitem__("debug_capture", {"purpose": "x", "parts": {"allowed": ["accepted"], "default": ["accepted"]}, "readers": "maker"}),
     "not one the gateway can capture"),
    (lambda d: d.__setitem__("widening", "maker_share"),
     "widening"),
])
def test_the_one_validator_refuses_broken_request_history_rules(breaks, message):
    document = copy.deepcopy(_bundled_document())
    breaks(document)
    with pytest.raises(retention.RetentionDataError, match=message):
        _parse(document)


def test_the_bundled_file_is_valid_under_the_closed_schema():
    contract = retention.bundled()
    assert set(contract.data["groups"]) == {"prompts", "request_history"}
    assert set(contract.data["kinds"]) == {"library_prompt", "conversation", "request_history"}
