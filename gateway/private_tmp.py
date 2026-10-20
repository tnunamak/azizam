"""Private scratch storage for request content, held by one gateway for its lifetime.

Launchers use ``python3 -m gateway.private_tmp exec COMMAND ...`` to sweep while locked,
then pass the lock and TMPDIR through exec. Retained-media thumbnail staging is deliberately
beside its retained asset for atomic replacement; see REQUEST_RETENTION.md 5.1 rows 4-5.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# The two names are read as literals below: gateway/ code that reads an environment variable by a non-literal name
# is checked by tests/test_secret_registration.py as a possible secret reader.
ENV_DIR = "AZIZAM_PRIVATE_TMPDIR"
# Exported by the exec launcher with the lifetime lock.
ENV_PREPARED = "AZIZAM_PRIVATE_TMPDIR_PREPARED"
ENV_LOCK_FD = "AZIZAM_PRIVATE_TMPDIR_LOCK_FD"
_LOCK_NAME = ".owner.lock"
_RAM_FILESYSTEMS = frozenset({"tmpfs", "ramfs"})

_active: Path | None = None
_lock_fd: int | None = None
_lock_pid: int | None = None


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


async def run_worker(function, /, *args, **kwargs):
    """Keep ownership of a scratch-writing thread until its cleanup finishes, even on repeated cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()  # cancellation wins over a worker error; consume it
        raise


def sweep(directory_fd: int) -> list[str]:
    """Remove scratch entries relative to the opened directory, never the lifetime lock or a link target."""
    failed: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in list(entries):
            if entry.name == _LOCK_NAME:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.name, dir_fd=directory_fd)
                else:
                    os.unlink(entry.name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except OSError:
                failed.append(entry.name)
    return failed


def prepare(directory: Path | None = None, *, clear: bool = True, hold: bool = False) -> Path:
    """Validate via directory FDs, acquire an exclusive lock, then sweep. ``hold`` keeps lifetime ownership."""
    global _lock_fd, _lock_pid
    target = Path(os.path.abspath(directory)) if directory is not None else configured_dir()
    parent_fd = directory_fd = lock_fd = None
    keep_lock = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Check every ancestor: trusting only the immediate parent still permits its replacement.
        parent_fd = os.open("/", os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in target.parent.parts[1:]:
            next_fd = os.open(component, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
            info = os.fstat(parent_fd)
            if info.st_uid not in {0, os.geteuid()} or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                raise PrivateTmpError(f"The private temporary directory parent {target.parent} is not trusted.")
        try:
            os.mkdir(target.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            directory_fd = os.open(target.name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as error:
            raise PrivateTmpError(f"The private temporary directory {target} is a link or not a directory.") from error
        info = os.fstat(directory_fd)
        if info.st_uid != os.geteuid():
            raise PrivateTmpError(f"The private temporary directory {target} belongs to another user; the service user must own it.")
        os.fchmod(directory_fd, 0o700)
        if _filesystem_type(Path(f"/proc/self/fd/{directory_fd}")) in _RAM_FILESYSTEMS:
            raise PrivateTmpError(
                f"The private temporary directory {target} is on a RAM-backed filesystem; long audio would fill memory. "
                f"Set {ENV_DIR} to a path on disk."
            )
        # An inherited descriptor is the same locked open-file description across launcher execs.
        inherited = os.environ.get("AZIZAM_PRIVATE_TMPDIR_LOCK_FD", "") if hold else ""
        owned = _lock_fd if hold and _lock_pid == os.getpid() else None
        if owned is not None or inherited:
            lock_fd = owned if owned is not None else int(inherited)
            lock_info = os.stat(_LOCK_NAME, dir_fd=directory_fd, follow_symlinks=False)
            inherited_info = os.fstat(lock_fd)
            if (inherited_info.st_dev, inherited_info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                raise PrivateTmpError("The inherited private temporary directory lock does not match its directory.")
            clear = False
            keep_lock = True
        else:
            lock_fd = os.open(_LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid() or lock_info.st_nlink != 1:
            raise PrivateTmpError("The private temporary directory lock is not a private regular file.")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PrivateTmpError(f"The private temporary directory {target} is in use by another gateway.") from error
        if clear:
            failed = sweep(directory_fd)
            if failed:
                raise PrivateTmpError(f"Could not remove stale entries in {target}: {', '.join(failed[:5])}")
        current_info = os.stat(target, follow_symlinks=False)
        if (current_info.st_dev, current_info.st_ino) != (info.st_dev, info.st_ino):
            raise PrivateTmpError(f"The private temporary directory {target} changed during preparation.")
        if hold:
            _lock_fd, _lock_pid = lock_fd, os.getpid()
            keep_lock = True
        return target
    except (OSError, ValueError) as error:
        raise PrivateTmpError(f"The private temporary directory {target} cannot be created or locked: {error}") from error
    finally:
        if lock_fd is not None and not keep_lock:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def activate(directory: Path | None = None) -> Path:
    """Claim lifetime ownership and point this process and its children at the private directory."""
    global _active
    target = Path(os.path.abspath(directory)) if directory is not None else configured_dir()
    target = prepare(target, hold=True)
    tempfile.tempdir = str(target)
    for name in ("TMPDIR", "TMP", "TEMP", ENV_DIR):
        os.environ[name] = str(target)
    os.environ[ENV_PREPARED] = str(target)
    os.environ[ENV_LOCK_FD] = str(_lock_fd)
    _active = target
    return target


def main(argv: list[str]) -> int:
    if argv != ["prepare"] and not (len(argv) > 1 and argv[0] == "exec"):
        print("usage: python3 -m gateway.private_tmp prepare | exec COMMAND ...", file=sys.stderr)
        return 2
    try:
        if argv[0] == "exec":
            activate()
            os.set_inheritable(_lock_fd, True)
            os.execvp(argv[1], argv[1:])
        print(prepare())
    except PrivateTmpError as error:
        print(f"[private-tmp] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
