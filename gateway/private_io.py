"""Create private content files and same-directory replacements."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path


def ensure_private_dir(path: Path | str) -> Path:
    """Create ``path`` with mode 0700 and tighten that directory if it exists."""
    path = Path(path)
    for component in (path, *path.parents):
        try:
            if stat.S_ISLNK(component.lstat().st_mode):
                raise NotADirectoryError(component)
        except FileNotFoundError:
            pass
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _set_private_directory_mode(directory)
    if path.exists():
        _set_private_directory_mode(path)
    return path


def _set_private_directory_mode(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def create_private_file(path: Path | str) -> Path:
    """Exclusively create an empty mode-0600 file for writers such as SQLite."""
    path = Path(path)
    ensure_private_dir(path.parent)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return path


def prepare_private_database(path: Path | str) -> Path:
    """Privately precreate a new SQLite database; leave existing database modes alone."""
    path = Path(path)
    ensure_private_dir(path.parent)
    if path.exists():
        return path
    try:
        return create_private_file(path)
    except FileExistsError:
        # Another initializer won the create race. This was absent at entry, so
        # validate and tighten only the newly-created concurrent result.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise FileExistsError(path)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return path


def _stage(path: Path) -> tuple[int, Path]:
    ensure_private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.fchmod(fd, 0o600)
    return fd, Path(name)


def atomic_write_bytes(path: Path | str, content: bytes) -> Path:
    """Atomically replace ``path`` with bytes in a private same-directory file."""
    path = Path(path)
    fd, temporary = _stage(path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def atomic_write_text(path: Path | str, content: str, *, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, content.encode(encoding))


def atomic_copy_file(source: Path | str, target: Path | str) -> Path:
    """Copy a file into a private atomic replacement without preserving its mode."""
    source, target = Path(source), Path(target)
    fd, temporary = _stage(target)
    target_file = os.fdopen(fd, "wb")
    try:
        with target_file:
            with source.open("rb") as source_file:
                shutil.copyfileobj(source_file, target_file)
                target_file.flush()
                os.fchmod(target_file.fileno(), 0o600)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def copy_private_tree(source: Path | str, target: Path | str) -> Path:
    """Copy regular files and directories without following symlinks or source modes."""
    source, target = Path(source), Path(target)
    ensure_private_dir(target)
    for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        destination_dir = target / relative
        ensure_private_dir(destination_dir)
        dirnames[:] = [name for name in dirnames if not (current_path / name).is_symlink()]
        for name in filenames:
            source_file = current_path / name
            if source_file.is_symlink() or not source_file.is_file():
                continue
            atomic_copy_file(source_file, destination_dir / name)
    return target
