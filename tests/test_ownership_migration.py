"""Hermetic coverage for the credential-hash to principal ownership migration."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/migrations/ownership_to_principal.py"
SPEC = importlib.util.spec_from_file_location("ownership_to_principal", SCRIPT)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


def _owner(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def _json(owner: str) -> str:
    return json.dumps({"owner_key_hash": owner, "owner_key_hashes": [owner]})


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, str, str, str]:
    data, cache = tmp_path / "data", tmp_path / "cache/images"
    data.mkdir(parents=True)
    cache.mkdir(parents=True)
    admin = data / "admin.db"
    first, second, unknown = ("sha256:" + character * 64 for character in ("a", "b", "f"))
    with sqlite3.connect(admin) as db:
        db.execute("CREATE TABLE credentials(subject_kind TEXT, subject_id TEXT, app_id TEXT, hash TEXT, revoked_at INTEGER)")
        db.executemany(
            "INSERT INTO credentials VALUES(?,?,?,?,?)",
            [("app", "fallback", "app-1", first, None), ("app", "fallback", "app-1", second, 1)],
        )
    with sqlite3.connect(data / "media_jobs.db") as db:
        db.execute("CREATE TABLE media_jobs(job_id TEXT PRIMARY KEY, job_json TEXT)")
        db.execute("CREATE TABLE video_job_records(prompt_id TEXT PRIMARY KEY, owner_key_hash TEXT, record_json TEXT)")
        db.execute("INSERT INTO media_jobs VALUES('job-1', ?)", (_json(first),))
        db.execute("INSERT INTO media_jobs VALUES('job-unknown', ?)", (_json(unknown),))
        db.execute("INSERT INTO video_job_records VALUES('video-1', ?, ?)", (second, _json(second)))
    with sqlite3.connect(data / "receipts.db") as db:
        db.execute("CREATE TABLE receipts(request_id TEXT PRIMARY KEY, receipt_json TEXT)")
        db.execute("INSERT INTO receipts VALUES('request-1', ?)", (_json(first),))
    (cache / "asset.meta.json").write_text(json.dumps({"owner_key_hash": second, "owner_key_hashes": [first, second]}), encoding="utf-8")
    (cache / "unknown.meta.json").write_text(_json(unknown), encoding="utf-8")
    for digest, voice, consent in ((first, b"same", b"same-consent"), (second, b"same", b"same-consent")):
        base = data / "voices" / _owner(digest)
        (base / "voices/voice_0123456789abcdef01234567").mkdir(parents=True)
        (base / "voices/voice_0123456789abcdef01234567/reference.wav").write_bytes(voice)
        (base / "consents/cons_0123456789abcdef01234567").mkdir(parents=True)
        (base / "consents/cons_0123456789abcdef01234567/recording.wav").write_bytes(consent)
    (data / "voices" / _owner(unknown)).mkdir()
    return data, cache, admin, first, second, unknown


def test_dry_run_is_byte_identical_and_apply_migrates_every_surface(tmp_path, capsys):
    data, cache, admin, first, second, unknown = _fixture(tmp_path)
    paths = [*data.rglob("*"), *cache.rglob("*")]
    before = {path: path.read_bytes() for path in paths if path.is_file()}
    arguments = ["--data-dir", str(data), "--cache-dir", str(cache), "--admin-db", str(admin)]

    assert migration.main(arguments) == 0
    output = capsys.readouterr().out
    assert "DRY RUN (no writes)" in output
    assert unknown in output
    assert before == {path: path.read_bytes() for path in before}
    assert migration.main([*arguments, "--dry-run"]) == 0
    assert "DRY RUN (no writes)" in capsys.readouterr().out

    assert migration.main([*arguments, "--apply"]) == 0
    output = capsys.readouterr().out
    assert "Backup:" in output
    backup = next((data / "migration-backups").glob("ownership-to-principal.backup.*"))
    assert (backup / "db/media_jobs.db").is_file()
    assert (backup / "db/receipts.db").is_file()
    assert (backup / "cache-images/asset.meta.json").is_file()
    assert (backup / "voices" / _owner(first)).is_dir()
    with sqlite3.connect(backup / "db/media_jobs.db") as db:
        assert json.loads(db.execute("SELECT job_json FROM media_jobs WHERE job_id='job-1'").fetchone()[0])["owner_key_hash"] == first

    principal = "app:app-1"
    with sqlite3.connect(data / "media_jobs.db") as db:
        assert json.loads(db.execute("SELECT job_json FROM media_jobs WHERE job_id='job-1'").fetchone()[0])["owner_key_hash"] == principal
        assert json.loads(db.execute("SELECT job_json FROM media_jobs WHERE job_id='job-unknown'").fetchone()[0])["owner_key_hash"] == unknown
        owner, record = db.execute("SELECT owner_key_hash, record_json FROM video_job_records").fetchone()
        assert owner == principal
        assert json.loads(record)["owner_key_hash"] == principal
    with sqlite3.connect(data / "receipts.db") as db:
        assert json.loads(db.execute("SELECT receipt_json FROM receipts").fetchone()[0])["owner_key_hash"] == principal
    assert json.loads((cache / "asset.meta.json").read_text())["owner_key_hash"] == principal
    assert json.loads((cache / "asset.meta.json").read_text())["owner_key_hashes"] == [principal]
    assert json.loads((cache / "unknown.meta.json").read_text())["owner_key_hash"] == unknown
    destination = data / "voices" / _owner(principal)
    assert (destination / "voices/voice_0123456789abcdef01234567/reference.wav").read_bytes() == b"same"
    assert not (data / "voices" / _owner(first)).exists()
    assert not (data / "voices" / _owner(second)).exists()
    assert (data / "voices" / _owner(unknown)).is_dir()

    assert migration.main([*arguments, "--apply"]) == 0
    assert len(list((data / "migration-backups").glob("ownership-to-principal.backup.*"))) == 1
    assert "0 changes applied" in capsys.readouterr().out


def test_migration_uses_shared_principal_mapping():
    from gateway.ownership import principal_key_for_credential
    assert migration.principal_key_for_credential is principal_key_for_credential
    for kind in ("app", "user", "service_account"):
        assert migration.principal_key_for_credential({"subject_kind": kind, "subject_id": "same"}) == f"{kind}:same"
    assert migration.principal_key_for_credential({"subject_kind": "unknown", "subject_id": "same"}) is None


def test_conflicting_voice_ids_refuse_without_writing(tmp_path, capsys):
    data, cache, admin, first, second, _unknown = _fixture(tmp_path)
    record = data / "voices" / _owner(second) / "voices/voice_0123456789abcdef01234567/reference.wav"
    record.write_bytes(b"different")
    original = record.read_bytes()
    args = ["--data-dir", str(data), "--cache-dir", str(cache), "--admin-db", str(admin), "--apply"]

    assert migration.main(args) == 1
    assert record.read_bytes() == original
    assert not list((data / "migration-backups").glob("ownership-to-principal.backup.*"))
    assert "CONFLICT:" in capsys.readouterr().err
