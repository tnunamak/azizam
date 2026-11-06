"""The http-v1 executor stays within a small multiple of a hand-written mapping (contract v2.1 section 4)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "scripts" / "bench_engine_protocol.py"

# Cases that include the work every request needs anyway. The 3x aim is documented in
# docs/design/ENGINE_PROTOCOL_EXECUTOR.md; this guard is looser so a loaded host cannot fail it,
# yet an order-of-magnitude regression (a JSONata-class interpreter is 375x or more) still does.
GUARDED = ("request map + JSON write", "SSE event, parsed and mapped", "whole invoke (loop + stub transport)")
GUARD_RATIO = 6.0


@pytest.fixture(scope="module")
def rows() -> dict[str, dict[str, float]]:
    spec = importlib.util.spec_from_file_location("bench_engine_protocol", BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.measure(scale=0.05)


def test_the_benchmark_reports_executor_and_hand_written_cost_per_case(rows) -> None:
    for name in GUARDED:
        row = rows[name]
        assert row["executor_us"] > 0 and row["hand_written_us"] > 0
        assert row["ratio"] == pytest.approx(row["executor_us"] / row["hand_written_us"], rel=0.01)


@pytest.mark.parametrize("case", GUARDED)
def test_executor_overhead_stays_near_a_hand_written_mapping(rows, case) -> None:
    assert rows[case]["ratio"] < GUARD_RATIO, rows[case]
