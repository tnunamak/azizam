"""Observation contracts using representative, anonymized CSV output."""
import pytest
import yaml

from gateway.gpu_observation import load_registry, observe


LINE = "0, device-id, Example device, 1024, 24576, 7, 42, 25.50, 300.0, N/A, 350, 1200, 5000, N/A, 0x00000000, 4, 5, 16, 16"
EXPECTED = {
    "index": 0, "uuid": "device-id", "name": "Example device",
    "memory_used_mb": 1024, "memory_total_mb": 24576,
    "instantaneous_util_pct": 7, "temp_c": 42, "power_draw_w": 25.5,
    "power_limit_w": 300.0, "power_default_limit_w": None, "power_max_limit_w": 350.0,
    "clocks_gr_mhz": 1200, "clocks_mem_mhz": 5000, "fan_speed_pct": None,
    "throttle_reasons_hex": "0x00000000", "pcie_link_gen": 4, "pcie_link_gen_max": 5,
    "pcie_link_width": 16, "pcie_link_width_max": 16,
}


def test_csv_matches_legacy_dict():
    calls = []
    def runner(argv):
        calls.append(argv)
        return 0, LINE, ""
    actual = observe(runner)
    assert actual == [EXPECTED]
    assert {key: type(value) for key, value in actual[0].items()} == {
        key: type(value) for key, value in EXPECTED.items()
    }
    assert calls == [load_registry()["argv"]]


def test_missing_tool():
    def missing(_argv):
        raise FileNotFoundError("missing")
    assert observe(missing) is None
    assert observe(lambda _argv: (127, "", "not found")) is None


def test_failed_or_empty_output():
    assert observe(lambda _argv: (1, LINE, "failed")) is None
    assert observe(lambda _argv: (124, "", "timeout")) is None
    assert observe(lambda _argv: (0, "  \n", "")) is None


def test_short_invalid_and_extra_columns():
    invalid = LINE.replace("1024", "N/A")
    assert observe(lambda _argv: (0, "short,row\n" + invalid, "")) is None
    assert observe(lambda _argv: (0, invalid + "\n" + LINE + ", ignored", "")) == [EXPECTED]


def test_registry_loads_and_rejects_unknown_type(tmp_path):
    config = load_registry()
    assert len(config["fields"]) == 19
    assert len(config["argv"]) == 3
    config["fields"][0]["type"] = "unknown"
    path = tmp_path / "observation.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="observation_field_type_unknown"):
        load_registry(path)
