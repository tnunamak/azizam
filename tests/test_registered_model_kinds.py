"""A registered model may declare its kind: the one capability it serves.

An engine that also embeds used to bind its embeddings capability to every
model it registered, so a chat model listed as an embedding model as well. A
model with a kind binds only the capability that kind names. A model without
one keeps every capability its engine offers, so existing records do not
change meaning.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import capability_ids, engine_admin, engine_check  # noqa: E402
from gateway.engine_definitions import load_engine_catalog  # noqa: E402
from gateway.models_catalog import build_models_catalog, registered_model_capabilities  # noqa: E402

CHAT_AND_EMBED = ["chat", "completions", "embeddings"]
MODELS = [
    {"model_group": "chat-model", "upstream_model": "chat-upstream", "kind": "chat"},
    {"model_group": "embed-model", "upstream_model": "embed-upstream", "kind": "embed"},
    {"model_group": "plain-model", "upstream_model": "plain-upstream"},
]


def _engine(models: list[dict]) -> dict:
    return {"services": {"engine": {"enabled": True, "capabilities": CHAT_AND_EMBED, "config": {"models": models}}}}


def _intents_by_model(services: dict) -> dict[str, set[str]]:
    bound: dict[str, set[str]] = {}
    for capability in registered_model_capabilities(services):
        bound.setdefault(capability.model, set()).add(capability.intent)
    return bound


class RegisteredModelKindTests(unittest.TestCase):
    def test_a_kinded_model_binds_only_the_capability_its_kind_names(self) -> None:
        bound = _intents_by_model(_engine(MODELS))
        self.assertEqual(bound["chat-model"], {"chat.completions", "text.completions"})
        self.assertEqual(bound["embed-model"], {"text.embeddings"})

    def test_a_model_without_a_kind_keeps_every_capability_its_engine_offers(self) -> None:
        bound = _intents_by_model(_engine(MODELS))
        self.assertEqual(bound["plain-model"], {"chat.completions", "text.completions", "text.embeddings"})

    def test_a_kind_outside_the_vocabulary_binds_like_no_kind(self) -> None:
        bound = _intents_by_model(_engine([{"model_group": "odd", "upstream_model": "odd-upstream", "kind": "video"}]))
        self.assertEqual(bound["odd"], {"chat.completions", "text.completions", "text.embeddings"})

    def test_the_catalog_lists_each_model_under_its_own_kind(self) -> None:
        services = _engine(MODELS)
        catalog = build_models_catalog(capabilities=registered_model_capabilities(services), service_records=services)
        listed = {row["id"]: {cap["capability"] for cap in row["capabilities"]} for row in catalog["models"]}
        self.assertEqual(listed["chat-model"], {"chat"})
        self.assertEqual(listed["embed-model"], {"embed"})
        self.assertEqual(listed["plain-model"], {"chat", "embed"})


class EngineFormKindTests(unittest.TestCase):
    def test_the_engine_form_keeps_a_kind_from_the_vocabulary(self) -> None:
        rows = engine_admin._models_config({}, {}, [{"id": "e", "public_name": "e", "kind": "embed"}])
        self.assertEqual(rows[0]["kind"], "embed")

    def test_a_row_without_a_kind_saves_no_kind(self) -> None:
        rows = engine_admin._models_config({}, {}, [{"id": "c", "public_name": "c", "kind": ""}])
        self.assertNotIn("kind", rows[0])

    def test_the_engine_form_refuses_a_kind_outside_the_vocabulary(self) -> None:
        with self.assertRaises(engine_check.DraftError):
            engine_admin._models_config({}, {}, [{"id": "v", "public_name": "v", "kind": "video"}])

    def test_the_definitions_api_offers_the_kinds_with_builder_labels(self) -> None:
        kinds = load_engine_catalog().public()["model_kinds"]
        self.assertEqual([kind["id"] for kind in kinds], list(capability_ids.MODEL_KINDS))
        self.assertEqual({kind["id"]: kind["label"] for kind in kinds}["embed"], "Embeddings")


class ProbedModelKindTests(unittest.TestCase):
    def test_a_probed_model_suggests_the_kind_its_one_capability_names(self) -> None:
        catalog = load_engine_catalog()
        self.assertEqual(engine_check.suggested_model_kind(["embeddings"], catalog), "embed")
        self.assertEqual(engine_check.suggested_model_kind(["chat"], catalog), "chat")

    def test_a_probed_model_with_two_capabilities_or_none_suggests_nothing(self) -> None:
        catalog = load_engine_catalog()
        self.assertIsNone(engine_check.suggested_model_kind(["chat", "embeddings"], catalog))
        self.assertIsNone(engine_check.suggested_model_kind([], catalog))


if __name__ == "__main__":
    unittest.main()
