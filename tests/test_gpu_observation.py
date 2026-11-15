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


def test_executable_comes_from_registry(monkeypatch):
    from gateway import gpu_observation

    monkeypatch.setattr(gpu_observation, "load_registry", lambda path: {"argv": ["alternate-probe"]})
    assert gpu_observation.executable() == "alternate-probe"


def test_sampler_pci_query_uses_configured_executable(monkeypatch):
    import metrics_sampler
    from gateway import gpu_observation

    calls = []
    monkeypatch.setattr(gpu_observation, "executable", lambda: "alternate-probe")
    output = (
        "0, 00000000:01:00.0\n"
        "2, 0000:ab:02.0\n"
        "3, 0001:01:00.0\n"
        "bad, 0000:03:00.0\n"
        "4, malformed\n"
        "5, 0000:01:00.0\n"
    )
    def runner(argv):
        calls.append(argv)
        return 0, output, ""
    monkeypatch.setattr(metrics_sampler.status_endpoint, "_run", runner)
    assert metrics_sampler._gpu_index_by_drm_id() == {0x100: 5, 0xab02: 2}
    assert calls == [["alternate-probe", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"]]
    monkeypatch.setattr(metrics_sampler.status_endpoint, "_run", lambda argv: (1, output, "failure"))
    assert metrics_sampler._gpu_index_by_drm_id() == {}


def test_renderer_tooltip_uses_configured_executable(monkeypatch):
    import firehose_render
    from gateway import gpu_observation

    tool = load_registry()["argv"][0]
    expected = (
        '<span class="muted" title="processes hold /dev/nvidia* via DRM '
        "(Xwayland, plasmashell, browsers, etc.) but don't appear in "
        + tool + ' --query-compute-apps because they have no CUDA '
        'context">[graphics + system, no CUDA context]</span>'
    )
    assert firehose_render._vram_row_label({"service": "__unattributed"}) == expected
    monkeypatch.setattr(gpu_observation, "executable", lambda: 'alternate<&"probe')
    assert firehose_render._vram_row_label({"service": "__unattributed"}) == expected.replace(
        tool + " --query-compute-apps", "alternate&lt;&amp;&quot;probe --query-compute-apps"
    )
    assert firehose_render._vram_row_label({"service": "__total"}) == (
        '<b title="GPU memory.used (authoritative)">[total]</b>'
    )


def _status_budget_fixture(monkeypatch, tmp_path, reversed_indexes=False):
    import status_endpoint as status
    from gateway import gpu_observation

    config = load_registry()
    if reversed_indexes:
        for device in config["devices"]:
            device["index"] = 1 - device["index"]
        config["runtime_display"]["device_index"] = 1
    path = tmp_path / "observation.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    original = gpu_observation.display_config
    monkeypatch.setattr(gpu_observation, "display_config", lambda: original(path))
    budget = tmp_path / "budget.sh"
    budget.write_text(
        "GPU_TOTAL_GB=24\nGPU0_DESKTOP=2\nGPU0_TTS=3\n"
        + status._GRAPH_WORDS["gpu_budget_var"] + "=4\n"
        "GPU0_HEADROOM=5\nGPU1_LLM=18\nGPU1_HEADROOM=6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(status, "_GPU_BUDGET_PATH", budget)
    monkeypatch.setattr(status, "_BUDGET_CACHE", {"ts": 0.0, "data": None})
    monkeypatch.setattr(status, "_get_compute_apps", lambda **kwargs: [])
    monkeypatch.setattr(status, "_service_pids", lambda *args, **kwargs: {})
    return status


def _headroom_rows(status, indexes):
    gpus = [{"index": index, "uuid": str(index), "memory_used_mb": 4096,
             "memory_total_mb": 24576} for index in indexes]
    return status._vram_attribution(gpus)["per_gpu"]


def test_status_budget_and_headroom_shipped_rows(monkeypatch, tmp_path):
    status = _status_budget_fixture(monkeypatch, tmp_path)
    graph_key = status._GRAPH_WORDS["gpu_budget_key"]
    graph_var = status._GRAPH_WORDS["gpu_budget_var"]
    assert status._read_gpu_budget() == {
        "gpu0": {"total_gb": 24, "desktop_gb": 2, "tts_gb": 3,
                 graph_key: 4, "headroom_gb": 5},
        "gpu1": {"total_gb": 24, "llm_gb": 18, "headroom_gb": 6},
        "raw": {"GPU_TOTAL_GB": 24, "GPU0_DESKTOP": 2, "GPU0_TTS": 3,
                graph_var: 4, "GPU0_HEADROOM": 5, "GPU1_LLM": 18, "GPU1_HEADROOM": 6},
    }
    rows = _headroom_rows(status, [0, 1])
    assert [next(row for row in device["services"] if row["service"] == "__headroom")
            for device in rows] == [
        {"service": "__headroom", "used_mb": 20480, "used_gb": 20.0,
         "budget_gb": 5, "pct_of_budget": 400.0, "over_budget": False,
         "headroom_short": False, "spillover": False, "note": "free VRAM (total − used)"},
        {"service": "__headroom", "used_mb": 20480, "used_gb": 20.0,
         "budget_gb": 6, "pct_of_budget": 333.3, "over_budget": False,
         "headroom_short": False, "spillover": False, "note": "free VRAM (total − used)"},
    ]
    assert [device["services"][-1]["budget_gb"] for device in rows] == [24, 24]


def test_status_budget_roles_follow_reversed_indexes(monkeypatch, tmp_path):
    status = _status_budget_fixture(monkeypatch, tmp_path, reversed_indexes=True)
    rows = _headroom_rows(status, [0, 1])
    assert [next(row["budget_gb"] for row in device["services"]
                 if row["service"] == "__headroom") for device in rows] == [6, 5]
    import asyncio
    workloads = asyncio.run(status._gpu_workloads(
        [{"index": 0}, {"index": 1}], {"running": 1, "pending": 2}, []))
    assert [row["active_workload"]["label"] for row in workloads] == ["loaded", "running"]


def test_status_unmapped_device_has_unknown_role_and_no_budget(monkeypatch, tmp_path):
    status = _status_budget_fixture(monkeypatch, tmp_path)
    third = _headroom_rows(status, [0, 1, 2])[2]
    assert third["role"] == "unknown"
    assert all(row["budget_gb"] is None for row in third["services"])
    assert "idle" not in third.values()
    import asyncio
    devices = asyncio.run(status._gpu_workloads([{"index": 2}], {"running": 0}, []))
    assert devices[0]["active_workload"]["label"] == "unknown"


@pytest.mark.parametrize("case,expected", [
    ("empty-role-key", "devices[0].roles[0]"),
    ("malformed-role-key", "devices[0].roles[0]"),
    ("unknown-role-template", "devices[0].roles[0]"),
    ("duplicate-role-key", "devices[0].roles[1]"),
    ("duplicate-bucket", "devices[0].roles[2]"),
    ("duplicate-budget-key", "devices[1]"),
    ("unknown-source-template", "devices[0].roles[0]"),
])
def test_status_registry_rejects_ambiguous_rows_at_load(tmp_path, case, expected):
    config = load_registry()
    roles = config["devices"][0]["roles"]
    if case == "empty-role-key":
        roles[0]["key"] = ""
    elif case == "malformed-role-key":
        roles[0]["key"] = "not a key"
    elif case == "unknown-role-template":
        roles[0]["key"] = "{missing_word}"
    elif case == "duplicate-role-key":
        roles[1]["key"] = roles[0]["key"]
    elif case == "duplicate-bucket":
        roles[2]["bucket"] = roles[1]["bucket"]
    elif case == "duplicate-budget-key":
        config["devices"][1]["budget_key"] = config["devices"][0]["budget_key"]
    else:
        roles[0]["source"] = "{missing_word}"
    path = tmp_path / "invalid-observation.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    # Refuse before a status/renderer call, not merely during template expansion.
    with pytest.raises(ValueError) as error:
        load_registry(path)
    assert expected in str(error.value)


def test_observation_only_registry_remains_supported(tmp_path):
    config = {"argv": ["example-probe"], "fields": [{"key": "index", "type": "int"}]}
    path = tmp_path / "minimal-observation.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert load_registry(path) == config
    assert observe(lambda argv: (0, "2", ""), path) == [{"index": 2}]


@pytest.mark.parametrize("reversed_indexes", [False, True], ids=["original", "remapped"])
def test_process_attribution_follows_remapped_identity(monkeypatch, tmp_path, reversed_indexes):
    status = _status_budget_fixture(monkeypatch, tmp_path, reversed_indexes)
    config = load_registry()
    role = config["devices"][0]
    backend_role = next(item for item in role["roles"] if item["key"] == "{gpu_budget_key}")
    from gateway import gpu_observation
    expanded = gpu_observation.status_devices(status._GRAPH_WORDS)
    mapped = next(item for item in expanded if item["budget_key"] == role["budget_key"])
    index = mapped["index"]
    other = 1 - index
    source = status._GRAPH_WORDS[backend_role["source"][1:-1]]
    monkeypatch.setattr(status, "_get_compute_apps", lambda **kwargs: [
        {"gpu_uuid": "worker-device", "pid": 101, "used_mb": 1024},
        {"gpu_uuid": "worker-device", "pid": 202, "used_mb": 512},
    ])
    monkeypatch.setattr(status, "_service_pids", lambda *args, **kwargs: {"example-backend": {101}})
    monkeypatch.setattr(status, "_read_proc_comm", lambda pid: "example-process")
    gpus = [
        {"index": index, "uuid": "worker-device", "memory_used_mb": 2048, "memory_total_mb": 24576},
        {"index": other, "uuid": "other-device", "memory_used_mb": 0, "memory_total_mb": 24576},
    ]
    result = status._vram_attribution(
        gpus, {"backends": {"example-backend": {"vram_budget_key": source}}})
    by_index = {item["index"]: item for item in result["per_gpu"]}
    services = {item["service"]: item for item in by_index[index]["services"]}
    assert services["example-backend"]["used_mb"] == 1024
    assert services["example-backend"]["budget_gb"] == 4
    assert services["example-backend"]["spillover"] is False
    assert services["desktop"]["used_mb"] == 512
    assert services["desktop"]["budget_gb"] == 2
    assert services["desktop"]["top_contributors"] == [
        {"pid": 202, "used_mb": 512, "comm": "example-process"}]
    assert services["__headroom"]["budget_gb"] == 5
    assert not {"example-backend", "desktop"} & {
        item["service"] for item in by_index[other]["services"]}
