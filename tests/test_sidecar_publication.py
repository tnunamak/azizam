"""Regression coverage for sidecar startup and its watcher acknowledgement."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
import yaml

from gateway import provider_sidecar


ROOT = Path(__file__).resolve().parents[1]


def write_registry(path, model="model-a"):
    registry = {"services": {"local": {
        "id": "local", "kind": "llm", "capabilities": ["openai"],
        "config": {"base_url": "http://127.0.0.1:9999/v1", "models": [{"id": model}]},
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
    "invalid-shape", "unreadable",
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
                    payload = {"invalid-json": "{"}.get(failure, "[]")
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


@pytest.mark.parametrize("stored", ["{}", '{"services": null}'], ids=["empty-document", "no-services"])
def test_stored_document_without_owner_services_publishes_the_release_records(tmp_path, monkeypatch, stored):
    """A stored row with no owner services means the owner changed nothing: the gateway reads the release records,
    so the sidecar must publish them too, not refuse and keep serving a stale artifact."""
    from gateway.litellm_substrate import compile_litellm_config_from_registry
    from gateway.service_registry import release_registry

    db = tmp_path / "admin.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE operator_settings (key TEXT PRIMARY KEY, value_json TEXT)")
        conn.execute("INSERT INTO operator_settings VALUES ('service_registry', ?)", (stored,))
    artifact = tmp_path / "selected.yaml"
    artifact.write_bytes(b"model_list:\n- model_name: retained\n")
    stop_exec(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        provider_sidecar.main(["run", "--db", str(db), "--config", str(artifact), "--", "litellm"])
    assert exc.value.code == 23
    assert "retained" not in artifact.read_text()
    published = yaml.safe_load(artifact.read_text())
    assert published["model_list"] == compile_litellm_config_from_registry(release_registry())["model_list"]


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
                    "config": {"base_url": "http://127.0.0.1:9999/v1", "models": [{"id": "model-b"}]}}}}
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
        result = subprocess.run(["bash", "-c", script, "hash-test", str(artifact), str(db), sys.executable],
                                cwd=ROOT, capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"watcher hash failed ({result.returncode}): {result.stderr}"
        return result.stdout.strip()

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
    end = source.index("\n    watch_litellm_config {AZIZAM_PRIVATE_TMPDIR_LOCK_FD}>&- &", start)
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



def _run_hash_gated_watcher(tmp_path, polls, *, on_poll="", refresh_polls=60, age_s=600):
    """Run the real watcher loop for `polls` polls; return (hash calls, launches)."""
    source = (ROOT / "start.sh").read_text()
    start = source.index("    litellm_runtime_inputs() {")
    end = source.index("\n    watch_litellm_config {AZIZAM_PRIVATE_TMPDIR_LOCK_FD}>&- &", start)
    config = tmp_path / "selected.yaml"
    config.write_text("exists")
    db = tmp_path / "admin.db"
    db.write_text("db")
    old = time.time() - age_s
    for path in (config, db):
        os.utime(path, (old, old))
    (tmp_path / "hash-value").write_text("h1")
    script = '''
WORK=$1
LITELLM_CONFIG_PATH=$WORK/selected.yaml
AZIZAM_ADMIN_DB_PATH=$WORK/admin.db
LITELLM_RUNTIME_HASH_FILE=$WORK/runtime-hash
MAX_POLLS=$2
AZIZAM_LITELLM_HASH_REFRESH_POLLS=$3
LITELLM_PID=""
POLL=0
litellm_runtime_hash() { echo x >> "$WORK/hash-calls"; cat "$WORK/hash-value"; }
start_litellm() { echo x >> "$WORK/launches"; cat "$WORK/hash-value" > "$LITELLM_RUNTIME_HASH_FILE"; LITELLM_PID=123; }
kill() { return 0; }
sleep() {
    POLL=$((POLL + 1))
    if [ "$POLL" -ge "$MAX_POLLS" ]; then exit 0; fi
    on_poll
}
on_poll() { :; ON_POLL_HOOK; }
'''.replace("ON_POLL_HOOK", on_poll or ":") + source[start:end] + "\nwatch_litellm_config\n"
    subprocess.run(["bash", "-c", script, "gate-test", str(tmp_path), str(polls), str(refresh_polls)],
                   capture_output=True, text=True, check=True, timeout=60)

    def count(name):
        path = tmp_path / name
        return len(path.read_text().splitlines()) if path.exists() else 0

    return count("hash-calls"), count("launches")


def test_watcher_skips_the_hash_while_its_inputs_are_unchanged(tmp_path):
    # Each hash starts a Python; unchanged settled inputs must not start one per poll.
    assert _run_hash_gated_watcher(tmp_path, 10) == (1, 1)


def test_watcher_hashes_again_and_restarts_when_an_input_changes(tmp_path):
    # Poll 3 rewrites the DB with an old mtime; the size and mtime still differ.
    hook = ('if [ "$POLL" -eq 3 ]; then echo h2 > "$WORK/hash-value"; echo changed-db > "$WORK/admin.db"; '
            'touch -d @1000000000 "$WORK/admin.db"; fi')
    assert _run_hash_gated_watcher(tmp_path, 10, on_poll=hook) == (2, 2)


def test_watcher_sees_a_journal_appear_as_an_input_change(tmp_path):
    hook = ('if [ "$POLL" -eq 3 ]; then echo h2 > "$WORK/hash-value"; echo page > "$WORK/admin.db-wal"; '
            'touch -d @1000000000 "$WORK/admin.db-wal"; fi')
    assert _run_hash_gated_watcher(tmp_path, 10, on_poll=hook) == (2, 2)


def test_watcher_does_not_trust_an_input_changed_in_the_last_seconds(tmp_path):
    # A write in the same clock tick as a stat can keep its mtime: hash every poll until it
    # settles. A future mtime keeps the input unsettled however slow the host is.
    assert _run_hash_gated_watcher(tmp_path, 6, age_s=-3600) == (6, 1)


@pytest.mark.parametrize("setting", ["oops", "0", "-3", "007", "99999999999999999999"])
def test_a_bad_refresh_setting_falls_back_to_the_default(tmp_path, setting):
    # A bad value must not turn the refresh off: 61 polls hash at polls 1 and 61.
    assert _run_hash_gated_watcher(tmp_path, 62, refresh_polls=setting) == (2, 1)


def test_watcher_hashes_again_after_the_refresh_polls(tmp_path):
    # Polls 1, 5 and 9 hash.
    assert _run_hash_gated_watcher(tmp_path, 10, refresh_polls=4) == (3, 1)


def test_start_litellm_passes_private_snapshot_to_child(tmp_path):
    source = (ROOT / "start.sh").read_text()
    start = source.index("    start_litellm() {")
    end = source.index("\n    litellm_runtime_hash()", start)
    script = '''
LITELLM_CONFIG_PATH=$1/shared.yaml
LITELLM_PID_FILE=$1/child.pid
LITELLM_RUNTIME_HASH_FILE=$1/runtime-hash
AZIZAM_ADMIN_DB_PATH=$1/admin.db
mkfifo "$LITELLM_CONFIG_PATH.release"
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
    # A real sidecar remains alive after acknowledging startup.
    read -r release < "$LITELLM_CONFIG_PATH.release"
}
''' + source[start:end] + '''
start_litellm || exit 1
cat "$LITELLM_RUNTIME_HASH_FILE"
cat "$LITELLM_LAUNCH_CONFIG_PATH"
echo release > "$LITELLM_CONFIG_PATH.release"
wait "$LITELLM_PID"
'''
    result = subprocess.run(["bash", "-c", script, "launch-test", str(tmp_path)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.splitlines() == ["acknowledged", "snapshot"]



@pytest.mark.parametrize("failure", ["failed", "empty", "missing-config"])
def test_runtime_hash_rejects_an_unavailable_part(tmp_path, failure):
    # Catch the old pipeline hiding a failed/empty fingerprint or config digest.
    source = (ROOT / "start.sh").read_text()
    start = source.index("    litellm_runtime_hash() {")
    end = source.index("\n    watch_litellm_config()", start)
    config = tmp_path / "selected.yaml"
    if failure != "missing-config":
        config.write_text("exists")
    script = '''
LITELLM_CONFIG_PATH=$1
AZIZAM_ADMIN_DB_PATH=unused
failure=$2
python3() {
    [ "$failure" = empty ] && return 0
    printf '%064d\\n' 0
    [ "$failure" != failed ]
}
''' + source[start:end] + '\nlitellm_runtime_hash\n'
    result = subprocess.run(["bash", "-c", script, "hash-failure", str(config), failure],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode != 0
    assert result.stdout == ""


def test_runtime_hash_preserves_two_digest_receipt_bytes(tmp_path):
    # Catch a success-path format change while making the two input reads fail-safe.
    import hashlib
    config = tmp_path / "selected.yaml"
    config.write_bytes(b"fixture config\n")
    fingerprint = "a" * 64
    expected = hashlib.sha256((hashlib.sha256(config.read_bytes()).hexdigest() + "\n" + fingerprint + "\n").encode()).hexdigest()
    source = (ROOT / "start.sh").read_text()
    start = source.index("    litellm_runtime_hash() {")
    end = source.index("\n    watch_litellm_config()", start)
    script = 'LITELLM_CONFIG_PATH=$1\nAZIZAM_ADMIN_DB_PATH=unused\nFIXTURE_FINGERPRINT=$2\npython3() { echo "$FIXTURE_FINGERPRINT"; }\n' + source[start:end] + '\nlitellm_runtime_hash\n'
    result = subprocess.run(["bash", "-c", script, "receipt-bytes", str(config), fingerprint],
                            capture_output=True, text=True, check=True, timeout=2)
    assert result.stdout == expected + "\n"


@pytest.mark.parametrize("failure", ["failed", "empty"])
def test_watcher_keeps_child_and_hash_then_recovers_without_launch(tmp_path, failure):
    # Catch treating an unreadable fingerprint as a new hash, or forgetting to retry it.
    source = (ROOT / "start.sh").read_text()
    start = source.index("    litellm_runtime_hash() {")
    end = source.index("\n    watch_litellm_config {AZIZAM_PRIVATE_TMPDIR_LOCK_FD}>&- &", start)
    config = tmp_path / "selected.yaml"
    config.write_text("exists")
    script = '''
WORK=$1
failure=$2
LITELLM_CONFIG_PATH=$WORK/selected.yaml
AZIZAM_ADMIN_DB_PATH=$WORK/admin.db
LITELLM_RUNTIME_HASH_FILE=$WORK/runtime-hash
LITELLM_PID=""
POLL=0
AZIZAM_LITELLM_HASH_REFRESH_POLLS=2
python3() {
    echo call >> "$WORK/fingerprints"
    if [ "$POLL" -eq 2 ]; then
        [ "$failure" = empty ] && return 0
        printf '%064d\\n' 0
        return 1
    fi
    printf '%064d\\n' 0
}
start_litellm() {
    echo launch >> "$WORK/launches"
    echo "$NEXT_HASH" > "$LITELLM_RUNTIME_HASH_FILE"
    LITELLM_PID=123
}
kill() { [ "$1" = -0 ] || echo kill >> "$WORK/kills"; }
wait() { :; }
sleep() {
    echo "$LAST_HASH" >> "$WORK/observed-hashes"
    POLL=$((POLL + 1))
    if [ "$POLL" -eq 4 ]; then trap - EXIT INT TERM; exit 0; fi
}
''' + source[start:end] + '''
litellm_runtime_inputs() { echo stable; }
litellm_runtime_inputs_settled() { return 0; }
watch_litellm_config
'''
    result = subprocess.run(["bash", "-c", script, "hash-recovery", str(tmp_path), failure],
                            capture_output=True, text=True, check=True, timeout=2)
    assert (tmp_path / "launches").read_text().splitlines() == ["launch"]
    assert not (tmp_path / "kills").exists()
    hashes = (tmp_path / "observed-hashes").read_text().splitlines()
    assert len(hashes) == 4 and len(set(hashes)) == 1 and len(hashes[0]) == 64
    assert len((tmp_path / "fingerprints").read_text().splitlines()) == 3
    assert result.stderr.splitlines() == [
        "[start] LiteLLM runtime hash unavailable; keeping the running child and retrying next poll.",
        "[start] LiteLLM runtime hash is available again.",
    ]


def test_a_long_hash_outage_logs_once_and_still_restarts_a_dead_child(tmp_path):
    # Catch skipping the liveness check while the hash is unavailable, or logging every poll.
    source = (ROOT / "start.sh").read_text()
    start = source.index("    litellm_runtime_hash() {")
    end = source.index("\n    watch_litellm_config {AZIZAM_PRIVATE_TMPDIR_LOCK_FD}>&- &", start)
    (tmp_path / "selected.yaml").write_text("exists")
    script = '''
WORK=$1
LITELLM_CONFIG_PATH=$WORK/selected.yaml
AZIZAM_ADMIN_DB_PATH=$WORK/admin.db
LITELLM_RUNTIME_HASH_FILE=$WORK/runtime-hash
LITELLM_PID=""
POLL=0
AZIZAM_LITELLM_HASH_REFRESH_POLLS=60
python3() {
    if [ "$POLL" -ge 1 ]; then echo "fingerprint failed" >&2; return 1; fi
    printf '%064d\\n' 0
}
start_litellm() {
    echo launch >> "$WORK/launches"
    echo "$NEXT_HASH" > "$LITELLM_RUNTIME_HASH_FILE"
    LITELLM_PID=123
}
kill() {
    if [ "$1" = -0 ]; then [ "$POLL" -ne 3 ]; return; fi
    echo kill >> "$WORK/kills"
}
wait() { :; }
sleep() {
    POLL=$((POLL + 1))
    if [ "$POLL" -eq 6 ]; then trap - EXIT INT TERM; exit 0; fi
}
''' + source[start:end] + '''
litellm_runtime_inputs() { echo "changed-$POLL"; }
litellm_runtime_inputs_settled() { return 0; }
watch_litellm_config
'''
    result = subprocess.run(["bash", "-c", script, "hash-outage", str(tmp_path)],
                            capture_output=True, text=True, check=True, timeout=10)
    # The first launch, then one relaunch when the child is found dead at poll 3.
    assert (tmp_path / "launches").read_text().splitlines() == ["launch", "launch"]
    assert result.stderr.splitlines() == [
        "fingerprint failed",
        "[start] LiteLLM runtime hash unavailable; keeping the running child and retrying next poll.",
    ]


@pytest.mark.parametrize("first_start_fails", [False, True])
def test_no_tracked_child_launches_even_while_the_hash_is_unavailable(tmp_path, first_start_fails):
    # Catch `kill -0 0` (the watcher's own process group) standing in for a child that was never started.
    source = (ROOT / "start.sh").read_text()
    start = source.index("    litellm_runtime_hash() {")
    end = source.index("\n    watch_litellm_config {AZIZAM_PRIVATE_TMPDIR_LOCK_FD}>&- &", start)
    (tmp_path / "selected.yaml").write_text("exists")
    script = """
WORK=$1
FIRST_START_FAILS=$2
LITELLM_CONFIG_PATH=$WORK/selected.yaml
AZIZAM_ADMIN_DB_PATH=$WORK/admin.db
LITELLM_RUNTIME_HASH_FILE=$WORK/runtime-hash
LITELLM_PID=""
POLL=0
python3() { return 1; }
start_litellm() {
    echo launch >> "$WORK/launches"
    if [ "$FIRST_START_FAILS" = True ] && [ "$POLL" -eq 0 ]; then return 1; fi
    echo receipt > "$LITELLM_RUNTIME_HASH_FILE"
    LITELLM_PID=$$
}
sleep() {
    POLL=$((POLL + 1))
    if [ "$POLL" -eq 4 ]; then trap - EXIT INT TERM; exit 0; fi
}
""" + source[start:end] + """
litellm_runtime_inputs() { echo stable; }
litellm_runtime_inputs_settled() { return 0; }
watch_litellm_config
"""
    result = subprocess.run(["bash", "-c", script, "no-child", str(tmp_path), str(first_start_fails)],
                            capture_output=True, text=True, check=True, timeout=10)
    # The real `kill -0` sees the live tracked PID afterwards, so there is no relaunch.
    expected = ["launch", "launch"] if first_start_fails else ["launch"]
    assert (tmp_path / "launches").read_text().splitlines() == expected
    assert result.stderr.splitlines() == [
        "[start] LiteLLM runtime hash unavailable; keeping the running child and retrying next poll."]


def test_the_watcher_survives_errexit_without_the_optional_journals(tmp_path):
    # Catch a partial stat (no -wal or -journal file) ending a watcher that inherited `set -e`.
    source = (ROOT / "start.sh").read_text()
    start = source.index("    litellm_runtime_inputs() {")
    end = source.index("\n    watch_litellm_config {AZIZAM_PRIVATE_TMPDIR_LOCK_FD}>&- &", start)
    (tmp_path / "selected.yaml").write_text("exists")
    (tmp_path / "admin.db").write_text("db")
    script = """
set -e
WORK=$1
LITELLM_CONFIG_PATH=$WORK/selected.yaml
AZIZAM_ADMIN_DB_PATH=$WORK/admin.db
LITELLM_RUNTIME_HASH_FILE=$WORK/runtime-hash
LITELLM_PID=""
POLL=0
start_litellm() { echo launch >> "$WORK/launches"; echo h1 > "$LITELLM_RUNTIME_HASH_FILE"; LITELLM_PID=$$; }
sleep() {
    echo poll >> "$WORK/polls"
    POLL=$((POLL + 1))
    if [ "$POLL" -eq 3 ]; then trap - EXIT INT TERM; exit 0; fi
}
""" + source[start:end] + """
litellm_runtime_hash() { echo h1; }
watch_litellm_config
"""
    subprocess.run(["bash", "-c", script, "errexit", str(tmp_path)],
                   capture_output=True, text=True, check=True, timeout=10)
    assert not (tmp_path / "admin.db-wal").exists() and not (tmp_path / "admin.db-journal").exists()
    assert (tmp_path / "launches").read_text().splitlines() == ["launch"]
    assert len((tmp_path / "polls").read_text().splitlines()) == 3


def test_a_new_watcher_removes_launch_files_left_by_earlier_runs(tmp_path):
    # Catch one leaked LiteLLM launch file per gateway restart when the old watcher was killed before its trap ran.
    source = (ROOT / "start.sh").read_text()
    start = source.index("    litellm_runtime_inputs() {")
    end = source.index("\n    watch_litellm_config {AZIZAM_PRIVATE_TMPDIR_LOCK_FD}>&- &", start)
    for name in ("selected.yaml", "selected.yaml.launch.AAAAAAAA", "selected.yaml.launch.BBBBBBBB",
                 "selected.yaml.launch", "selected.yaml.launch.short", "selected.yaml.launched.CCCCCCCC"):
        (tmp_path / name).write_text("x")
    script = """
set -e
WORK=$1
LITELLM_CONFIG_PATH=$WORK/selected.yaml
AZIZAM_ADMIN_DB_PATH=$WORK/admin.db
LITELLM_RUNTIME_HASH_FILE=$WORK/runtime-hash
LITELLM_PID=""
POLL=0
start_litellm() {
    LITELLM_LAUNCH_CONFIG_PATH="$(mktemp "${LITELLM_CONFIG_PATH}.launch.XXXXXXXX")"
    echo h1 > "$LITELLM_RUNTIME_HASH_FILE"
    LITELLM_PID=$$
}
sleep() {
    POLL=$((POLL + 1))
    if [ "$POLL" -eq 2 ]; then ls "$WORK" > "$WORK/while-running"; trap - EXIT INT TERM; exit 0; fi
}
""" + source[start:end] + """
litellm_runtime_hash() { echo h1; }
watch_litellm_config
"""
    subprocess.run(["bash", "-c", script, "sweep", str(tmp_path)],
                   capture_output=True, text=True, check=True, timeout=10)
    listing = (tmp_path / "while-running").read_text().split()
    launch_files = [n for n in listing if n.startswith("selected.yaml.launch.") and len(n) == len("selected.yaml.launch.") + 8]
    assert "selected.yaml.launch.AAAAAAAA" not in listing and "selected.yaml.launch.BBBBBBBB" not in listing
    assert len(launch_files) == 1 and launch_files[0] not in ("selected.yaml.launched.CCCCCCCC",)
    for kept in ("selected.yaml", "selected.yaml.launch", "selected.yaml.launch.short", "selected.yaml.launched.CCCCCCCC"):
        assert kept in listing


@pytest.mark.parametrize("setting, bound", [("2", 2), ("", 120), ("oops", 120), ("0", 120),
                                            ("-1", 120), ("007", 120), ("1.5", 120), ("999999999", 120)])
def test_start_litellm_bounds_missing_receipt_and_validates_timeout(tmp_path, setting, bound):
    # Catch an infinite live-child receipt wait, including malformed timeout settings.
    source = (ROOT / "start.sh").read_text()
    start = source.index("    start_litellm() {")
    end = source.index("\n    litellm_runtime_hash()", start)
    script = '''
WORK=$1
LITELLM_CONFIG_PATH=$WORK/shared.yaml
LITELLM_PID_FILE=$WORK/child.pid
LITELLM_RUNTIME_HASH_FILE=$WORK/runtime-hash
AZIZAM_ADMIN_DB_PATH=unused
AZIZAM_LITELLM_RECEIPT_TIMEOUT_S=$2
# Unset first: the special SECONDS also counts real time, so a slow host would add a second.
unset SECONDS
SECONDS=0
SLEEPS=0
python3() { :; }
kill() { [ "$1" = -0 ] || echo "killed:$1" >> "$WORK/events"; }
wait() { echo reaped >> "$WORK/events"; }
sleep() {
    SLEEPS=$((SLEEPS + 1))
    SECONDS=$((SECONDS + 1))
    if [ "$SLEEPS" -gt 121 ]; then echo unbounded; exit 99; fi
}
''' + source[start:end] + '''
if start_litellm; then echo unexpected-success; exit 1; fi
echo "sleeps:$SLEEPS"
'''
    result = subprocess.run(["bash", "-c", script, "receipt-timeout", str(tmp_path), setting],
                            capture_output=True, text=True, check=True, timeout=1)
    assert result.stdout.splitlines() == [f"sleeps:{bound}"]
    assert (tmp_path / "events").read_text().splitlines() == ["killed:-KILL", "reaped"]
    lines = result.stderr.splitlines()
    assert lines[-1] == "[start] LiteLLM sidecar timed out before acknowledging its registry snapshot."
    assert len(lines) == (1 if setting in ("2", "") else 2)
