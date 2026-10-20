"""The gateway's private temporary directory (docs/design/REQUEST_RETENTION.md 5.1 rows 4-5, build slice 4)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from gateway import admin_store, private_tmp, stt_chunking

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def clean_process(monkeypatch: pytest.MonkeyPatch):
    """Let a test call ``activate`` and leave this process's temporary-file settings as they were."""
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    monkeypatch.setattr(private_tmp, "_active", None)
    monkeypatch.setattr(private_tmp, "_lock_fd", None)
    monkeypatch.setattr(private_tmp, "_lock_pid", None)
    for name in ("TMPDIR", "TMP", "TEMP", private_tmp.ENV_DIR, private_tmp.ENV_PREPARED, private_tmp.ENV_LOCK_FD):
        previous = os.environ.get(name)
        monkeypatch.setenv(name, previous if previous is not None else "")  # records how to undo the change
        if previous is None:
            monkeypatch.delenv(name)
    monkeypatch.delenv(private_tmp.ENV_LOCK_FD, raising=False)
    yield
    if private_tmp._lock_fd is not None:
        os.close(private_tmp._lock_fd)


def test_default_directory_sits_beside_the_admin_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(private_tmp.ENV_DIR, raising=False)
    monkeypatch.setattr(admin_store, "DB_PATH", tmp_path / "data" / "admin.db")
    assert private_tmp.configured_dir() == tmp_path / "data" / "private-tmp"


def test_environment_names_the_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(private_tmp.ENV_DIR, str(tmp_path / "elsewhere"))
    assert private_tmp.configured_dir() == tmp_path / "elsewhere"


def test_prepare_makes_a_private_directory_on_disk(host_disk_dir: Path) -> None:
    target = host_disk_dir / "deep" / "private-tmp"
    assert private_tmp.prepare(target) == target
    info = target.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert info.st_uid == os.geteuid()
    assert private_tmp._filesystem_type(target) not in {"tmpfs", "ramfs"}


def test_prepare_tightens_a_loose_directory(host_disk_dir: Path) -> None:
    target = host_disk_dir / "private-tmp"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    private_tmp.prepare(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_prepare_empties_stale_files_and_follows_no_link(host_disk_dir: Path) -> None:
    target = host_disk_dir / "private-tmp"
    outside = host_disk_dir / "outside"
    outside.mkdir()
    (outside / "keep").write_text("kept")
    private_tmp.prepare(target)
    (target / "stale.part").write_text("old audio")
    (target / "request-1" / "nested").mkdir(parents=True)
    (target / "request-1" / "nested" / "upload.part").write_text("old audio")
    (target / "link").symlink_to(outside)
    private_tmp.prepare(target)
    assert [p.name for p in target.iterdir()] == [".owner.lock"]
    assert (outside / "keep").read_text() == "kept"


def test_prepare_can_keep_what_is_there(host_disk_dir: Path) -> None:
    target = host_disk_dir / "private-tmp"
    private_tmp.prepare(target)
    (target / "live").write_text("in use")
    private_tmp.prepare(target, clear=False)
    assert (target / "live").read_text() == "in use"


def test_prepare_refuses_a_link(host_disk_dir: Path) -> None:
    real = host_disk_dir / "real"
    real.mkdir()
    link = host_disk_dir / "private-tmp"
    link.symlink_to(real)
    with pytest.raises(private_tmp.PrivateTmpError, match="link or not a directory"):
        private_tmp.prepare(link)


def test_prepare_refuses_a_directory_of_another_user(host_disk_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = host_disk_dir / "private-tmp"
    target.mkdir()
    fstat = os.fstat
    target_info = target.stat()

    def foreign_owner(fd):
        info = fstat(fd)
        if info.st_ino == target_info.st_ino and info.st_dev == target_info.st_dev:
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(private_tmp.PrivateTmpError, match="belongs to another user"):
        private_tmp.prepare(target)


@pytest.mark.parametrize("kind", ["tmpfs", "ramfs"])
def test_prepare_refuses_ram(host_disk_dir: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    monkeypatch.setattr(private_tmp, "_filesystem_type", lambda path: kind)
    with pytest.raises(private_tmp.PrivateTmpError, match="RAM-backed"):
        private_tmp.prepare(host_disk_dir / "private-tmp")


def test_filesystem_type_reads_the_longest_matching_mount(tmp_path: Path) -> None:
    table = tmp_path / "mountinfo"
    table.write_text(
        "20 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "30 20 0:46 / /tmp rw,nosuid - tmpfs tmpfs rw\n"
        "31 20 0:47 / /srv/my\\040disk rw - xfs /dev/sdb1 rw\n"
        "32 31 0:48 / /srv/my\\040disk/ram rw - ramfs ramfs rw\n"
    )
    kinds = {
        "/home/x": "ext4", "/tmp": "tmpfs", "/tmp/a/b": "tmpfs", "/tmpx/a": "ext4",
        "/srv/my disk/data": "xfs", "/srv/my disk/ram/data": "ramfs",
    }
    assert {path: private_tmp._filesystem_type(Path(path), str(table)) for path in kinds} == kinds
    assert private_tmp._filesystem_type(Path("/x"), str(tmp_path / "missing")) is None


def test_activate_points_this_process_and_its_children_at_the_directory(host_disk_dir: Path, clean_process: None) -> None:
    target = host_disk_dir / "private-tmp"
    target.mkdir()
    (target / "stale").write_text("old")
    assert private_tmp.activate(target) == target
    assert tempfile.gettempdir() == str(target)
    for name in ("TMPDIR", "TMP", "TEMP", private_tmp.ENV_DIR):
        assert os.environ[name] == str(target)
    assert private_tmp.current() == target and private_tmp.inside(target / "x") and not private_tmp.inside(host_disk_dir)
    assert not (target / "stale").exists()
    with tempfile.NamedTemporaryFile() as handle:
        assert Path(handle.name).parent == target
    child = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"], env={**os.environ}, capture_output=True, text=True, check=True,
    )
    assert child.stdout.strip() == str(target)


def test_activate_does_not_sweep_after_the_launcher_did(host_disk_dir: Path, clean_process: None, monkeypatch: pytest.MonkeyPatch) -> None:
    target = host_disk_dir / "private-tmp"
    private_tmp.activate(target)
    (target / "litellm-live").write_text("written after start.sh swept")
    monkeypatch.setenv(private_tmp.ENV_PREPARED, str(target))
    private_tmp.activate(target)
    assert (target / "litellm-live").read_text() == "written after start.sh swept"


def test_the_launcher_command_prints_the_swept_directory(host_disk_dir: Path) -> None:
    target = host_disk_dir / "from-env"
    target.mkdir()
    (target / "stale").write_text("old")
    done = subprocess.run(
        [sys.executable, "-m", "gateway.private_tmp", "prepare"], cwd=ROOT,
        env={**os.environ, private_tmp.ENV_DIR: str(target)}, capture_output=True, text=True,
    )
    assert (done.returncode, done.stdout.strip()) == (0, str(target)), done.stderr
    assert [p.name for p in target.iterdir()] == [".owner.lock"]
    (host_disk_dir / "blocker").write_text("a file")
    broken = subprocess.run(
        [sys.executable, "-m", "gateway.private_tmp", "prepare"], cwd=ROOT,
        env={**os.environ, private_tmp.ENV_DIR: str(host_disk_dir / "blocker" / "private-tmp")}, capture_output=True, text=True,
    )
    assert broken.returncode == 1 and "cannot be created" in broken.stderr and broken.stdout == ""


def test_start_script_prepares_before_anything_runs() -> None:
    script = (ROOT / "start.sh").read_text()
    call = 'exec python3 -m gateway.private_tmp exec /bin/bash "$0" "$@"'
    prepare = script.index(call)
    assert 'AZIZAM_PRIVATE_TMPDIR_LOCK_FD:-' in script[:prepare]
    for later in ("start_litellm()", "python3 proxy.py", "gateway.provider_sidecar run"):
        assert script.index(later) > prepare


def test_the_unit_does_not_claim_a_private_tmp_it_cannot_get() -> None:
    lines = [line.strip() for line in (ROOT / "azizam.service").read_text().splitlines()]
    assert "PrivateTmp=yes" not in lines


def test_relative_stt_workspace_is_inside_the_private_directory(host_disk_dir: Path, clean_process: None) -> None:
    private = private_tmp.activate(host_disk_dir / "private-tmp")
    config = stt_chunking.SttExecutionConfig.from_service(service_id="s", service_config={}, config_directory=host_disk_dir)
    assert config.spool_dir == private / "stt"
    named = stt_chunking.SttExecutionConfig.from_service(
        service_id="s", service_config={"chunking": {"spool_dir": "data/stt-spool"}}, config_directory=host_disk_dir)
    assert named.spool_dir == private / "data" / "stt-spool"
    explicit = stt_chunking.SttExecutionConfig.from_service(
        service_id="s", service_config={"chunking": {"spool_dir": str(host_disk_dir / "mine")}}, config_directory=host_disk_dir)
    assert explicit.spool_dir == host_disk_dir / "mine"


@pytest.mark.parametrize("command", ["prepare", "gateway"])
def test_a_second_owner_cannot_sweep_live_files(host_disk_dir: Path, command: str) -> None:
    target = host_disk_dir / "private-tmp"
    env = {**os.environ, private_tmp.ENV_DIR: str(target), private_tmp.ENV_PREPARED: str(target)}
    owner_code = "from gateway import private_tmp; import sys; p=private_tmp.activate(); (p/'live').write_text('upload'); print('ready', flush=True); sys.stdin.read()"
    owner = subprocess.Popen([sys.executable, "-c", owner_code], cwd=ROOT, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == "ready"
        args = ["-m", "gateway.private_tmp", "prepare"] if command == "prepare" else ["-c", "from gateway import private_tmp; private_tmp.activate()"]
        other = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        assert (target / "live").read_text() == "upload"
        assert other.returncode != 0 and "in use" in other.stderr
    finally:
        owner.communicate(timeout=10)
    assert owner.returncode == 0


def test_a_directory_swapped_for_a_link_cannot_sweep_outside(host_disk_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target, moved, outside = [host_disk_dir / name for name in ("private-tmp", "moved", "outside")]
    target.mkdir()
    outside.mkdir()
    (outside / "keep").write_text("retained")
    filesystem = private_tmp._filesystem_type

    def swap(path):
        target.rename(moved)
        target.symlink_to(outside, target_is_directory=True)
        return filesystem(path)

    monkeypatch.setattr(private_tmp, "_filesystem_type", swap)
    try:
        private_tmp.prepare(target)
    except private_tmp.PrivateTmpError:
        pass
    assert (outside / "keep").read_text() == "retained"


@pytest.mark.parametrize("mode", [0o777, 0o770])
def test_prepare_refuses_an_untrusted_parent(host_disk_dir: Path, mode: int) -> None:
    parent = host_disk_dir / "shared"
    parent.mkdir()
    parent.chmod(mode)
    with pytest.raises(private_tmp.PrivateTmpError, match="parent"):
        private_tmp.prepare(parent / "private-tmp")


def test_relative_stt_spool_cannot_escape_the_private_directory(host_disk_dir: Path, clean_process: None) -> None:
    private_tmp.activate(host_disk_dir / "private-tmp")
    with pytest.raises(stt_chunking.SttConfigurationError, match="inside"):
        stt_chunking.SttExecutionConfig.from_service(
            service_id="s", service_config={"chunking": {"spool_dir": "../outside"}}, config_directory=host_disk_dir)


def test_exec_holds_the_lock_in_the_serving_process(host_disk_dir: Path) -> None:
    target = host_disk_dir / "private-tmp"
    code = "from gateway import private_tmp; import os,sys; private_tmp.activate(); p=private_tmp.current(); (p/'live').write_text('upload'); print(os.getpid(), flush=True); sys.stdin.read()"
    owner = subprocess.Popen([sys.executable, "-m", "gateway.private_tmp", "exec", sys.executable, "-c", code],
                             cwd=ROOT, env={**os.environ, private_tmp.ENV_DIR: str(target)},
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert int(owner.stdout.readline()) == owner.pid, "launcher must exec, preserving PID and lock"
        refused = subprocess.run([sys.executable, "-m", "gateway.private_tmp", "prepare"],
                                 cwd=ROOT, env={**os.environ, private_tmp.ENV_DIR: str(target)},
                                 capture_output=True, text=True, timeout=10)
        assert refused.returncode == 1 and "in use" in refused.stderr
        assert (target / "live").read_text() == "upload"
    finally:
        owner.communicate(timeout=10)
    assert owner.returncode == 0
    private_tmp.prepare(target)
    assert not (target / "live").exists(), "dead owners release the lock and permit startup sweep"


@pytest.mark.parametrize("mode,allowed", [(0o777, False), (0o770, False), (0o1777, True)])
def test_prepare_checks_writable_ancestors_not_only_the_parent(host_disk_dir: Path, mode: int, allowed: bool) -> None:
    shared = host_disk_dir / "shared"
    shared.mkdir()
    shared.chmod(mode)
    parent = shared / "owned"
    parent.mkdir(mode=0o700)
    target = parent / "private-tmp"
    if allowed:
        private_tmp.prepare(target)
    else:
        with pytest.raises(private_tmp.PrivateTmpError, match="parent"):
            private_tmp.prepare(target)
