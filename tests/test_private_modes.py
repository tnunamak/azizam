"""Private modes for content and identity writes, independent of process umask."""

from __future__ import annotations

import os
import stat
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import importlib.util

import pytest

from gateway import (
    admin_store, category_store, custom_voices, media_jobs,
    private_io, prompt_metadata_migration, providers, receipt_store, thumbnails,
)
import metrics_store
import responses_store
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


def test_private_directory_creation_tolerates_a_concurrent_winner(monkeypatch, tmp_path, permissive_umask) -> None:
    target = tmp_path / "race" / "private"
    barrier = threading.Barrier(2)
    exists = Path.exists

    def synchronized_exists(path):
        result = exists(path)
        if path == target and not result:
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(Path, "exists", synchronized_exists)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(private_io.ensure_private_dir, (target, target)))
    assert results == [target, target]
    assert mode(target) == 0o700


def test_atomic_copy_closes_stage_fd_when_source_open_fails(monkeypatch, tmp_path, permissive_umask) -> None:
    source = tmp_path / "missing-source"
    target = tmp_path / "copy" / "target"
    mkstemp = private_io.tempfile.mkstemp
    staged_fds = []

    def track_fd(*args, **kwargs):
        fd, name = mkstemp(*args, **kwargs)
        staged_fds.append(fd)
        return fd, name

    monkeypatch.setattr(private_io.tempfile, "mkstemp", track_fd)
    with pytest.raises(FileNotFoundError):
        private_io.atomic_copy_file(source, target)
    assert len(staged_fds) == 1
    with pytest.raises(OSError):
        os.fstat(staged_fds[0])
    assert not list(target.parent.iterdir())


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
    class Report:
        @staticmethod
        def as_dict():
            return {}

    db = tmp_path / "admin.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (6)")
    conn.commit()
    monkeypatch.setattr("gateway.access_plan_migration.migrate", lambda _conn: Report())
    try:
        admin_store._migrate_to_access_plans(conn)
    finally:
        conn.close()
    assert mode(admin_store.access_plans_backup_path(db)) == 0o600


def test_local_register_backup_precreates_private_sqlite_target(tmp_path, permissive_umask) -> None:
    from scripts.local.register_service_from_config import _backup

    db = tmp_path / "admin.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE identity (value TEXT)")
        conn.execute("INSERT INTO identity VALUES ('private')")
    backup = _backup(db)
    assert mode(backup) == 0o600
    assert mode(backup.parent) == 0o700


@pytest.mark.parametrize("store", ["media_jobs", "responses", "metrics", "receipts", "providers"])
def test_content_sqlite_stores_create_private_databases(tmp_path, permissive_umask, store) -> None:
    db = tmp_path / store / "nested" / f"{store}.db"
    if store == "media_jobs":
        media_jobs.init_db(db)
    elif store == "responses":
        responses_store.init_db(db)
    elif store == "metrics":
        metrics_store.init_db(db)
    elif store == "receipts":
        receipt_store.init_db(db)
    else:
        providers.ensure_ledger(db)
    assert mode(db) == 0o600
    assert mode(db.parent) == 0o700
    assert mode(db.parent.parent) == 0o700


def test_existing_sqlite_database_mode_is_left_for_operator_migration(tmp_path, permissive_umask) -> None:
    db = tmp_path / "existing" / "media_jobs.db"
    media_jobs.init_db(db)
    os.chmod(db, 0o644)
    media_jobs.init_db(db)
    assert mode(db) == 0o644


def test_prompt_metadata_migration_database_is_private(tmp_path, permissive_umask) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    state = tmp_path / "state" / "deep" / "migration.db"
    prompt_metadata_migration.strip_cached_assets(cache, state)
    assert mode(state) == 0o600
    assert mode(state.parent) == mode(state.parent.parent) == 0o700


def test_category_store_database_is_private(tmp_path, permissive_umask) -> None:
    path = tmp_path / "category" / "nested" / "categories.db"
    category_store.CategoryStore(path)
    assert mode(path) == 0o600
    assert mode(path.parent) == mode(path.parent.parent) == 0o700


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
    node_info = root / "cache" / "node-info"
    node_info.mkdir()
    (node_info / "private-node-name.json").write_text("{}")
    os.chmod(images, 0o777)
    os.chmod(images / "sensitive-name.bin", 0o666)
    command = [sys.executable, str(ROOT / "scripts" / "check-private-modes"), "--root", str(root)]
    report = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert report.returncode == 1
    assert "cache" in report.stdout
    assert "sensitive-name.bin" not in report.stdout
    assert "private-node-name.json" not in report.stdout
    fixed = subprocess.run([*command, "--fix"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert fixed.returncode == 0
    assert mode(images) == 0o700
    assert mode(images / "sensitive-name.bin") == 0o600
    assert mode(node_info) == 0o700
    assert mode(node_info / "private-node-name.json") == 0o600


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
    assert result.returncode == 1
    assert "cache" in result.stdout
    assert "errors=1" in result.stdout
    assert mode(private_file) == 0o666


def test_mode_report_contains_directory_errors_without_names(tmp_path, monkeypatch, capsys) -> None:
    script = ROOT / "scripts" / "check-private-modes"
    spec = importlib.util.spec_from_file_location("check_private_modes", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "fixture"
    denied = root / "data" / "sensitive-dir"
    denied.mkdir(parents=True)
    open_file = os.open

    def denied_open(path, flags, *args, **kwargs):
        if Path(path) == denied:
            raise PermissionError("simulated unreadable directory")
        return open_file(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", denied_open)
    assert module.main(["--root", str(root)]) == 1
    report = capsys.readouterr().out
    assert "data" in report and "errors=1" in report
    assert "sensitive-dir" not in report
    assert "Traceback" not in report


def test_mode_report_handles_directory_replaced_by_symlink(tmp_path, monkeypatch, capsys) -> None:
    script = ROOT / "scripts" / "check-private-modes"
    spec = importlib.util.spec_from_file_location("check_private_modes_race", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "fixture"
    swapped = root / "cache" / "private-name"
    moved = root / "cache" / "moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    exposed = outside / "outside-name.bin"
    exposed.write_bytes(b"keep mode")
    os.chmod(exposed, 0o666)
    swapped.mkdir(parents=True)
    original_open = os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if Path(path) == swapped and not replaced:
            replaced = True
            swapped.rename(moved)
            swapped.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", replace_before_open)
    assert module.main(["--root", str(root), "--fix"]) == 1
    assert replaced
    report = capsys.readouterr().out
    assert "cache" in report and "errors=1" in report
    assert "private-name" not in report and "outside-name" not in report
    assert mode(exposed) == 0o666
