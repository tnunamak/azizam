"""Registry-driven frame-grid and size defaults, without mutating the shared module."""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from gateway import video_grid


def document():
    return {
        "stride_reasons": {"default": "generic_stride", "by_step": {4: "selected_stride"}},
        "grid_defaults": {
            "length_step": 4, "length_offset": 0, "minimum_side": 32,
            "size_when_unparsed": [512, 512],
        },
    }


def test_synthetic_registry_controls_all_defaults(monkeypatch, tmp_path):
    path = tmp_path / "synthetic-grid.yml"
    path.write_text(yaml.safe_dump(document()), encoding="utf-8")
    source = Path(video_grid.__file__)
    spec = importlib.util.spec_from_file_location("_video_grid_defaults_fixture", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original_read = Path.read_text
    reads = []

    def read_text(candidate, *args, **kwargs):
        if candidate == video_grid.BUNDLED_PATH:
            reads.append(candidate)
            return original_read(path, *args, **kwargs)
        return original_read(candidate, *args, **kwargs)

    # A fresh module exercises import-time dataclass and keyword defaults; the
    # production module used by other tests remains untouched.
    with monkeypatch.context() as context:
        context.setattr(Path, "read_text", read_text)
        context.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    assert reads == [video_grid.BUNDLED_PATH]
    assert (module.GridSpec(fps=24).step, module.GridSpec(fps=24).offset) == (4, 0)
    facts = module.video_facts({"capability": "clip", "clip": {"grid": {"fps": 24}}})
    assert (facts.grid.step, facts.grid.offset) == (4, 0)
    assert module.seconds_to_length.__kwdefaults__ == {"step": 4, "offset": 0}
    assert module.seconds_to_length(5, 24) == 120
    assert module.seconds_to_length(5, 24, step=8, offset=1) == 121
    assert module.DEFAULT_SIZE_WHEN_UNPARSED == (512, 512)
    assert module.MINIMUM_SIDE == 32
    assert module.parse_size("wide") == (512, 512)
    assert module.resolve_size("40x40", module.SizeSpec(default="512x512", max="1024x1024", multiple=32)) == (32, 32)
    assert module.stride_reason(4) == "selected_stride"
    assert module.stride_reason(7) == "generic_stride"
    assert module.load_stride_reasons(path) == document()["stride_reasons"]


@pytest.mark.parametrize("case", [
    "missing", "extra", "zero-step", "three-size", "negative-offset",
    "zero-side", "zero-size", "bool-step",
])
def test_invalid_defaults_name_file(tmp_path, case):
    data = document()
    if case == "missing":
        del data["grid_defaults"]
    elif case == "extra":
        data["grid_defaults"]["unknown"] = 1
    elif case == "zero-step":
        data["grid_defaults"]["length_step"] = 0
    elif case == "three-size":
        data["grid_defaults"]["size_when_unparsed"] = [512, 512, 512]
    elif case == "negative-offset":
        data["grid_defaults"]["length_offset"] = -1
    elif case == "zero-side":
        data["grid_defaults"]["minimum_side"] = 0
    elif case == "zero-size":
        data["grid_defaults"]["size_when_unparsed"] = [0, 512]
    else:
        data["grid_defaults"]["length_step"] = True
    path = tmp_path / "invalid-grid.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        video_grid.load_stride_reasons(path)
    assert str(path) in str(caught.value)


@pytest.mark.parametrize(("key", "value"), [
    pytest.param("length_step", 4.0, id="float-length-step"),
    pytest.param("length_offset", 0.0, id="float-length-offset"),
    pytest.param("minimum_side", 32.0, id="float-minimum-side"),
    pytest.param("size_when_unparsed", [512.0, 512], id="float-size-first"),
    pytest.param("size_when_unparsed", [512, 512.0], id="float-size-second"),
    pytest.param("size_when_unparsed", [512.0, 512.0], id="float-size-both"),
    pytest.param("length_step", True, id="bool-length-step"),
])
def test_defaults_require_exact_integers(tmp_path, key, value):
    data = document()
    data["grid_defaults"][key] = value
    path = tmp_path / "non-integer-grid.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        video_grid.load_stride_reasons(path)

    message = str(caught.value)
    assert str(path) in message
    assert key in message


def test_shipped_defaults_preserve_base_behavior():
    data = yaml.safe_load(video_grid.BUNDLED_PATH.read_text(encoding="utf-8"))["grid_defaults"]
    assert data == {
        "length_step": 8, "length_offset": 1, "minimum_side": 64,
        "size_when_unparsed": [1024, 1024],
        "fallback_fps": 25, "fallback_size": "1280x720", "fallback_size_multiple": 32,
        "fallback_min_seconds": 1, "fallback_max_seconds": 8,
    }
    assert (video_grid.GridSpec(fps=24).step, video_grid.GridSpec(fps=24).offset) == (8, 1)
    facts = video_grid.video_facts({"capability": "clip", "clip": {"grid": {"fps": 24}}})
    assert (facts.grid.step, facts.grid.offset) == (8, 1)
    assert video_grid.seconds_to_length.__kwdefaults__ == {"step": 8, "offset": 1}
    assert video_grid.DEFAULT_SIZE_WHEN_UNPARSED == (1024, 1024)
    assert video_grid.MINIMUM_SIDE == 64
    assert video_grid.parse_size("wide") == (1024, 1024)
    assert video_grid.resolve_size("40x40", video_grid.SizeSpec(default="512x512", max="1024x1024", multiple=32)) == (64, 64)
    # Base formula: frames=120; k=round((120-1)/8)=15; 15*8+1=121.
    assert video_grid.seconds_to_length(5, 24) == 121
