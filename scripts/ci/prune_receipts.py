#!/usr/bin/env python3
"""Bound the gate's receipt directory: old runs keep only their verdict.

Receipts live in ROOT/<tree>/. The newest KEEP runs, and any run with a signoff receipt written
in the last SIGNOFF_DAYS days, keep everything. Every other run keeps only run.tsv, PASS, FAIL and
signoff-*.tsv, except a run with a FAIL verdict: for FAIL_DAYS days it also keeps logs/ and the
e2e failure artifacts in e2e-failures/ (screenshots, error-context files, traces of failed tests).
ARTIFACT_CAP bounds e2e-failures/ only (logs/ are the gate's step logs and stay whole): collection
copies error contexts first, then screenshots, then traces, and skips what would pass the cap; an
existing directory over the cap loses its traces, then its screenshots, then its largest files.
Whatever is left out is named in OMITTED.txt. A symlink is never followed: a symlinked SRC, DEST or
kept directory is refused or removed, never traversed.
The run named by --keep (the gate that is starting) and ROOT/scheduled are never touched.

Usage: prune_receipts.py ROOT [--keep DIR] [--runs N] [--signoff-days N] [--failed-days N]
       prune_receipts.py --collect SRC DEST   copy the failure artifacts under SRC (Playwright's
                                              output directories) into DEST
"""

from __future__ import annotations

import argparse
import os
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


OMITTED = "OMITTED.txt"


def tree_files(directory: Path) -> list[Path]:
    """The regular files under directory, without following any symlink."""
    files = []
    for parent, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
        files += [Path(parent) / name for name in names if not (Path(parent) / name).is_symlink()]
    return [path for path in files if path.is_file()]


def real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def note_omitted(directory: Path, paths: list[Path]) -> None:
    if paths:
        with (directory / OMITTED).open("a") as out:
            out.writelines(f"{path.relative_to(directory)}\n" for path in paths)


def priority(path: Path) -> int:
    """Error contexts first, then screenshots, then traces: the order evidence is worth keeping in."""
    return 0 if path.name == "error-context.md" else 1 if path.suffix == ".png" else 2


def bound_artifacts(directory: Path, cap: int = ARTIFACT_CAP) -> None:
    """Drop traces, then screenshots, then the largest other files, until the directory is within cap bytes."""
    if not real_dir(directory):
        return
    for suffix in (".zip", ".png"):
        files = tree_files(directory)
        if sum(path.stat().st_size for path in files) <= cap:
            return
        for path in files:
            if path.suffix == suffix:
                path.unlink(missing_ok=True)
    files = sorted((path for path in tree_files(directory) if path.name != OMITTED), key=lambda path: path.stat().st_size)
    total = sum(path.stat().st_size for path in files)
    dropped = []
    while files and total > cap:
        path = files.pop()
        total -= path.stat().st_size
        path.unlink(missing_ok=True)
        dropped.append(path)
    note_omitted(directory, dropped)


def collect_failures(src: Path, dest: Path, cap: int = ARTIFACT_CAP) -> int:
    """Copy each failed test's artifacts from the Playwright output directories under src into dest.

    A passing test leaves no such files there, so every match belongs to a failed test. The path under
    src is kept, so two ports never collide. Files are copied in priority order while they fit in cap
    bytes; the rest are named in OMITTED.txt. Returns the number of files copied.
    """
    if not real_dir(src) or dest.is_symlink() or dest.parent.is_symlink():
        return 0
    found = [path for path in tree_files(src) if path.name in ARTIFACT_NAMES or path.suffix in ARTIFACT_SUFFIXES]
    found.sort(key=lambda path: (priority(path), str(path)))
    copied, total, left_out = 0, 0, []
    for path in found:
        size = path.stat().st_size
        target = dest / path.relative_to(src)
        if total + size > cap:
            left_out.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
        total += size
    if left_out:
        dest.mkdir(parents=True, exist_ok=True)
        note_omitted(dest, left_out)
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
            # Only real directories are kept; a symlink in their place is removed like any other entry.
            compact(run, keep=tuple(name for name in FAILED_DIRS if real_dir(run / name)))
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
