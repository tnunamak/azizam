#!/usr/bin/env python3
"""Add the two halves of the first-run journey and enforce the ten-minute bar.

docs/PRODUCT_STRATEGY.md release bar 1: a new operator runs `docker compose up`,
follows /setup and gets a chat in under ten minutes. Two gate steps each time
their half and write it to a file:

- fresh-compose (tests/e2e/fresh_instance.py --compose): `docker compose pull`,
  `build` and `up` until the gateway answers /health ("bringup_s").
- e2e-main (web/e2e/first-run.spec.ts): /setup to the chat reply ("elapsed_seconds").

This script sums them into the "journey" section of cost.json and exits 1 unless
the total is strictly under the limit. cost.json is the one file scripts/ci/gate_cost.py
also writes: this step creates it if absent, replaces only its "journey" key otherwise,
and gate_cost.py report keeps the section when it writes the rest. A missing or
malformed half is a failure with no section written, never a pass.
"""
import argparse
import json
import sys
from pathlib import Path


def seconds(path: Path, key: str, label: str) -> float:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))[key]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        sys.exit(f"journey-clock: no usable {label} time in {path} ({type(exc).__name__})")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        sys.exit(f"journey-clock: {label} time in {path} is not a number of seconds")
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--first-run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--budget", type=int, default=600)
    args = parser.parse_args()
    bringup = seconds(args.compose, "bringup_s", "Compose bring-up")
    browser = seconds(args.first_run, "elapsed_seconds", "first-run browser")
    total = round(bringup + browser, 1)
    journey = {
        "compose_bringup_s": bringup,
        "first_run_ui_s": browser,
        "total_s": total,
        "limit_s": args.budget,
        "passed": total < args.budget,
    }
    cost = {}
    if args.out.is_file():
        try:
            cost = json.loads(args.out.read_text(encoding="utf-8"))
        except ValueError:
            cost = None
        if not isinstance(cost, dict):
            sys.exit(f"journey-clock: {args.out} is not a JSON object; refusing to overwrite it")
    cost["journey"] = journey
    args.out.write_text(json.dumps(cost, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"first-run journey: Compose {bringup} s + browser {browser} s = {total} s of {args.budget} s")
    if not journey["passed"]:
        sys.exit(f"journey-clock: the first-run journey took {total} s; the bar is under {args.budget} s")


if __name__ == "__main__":
    main()
