"""Retired and stale screenshots must not satisfy cutover evidence coverage."""
import os
import sys
from pathlib import Path

from scripts.ui import collect_screens


def test_collector_excludes_retired_and_pre_run_captures(tmp_path, monkeypatch):
    source = tmp_path / "screens"
    source.mkdir()
    for name, timestamp in (
        ("status-seeded-light-390.png", 200),
        ("status-empty-light-390.png", 100),
        ("admin-status-seeded-light-390.png", 200),
        ("admin-profiles-p1v-fixture-n-a-light-390.png", 200),
    ):
        path = source / name
        path.write_bytes(b"test-owned capture")
        os.utime(path, (timestamp, timestamp))
    destination = tmp_path / "review"
    monkeypatch.setattr(collect_screens, "SOURCE", source)
    monkeypatch.setattr(sys, "argv", ["collect_screens", "--destination", str(destination), "--since", "150"])
    assert collect_screens.main() == 0
    assert {path.name for path in destination.glob("*.png")} == {"status-seeded-light-390.png"}
    index = (destination / "INDEX.md").read_text()
    assert "admin-status-seeded" not in index
    assert "p1v-fixture" not in index
    assert "/admin/status" not in index
    assert "status:empty:light:390" in index
