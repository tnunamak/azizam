"""The shipped proxy_config.yml enables the trellis pool with the loopback VRAM observer, and nothing else changes."""

from __future__ import annotations

from pathlib import Path

import yaml

from gateway import exclusive_pool, vram_observer
from gateway.exclusive_pool import MIB

CONFIG = yaml.safe_load((Path(__file__).resolve().parents[1] / "proxy_config.yml").read_text(encoding="utf-8"))
BACKENDS = CONFIG["backends"]


def test_trellis_capacity_is_enabled_as_an_exclusive_pool_on_gpu0():
    spec = exclusive_pool.parse_resource_capacity("trellis", BACKENDS["trellis"]["resource_capacity"])

    assert spec is not None
    assert spec.pool_id == "gpu0"
    assert spec.observer_service == "vram-observer"
    assert spec.observer_path == "/vram"
    assert spec.free_pointer == "/memory_free_bytes"
    assert spec.total_pointer == "/memory_total_bytes"


def test_the_claim_and_reserve_are_positive_whole_mib_values_from_the_measured_peak():
    block = BACKENDS["trellis"]["resource_capacity"]

    assert block["max_claim_bytes"] % MIB == 0
    assert block["reserve_bytes"] % MIB == 0
    assert block["max_claim_bytes"] >= block["reserve_bytes"] > 0


def test_the_observer_backend_is_the_loopback_vram_observer():
    observer = BACKENDS["vram-observer"]

    assert observer["host"] == "127.0.0.1"
    assert observer["port"] == 8891
    assert observer["base_path"] == "/"
    assert observer["health_endpoint"] == "/health"
    assert observer["managed"] is False
    assert observer["role"] == "optional"
    assert observer.get("capabilities", []) == []
    assert vram_observer.LOOPBACK_HOSTS[0] == observer["host"]


def test_the_observer_port_is_not_the_trellis_port_or_another_backend_port():
    ports = [entry.get("port") for entry in BACKENDS.values() if isinstance(entry, dict)]
    assert ports.count(8891) == 1
    assert BACKENDS["trellis"]["port"] == 8890


def test_the_observer_unit_runs_the_observer_on_the_configured_port_and_gpu():
    unit = (Path(__file__).resolve().parents[1] / "docs/ops/vram-observer.service").read_text(encoding="utf-8")
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))

    assert "-m gateway.vram_observer" in exec_line
    assert f"--port {BACKENDS['vram-observer']['port']}" in exec_line
    assert "--gpu 0" in exec_line
    assert "--host" not in exec_line
    assert "WantedBy=default.target" in unit


def test_the_claim_covers_the_largest_measured_increase_with_a_ten_percent_margin():
    # Measured 2026-10-30 on GPU0 (teapot.jpg): resolution 1024 grew use by 6934 MiB, 1536 by 4940 MiB.
    largest_increase_mib = 6934
    claim = BACKENDS["trellis"]["resource_capacity"]["max_claim_bytes"]

    assert claim == 7680 * MIB
    assert claim >= largest_increase_mib * 1.1 * MIB
    assert claim - largest_increase_mib * MIB < 1024 * MIB
