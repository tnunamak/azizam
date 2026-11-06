"""Workflow manifests carry plain words for people; research notes live in docs.

Every string a member can read from a manifest (the display name, purpose,
summary or fallback description, and each input's label, help and description)
is checked against the UI quality sweep's research-note list and the required
copy terms below. Provenance, forum
reports and bake-off results belong in docs/workflows/<id>.md.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from gateway.manifests import ManifestError, validate_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "manifests" / "workflows"
# Exact legacy filenames only; remove entries as their copy is migrated.
LEGACY_COPY_ALLOWLIST: set[str] = set()
REQUIRED_TERMS = (
    "reddit", "r/", "community", "consensus", "bake-off", "bakeoff",
    "footgun", "sweet spot", "our test", "we tested", "piss", "TODO",
    "http", ".safetensors", ".gguf",
)
NOTES = ROOT / "docs" / "workflows"
TERMS = ROOT / "web" / "e2e" / "sweep" / "lists" / "research-notes.txt"


def load_terms(path: Path = TERMS) -> list[tuple[str, re.Pattern[str]]]:
    """The sweep's term-list syntax: plain terms are whole words, optional plural; `re:` is a regex."""
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            terms.append((line, re.compile(line[3:], re.IGNORECASE)))
        else:
            escaped = re.escape(line).replace(r"\ ", r"\s+")
            terms.append((line, re.compile(rf"(?<![\w-]){escaped}s?(?![\w-])", re.IGNORECASE)))
    return terms


def research_terms_in(text: str, terms=None) -> list[str]:
    return [term for term, pattern in (terms or load_terms()) if pattern.search(text)]


def member_strings(manifest: dict) -> list[tuple[str, str]]:
    found = []
    for key in ("display_name", "purpose"):
        if isinstance(manifest.get(key), str):
            found.append((key, manifest[key]))
    description_key = "summary" if "summary" in manifest else "description"
    if isinstance(manifest.get(description_key), str):
        found.append((description_key, manifest[description_key]))
    for name, spec in (manifest.get("inputs") or {}).items():
        if not isinstance(spec, dict):
            continue
        for key in ("label", "help", "description"):
            if isinstance(spec.get(key), str):
                found.append((f"inputs.{name}.{key}", spec[key]))
    return found


def copy_problems(manifests: dict, terms) -> list[str]:
    return [
        f"{file} {field}: {', '.join(hits)}"
        for file, manifest in manifests.items()
        for field, text in member_strings(manifest)
        if file not in LEGACY_COPY_ALLOWLIST
        if (hits := research_terms_in(text, terms) + [
            term for term in REQUIRED_TERMS if term.casefold() in text.casefold()
        ])
    ]


class ManifestCopyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.terms = load_terms()
        self.manifests = {
            path.relative_to(MANIFESTS).as_posix(): yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in sorted(MANIFESTS.rglob("*"))
            if path.is_file() and path.suffix in {".yml", ".yaml"}
        }

    def test_the_list_is_the_sweep_list(self) -> None:
        self.assertTrue(TERMS.is_file())
        self.assertIn("footgun", [term for term, _ in self.terms])
        self.assertIn("sweet spot", [term for term, _ in self.terms])

    def test_no_member_visible_string_carries_research_notes(self) -> None:
        self.assertTrue(self.manifests, "the manifest directory must not be empty")
        problems = copy_problems(self.manifests, self.terms)
        self.assertEqual(problems, [], "move research notes to docs/workflows/<id>.md")

    def test_summary_is_short(self) -> None:
        for file, manifest in self.manifests.items():
            if "summary" in manifest and file not in LEGACY_COPY_ALLOWLIST:
                with self.subTest(file=file):
                    self.assertLessEqual(len(manifest["summary"].split()), 40)

    def test_summary_replaces_research_description_in_guard(self) -> None:
        manifest = {"summary": "Make a picture.", "description": "Reddit community research.", "inputs": {}}
        self.assertIn(("summary", "Make a picture."), member_strings(manifest))
        self.assertNotIn(("description", manifest["description"]), member_strings(manifest))
        del manifest["summary"]
        self.assertIn(("description", manifest["description"]), member_strings(manifest))

    def test_new_manifest_cannot_inherit_legacy_copy_exemption(self) -> None:
        self.assertNotIn("brand-new-workflow.yml", LEGACY_COPY_ALLOWLIST)
        for term in REQUIRED_TERMS:
            with self.subTest(term=term):
                text = f"An example of {term.upper()} in member copy."
                for field in ("summary", "description", "label", "help"):
                    manifest = {field: text} if field in {"summary", "description"} else {
                        "inputs": {"prompt": {field: text}}
                    }
                    self.assertTrue(copy_problems({"brand-new-workflow.yml": manifest}, self.terms))

    def test_validator_accepts_optional_summary_and_license(self) -> None:
        base = {"schema_version": 1, "id": "x", "intent": "image.generate",
                "inputs": {}, "outputs": {}, "bindings": {}}
        validate_manifest(base)
        validate_manifest({**base, "summary": "Make a picture.", "license": "Apache-2.0"})
        validate_manifest({**base, "summary": " ".join(["word"] * 40)})

    def test_validator_rejects_invalid_summary_and_license(self) -> None:
        base = {"schema_version": 1, "id": "x", "intent": "image.generate",
                "inputs": {}, "outputs": {}, "bindings": {}}
        for key in ("summary", "license"):
            for value in (None, False, 1, [], {}, "", " "):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ManifestError, f"manifest {key} must be a non-empty string"):
                        validate_manifest({**base, key: value})
        with self.assertRaisesRegex(ManifestError, "summary must contain at most 40 words"):
            validate_manifest({**base, "summary": " ".join(["word"] * 41)})

    def test_labels_are_short_plain_words(self) -> None:
        for file, manifest in self.manifests.items():
            for name, spec in (manifest.get("inputs") or {}).items():
                label = spec.get("label") if isinstance(spec, dict) else None
                if label is None:
                    continue
                with self.subTest(file=file, input=name):
                    self.assertLessEqual(len(label.split()), 4)
                    self.assertNotIn("_", label)

    def test_help_is_short(self) -> None:
        for file, manifest in self.manifests.items():
            for name, spec in (manifest.get("inputs") or {}).items():
                help_text = spec.get("help") if isinstance(spec, dict) else None
                if help_text is None:
                    continue
                with self.subTest(file=file, input=name):
                    self.assertLessEqual(len(help_text.split()), 20)
                    self.assertNotIn("_", help_text)

    def test_every_picture_input_has_plain_words(self) -> None:
        for file, manifest in self.manifests.items():
            for name, spec in (manifest.get("inputs") or {}).items():
                if not isinstance(spec, dict) or spec.get("type") != "asset":
                    continue
                with self.subTest(file=file, input=name):
                    self.assertTrue(spec.get("label"), "an asset input needs a label")
                    self.assertTrue(spec.get("help"), "an asset input needs help")

    def test_notes_files_name_a_manifest(self) -> None:
        ids = {manifest["id"] for manifest in self.manifests.values()}
        for notes in sorted(NOTES.glob("*.md")):
            with self.subTest(notes=notes.name):
                self.assertIn(notes.stem, ids)

    def test_the_guard_catches_a_note(self) -> None:
        bad = {"display_name": "X", "inputs": {"steps": {"type": "integer", "help": "8 is the community sweet spot."}}}
        hits = [h for _, text in member_strings(bad) for h in research_terms_in(text, self.terms)]
        self.assertTrue(hits)
        self.assertTrue(research_terms_in("Reddit says CFG 3 avoids the piss filter.", self.terms))
        self.assertFalse(research_terms_in("More steps add detail but take longer.", self.terms))

    def test_validator_rejects_empty_label_or_help(self) -> None:
        base = {"schema_version": 1, "id": "x", "intent": "image.generate", "outputs": {}, "bindings": {}}
        for key in ("label", "help"):
            with self.subTest(key=key), self.assertRaises(ManifestError):
                validate_manifest({**base, "inputs": {"steps": {"type": "integer", key: " "}}})


if __name__ == "__main__":
    unittest.main()
