#!/usr/bin/env python3
"""Split Playwright spec files into N lanes of similar measured duration.

Usage:
  plan_shards.py plan LIST_OUTPUT WEIGHTS N OUT_DIR [SEED]
  plan_shards.py weights LOG... > web/e2e/shard-weights.tsv  (one run's shard logs)

`plan` reads `playwright test --list` output, assigns each spec file to the
lane with the least estimated time so far (longest file first), and writes
OUT_DIR/lane-<i>.txt for `playwright test --test-list`. With SEED, each file
goes to a seeded random choice among the lanes within half the file's own
estimate of the least-loaded lane, so runs vary which files share a fixture
server; on the 2026-10 weights this costs at most ~2.5% on the slowest lane. Files keep their
internal order, as with `--shard`. It fails unless every listed file lands in
exactly one lane. Weights only steer balance: a file missing from WEIGHTS is
estimated from its test count.

`weights` sums per-file test durations from Playwright list-reporter logs.
"""

from __future__ import annotations

import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

LISTED = re.compile(r"^\s*\[[^\]]+\] › (?:e2e/)?([^:]+):\d+:\d+ › ")
TIMED = re.compile(r"[✓✘-]\s+\d+ \[[^\]]+\] › (?:e2e/)?([^:]+):\d+:\d+ › .*\((\d+(?:\.\d+)?)(ms|s|m)\)\s*$")
UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0}
SECONDS_PER_UNKNOWN_TEST = 2.5
SEEDED_SLACK = 0.5  # share of a file's estimate a seeded choice may add over the minimum


def plan(list_output: Path, weights_file: Path, lanes: int, out_dir: Path, seed: int | None = None) -> None:
    counts: Counter[str] = Counter()
    for line in list_output.read_text(encoding="utf-8").splitlines():
        if match := LISTED.match(line):
            counts[match.group(1)] += 1
    if not counts:
        sys.exit("plan_shards: no tests in the --list output")
    weights: dict[str, float] = {}
    if weights_file.is_file():
        for line in weights_file.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                name, seconds = line.split("\t")
                weights[name] = float(seconds)
    estimate = {name: weights.get(name, count * SECONDS_PER_UNKNOWN_TEST)
                for name, count in counts.items()}
    loads = [0.0] * lanes
    assigned: list[list[str]] = [[] for _ in range(lanes)]
    rng = random.Random(seed)
    for name in sorted(estimate, key=lambda n: (-estimate[n], n)):
        lane = loads.index(min(loads))
        if seed is not None:
            limit = loads[lane] + SEEDED_SLACK * estimate[name]
            lane = rng.choice([index for index, load in enumerate(loads) if load <= limit])
        loads[lane] += estimate[name]
        assigned[lane].append(name)
    placed = [name for files in assigned for name in files]
    if sorted(placed) != sorted(counts) or len(placed) != len(set(placed)):
        sys.exit("plan_shards: every spec file must be in exactly one lane")
    if any(not files for files in assigned):
        sys.exit(f"plan_shards: {lanes} lanes but only {len(counts)} spec files")
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, files in enumerate(assigned, 1):
        (out_dir / f"lane-{index}.txt").write_text("".join(f"{name}\n" for name in sorted(files)))
        tests = sum(counts[name] for name in files)
        print(f"lane {index}: {len(files)} files, {tests} tests, ~{loads[index - 1]:.0f} s")


def weights(logs: list[Path]) -> None:
    totals: defaultdict[str, float] = defaultdict(float)
    for log in logs:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if match := TIMED.search(line):
                totals[match.group(1)] += float(match.group(2)) * UNIT_SECONDS[match.group(3)]
    print("# spec file\tseconds (summed test durations; regenerate with plan_shards.py weights)")
    for name in sorted(totals):
        print(f"{name}\t{totals[name]:.1f}")


if __name__ == "__main__":
    if len(sys.argv) in (6, 7) and sys.argv[1] == "plan":
        plan(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]), Path(sys.argv[5]),
             int(sys.argv[6]) if len(sys.argv) == 7 else None)
    elif len(sys.argv) > 2 and sys.argv[1] == "weights":
        weights([Path(arg) for arg in sys.argv[2:]])
    else:
        sys.exit(__doc__)
