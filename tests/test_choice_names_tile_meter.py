from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _assert_labels(manifest, name, expected):
    with (ROOT / "manifests" / "workflows" / manifest).open(encoding="utf-8") as stream:
        input_spec = yaml.safe_load(stream)["inputs"][name]
    assert input_spec.get("choice_labels") == expected
    assert set(input_spec["choice_labels"]) == {str(value) for value in input_spec["enum"]}


def test_sdxl_tile_sampler_choice_labels():
    _assert_labels(
        "sdxl-seamless-tile.yml",
        "sampler",
        {
            "euler": "Euler",
            "euler_ancestral": "Euler ancestral",
            "dpmpp_2m": "DPM++ 2M",
            "dpmpp_2m_sde": "DPM++ 2M SDE",
            "dpmpp_sde": "DPM++ SDE",
            "uni_pc": "UniPC",
        },
    )


def test_sdxl_tile_scheduler_choice_labels():
    _assert_labels(
        "sdxl-seamless-tile.yml",
        "scheduler",
        {
            "karras": "Karras",
            "normal": "Normal",
            "simple": "Simple",
            "sgm_uniform": "SGM uniform",
            "beta": "Beta",
        },
    )


def test_ace_step_timesignature_choice_labels():
    _assert_labels(
        "ace-step-1.5-xl-turbo.yml",
        "timesignature",
        {
            "2": "2 beats per bar",
            "3": "3 beats per bar",
            "4": "4 beats per bar",
            "6": "6 beats per bar",
        },
    )
