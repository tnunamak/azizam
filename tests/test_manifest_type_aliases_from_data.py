"""Data-driven compatibility for legacy input specs; strict workflow loading stays strict."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from gateway import manifests as m

ROOT = Path(__file__).resolve().parents[1]


def write_registry(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "aliases.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def fixture_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_registry(tmp_path, {
        "schema_version": 1,
        "aliases": {"legacy_scan": {"type": "asset", "accepts": ["application/pdf"]}},
    })
    aliases = m.load_input_type_aliases(path)
    monkeypatch.setattr(m, "_shipped_input_type_aliases", lambda: aliases)


def test_legacy_input_alias_normalizes_to_asset_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_aliases(tmp_path, monkeypatch)
    legacy = {"type": "legacy_scan", "required": True, "label": "Scan"}
    before = copy.deepcopy(legacy)
    canonical = m.normalize_input_spec(legacy)
    assert canonical == {"type": "asset", "accepts": ["application/pdf"], "required": True, "label": "Scan"}
    assert legacy == before
    assert list(m.asset_leaf_specs(canonical)) == [canonical]
    assert m.accepts_of(canonical) == ("application/pdf",)
    for reference in ("asset:one", "https://example.test/a.pdf", {"asset_id": "one"}, "", {}):
        assert m.validate_input_value("source", reference, legacy) is None
        assert m.validate_input_value("source", reference, canonical) is None
    for value in (None, False, 7, 1.5, []):
        assert m.validate_input_value("source", value, legacy) == m.validate_input_value("source", value, canonical)
        assert m.validate_input_value("source", value, legacy) == "source must be an asset reference"
    assert m.validate_input_value("source", "asset:one", {"type": "unregistered_alias"}) == (
        "source has unsupported type: unregistered_alias"
    )


def test_supported_legacy_reference_acceptance_is_unchanged() -> None:
    # This test also runs unchanged on the base, independently of the new normalizer.
    legacy = {"type": "image"}
    for reference in ("", "asset:one", "https://example.test/a.png", "data:image/png;base64,AA==", {}, {"asset_id": "one"}):
        assert m.validate_input_value("source", reference, legacy) is None
    for value in (None, False, 7, 1.5, [], ["asset:one"]):
        assert m.validate_input_value("source", value, legacy) == "source must be an asset reference"
    assert m.validate_input_value("source", "other", {"type": "image", "enum": ["selected"]}) == (
        "source must be one of: selected"
    )


def test_shipped_alias_keeps_its_media_constraint_and_canonical_core() -> None:
    canonical = m.normalize_input_spec({"type": "image"})
    assert canonical == {"type": "asset", "accepts": ["image/*"]}
    assert m.asset_input_modalities(canonical) == ("image",)
    assert m._validate_value("source", "asset:one", {"type": "image"}) == "source has unsupported type: image"
    for reference in ("asset:one", {"asset_id": "one"}, False):
        assert m.validate_input_value("source", reference, {"type": "image"}) == (
            m.validate_input_value("source", reference, canonical)
        )


def test_nested_aliases_use_the_generic_asset_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_aliases(tmp_path, monkeypatch)
    legacy = {"type": "object", "properties": {
        "pages": {"type": "array", "items": {"type": "legacy_scan"}},
        "title": {"type": "string"},
    }}
    before = copy.deepcopy(legacy)
    canonical = m.normalize_input_spec(legacy)
    assert list(m.asset_leaf_specs(canonical)) == [{"type": "asset", "accepts": ["application/pdf"]}]
    assert m.media_bearing(canonical)
    assert legacy == before
    for value in ({"pages": ["asset:one"], "title": "Report"}, {"pages": [False]}, {"pages": "bad"}):
        assert m.validate_input_value("document", value, legacy) == m.validate_input_value("document", value, canonical)
        assert list(m.value_problems(legacy, value, ("document",))) == list(
            m.value_problems(canonical, value, ("document",))
        )
    assert list(m.value_problems(legacy, {"pages": [False]}, ("document",))) == [
        (("document", "pages", 0), "value_invalid", "the value must be an asset reference"),
    ]


def test_declared_media_constraints_never_widen_an_alias() -> None:
    for declaration, expected in (
        ({"accepts": ["image/png"]}, ["image/png"]),
        ({"accepts": "image/jpeg"}, ["image/jpeg"]),
        ({"media_type": "image/webp"}, ["image/webp"]),
        ({"accepts": ["*/*"]}, ["image/*"]),
        ({"accepts": ["audio/*"]}, []),
        ({"accepts": ["image/png", "audio/*"]}, ["image/png"]),
        ({"accepts": []}, ["image/*"]),
    ):
        legacy = {"type": "image", **declaration}
        before = copy.deepcopy(legacy)
        canonical = m.normalize_input_spec(legacy)
        assert canonical["type"] == "asset"
        assert canonical["accepts"] == expected
        assert "media_type" not in canonical
        assert legacy == before
        # MIME resolution remains separate from the historical value-shape check.
        assert m.validate_input_value("source", "asset:one", legacy) is None


@pytest.mark.parametrize("document", [
    None,
    [],
    {},
    {"schema_version": True, "aliases": {}},
    {"schema_version": 2, "aliases": {}},
    {"schema_version": 1, "aliases": {}, "module": "package.handler"},
    {"schema_version": 1, "aliases": []},
    {"schema_version": 1, "aliases": {"asset": {"type": "asset", "accepts": ["image/*"]}}},
    {"schema_version": 1, "aliases": {"package.handler": {"type": "asset", "accepts": ["image/*"]}}},
    {"schema_version": 1, "aliases": {"legacy": None}},
    {"schema_version": 1, "aliases": {"legacy": {"type": "unknown", "accepts": ["image/*"]}}},
    {"schema_version": 1, "aliases": {"legacy": {"type": "string", "accepts": ["image/*"]}}},
    {"schema_version": 1, "aliases": {"legacy": {"type": "asset"}}},
    {"schema_version": 1, "aliases": {"legacy": {"type": "asset", "accepts": []}}},
    {"schema_version": 1, "aliases": {"legacy": {"type": "asset", "accepts": "image/*"}}},
    {"schema_version": 1, "aliases": {"legacy": {"type": "asset", "accepts": [7]}}},
    {"schema_version": 1, "aliases": {"legacy": {"type": "asset", "accepts": ["not-a-media-type"]}}},
    {"schema_version": 1, "aliases": {"legacy": {"type": "asset", "accepts": ["image/*", "image/*"]}}},
    {"schema_version": 1, "aliases": {"legacy": {"type": "asset", "accepts": ["image/*"], "import": "package.handler"}}},
])
def test_alias_registry_fails_closed(tmp_path: Path, document: object) -> None:
    with pytest.raises(m.ManifestError):
        m.load_input_type_aliases(write_registry(tmp_path, document))


def test_alias_registry_missing_or_invalid_yaml_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(m.ManifestError):
        m.load_input_type_aliases(tmp_path / "missing.yml")
    path = tmp_path / "broken.yml"
    path.write_text("aliases: [", encoding="utf-8")
    with pytest.raises(m.ManifestError):
        m.load_input_type_aliases(path)


def test_strict_loader_does_not_convert_unsupported_schema(tmp_path: Path) -> None:
    # This assertion must remain green on the base and patched trees.
    path = tmp_path / "old.yml"
    path.write_text(yaml.safe_dump({
        "schema_version": 1, "id": "legacy", "intent": "test.input",
        "inputs": {"source": {"type": "image"}}, "outputs": {}, "bindings": {},
    }), encoding="utf-8")
    with pytest.raises(m.ContractErrors) as raised:
        m.load_workflow_manifest(path)
    assert raised.value.primary.code == "schema_version_unsupported"


def test_every_shipped_manifest_loads_without_rewriting() -> None:
    # Run this same test file on base and patched trees. The expected result is
    # unchanged successful loading and byte-equivalent parsed document content.
    paths = sorted([*(ROOT / "manifests").rglob("*.yml"), *(ROOT / "manifests").rglob("*.yaml")])
    assert paths
    for path in paths:
        original = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert m.load_workflow_manifest(path) == original, str(path.relative_to(ROOT))
