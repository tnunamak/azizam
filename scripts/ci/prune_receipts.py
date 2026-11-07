#!/usr/bin/env python3
"""Bound the gate's receipt directory: old runs keep only their verdict.

Receipts live in ROOT/<tree>/. The newest KEEP runs, and any run with a signoff receipt written
in the last SIGNOFF_DAYS days, keep everything. Every other run keeps only run.tsv, PASS, FAIL and
signoff-*.tsv, except a run with a FAIL verdict: for FAIL_DAYS days it also keeps logs/ and the
e2e failure artifacts in e2e-failures/ (screenshots, error-context files, traces of failed tests).
ARTIFACT_CAP bounds e2e-failures/ only, its OMITTED.txt note included (logs/ are the gate's step
logs and stay whole). Collection writes into a new DEST it creates itself, error contexts first,
then screenshots, then traces, and skips what would pass the cap; an existing directory over the
cap loses its traces, then its screenshots, then its largest files. Every file left out or dropped
is named in OMITTED.txt, cut short to fit the cap. A symlink present in the evidence tree is never
followed: a symlinked SRC, DEST parent or kept directory is refused or removed, walks never enter one,
collection makes every directory and file under DEST relative to the descriptor of the one above with
O_NOFOLLOW, and copies stop at the size of the opened source. The run directory and SRC are the gate's
own and are trusted: a process that can rename DEST or a SRC directory while collection runs can write
the run directory anyway, so such a swap is outside what this guards.
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
# Room collection keeps for the OMITTED.txt note inside the cap: 4 KiB, or a tenth of a smaller cap.
NOTE_ROOM = 4096


def tree_files(directory: Path) -> list[Path]:
    """The regular files under directory, without following any symlink."""
    files = []
    for parent, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
        files += [Path(parent) / name for name in names if not (Path(parent) / name).is_symlink()]
    return [path for path in files if path.is_file()]


def real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def size(paths: list[Path]) -> int:
    return sum(path.stat().st_size for path in paths)


def open_new(path: Path, append: bool = False):
    """Open path for writing, never through a symlink (O_NOFOLLOW); a new file only, unless appending."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_EXCL)
    return os.fdopen(os.open(path, flags, 0o644), "wb")


def open_dir(name: str | Path, parent: int | None = None) -> int:
    """A descriptor for a directory, refusing a symlink in its place."""
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def copy_new(source: Path, root: int, relative: Path, allowance: int) -> int | None:
    """Copy source to root/relative, making each directory below root here, never through a symlink.

    Every directory and the file are made and opened relative to the descriptor of the directory above them,
    so a link put in place of one cannot redirect the write. The size is read from the opened source; a
    source over allowance is not copied (None). Returns the bytes written.
    """
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as read:
        length = os.fstat(read.fileno()).st_size
        if length > allowance:
            return None
        directory = os.dup(root)
        try:
            for part in relative.parts[:-1]:
                try:
                    os.mkdir(part, 0o755, dir_fd=directory)
                except FileExistsError:
                    pass
                inner = open_dir(part, directory)
                os.close(directory)
                directory = inner
            target = os.open(relative.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(target, "wb") as write:
            written = 0
            while written < length:
                chunk = read.read(min(1 << 20, length - written))
                if not chunk:
                    break
                write.write(chunk)
                written += len(chunk)
        return written


def note_omitted(directory: Path, paths: list[Path], room: int) -> None:
    """Name paths in directory/OMITTED.txt, keeping the note within room bytes (whole lines only)."""
    note = directory / OMITTED
    if note.is_symlink():
        note.unlink()
    if not paths or room <= 0:
        return
    old = note.read_bytes() if note.is_file() else b""
    lines = [f"{path.relative_to(directory)}\n".encode() for path in paths]
    added = b""
    for line in lines:
        if len(old) + len(added) + len(line) > room:
            break
        added += line
    if added:
        with open_new(note, append=True) as out:
            out.write(added)


def priority(path: Path) -> int:
    """Error contexts first, then screenshots, then traces: the order evidence is worth keeping in."""
    return 0 if path.name == "error-context.md" else 1 if path.suffix == ".png" else 2


def bound_artifacts(directory: Path, cap: int = ARTIFACT_CAP) -> None:
    """Drop traces, then screenshots, then the largest other files, until the files and the note fit in cap."""
    if not real_dir(directory):
        return
    note = directory / OMITTED
    if note.is_symlink():
        note.unlink()
    files = [path for path in tree_files(directory) if path != note]
    note_size = note.stat().st_size if note.is_file() and not note.is_symlink() else 0
    total = size(files)
    if total + note_size <= cap:
        return
    order = sorted(files, key=lambda path: (path.suffix != ".zip", path.suffix != ".png", -path.stat().st_size))
    dropped, named = [], 0
    for path in order:
        if total + note_size + named <= cap:
            break
        total -= path.stat().st_size
        path.unlink()
        dropped.append(path)
        named += len(f"{path.relative_to(directory)}\n".encode())
    if note_size > cap - total:
        note.unlink(missing_ok=True)
    note_omitted(directory, dropped, cap - total)


def collect_failures(src: Path, dest: Path, cap: int = ARTIFACT_CAP) -> int:
    """Copy each failed test's artifacts from the Playwright output directories under src into a new dest.

    A passing test leaves no such files there, so every match belongs to a failed test. The path under
    src is kept, so two ports never collide. Files are copied in priority order while they fit in cap
    bytes less the note's room; the rest are named in OMITTED.txt. dest must not exist and its parent must:
    every directory and file under dest is made here (copy_new). Returns the number of files copied.
    """
    if not real_dir(src) or not real_dir(dest.parent) or dest.exists() or dest.is_symlink():
        return 0
    found = [path for path in tree_files(src) if path.name in ARTIFACT_NAMES or path.suffix in ARTIFACT_SUFFIXES]
    if not found:
        return 0
    found.sort(key=lambda path: (priority(path), str(path)))
    parent = open_dir(dest.parent)
    try:
        os.mkdir(dest.name, 0o755, dir_fd=parent)
        root = open_dir(dest.name, parent)
    finally:
        os.close(parent)
    budget = cap - min(NOTE_ROOM, cap // 10)
    copied, total, left_out = 0, 0, []
    try:
        for path in found:
            relative = path.relative_to(src)
            written = copy_new(path, root, relative, budget - total)
            if written is None:
                left_out.append(dest / relative)
                continue
            copied += 1
            total += written
    finally:
        os.close(root)
    note_omitted(dest, left_out, cap - total)
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
