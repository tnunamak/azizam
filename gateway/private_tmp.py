"""The gateway's one private temporary directory.

Every temporary file the gateway or a child process creates lands here: Starlette's multipart spill, ffmpeg and
ffprobe scratch files, the STT workspace, and what LiteLLM and engines write through ``TMPDIR``. The directory is
mode 0700, owned by the service user, on disk (long audio would fill a RAM-backed one) and emptied at start, so a
crash leaves nothing behind. See docs/design/REQUEST_RETENTION.md section 5.1 rows 4-5.

``start.sh`` calls ``python3 -m gateway.private_tmp prepare`` before it starts LiteLLM, so the sweep runs once and
before any child writes. ``proxy.py`` then calls ``activate`` to point this process's ``tempfile`` at the same
directory. Run one gateway per directory: a second gateway on the same directory would sweep the first one's files.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# The two names are read as literals below: gateway/ code that reads an environment variable by a non-literal name
# is checked by tests/test_secret_registration.py as a possible secret reader.
ENV_DIR = "AZIZAM_PRIVATE_TMPDIR"
# Set by ``start.sh`` after it has swept, so ``activate`` does not sweep a second time under a running LiteLLM.
ENV_PREPARED = "AZIZAM_PRIVATE_TMPDIR_PREPARED"
_RAM_FILESYSTEMS = frozenset({"tmpfs", "ramfs"})

_active: Path | None = None


class PrivateTmpError(RuntimeError):
    """The private temporary directory cannot be made safe; the gateway must not start without it."""


def configured_dir() -> Path:
    """Where the directory is: ``AZIZAM_PRIVATE_TMPDIR``, else ``private-tmp`` beside the admin database (``data/``)."""
    explicit = os.environ.get("AZIZAM_PRIVATE_TMPDIR", "").strip()
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    from gateway import admin_store

    return Path(os.path.abspath(admin_store.DB_PATH.parent / "private-tmp"))


def current() -> Path:
    """The directory this process uses for temporary files: the activated one, else the configured one."""
    return _active if _active is not None else configured_dir()


def inside(path: Path | str) -> bool:
    """True when ``path`` is the private directory or below it."""
    root = os.path.realpath(current())
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


def _filesystem_type(path: Path, mountinfo: str = "/proc/self/mountinfo") -> str | None:
    """The type of the mount holding ``path``, or None when the host cannot say."""
    try:
        target = os.path.realpath(path)
        best, best_type = "", None
        with open(mountinfo, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                head, _, tail = line.partition(" - ")
                mount_point = head.split()[4].replace("\\040", " ")
                if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) and len(mount_point) >= len(best):
                    best, best_type = mount_point, tail.split()[0]
        return best_type
    except (OSError, IndexError):
        return None


def sweep(directory: Path) -> list[str]:
    """Remove everything inside ``directory`` without following links; return the names that could not be removed."""
    failed: list[str] = []
    with os.scandir(directory) as entries:
        for entry in list(entries):
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                else:
                    os.unlink(entry.path)
            except FileNotFoundError:
                continue
            except OSError:
                failed.append(entry.name)
    return failed


def prepare(directory: Path | None = None, *, clear: bool = True) -> Path:
    """Create the directory (mode 0700, ours, on disk), refuse an unsafe one, and by default empty it."""
    target = Path(os.path.abspath(directory)) if directory is not None else configured_dir()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(target, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(target)
    except OSError as error:
        raise PrivateTmpError(f"The private temporary directory {target} cannot be created: {error.strerror}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise PrivateTmpError(f"The private temporary directory {target} is a link or not a directory; remove it.")
    if info.st_uid != os.geteuid():
        raise PrivateTmpError(f"The private temporary directory {target} belongs to another user; the service user must own it.")
    if info.st_mode & 0o077:
        try:
            os.chmod(target, 0o700)
        except OSError as error:
            raise PrivateTmpError(f"The private temporary directory {target} cannot be set to mode 0700: {error.strerror}") from error
    if _filesystem_type(target) in _RAM_FILESYSTEMS:
        raise PrivateTmpError(
            f"The private temporary directory {target} is on a RAM-backed filesystem; long audio would fill memory. "
            f"Set {ENV_DIR} to a path on disk."
        )
    if clear:
        failed = sweep(target)
        if failed:
            print(f"[private-tmp] could not remove {len(failed)} stale entries in {target}: {', '.join(failed[:5])}",
                  file=sys.stderr, flush=True)
    return target


def activate(directory: Path | None = None) -> Path:
    """Point this process and its children at the private directory. Sweeps unless the launcher already did."""
    global _active
    target = Path(os.path.abspath(directory)) if directory is not None else configured_dir()
    launcher_swept = os.environ.get("AZIZAM_PRIVATE_TMPDIR_PREPARED") == str(target)
    target = prepare(target, clear=not launcher_swept)
    tempfile.tempdir = str(target)
    for name in ("TMPDIR", "TMP", "TEMP", ENV_DIR):
        os.environ[name] = str(target)
    _active = target
    return target


def main(argv: list[str]) -> int:
    if argv != ["prepare"]:
        print("usage: python3 -m gateway.private_tmp prepare", file=sys.stderr)
        return 2
    try:
        print(prepare())
    except PrivateTmpError as error:
        print(f"[private-tmp] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
