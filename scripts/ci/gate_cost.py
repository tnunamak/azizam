#!/usr/bin/env python3
"""Per-step cost for scripts/ci-local: measure one step, or report a finished run.

Usage:
  gate_cost.py run OUT -- COMMAND...    run COMMAND, write "wall user system max_rss_kb" to OUT
  gate_cost.py report RECEIPTS [--measure MODE]
                                        write RECEIPTS/cost.json and print the cost table; the
                                        "journey" section from journey_clock.py is kept

run is the fallback when GNU time is absent. It returns COMMAND's exit status (128+N when
COMMAND dies from signal N), as the shell would. Every measurement is the step's own rusage,
which includes each child the step waited for. Peak RSS is the largest single process's peak
(ru_maxrss), not the sum over processes alive at once.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# What each gate step waits for, as the job structure in scripts/ci-local makes it. A step
# starts when its last predecessor finishes, so the critical path follows these edges. Keep
# this in step with scripts/ci-local: a missing edge makes the critical path too short.
BARRIER = ("e2e-browser", "fresh-compose", "mcp-conformance")  # the web, fresh and compat jobs
GATE_DEPS: dict[str, tuple[str, ...]] = {
    "python-hermetic": (),
    "fresh-venv": (),
    "fresh-compose": ("fresh-venv",),
    "web-npm-ci": (),
    "web-unit": ("web-npm-ci",),
    "web-build": ("web-npm-ci",),
    "e2e-venv": ("web-unit", "web-build"),
    "e2e-browser": ("e2e-venv",),
    "compat-node": (),
    "pi-unit": ("compat-node",),
    "mcp-conformance": ("pi-unit",),
    "e2e-main": BARRIER,
    "e2e-live-data": BARRIER,
    "e2e-household": BARRIER,
    "web-check": BARRIER,
    "fresh-native": ("web-check",),
    "e2e-tree-unchanged": ("python-hermetic", "e2e-main", "e2e-live-data", "e2e-household", "web-check", "fresh-native"),
    # Runs after every job has joined, then cases follows it: the sequential tail of scripts/ci-local.
    "journey-clock": ("python-hermetic", "e2e-tree-unchanged", "fresh-native", "web-check", "e2e-main", "e2e-live-data", "e2e-household", "fresh-compose"),
    "cases": ("journey-clock",),
    "policy": ("cases",),
}


def critical_path(durations: dict[str, float], deps: dict[str, tuple[str, ...]]) -> tuple[list[str], float]:
    """Return (chain, seconds) for the longest chain of measured steps.

    A step starts when its last measured predecessor finishes. Predecessors that were never
    measured (skipped or not started) are left out. Ties go to the step later in deps, which
    lists the steps in run order.
    """
    order = {name: index for index, name in enumerate(deps)}
    finish: dict[str, float] = {}
    previous: dict[str, str | None] = {}

    def rank(name: str) -> tuple[float, int]:
        return finish_at(name), order.get(name, -1)

    def finish_at(name: str) -> float:
        if name not in finish:
            known = [dep for dep in deps.get(name, ()) if dep in durations]
            before = max(known, key=rank, default=None)
            previous[name] = before
            finish[name] = durations[name] + (finish_at(before) if before is not None else 0.0)
        return finish[name]

    if not durations:
        return [], 0.0
    end = max(sorted(durations), key=rank)
    seconds = finish_at(end)  # fills previous for every step on the chain
    chain: list[str] = []
    cursor: str | None = end
    while cursor is not None:
        chain.append(cursor)
        cursor = previous[cursor]
    return chain[::-1], round(seconds, 2)


def number(text: str, cast):
    """A cost cell: '-' means the step was not measured."""
    if text == "-":
        return None
    try:
        return cast(text)
    except ValueError:
        return None


def read_steps(path: Path) -> dict[str, dict]:
    """Rows of steps.tsv: name, exit code, whole seconds, then wall, CPU and peak RSS kB.

    Receipts from before the cost columns have whole seconds only; their wall is that value.
    """
    steps = {}
    for line in path.read_text().splitlines():
        fields = line.split("\t")
        name, rc = fields[0], fields[1]
        wall = number(fields[3], float) if len(fields) > 3 else number(fields[2], float)
        cpu = number(fields[4], float) if len(fields) > 4 else None
        peak = number(fields[5], int) if len(fields) > 5 else None
        steps[name] = {"rc": int(rc) if rc.isdigit() else rc, "wall_s": wall, "cpu_s": cpu, "max_rss_kb": peak}
    return steps


def read_journey(path: Path) -> dict | None:
    """The "journey" section scripts/ci/journey_clock.py left in cost.json, if any."""
    try:
        journey = json.loads(path.read_text())["journey"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return journey if isinstance(journey, dict) else None


def run_elapsed(path: Path) -> int | None:
    """Whole seconds from run.tsv's started line to its finished line, if both exist."""
    if not path.is_file():
        return None
    fields = dict(line.split("\t", 1) for line in path.read_text().splitlines() if "\t" in line)
    try:
        started = datetime.fromisoformat(fields["started"])
        finished = datetime.fromisoformat(fields["finished"])
    except (KeyError, ValueError):
        return None
    return round((finished - started).total_seconds())


def run(out: Path, command: list[str]) -> int:
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        try:
            os.execvp(command[0], command)
        except OSError as error:
            os.write(2, f"{command[0]}: {error.strerror}\n".encode())
            os._exit(127)
    _, status, usage = os.wait4(pid, 0)
    wall = time.monotonic() - started
    try:
        out.write_text(f"{wall:.2f} {usage.ru_utime:.2f} {usage.ru_stime:.2f} {usage.ru_maxrss}\n")
    except OSError:
        pass  # The cost is evidence only: a step never fails for lack of it.
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def cell(value, fmt: str) -> str:
    return "-" if value is None else format(value, fmt)


def report(receipts: Path, mode: str) -> None:
    steps = read_steps(receipts / "steps.tsv")
    walls = [step["wall_s"] for step in steps.values() if step["wall_s"] is not None]
    cpus = [step["cpu_s"] for step in steps.values() if step["cpu_s"] is not None]
    peaks = [step["max_rss_kb"] for step in steps.values() if step["max_rss_kb"] is not None]
    chain, chain_s = critical_path({name: step["wall_s"] for name, step in steps.items() if step["wall_s"] is not None}, GATE_DEPS)
    elapsed = run_elapsed(receipts / "run.tsv")
    totals = {"wall_s": round(sum(walls), 2), "cpu_s": round(sum(cpus), 2), "max_step_rss_kb": max(peaks, default=None)}
    cost = {
        "schema": 1,
        "measure": mode,
        "elapsed_s": elapsed,
        "critical_path": {"steps": chain, "wall_s": chain_s},
        "totals": totals,
        "steps": steps,
    }
    journey = read_journey(receipts / "cost.json")
    if journey is not None:
        cost["journey"] = journey  # written earlier by scripts/ci/journey_clock.py
    (receipts / "cost.json").write_text(json.dumps(cost, indent=2, sort_keys=True) + "\n")

    print(f"{'step':<22} {'rc':>7} {'wall_s':>8} {'cpu_s':>8} {'peak_mb':>9}")
    slowest_first = sorted(steps.items(), key=lambda item: (-(item[1]["wall_s"] or 0.0), item[0]))
    for name, step in slowest_first:
        peak = None if step["max_rss_kb"] is None else step["max_rss_kb"] / 1024
        print(f"{name:<22} {str(step['rc']):>7} {cell(step['wall_s'], '.2f'):>8} {cell(step['cpu_s'], '.2f'):>8} "
              f"{cell(peak, '.1f'):>9}")
    if chain:
        print(f"critical path {chain_s:.2f} s: {' > '.join(chain)}")
    peak_text = "-" if totals["max_step_rss_kb"] is None else f"{totals['max_step_rss_kb'] / 1024:.1f} MB"
    print(f"elapsed {cell(elapsed, 'd')} s; step wall sum {totals['wall_s']:.2f} s; cpu {totals['cpu_s']:.2f} s; "
          f"largest step peak {peak_text}")


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[0] == "run" and argv[2] == "--":
        return run(Path(argv[1]), argv[3:])
    if argv[:1] == ["report"] and len(argv) in (2, 4) and (len(argv) == 2 or argv[2] == "--measure"):
        report(Path(argv[1]), argv[3] if len(argv) == 4 else "unknown")
        return 0
    print("usage: gate_cost.py run OUT -- COMMAND... | report RECEIPTS [--measure MODE]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
