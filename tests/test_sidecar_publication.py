"""Regression coverage for sidecar startup and its watcher acknowledgement."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import yaml

from gateway import provider_sidecar


ROOT = Path(__file__).resolve().parents[1]


def write_registry(path, model="model-a"):
    registry = {"services": {"local": {
        "id": "local", "kind": "llm", "capabilities": ["openai"],
        "config": {"base_url": "http://127.0.0.1:9999/v1", "models": [model]},
    }}}
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS operator_settings (key TEXT PRIMARY KEY, value_json TEXT)")
        conn.execute("INSERT OR REPLACE INTO operator_settings VALUES ('service_registry', ?)", (json.dumps(registry),))
    return registry


def stop_exec(monkeypatch):
    def execvpe(*args):
        raise SystemExit(23)
    monkeypatch.setattr(os, "execvpe", execvpe)


def test_sidecar_resolves_yaml_admin_db_like_gateway(tmp_path, monkeypatch):
    db = tmp_path / "owner.db"
    write_registry(db)
    config = tmp_path / "gateway.yml"
    # The gateway anchors relative admin paths to the checkout, not YAML or cwd.
    config.write_text(yaml.safe_dump({"admin": {"db_path": os.path.relpath(db, ROOT)}}))
    monkeypatch.setenv("AZIZAM_CONFIG_PATH", str(config))
    monkeypatch.delenv("AZIZAM_ADMIN_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / "selected.yaml"
    stop_exec(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        provider_sidecar.main(["run", "--config", str(artifact), "--", "litellm"])
    assert exc.value.code == 23
    assert "model-a" in artifact.read_text()


@pytest.mark.parametrize("failure", [
    "missing-db", "missing-table", "missing-row", "invalid-json",
    "invalid-shape", "empty-document", "invalid-services", "unreadable",
])
def test_unavailable_registry_preserves_artifact(tmp_path, monkeypatch, failure):
    db = tmp_path / "admin.db"
    if failure == "unreadable":
        db.write_bytes(b"not a sqlite database")
    elif failure != "missing-db":
        with sqlite3.connect(db) as conn:
            if failure != "missing-table":
                conn.execute("CREATE TABLE operator_settings (key TEXT PRIMARY KEY, value_json TEXT)")
                if failure != "missing-row":
                    payload = {"invalid-json": "{", "empty-document": "{}", "invalid-services": '{"services": null}'}.get(failure, "[]")
                    conn.execute("INSERT INTO operator_settings VALUES ('service_registry', ?)",
                                 (payload,))
    artifact = tmp_path / "selected.yaml"
    original = b"model_list:\n- model_name: retained\n"
    artifact.write_bytes(original)
    stop_exec(monkeypatch)
    with pytest.raises((RuntimeError, sqlite3.Error), match="registry|database"):
        provider_sidecar.main(["run", "--db", str(db), "--config", str(artifact), "--", "litellm"])
    assert artifact.read_bytes() == original
    if failure == "missing-db":
        assert not db.exists()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_registry_writer_cannot_commit_between_sidecar_snapshot_and_publish(tmp_path, monkeypatch, journal_mode):
    db = tmp_path / "admin.db"
    write_registry(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
    artifact = tmp_path / "selected.yaml"
    read = provider_sidecar._read_service_registry
    writer_blocked = []

    def interleave(path):
        snapshot = read(path)
        with sqlite3.connect(db, timeout=0) as writer:
            try:
                writer.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                writer_blocked.append(True)
            else:
                # Original race: B commits and publishes before sidecar writes A.
                snapshot_b = {"services": {"local": {"id": "local", "kind": "llm", "capabilities": ["openai"],
                    "config": {"base_url": "http://127.0.0.1:9999/v1", "models": ["model-b"]}}}}
                writer.execute("UPDATE operator_settings SET value_json=?", (json.dumps(snapshot_b),))
                writer.commit()
                provider_sidecar.write_litellm_config_artifact(snapshot_b, base_dir=tmp_path, path=artifact)
        return snapshot

    monkeypatch.setattr(provider_sidecar, "_read_service_registry", interleave)
    stop_exec(monkeypatch)
    with pytest.raises(SystemExit):
        provider_sidecar.main(["run", "--db", str(db), "--config", str(artifact), "--", "litellm"])
    assert writer_blocked == [True], "sidecar overwrote a newer committed registry artifact"
    # The transaction must be released after publication, including exec failure.
    write_registry(db, "model-b")


def test_launch_acknowledgement_matches_watcher_hash_and_detects_later_edit(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    registry = write_registry(db)
    artifact = tmp_path / "selected.yaml"
    launch_config = tmp_path / "launch.yaml"
    receipt = tmp_path / "runtime-hash"
    stop_exec(monkeypatch)
    with pytest.raises(SystemExit):
        provider_sidecar.main(["run", "--db", str(db), "--config", str(artifact),
                               "--launch-config", str(launch_config), "--runtime-hash-file", str(receipt),
                               "--", "litellm", "--config", str(launch_config)])
    source = (ROOT / "start.sh").read_text()
    start = source.index("    litellm_runtime_hash() {")
    end = source.index("\n    watch_litellm_config()", start)
    script = '''
LITELLM_CONFIG_PATH=$1
AZIZAM_ADMIN_DB_PATH=$2
test_python=$3
python3() { "$test_python" "$@"; }
''' + source[start:end] + '\nlitellm_runtime_hash\n'

    def watcher_hash():
        return subprocess.run(["bash", "-c", script, "hash-test", str(artifact), str(db), sys.executable],
                              cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()

    assert receipt.read_text().strip() == watcher_hash()
    old_secret_hash = provider_sidecar.provider_secret_fingerprint(db, registry=registry)
    newer = write_registry(db, "model-b")
    provider_sidecar.write_litellm_config_artifact(newer, base_dir=tmp_path, path=artifact)
    assert old_secret_hash == provider_sidecar.provider_secret_fingerprint(db)
    assert receipt.read_text().strip() != watcher_hash()


def test_child_reads_acknowledged_snapshot_after_shared_artifact_changes(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    original = write_registry(db)
    artifact = tmp_path / "selected.yaml"
    launch = tmp_path / "launch.yaml"
    receipt = tmp_path / "runtime-hash"

    def interleave_exec(file, args, env):
        # The DB lock has been released, but the child has yet to read YAML.
        newer = write_registry(db, "model-b")
        provider_sidecar.write_litellm_config_artifact(newer, base_dir=tmp_path, path=artifact)
        actual_config = Path(args[args.index("--config") + 1]).read_bytes()
        assert "model-b" in artifact.read_text()
        assert b"model-a" in actual_config and b"model-b" not in actual_config
        # Reverting before the watcher polls must not hide an intermediate
        # configuration that the running process consumed.
        write_registry(db)
        provider_sidecar.write_litellm_config_artifact(original, base_dir=tmp_path, path=artifact)
        assert actual_config == artifact.read_bytes()
        raise SystemExit(23)

    monkeypatch.setattr(os, "execvpe", interleave_exec)
    with pytest.raises(SystemExit):
        provider_sidecar.main(["run", "--db", str(db), "--config", str(artifact),
                               "--launch-config", str(launch), "--runtime-hash-file", str(receipt),
                               "--", "litellm", "--config", str(launch)])


def test_launcher_uses_yaml_admin_db_resolution(tmp_path, monkeypatch):
    config = tmp_path / "gateway.yml"
    db = tmp_path / "owner.db"
    config.write_text(yaml.safe_dump({"admin": {"db_path": str(db)}}))
    monkeypatch.setenv("AZIZAM_CONFIG_PATH", str(config))
    monkeypatch.delenv("AZIZAM_ADMIN_DB", raising=False)
    source = (ROOT / "start.sh").read_text()
    assignment = next(line for line in source.splitlines() if line.strip().startswith("AZIZAM_ADMIN_DB_PATH="))
    script = 'test_python=$1\npython3() { "$test_python" "$@"; }\n' + assignment + '\necho "$AZIZAM_ADMIN_DB_PATH"\n'
    result = subprocess.run(["bash", "-c", script, "db-test", sys.executable], cwd=ROOT,
                            capture_output=True, text=True, check=True)
    assert Path(result.stdout.strip()) == db


def test_watcher_records_launched_hash_instead_of_prelaunch_hash(tmp_path):
    source = (ROOT / "start.sh").read_text()
    start = source.index("    watch_litellm_config() {")
    end = source.index("\n    watch_litellm_config &", start)
    watcher = source[start:end]
    artifact = tmp_path / "selected.yaml"
    artifact.write_text("exists")
    receipt = tmp_path / "runtime-hash"
    # One controlled watcher iteration. The child acknowledges what it actually
    # launched after regenerating; the prelaunch hash belongs to an older file.
    script = '''
LITELLM_CONFIG_PATH=$1
LITELLM_RUNTIME_HASH_FILE=$2
LITELLM_PID=""
litellm_runtime_hash() { echo prelaunch; }
start_litellm() { echo launched > "$LITELLM_RUNTIME_HASH_FILE"; LITELLM_PID=123; }
kill() { return 0; }
sleep() { echo "$LAST_HASH"; exit 0; }
''' + watcher + '\nwatch_litellm_config\n'
    result = subprocess.run(["bash", "-c", script, "watcher-test", str(artifact), str(receipt)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "launched"


def test_start_litellm_passes_private_snapshot_to_child(tmp_path):
    source = (ROOT / "start.sh").read_text()
    start = source.index("    start_litellm() {")
    end = source.index("\n    litellm_runtime_hash()", start)
    script = '''
LITELLM_CONFIG_PATH=$1/shared.yaml
LITELLM_PID_FILE=$1/child.pid
LITELLM_RUNTIME_HASH_FILE=$1/runtime-hash
AZIZAM_ADMIN_DB_PATH=$1/admin.db
python3() {
    local launch="" receipt=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --launch-config) launch=$2; shift 2;;
            --runtime-hash-file) receipt=$2; shift 2;;
            --) shift; break;;
            *) shift;;
        esac
    done
    if [ "$1" != litellm ] || [ "$2" != --config ] || [ "$3" != "$launch" ] || [ "$launch" = "$LITELLM_CONFIG_PATH" ]; then
        return 1
    fi
    echo snapshot > "$launch"
    echo acknowledged > "$receipt"
}
''' + source[start:end] + '''
start_litellm || exit 1
cat "$LITELLM_RUNTIME_HASH_FILE"
cat "$LITELLM_LAUNCH_CONFIG_PATH"
wait "$LITELLM_PID"
'''
    result = subprocess.run(["bash", "-c", script, "launch-test", str(tmp_path)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.splitlines() == ["acknowledged", "snapshot"]
