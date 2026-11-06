#!/usr/bin/env python3
"""Bound the gate's receipt directory: old runs keep only their verdict.

Receipts live in ROOT/<tree>/. The newest KEEP runs, and any run with a signoff receipt written
in the last SIGNOFF_DAYS days, keep everything. Every other run keeps only run.tsv, PASS, FAIL and
signoff-*.tsv. The run named by --keep (the gate that is starting) and ROOT/scheduled are never
touched. Usage: prune_receipts.py ROOT [--keep DIR] [--runs N] [--signoff-days N]
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

VERDICT_FILES = ("run.tsv", "PASS", "FAIL")


def stamp(run: Path) -> float:
    marker = run / "run.tsv"
    return (marker if marker.exists() else run).stat().st_mtime


def has_recent_signoff(run: Path, since: float) -> bool:
    return any(path.stat().st_mtime >= since for path in run.glob("signoff-*.tsv"))


def compact(run: Path) -> None:
    for entry in run.iterdir():
        if entry.name in VERDICT_FILES or (entry.name.startswith("signoff-") and entry.suffix == ".tsv"):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def prune(root: Path, keep: Path | None, runs: int, signoff_days: float, now: float | None = None) -> list[Path]:
    """Compact old runs; return the runs compacted."""
    since = (now if now is not None else time.time()) - signoff_days * 86400
    candidates = [d for d in root.iterdir() if d.is_dir() and not d.is_symlink() and d.name != "scheduled"]
    candidates.sort(key=stamp, reverse=True)
    compacted = []
    for index, run in enumerate(candidates):
        if index < runs or run == keep or has_recent_signoff(run, since):
            continue
        compact(run)
        compacted.append(run)
    return compacted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--keep", type=Path)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--signoff-days", type=float, default=7)
    args = parser.parse_args()
    if not args.root.is_dir():
        return 0
    keep = args.keep.resolve() if args.keep else None
    compacted = prune(args.root.resolve(), keep, args.runs, args.signoff_days)
    if compacted:
        print(f"pruned {len(compacted)} old receipt(s) to their verdicts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
