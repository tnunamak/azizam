"""Private scratch storage for request content, held by one gateway for its lifetime.

Launchers use ``python3 -m gateway.private_tmp exec COMMAND ...`` to sweep while locked,
then pass the lock and TMPDIR through exec. Retained-media thumbnail and metadata-clean
staging stays beside its retained asset for atomic replacement; see REQUEST_RETENTION.md 5.1.
"""
from __future__ import annotations

import asyncio
import fcntl
import grp
import os
import pwd
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# Environment reads use literals: tests/test_secret_registration.py checks non-literal readers as possible secrets.
ENV_DIR = "AZIZAM_PRIVATE_TMPDIR"
ENV_LOCK_FD = "AZIZAM_PRIVATE_TMPDIR_LOCK_FD"
_LOCK_NAME = ".owner.lock"
_RAM_FILESYSTEMS = frozenset({"tmpfs", "ramfs"})

_active: Path | None = None
_lock_fd: int | None = None
_lock_pid: int | None = None


class PrivateTmpError(RuntimeError):
    """The private temporary directory cannot be made safe; the gateway must not start without it."""


def configured_dir(admin_db: Path | None = None) -> Path:
    """Where the directory is: ``AZIZAM_PRIVATE_TMPDIR``, else ``private-tmp`` beside the admin database (``data/``).

    ``admin_db`` names the database the directory sits beside; the gateway passes nothing and gets its own.
    """
    explicit = os.environ.get("AZIZAM_PRIVATE_TMPDIR", "").strip()
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    if admin_db is None:
        from gateway import admin_store

        admin_db = admin_store.DB_PATH
    return Path(os.path.abspath(admin_db.parent / "private-tmp"))


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


def _validate_ancestor(path: Path, info: os.stat_result) -> None:
    reason = None
    if info.st_uid not in {0, os.geteuid()}:
        reason = "owner is neither root nor the service user"
    elif info.st_mode & 0o002 and not info.st_mode & stat.S_ISVTX:
        reason = "world-writable without the sticky bit"
    elif info.st_mode & 0o020 and not info.st_mode & stat.S_ISVTX:
        # Debian OpenSSH user-group-modes patch (secure_permissions): allow owner-only groups.
        # Like Debian, getpwall() may not list NSS/LDAP users whose primary group is this one (enumerate=false).
        try:
            primary = [user for user in pwd.getpwall() if user.pw_gid == info.st_gid]
            supplementary = grp.getgrgid(info.st_gid).gr_mem
            owner_name = pwd.getpwuid(info.st_uid).pw_name
            if (any(user.pw_uid != info.st_uid for user in primary)
                    or len(supplementary) > 1
                    or any(name != owner_name for name in supplementary)):
                reason = "writable group includes someone other than the owner"
            elif not primary and not supplementary:
                reason = "writable group has zero members"
        except (KeyError, OSError) as error:
            reason = f"cannot resolve writable group membership: {error}"
    if reason:
        raise PrivateTmpError(
            f"The private temporary directory parent chain is not trusted "
            f"(ancestor {path}, uid {info.st_uid}, mode {stat.S_IMODE(info.st_mode):04o}, "
            f"service uid {os.geteuid()}): {reason}."
        )


def _open_ancestor_chain(directory: Path, *, create: bool = False) -> int:
    """Open and validate a chain without following links; caller owns the returned FD."""
    fd = os.open("/", os.O_DIRECTORY | os.O_NOFOLLOW)
    ancestor = Path("/")
    try:
        for component in (".", *directory.parts[1:]):
            ancestor = ancestor / component
            try:
                next_fd = os.open(component, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=fd)
                next_fd = os.open(component, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            _validate_ancestor(ancestor, os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


def validate_ancestors(directory: Path) -> None:
    """Read-only validation of an existing directory and all its ancestors; never mkdir or sweep."""
    os.close(_open_ancestor_chain(Path(os.path.abspath(directory))))


def prepare(directory: Path | None = None, *, clear: bool = True, hold: bool = False) -> Path:
    """Validate via directory FDs, acquire an exclusive lock, then sweep. ``hold`` keeps lifetime ownership."""
    global _lock_fd, _lock_pid
    target = Path(os.path.abspath(directory)) if directory is not None else configured_dir()
    parent_fd = directory_fd = lock_fd = None
    keep_lock = False
    try:
        # Validate the whole parent chain: a writable grandparent can replace even a 0700 parent
        # after activation, redirecting later TMPDIR allocations away from the locked inode.
        parent_fd = _open_ancestor_chain(target.parent, create=True)
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
            keep_lock = True  # borrowed descriptor: never close a caller's FD on validation failure
            lock_info = os.stat(_LOCK_NAME, dir_fd=directory_fd, follow_symlinks=False)
            inherited_info = os.fstat(lock_fd)
            if (inherited_info.st_dev, inherited_info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                raise PrivateTmpError("The inherited private temporary directory lock does not match its directory.")
            clear = False
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
