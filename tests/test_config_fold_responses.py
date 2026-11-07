"""Slice C6 goldens: the responses of the three folded owner settings did not change (docs/design/CONFIG_HOMES.md, 10.1).

``tests/fixtures/config_homes/c6_responses.json`` was captured from the code before the settings moved into the config
layer. The only difference C6 allows is the conflict status of a stale write, 409 to 412 (RFC 9110): those two cells of the fixture
were edited by hand from 409 to 412, and nothing else in it was touched.
C10 removed the effective settings view with its route: its step and the two keys that held its answer were
deleted from the fixture.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import config_fold_scenario

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "config_homes" / "c6_responses.json"


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    result = config_fold_scenario.run(tmp_path_factory.mktemp("c6-responses") / "admin.db")
    return json.loads(json.dumps(result, sort_keys=True))


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_every_step_of_the_journey_answers_as_before(recorded, golden):
    assert [s["step"] for s in recorded["steps"]] == [s["step"] for s in golden["steps"]]
    for got, want in zip(recorded["steps"], golden["steps"]):
        assert (got["status"], got["body"]) == (want["status"], want["body"]), got["step"]


def test_the_audit_feed_is_unchanged(recorded, golden):
    assert recorded["audit"] == golden["audit"]


def test_only_stale_writes_answer_412(recorded):
    stale = {s["step"] for s in recorded["steps"] if s["status"] == 412}
    assert stale == {"instance: stale write", "media: stale write"}
