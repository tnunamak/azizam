"""Tests for ``scripts/local/upgrade_profiles.py``.

Goals:

* Verify the upgrade is idempotent and preserves key_hash / key / key_env /
  downstream blocks byte-equivalently.
* Verify dry-run never modifies the file; --write does.
* Verify existing profile blocks are preserved unless --replace is passed.
* Verify the resulting profile blocks parse cleanly via gateway.profiles, and
  that builtin templates are conservative (no cloud fallback, no native
  passthrough).
* Verify the CLI never prints secret-looking values.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.profiles import load_profile_from_entry  # noqa: E402
from scripts.local.upgrade_profiles import (  # noqa: E402
    BUILTIN_TEMPLATES,
    STATUS_ADDED,
    STATUS_NO_TEMPLATE,
    STATUS_REPLACED,
    STATUS_UNCHANGED,
    builtin_template,
    main,
    upgrade_document,
)


FAKE_HASH = "sha256:" + "ab" * 32  # 64 hex chars, obviously fake
FAKE_HASH_2 = "sha256:" + "cd" * 32
FAKE_ENV = "FAKE_DOWNSTREAM_KEY_ENV"


def _write_sample(path: Path) -> str:
    text = (
        "keys:\n"
        "  - name: simon\n"
        f"    key_hash: {FAKE_HASH}\n"
        "    downstream:\n"
        "      tabby:\n"
        f"        api_key_env: {FAKE_ENV}\n"
        "  - name: daisy\n"
        f"    key_hash: {FAKE_HASH_2}\n"
        "    downstream:\n"
        "      tabby: {}\n"
        "  - name: stranger\n"
        f"    key_hash: {FAKE_HASH}\n"
    )
    path.write_text(text)
    return text


class TestUpgradeDocument(unittest.TestCase):
    def test_adds_profile_blocks_for_known_names(self) -> None:
        doc = {
            "keys": [
                {"name": "simon", "key_hash": FAKE_HASH},
                {"name": "daisy", "key_hash": FAKE_HASH_2},
            ]
        }
        results = upgrade_document(doc)
        self.assertEqual(results, [("simon", STATUS_ADDED), ("daisy", STATUS_ADDED)])
        self.assertIn("profile", doc["keys"][0])
        self.assertEqual(doc["keys"][0]["profile"]["id"], "simon")
        self.assertEqual(doc["keys"][1]["profile"]["id"], "daisy")

    def test_preserves_existing_fields_byte_equivalent(self) -> None:
        original = {
            "name": "simon",
            "key_hash": FAKE_HASH,
            "downstream": {"tabby": {"api_key_env": FAKE_ENV}},
        }
        doc = {"keys": [dict(original)]}
        upgrade_document(doc)
        entry = doc["keys"][0]
        # All original fields preserved with identical values.
        self.assertEqual(entry["name"], original["name"])
        self.assertEqual(entry["key_hash"], original["key_hash"])
        self.assertEqual(entry["downstream"], original["downstream"])

    def test_idempotent_second_run_is_noop(self) -> None:
        doc = {"keys": [{"name": "simon", "key_hash": FAKE_HASH}]}
        upgrade_document(doc)
        results = upgrade_document(doc)
        self.assertEqual(results, [("simon", STATUS_UNCHANGED)])

    def test_existing_profile_preserved_without_replace(self) -> None:
        custom_profile = {"id": "simon", "status_visibility": {"level": "full"}}
        doc = {
            "keys": [
                {
                    "name": "simon",
                    "key_hash": FAKE_HASH,
                    "profile": dict(custom_profile),
                }
            ]
        }
        results = upgrade_document(doc)
        self.assertEqual(results, [("simon", STATUS_UNCHANGED)])
        self.assertEqual(doc["keys"][0]["profile"], custom_profile)

    def test_replace_overrides_existing_profile(self) -> None:
        doc = {
            "keys": [
                {
                    "name": "simon",
                    "key_hash": FAKE_HASH,
                    "profile": {"id": "simon", "status_visibility": {"level": "full"}},
                }
            ]
        }
        results = upgrade_document(doc, replace=True)
        self.assertEqual(results, [("simon", STATUS_REPLACED)])
        self.assertEqual(
            doc["keys"][0]["profile"]["status_visibility"]["level"], "basic"
        )

    def test_unknown_name_reports_no_template(self) -> None:
        doc = {"keys": [{"name": "stranger", "key_hash": FAKE_HASH}]}
        results = upgrade_document(doc)
        self.assertEqual(results, [("stranger", STATUS_NO_TEMPLATE)])
        self.assertNotIn("profile", doc["keys"][0])

    def test_only_filter_limits_scope(self) -> None:
        doc = {
            "keys": [
                {"name": "simon", "key_hash": FAKE_HASH},
                {"name": "daisy", "key_hash": FAKE_HASH_2},
            ]
        }
        results = upgrade_document(doc, only={"simon"})
        self.assertEqual(results, [("simon", STATUS_ADDED)])
        self.assertIn("profile", doc["keys"][0])
        self.assertNotIn("profile", doc["keys"][1])

    def test_builtin_templates_are_conservative(self) -> None:
        for name, template in BUILTIN_TEMPLATES.items():
            self.assertFalse(
                template.get("fallback", {}).get("cloud_allowed", False),
                f"{name}: cloud_allowed must default to False",
            )
            self.assertFalse(
                template.get("native_passthrough", {}).get("enabled", False),
                f"{name}: native_passthrough.enabled must default to False",
            )
            # Should have no model/workflow/backend allow-lists.
            self.assertNotIn("models", template, name)
            self.assertNotIn("workflows", template, name)
            self.assertNotIn("backends", template, name)

    def test_builtin_templates_parse_via_gateway_profiles(self) -> None:
        for name, template in BUILTIN_TEMPLATES.items():
            entry = {"name": name, "key_hash": FAKE_HASH, "profile": dict(template)}
            profile = load_profile_from_entry(entry)
            self.assertEqual(profile.id, name)
            self.assertFalse(profile.fallback.cloud_allowed)
            self.assertFalse(profile.native_passthrough.enabled)

    def test_builtin_template_returns_deep_copy(self) -> None:
        a = builtin_template("simon")
        b = builtin_template("simon")
        self.assertIsNotNone(a)
        a["fallback"]["cloud_allowed"] = True
        self.assertFalse(b["fallback"]["cloud_allowed"])  # type: ignore[index]


class TestCLI(unittest.TestCase):
    def _run(self, *args: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(args))
        return code, buf.getvalue()

    def _assert_no_secrets(self, output: str) -> None:
        # Hash and env var names must never appear in CLI output.
        self.assertNotIn(FAKE_HASH, output)
        self.assertNotIn(FAKE_HASH_2, output)
        self.assertNotIn(FAKE_ENV, output)
        self.assertNotIn("sha256:", output)
        self.assertNotIn("key_hash", output)
        self.assertNotIn("api_key_env", output)
        self.assertNotIn("downstream", output)

    def test_dry_run_does_not_modify_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.yml"
            original_text = _write_sample(path)
            code, out = self._run("--keys-file", str(path))
            self.assertEqual(code, 0)
            self.assertEqual(path.read_text(), original_text)
            # Reports a plan.
            self.assertIn("DRY-RUN", out)
            self.assertIn("simon: added", out)
            self.assertIn("daisy: added", out)
            self.assertIn("stranger: no-template", out)
            self._assert_no_secrets(out)

    def test_write_modifies_file_and_preserves_existing_fields(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.yml"
            _write_sample(path)
            code, out = self._run("--keys-file", str(path), "--write")
            self.assertEqual(code, 0)
            self.assertIn("WRITE", out)
            self._assert_no_secrets(out)

            doc = yaml.safe_load(path.read_text())
            entries = {e["name"]: e for e in doc["keys"]}
            self.assertEqual(entries["simon"]["key_hash"], FAKE_HASH)
            self.assertEqual(
                entries["simon"]["downstream"]["tabby"]["api_key_env"], FAKE_ENV
            )
            self.assertEqual(entries["daisy"]["key_hash"], FAKE_HASH_2)
            self.assertIn("profile", entries["simon"])
            self.assertIn("profile", entries["daisy"])
            self.assertNotIn("profile", entries["stranger"])

    def test_write_is_idempotent(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.yml"
            _write_sample(path)
            self._run("--keys-file", str(path), "--write")
            first = path.read_text()
            code, out = self._run("--keys-file", str(path), "--write")
            self.assertEqual(code, 0)
            self.assertEqual(path.read_text(), first)
            self.assertIn("simon: unchanged", out)
            self.assertIn("daisy: unchanged", out)
            self._assert_no_secrets(out)

    def test_only_filter_via_cli(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.yml"
            _write_sample(path)
            code, out = self._run(
                "--keys-file", str(path), "--write", "--only", "simon"
            )
            self.assertEqual(code, 0)
            doc = yaml.safe_load(path.read_text())
            entries = {e["name"]: e for e in doc["keys"]}
            self.assertIn("profile", entries["simon"])
            self.assertNotIn("profile", entries["daisy"])
            self.assertIn("simon: added", out)
            self.assertNotIn("daisy:", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
