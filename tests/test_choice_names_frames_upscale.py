from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _input(manifest, name):
    with (ROOT / "manifests" / "workflows" / manifest).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)["inputs"][name]


def _assert_labels(input_spec, expected):
    assert input_spec.get("choice_labels") == expected
    assert set(input_spec["choice_labels"]) == {str(value) for value in input_spec["enum"]}


def test_rife_multiplier_choice_labels():
    _assert_labels(
        _input("rife-interpolate.yml", "multiplier"),
        {
            "2": "Twice the frames",
            "3": "Three times the frames (not tested here)",
            "4": "Four times the frames (not tested here)",
        },
    )


def test_seedvr2_scale_choice_labels():
    scale = _input("seedvr2-upscale.yml", "scale")
    assert scale["enum"] == [2, 4]
    assert all(type(value) is int for value in scale["enum"])
    _assert_labels(scale, {"2": "2× width and height", "4": "4× width and height"})


def test_seedvr2_color_correction_choice_labels():
    _assert_labels(
        _input("seedvr2-upscale.yml", "color_correction_method"),
        {
            "none": "No color matching",
            "lab": "Most faithful colors",
            "wavelet": "Keep fine detail",
            "adain": "Fastest, roughest",
        },
    )
