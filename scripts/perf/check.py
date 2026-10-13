#!/usr/bin/env python3
"""Judge api_bench.py and ui_bench.mjs results against budgets.toml.

    scripts/perf/check.py --api api.json [--ui ui.json] [--budgets budgets.toml] [--counts-only]

Prints a ranked table of what exceeds a budget (worst first) and exits 1 when anything does.
--counts-only judges only statements, connections and processes (no timing), for hosts
where times are meaningless.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def api_violations(rows: list[dict], budget: dict, counts_only: bool) -> list[dict]:
    found = []
    for row in rows:
        if "p95_ms" not in row:
            continue
        limits = {**budget, **(budget.get("routes", {}).get(row["route"]) or {})}
        checks = [("statements", row["sql"]), ("connections", row["connects"]), ("processes", row["subproc"])]
        if not counts_only:
            checks.append(("p95_ms", row["p95_ms"]))
        for name, value in checks:
            if value > limits[name]:
                found.append({"kind": "api", "what": f"{row['route']} ({row['who']})", "metric": name,
                              "value": value, "limit": limits[name], "over": value / max(limits[name], 1)})
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
        checks = [("requests", row["requests"]), ("kilobytes", row["bytes"] / 1024)]
        if not counts_only:
            checks += [("ready_ms", row["ready_ms"]), ("long_task_ms", row["long_task_ms"])]
        for name, value in checks:
            if value > limits[name]:
                found.append({"kind": "ui", "what": f"{row['route']} ({row['who']})", "metric": name,
                              "value": round(value), "limit": limits[name], "over": value / max(limits[name], 1)})
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
    parser.add_argument("--budgets", type=Path, default=HERE / "budgets.toml")
    parser.add_argument("--counts-only", action="store_true")
    args = parser.parse_args()
    budgets = tomllib.loads(args.budgets.read_text(encoding="utf-8"))
    bad = api_violations(json.loads(args.api.read_text())["rows"], budgets["api"], args.counts_only)
    if args.ui:
        bad += ui_violations(json.loads(args.ui.read_text())["rows"], budgets["ui"], args.counts_only)
    if bad:
        print(table(bad))
        print(f"\n{len(bad)} over budget", file=sys.stderr)
        raise SystemExit(1)
    print("within budget")


if __name__ == "__main__":
    main()
