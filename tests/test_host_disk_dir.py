"""The runner keeps pytest's TMPDIR in RAM and offers a real disk only through
the `host_disk_dir` fixture (docs/design/TEST_STRATEGY.md 4.4, slice S0)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

RAM_FS = {"tmpfs", "ramfs"}


def _fs_type(path: Path) -> str:
    return subprocess.run(["stat", "-f", "-c", "%T", str(path)], check=True, capture_output=True, text=True).stdout.strip()


def test_tmp_path_is_ram_and_host_disk_dir_is_not(tmp_path: Path, host_disk_dir: Path) -> None:
    if os.environ.get("AZIZAM_PYTEST_TMPFS_FALLBACK"):
        return  # the runner announced a disk fallback
    assert _fs_type(tmp_path) in RAM_FS
    assert _fs_type(host_disk_dir) not in RAM_FS
    assert os.stat(tmp_path).st_dev != os.stat(host_disk_dir).st_dev


def test_host_disk_dir_is_private_and_writable(host_disk_dir: Path, tmp_path: Path) -> None:
    (host_disk_dir / "probe").write_bytes(b"x")
    assert (host_disk_dir / "probe").read_bytes() == b"x"
    assert not host_disk_dir.is_relative_to(tmp_path)
