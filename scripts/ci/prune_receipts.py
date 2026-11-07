#!/usr/bin/env python3
"""Bound the gate's receipt directory: old runs keep only their verdict.

Receipts live in ROOT/<tree>/. The newest KEEP runs, and any run with a signoff receipt written
in the last SIGNOFF_DAYS days, keep everything. Every other run keeps only run.tsv, PASS, FAIL and
signoff-*.tsv, except a run with a FAIL verdict: for FAIL_DAYS days it also keeps logs/ and the
e2e failure artifacts in e2e-failures/ (screenshots, error-context files, traces of failed tests).
When one run's artifacts pass ARTIFACT_CAP bytes, its traces go first, then its screenshots.
The run named by --keep (the gate that is starting) and ROOT/scheduled are never touched.

Usage: prune_receipts.py ROOT [--keep DIR] [--runs N] [--signoff-days N] [--failed-days N]
       prune_receipts.py --collect SRC DEST   copy the failure artifacts under SRC (Playwright's
                                              output directories) into DEST
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

VERDICT_FILES = ("run.tsv", "PASS", "FAIL")
FAILED_DIRS = ("logs", "e2e-failures")
FAIL_DAYS = 3
ARTIFACT_CAP = 200 * 1024 * 1024
# What Playwright leaves for a failed test (trace and screenshot are kept only on failure).
ARTIFACT_NAMES = ("error-context.md", "trace.zip")
ARTIFACT_SUFFIXES = (".png",)


def stamp(run: Path) -> float:
    marker = run / "run.tsv"
    return (marker if marker.exists() else run).stat().st_mtime


def has_recent_signoff(run: Path, since: float) -> bool:
    return any(path.stat().st_mtime >= since for path in run.glob("signoff-*.tsv"))


def tree_files(directory: Path) -> list[Path]:
    return [path for path in directory.rglob("*") if path.is_file() and not path.is_symlink()]


def bound_artifacts(directory: Path, cap: int = ARTIFACT_CAP) -> None:
    """Drop traces, then screenshots, until the directory is within cap bytes."""
    for suffix in (".zip", ".png"):
        files = tree_files(directory)
        if sum(path.stat().st_size for path in files) <= cap:
            return
        for path in files:
            if path.suffix == suffix:
                path.unlink(missing_ok=True)


def collect_failures(src: Path, dest: Path) -> int:
    """Copy each failed test's artifacts from the Playwright output directories under src into dest.

    A passing test leaves no such files there, so every match belongs to a failed test. The path under
    src is kept, so two ports never collide. Returns the number of files copied.
    """
    copied = 0
    if not src.is_dir():
        return copied
    for path in sorted(src.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name in ARTIFACT_NAMES or path.suffix in ARTIFACT_SUFFIXES:
            target = dest / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    if dest.is_dir():
        bound_artifacts(dest)
    return copied


def compact(run: Path, keep: tuple[str, ...] = ()) -> None:
    for entry in run.iterdir():
        if entry.name in VERDICT_FILES or entry.name in keep or (entry.name.startswith("signoff-") and entry.suffix == ".tsv"):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def is_recent_failure(run: Path, since: float) -> bool:
    return (run / "FAIL").is_file() and stamp(run) >= since


def prune(
    root: Path,
    keep: Path | None,
    runs: int,
    signoff_days: float,
    now: float | None = None,
    failed_days: float = FAIL_DAYS,
) -> list[Path]:
    """Compact old runs; return the runs compacted."""
    now = now if now is not None else time.time()
    since = now - signoff_days * 86400
    failed_since = now - failed_days * 86400
    candidates = [d for d in root.iterdir() if d.is_dir() and not d.is_symlink() and d.name != "scheduled"]
    candidates.sort(key=stamp, reverse=True)
    compacted = []
    for index, run in enumerate(candidates):
        if index < runs or run == keep or has_recent_signoff(run, since):
            continue
        if is_recent_failure(run, failed_since):
            compact(run, keep=FAILED_DIRS)
            if (run / "e2e-failures").is_dir():
                bound_artifacts(run / "e2e-failures")
        else:
            compact(run)
        compacted.append(run)
    return compacted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?")
    parser.add_argument("--collect", type=Path, nargs=2, metavar=("SRC", "DEST"))
    parser.add_argument("--keep", type=Path)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--signoff-days", type=float, default=7)
    parser.add_argument("--failed-days", type=float, default=FAIL_DAYS)
    args = parser.parse_args()
    if args.collect:
        count = collect_failures(*args.collect)
        print(f"kept {count} e2e failure artifact file(s)")
        return 0
    if args.root is None:
        parser.error("ROOT is required")
    if not args.root.is_dir():
        return 0
    keep = args.keep.resolve() if args.keep else None
    compacted = prune(args.root.resolve(), keep, args.runs, args.signoff_days, failed_days=args.failed_days)
    if compacted:
        print(f"pruned {len(compacted)} old receipt(s) to their verdicts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
