from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RESOLUTION_LABELS = {
    "512": "Low detail (fastest)",
    "1024": "Medium detail",
    "1536": "High detail (slowest)",
}


def _input(manifest, name):
    path = ROOT / "manifests" / "workflows" / manifest
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)["inputs"][name]


def _assert_labels(input_spec, expected):
    labels = input_spec.get("choice_labels")
    assert labels == expected
    assert set(labels) == {str(value) for value in input_spec["enum"]}


def test_trellis_resolution_choice_labels():
    _assert_labels(_input("mesh.generate.trellis-2.yml", "resolution"), RESOLUTION_LABELS)


def test_pixal3d_resolution_choice_labels():
    _assert_labels(_input("mesh.generate.pixal3d.yml", "resolution"), RESOLUTION_LABELS)


def test_trellis_background_removal_choice_labels():
    spec = _input("mesh.generate.trellis-2.yml", "bg_removal")
    _assert_labels(
        spec,
        {"threshold": "Quick cutout", "birefnet": "Careful cutout (AI)"},
    )
    assert spec["help"] == (
        "Choose how the background is cut away, or leave it empty to let the service choose."
    )
