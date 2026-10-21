"""Private modes for content and identity writes, independent of process umask."""

from __future__ import annotations

import os
import stat
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import admin_store, custom_voices, private_io, providers, thumbnails
from gateway.engine_protocol import node_info

ROOT = Path(__file__).resolve().parents[1]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def permissive_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def test_unit_sets_private_umask_and_disables_core_dumps() -> None:
    lines = [line.strip() for line in (ROOT / "azizam.service").read_text().splitlines()]
    assert "UMask=0077" in lines
    assert "LimitCORE=0" in lines


def test_media_cache_and_sidecar_are_private_under_permissive_umask(monkeypatch, tmp_path, permissive_umask) -> None:
    import proxy

    cache = tmp_path / "cache" / "images"
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    asset_id = proxy._store_asset_bytes(b"private media", kind="img", suffix=".bin")
    asset = next(cache.glob("*.bin"))
    sidecar = cache / f"{asset_id.split('_', 1)[1]}.meta.json"
    assert mode(cache) == 0o700
    assert mode(asset) == mode(sidecar) == 0o600
    assert not list(cache.glob("*.tmp"))


def test_custom_voice_record_is_private_under_permissive_umask(tmp_path, permissive_umask) -> None:
    store = custom_voices.VoiceStore(tmp_path / "voices")
    record = store.create_voice(
        None, name="Private", voice_type="audio_sample", reference_wav=b"wav",
        duration_seconds=1, original=None, original_extension="wav", consent_id=None,
        description=None, labels={}, measurements={}, warnings=[],
    )
    record_dir = next((tmp_path / "voices").rglob(record["id"]))
    assert mode(record_dir) == 0o700
    assert all(mode(path) == 0o600 for path in record_dir.iterdir() if path.is_file())
    assert all(mode(path) == 0o700 for path in record_dir.rglob("*") if path.is_dir())


def test_node_info_cache_and_staging_are_private_under_permissive_umask(monkeypatch, tmp_path, permissive_umask) -> None:
    node_info._CACHE.clear()
    node_info._FAILED_AT.clear()
    monkeypatch.setattr(node_info, "DISK_CACHE_DIR", tmp_path / "node-info")
    node_info._disk_write("SomeNode", {"identity": "private"})
    target = tmp_path / "node-info" / "SomeNode.json"
    assert mode(target.parent) == 0o700
    assert mode(target) == 0o600
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]


def test_private_atomic_writer_stages_mode_0600(monkeypatch, tmp_path, permissive_umask) -> None:
    target = tmp_path / "store" / "content.json"
    replace = private_io.os.replace
    staging_modes = []

    def inspect_stage(source, destination):
        staging_modes.append(mode(Path(source)))
        replace(source, destination)

    monkeypatch.setattr(private_io.os, "replace", inspect_stage)
    private_io.atomic_write_text(target, "private")
    assert staging_modes == [0o600]
    assert mode(target) == 0o600


def test_thumbnail_file_and_directory_are_private_under_permissive_umask(tmp_path, permissive_umask) -> None:
    from PIL import Image

    source = tmp_path / "images" / "source.png"
    source.parent.mkdir()
    Image.new("RGB", (8, 8), "red").save(source)
    thumbnail = thumbnails.thumbnail_for(source)
    assert thumbnail is not None
    assert mode(thumbnail.parent) == 0o700
    assert mode(thumbnail) == 0o600
    assert not list(thumbnail.parent.glob("*.tmp"))


def test_admin_store_schema_backup_is_private_under_permissive_umask(monkeypatch, tmp_path, permissive_umask) -> None:
    db = tmp_path / "admin.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (6)")
    conn.commit()
    monkeypatch.setattr("gateway.access_plan_migration.migrate", lambda _conn: None)
    try:
        admin_store._migrate_to_access_plans(conn)
    finally:
        conn.close()
    assert mode(admin_store.access_plans_backup_path(db)) == 0o600


def test_provider_migration_backup_is_private_under_permissive_umask(tmp_path, permissive_umask) -> None:
    db = tmp_path / "provider.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE provider_spend (profile_id TEXT)")
    providers._backup_ledger_before_plan_migration(db)
    backup = db.with_name("provider.db.bak.pre-access-plans")
    assert mode(backup) == 0o600


def test_migration_and_ui_sqlite_backups_are_private_under_permissive_umask(tmp_path, permissive_umask) -> None:
    from scripts.migrations.ownership_to_principal import backup_sqlite
    from scripts.ui.live_data_copy import _backup

    source = tmp_path / "source.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE identity (value TEXT)")
        conn.execute("INSERT INTO identity VALUES ('private')")
    migration_copy = tmp_path / "migration" / "copy.db"
    ui_copy = tmp_path / "ui" / "copy.db"
    backup_sqlite(source, migration_copy)
    _backup(source, ui_copy)
    assert mode(migration_copy) == mode(ui_copy) == 0o600
    assert mode(migration_copy.parent) == mode(ui_copy.parent) == 0o700


def test_mode_report_groups_paths_and_fix_only_changes_fixture_tree(tmp_path, permissive_umask) -> None:
    root = tmp_path / "fixture"
    images = root / "cache" / "images"
    images.mkdir(parents=True)
    (images / "sensitive-name.bin").write_bytes(b"secret")
    os.chmod(images, 0o777)
    os.chmod(images / "sensitive-name.bin", 0o666)
    command = [sys.executable, str(ROOT / "scripts" / "check-private-modes"), "--root", str(root)]
    report = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert report.returncode == 1
    assert "cache/images" in report.stdout
    assert "sensitive-name.bin" not in report.stdout
    fixed = subprocess.run([*command, "--fix"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert fixed.returncode == 0
    assert mode(images) == 0o700
    assert mode(images / "sensitive-name.bin") == 0o600


def test_mode_fixer_does_not_follow_symlink_directories(tmp_path, permissive_umask) -> None:
    root, outside = tmp_path / "fixture", tmp_path / "outside"
    outside.mkdir()
    private_file = outside / "must-stay-alone.bin"
    private_file.write_bytes(b"outside")
    os.chmod(private_file, 0o666)
    images = root / "cache" / "images"
    images.parent.mkdir(parents=True)
    images.symlink_to(outside, target_is_directory=True)
    command = [sys.executable, str(ROOT / "scripts" / "check-private-modes"), "--root", str(root), "--fix"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert mode(private_file) == 0o666
