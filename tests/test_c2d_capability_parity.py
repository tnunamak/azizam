"""Parity goldens for gateway/capability_ids.py, gateway/models_catalog.py and gateway/extension_catalog.py.

Each golden is today's output stored as literal JSON under tests/fixtures/c2d_parity/ and compared
field by field. A code-to-data move that changes any output fails here. The tests only read goldens;
``tests/fixtures/c2d_parity/capture.py`` is the one writer.

Ordering: every list is compared in the order the code returns it, except where a capture function
below sorts it and says why. Dict key order is never compared.

Stubbed helper: ``gateway.model_files._is_installed`` (the one disk read), in the fixture capability set
built from tests/test_local_capabilities.REPRESENTATIVE_CONFIG; every declared file is stubbed absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import capability_ids, extension_catalog, models_catalog  # noqa: E402
from tests.support import c2d_parity  # noqa: E402
from tests.support.c2d_snapshots import (  # noqa: E402
    capture_capability_ids,
    capture_extensions,
    assert_sized_rows,
    capture_models_catalog,
)


CAPABILITY_IDS_GOLDEN = c2d_parity.load_golden("capability_ids")


@pytest.mark.parametrize("word", CAPABILITY_IDS_GOLDEN["words"])
def test_capability_of_word_matches_golden(word):
    assert capability_ids.capability_of_word(word) == CAPABILITY_IDS_GOLDEN["capability_of_word"][word]


@pytest.mark.parametrize("section", sorted(k for k in CAPABILITY_IDS_GOLDEN if k not in {"words", "capability_of_word"}))
def test_capability_ids_sections_match_golden(section):
    actual = capture_capability_ids()
    assert actual["words"] == CAPABILITY_IDS_GOLDEN["words"], "the word list changed: a table gained or lost a key"
    assert actual[section] == CAPABILITY_IDS_GOLDEN[section]


MODELS_GOLDEN = c2d_parity.load_golden("models_catalog")
EXTENSIONS_GOLDEN = c2d_parity.load_golden("extension_catalog")
_ACTUAL_MODELS: dict[str, Any] = {}


def _actual_models() -> dict[str, Any]:
    if not _ACTUAL_MODELS:
        _ACTUAL_MODELS.update(capture_models_catalog())
    return _ACTUAL_MODELS


@pytest.mark.parametrize("operation", MODELS_GOLDEN["operations"])
def test_capabilities_for_operation_matches_golden(operation):
    actual = _actual_models()
    assert actual["capabilities_for_operation"][operation] == MODELS_GOLDEN["capabilities_for_operation"][operation]
    assert actual["intent_prefixes_for_operation"][operation] == MODELS_GOLDEN["intent_prefixes_for_operation"][operation]


@pytest.mark.parametrize("intent", MODELS_GOLDEN["intents"])
def test_examples_and_path_match_golden(intent):
    actual = _actual_models()
    assert actual["examples_by_intent"][intent] == MODELS_GOLDEN["examples_by_intent"][intent]
    assert actual["path_by_intent"][intent] == MODELS_GOLDEN["path_by_intent"][intent]


def test_models_catalog_fixture_and_counts_match_golden():
    actual = _actual_models()
    for key in ("fixture_capability_identities", "catalog_counts", "empty_inputs"):
        assert actual[key] == MODELS_GOLDEN[key], key


@pytest.mark.parametrize("section", ["catalog_aliases", "catalog_models"])
def test_models_catalog_rows_match_golden(section):
    assert_sized_rows(_actual_models()[section], MODELS_GOLDEN[section], section)


def test_registered_model_capabilities_match_golden():
    assert _actual_models()["registered_model_capabilities"] == MODELS_GOLDEN["registered_model_capabilities"]


def test_second_catalog_envelope_matches_golden():
    actual, golden = _actual_models()["second_catalog"], MODELS_GOLDEN["second_catalog"]
    assert actual["inputs"] == golden["inputs"]
    assert actual["envelope"] == golden["envelope"]


@pytest.mark.parametrize("section", ["aliases", "models"])
def test_second_catalog_rows_match_golden(section):
    actual, golden = _actual_models()["second_catalog"], MODELS_GOLDEN["second_catalog"]
    assert_sized_rows(actual[section], golden[section], f"second catalog {section}")


@pytest.mark.parametrize("audience", EXTENSIONS_GOLDEN["audiences"])
def test_summarize_extensions_matches_golden(audience):
    actual = capture_extensions()["summaries"][audience]
    golden = EXTENSIONS_GOLDEN["summaries"][audience]
    assert [e["namespace"] for e in actual["extensions"]] == [e["namespace"] for e in golden["extensions"]]
    for got, want in zip(actual["extensions"], golden["extensions"]):
        for field in sorted(set(got) | set(want)):
            assert got.get(field) == want.get(field), f"{want['namespace']} field {field}"
    assert {k: v for k, v in actual.items() if k != "extensions"} == {k: v for k, v in golden.items() if k != "extensions"}


_ACTUAL_EXTENSIONS: dict[str, Any] = {}


@pytest.mark.parametrize("key", sorted(EXTENSIONS_GOLDEN["model_capabilities"]))
def test_summarize_model_capabilities_matches_golden(key):
    if not _ACTUAL_EXTENSIONS:
        _ACTUAL_EXTENSIONS.update(capture_extensions())
    assert _ACTUAL_EXTENSIONS["model_capability_names"] == EXTENSIONS_GOLDEN["model_capability_names"]
    got, want = _ACTUAL_EXTENSIONS["model_capabilities"][key], EXTENSIONS_GOLDEN["model_capabilities"][key]
    if got["sha256"] != want["sha256"] and "full" in got and "full" in want:
        for field in sorted(set(got["full"]) | set(want["full"])):
            assert got["full"].get(field) == want["full"].get(field), f"{key}: field {field}"
    assert got["sha256"] == want["sha256"], f"{key}: canonical JSON differs"
