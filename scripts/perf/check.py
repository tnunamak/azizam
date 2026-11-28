#!/usr/bin/env python3
"""Judge api_bench.py and ui_bench.mjs results against budgets.toml.

    scripts/perf/check.py --api api.json [--ui ui.json] [--budgets budgets.toml] [--counts-only]

Prints a ranked table of what exceeds a budget (worst first) and exits 1 when anything does.
--counts-only judges counts and payload sizes (no timing), for hosts
where times are meaningless.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _finite(value) -> bool:
    return not isinstance(value, bool) and (
        isinstance(value, int) or isinstance(value, float) and math.isfinite(value)
    )


def _violation(kind, what, metric, value, limit, *, rounded=False, never=False):
    if not _finite(value):
        return {"kind": kind, "what": what, "metric": metric,
                "value": "never" if never and value is None else "invalid", "limit": limit, "over": 99}
    if value > limit:
        return {"kind": kind, "what": what, "metric": metric,
                "value": round(value) if rounded else value, "limit": limit, "over": value / max(limit, 1)}
    return None


def api_violations(rows: list[dict], budget: dict, counts_only: bool) -> list[dict]:
    found = []
    for row in rows:
        if "skipped" in row:
            continue
        if "error" in row:
            found.append({"kind": "api", "what": f"{row['route']} ({row['who']})", "metric": "error",
                          "value": "error", "limit": 0, "over": 99})
            continue
        if "p95_ms" not in row:
            continue  # standalone loop measurement, not an API sample
        limits = {**budget, **(budget.get("routes", {}).get(row["route"]) or {})}
        checks = [("statements", row.get("sql")), ("connections", row.get("connects")), ("processes", row.get("subproc"))]
        if not counts_only:
            checks.append(("p95_ms", row["p95_ms"]))
        for name, value in checks:
            violation = _violation("api", f"{row['route']} ({row['who']})", name, value, limits[name])
            if violation is not None:
                found.append(violation)
    return found


def loop_violations(rows: list[dict], budget: dict) -> list[dict]:
    """A cold read that stalls the event loop, as api_bench.py measures it (rows with stall_ms)."""
    found = []
    for row in rows:
        if "stall_ms" not in row:
            continue
        violation = _violation("loop", f"{row['route']} ({row['who']})", "stall_ms", row["stall_ms"], budget["max_stall_ms"])
        if violation is not None:
            found.append(violation)
    return found


def ui_violations(rows: list[dict], budget: dict, counts_only: bool) -> list[dict]:
    found = []
    for row in rows:
        if row["final"] != row["route"] and row["final"] != row["route"].rstrip("/") + "/":
            continue  # redirected away (a member opening an owner page): nothing to judge
        limits = {**budget, **(budget.get("pages", {}).get(row["route"]) or {})}
        if row.get("ready_ms") is None:
            found.append({"kind": "ui", "what": f"{row['route']} ({row['who']})", "metric": "ready_ms",
                          "value": "never", "limit": limits["ready_ms"], "over": 99})
            continue
        size = row.get("bytes")
        checks = [("requests", row.get("requests")), ("kilobytes", size / 1024 if _finite(size) else size)]
        if not counts_only:
            checks += [("ready_ms", row["ready_ms"]), ("long_task_ms", row.get("long_task_ms"))]
        for name, value in checks:
            violation = _violation("ui", f"{row['route']} ({row['who']})", name, value, limits[name], rounded=True)
            if violation is not None:
                found.append(violation)
    return found


def home_violations(report: dict, budget: dict, counts_only: bool = False) -> list[dict]:
    """Judge a home report; counts-only excludes content timings."""
    profiles = report["profiles"]
    checks = []
    if not counts_only:
        for device in ("desktop", "mobile"):
            for cache in ("cold", "warm"):
                checks.append((f"{device}_{cache}_ms", profiles[device][cache].get("content_ms")))
    cold = profiles["mobile"]["cold"]
    checks += [("js_wire_kb", cold.get("js_wire_kb")), ("js_requests", cold.get("js_requests")),
               ("uncompressed_js", cold.get("js_uncompressed"))]
    if report.get("who") == "member":
        # The owner's shell also reads status and registry.
        checks.append(("member_api_requests", profiles["desktop"]["cold"].get("member_api_requests")))
    found = []
    for name, value in checks:
        violation = _violation("home", "Home", name, value, budget[name], never=name.endswith("_ms"))
        if violation is not None:
            found.append(violation)
    return found


def judge(api_rows: list[dict], ui_rows: list[dict], home: dict | None, budgets: dict, counts_only: bool) -> list[dict]:
    found = api_violations(api_rows, budgets["api"], counts_only)
    if not counts_only:
        found += loop_violations(api_rows, budgets["loop"])
    if ui_rows:
        found += ui_violations(ui_rows, budgets["ui"], counts_only)
    if home is not None:
        found += home_violations(home, budgets["home"], counts_only)
    return found


def table(violations: list[dict]) -> str:
    lines = ["| kind | where | metric | value | limit |", "|---|---|---|--:|--:|"]
    for v in sorted(violations, key=lambda v: -v["over"]):
        lines.append(f"| {v['kind']} | {v['what']} | {v['metric']} | {v['value']} | {v['limit']} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", type=Path, required=True)
    parser.add_argument("--ui", type=Path)
    parser.add_argument("--home", type=Path, help="a home_bench.mjs report")
    parser.add_argument("--budgets", type=Path, default=HERE / "budgets.toml")
    parser.add_argument("--counts-only", action="store_true")
    args = parser.parse_args()
    budgets = tomllib.loads(args.budgets.read_text(encoding="utf-8"))
    api_rows = json.loads(args.api.read_text())["rows"]
    ui_rows = json.loads(args.ui.read_text())["rows"] if args.ui else []
    home = json.loads(args.home.read_text()) if args.home else None
    bad = judge(api_rows, ui_rows, home, budgets, args.counts_only)
    if bad:
        print(table(bad))
        print(f"\n{len(bad)} over budget", file=sys.stderr)
        raise SystemExit(1)
    print("within budget")


if __name__ == "__main__":
    main()
