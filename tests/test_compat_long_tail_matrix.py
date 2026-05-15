"""Schema and consistency tests for the long-tail compatibility matrix.

These tests are stdlib-only and never call live services. They prove
that ``tests/compat/matrix/long_tail_targets.json`` stays internally
consistent and that the curated markdown doc stays roughly in sync with
the JSON source of truth.

Run via::

    /home/tnunamak/applications/openai-proxy/venv/bin/python \\
        -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MATRIX_DIR = REPO_ROOT / "tests" / "compat" / "matrix"
TARGETS_PATH = MATRIX_DIR / "long_tail_targets.json"
SCHEMA_PATH = MATRIX_DIR / "long_tail_targets.schema.json"
DOC_PATH = REPO_ROOT / "docs" / "COMPATIBILITY_TARGET_MATRIX.md"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CASE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
ENDPOINT_RE = re.compile(r"^/")

ALLOWED_PROTOCOLS = {
    "openai-chat",
    "openai-completions",
    "openai-responses",
    "openai-embeddings",
    "openai-images",
    "openai-audio-speech",
    "openai-audio-transcriptions",
    "anthropic-messages",
    "ollama-native",
    "gateway-native",
}
ALLOWED_CATEGORIES = {
    "chat-frontend",
    "coding-agent",
    "automation",
    "voice-assistant",
    "sdk-framework",
    "roleplay-frontend",
    "agent-runtime",
}
ALLOWED_DEPLOYMENTS = {
    "self-hosted",
    "desktop",
    "cli",
    "library",
    "cloud-or-self-hosted",
}
ALLOWED_AUTH = {"bearer", "x-api-key", "custom-header", "none"}
ALLOWED_PRIORITIES = {"P0", "P1", "P2"}
ALLOWED_STATUS = {"unverified", "manual-smoke", "automated-probe", "broken"}

REQUIRED_TARGET_KEYS = (
    "id",
    "name",
    "category",
    "deployment",
    "protocols",
    "client_config",
    "endpoints",
    "smoke_cases",
    "priority",
    "status",
    "references",
)


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class TestLongTailMatrixFiles(unittest.TestCase):
    def test_files_exist(self) -> None:
        self.assertTrue(TARGETS_PATH.exists(), TARGETS_PATH)
        self.assertTrue(SCHEMA_PATH.exists(), SCHEMA_PATH)
        self.assertTrue(DOC_PATH.exists(), DOC_PATH)

    def test_json_parses(self) -> None:
        data = _load_json(TARGETS_PATH)
        self.assertIn("version", data)
        self.assertIn("generated_at", data)
        self.assertIn("targets", data)
        self.assertIsInstance(data["targets"], list)
        self.assertGreaterEqual(len(data["targets"]), 1)

    def test_schema_parses_and_is_draft_2020(self) -> None:
        schema = _load_json(SCHEMA_PATH)
        self.assertEqual(
            schema.get("$schema"),
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertEqual(schema.get("type"), "object")


class TestTargetRowShape(unittest.TestCase):
    """Lightweight schema enforcement without pulling in jsonschema."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = _load_json(TARGETS_PATH)
        cls.targets = cls.data["targets"]

    def test_top_level_fields(self) -> None:
        self.assertRegex(self.data["generated_at"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIsInstance(self.data["version"], str)
        self.assertTrue(self.data["version"])

    def test_required_keys_present(self) -> None:
        for target in self.targets:
            with self.subTest(id=target.get("id")):
                for key in REQUIRED_TARGET_KEYS:
                    self.assertIn(key, target, f"missing {key!r}")

    def test_ids_are_unique_and_kebab_case(self) -> None:
        seen: set[str] = set()
        for target in self.targets:
            tid = target["id"]
            with self.subTest(id=tid):
                self.assertRegex(tid, ID_RE.pattern)
                self.assertNotIn(tid, seen, f"duplicate id {tid!r}")
                seen.add(tid)

    def test_enum_fields(self) -> None:
        for target in self.targets:
            with self.subTest(id=target["id"]):
                self.assertIn(target["category"], ALLOWED_CATEGORIES)
                self.assertIn(target["deployment"], ALLOWED_DEPLOYMENTS)
                self.assertIn(target["priority"], ALLOWED_PRIORITIES)
                self.assertIn(target["status"], ALLOWED_STATUS)

    def test_protocols(self) -> None:
        for target in self.targets:
            with self.subTest(id=target["id"]):
                protocols = target["protocols"]
                self.assertIsInstance(protocols, list)
                self.assertGreaterEqual(len(protocols), 1)
                for proto in protocols:
                    self.assertIn(proto, ALLOWED_PROTOCOLS)
                self.assertEqual(
                    len(protocols),
                    len(set(protocols)),
                    "duplicate protocol entry",
                )

    def test_client_config(self) -> None:
        for target in self.targets:
            with self.subTest(id=target["id"]):
                cfg = target["client_config"]
                self.assertIn("base_url_template", cfg)
                self.assertIn("${PROXY_BASE_URL}", cfg["base_url_template"])
                self.assertIn(cfg["auth"], ALLOWED_AUTH)
                if cfg["auth"] == "custom-header":
                    self.assertIn("auth_header", cfg)
                    self.assertTrue(cfg["auth_header"])
                extra = cfg.get("extra_settings", {})
                self.assertIsInstance(extra, dict)
                for k, v in extra.items():
                    self.assertIsInstance(k, str)
                    self.assertIsInstance(v, str)

    def test_endpoints(self) -> None:
        for target in self.targets:
            with self.subTest(id=target["id"]):
                endpoints = target["endpoints"]
                self.assertGreaterEqual(len(endpoints), 1)
                for ep in endpoints:
                    self.assertRegex(ep, ENDPOINT_RE.pattern)
                self.assertEqual(
                    len(endpoints),
                    len(set(endpoints)),
                    "duplicate endpoint entry",
                )

    def test_smoke_cases(self) -> None:
        for target in self.targets:
            with self.subTest(id=target["id"]):
                cases = target["smoke_cases"]
                self.assertGreaterEqual(len(cases), 1)
                for case in cases:
                    self.assertRegex(case, CASE_RE.pattern)
                self.assertEqual(
                    len(cases), len(set(cases)), "duplicate smoke case"
                )

    def test_references_are_https(self) -> None:
        for target in self.targets:
            with self.subTest(id=target["id"]):
                refs = target["references"]
                self.assertGreaterEqual(len(refs), 1)
                for ref in refs:
                    self.assertTrue(
                        ref.startswith("https://"),
                        f"non-https reference: {ref!r}",
                    )


class TestDocSyncedWithJson(unittest.TestCase):
    """The markdown overview should mention every JSON id at least once."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = _load_json(TARGETS_PATH)
        cls.doc_text = DOC_PATH.read_text(encoding="utf-8")

    def test_doc_mentions_every_id(self) -> None:
        for target in self.data["targets"]:
            with self.subTest(id=target["id"]):
                self.assertIn(
                    target["id"],
                    self.doc_text,
                    f"docs/COMPATIBILITY_TARGET_MATRIX.md does not mention "
                    f"target id {target['id']!r}",
                )

    def test_doc_points_to_json_and_schema(self) -> None:
        self.assertIn("long_tail_targets.json", self.doc_text)
        self.assertIn("long_tail_targets.schema.json", self.doc_text)


if __name__ == "__main__":
    unittest.main()
