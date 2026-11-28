"""Retain private UI fixture logs outside the disposable state directory."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import stat


def retain_logs(state_dir: Path, dest_root: Path) -> Path:
    dest_root.mkdir(mode=0o700, exist_ok=True)
    root = dest_root.lstat()
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.getuid() or root.st_mode & 0o077:
        raise OSError('the failed-log root must be an owner-private directory')

    now = datetime.now(timezone.utc)
    while True:
        kept = dest_root / f"failed-{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"
        try:
            kept.mkdir(mode=0o700)
            break
        except FileExistsError:
            now += timedelta(microseconds=1)

    try:
        kept.chmod(0o700)
        for source in sorted(state_dir.glob('*.log')):
            if not stat.S_ISREG(source.lstat().st_mode):
                continue
            read_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(read_fd, 'rb') as reader:
                if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                    continue
                write_fd = os.open(kept / source.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(write_fd, 'wb') as writer:
                    os.fchmod(writer.fileno(), 0o600)
                    shutil.copyfileobj(reader, writer)
    except OSError:
        shutil.rmtree(kept, ignore_errors=True)
        raise
    return kept
