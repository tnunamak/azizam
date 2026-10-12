"""Focused proofs for the Wave 0 hermetic pytest boundary."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys

import pytest

from _hermetic import validate_test_environment


def test_state_config_and_environment_are_private_to_this_run(gateway_test_state_dir: Path) -> None:
    assert gateway_test_state_dir.is_relative_to(Path(os.environ["HERMETIC_STATE_DIR"]))
    assert "/.tmp/" in f"{gateway_test_state_dir}/"
    config_path = Path(os.environ["AZIZAM_CONFIG_PATH"])
    assert config_path.parent.is_relative_to(Path(os.environ["HERMETIC_STATE_DIR"]))
    assert config_path.is_file()
    assert os.environ.get("OPENAI_API_KEY") is None
    assert Path(os.environ["HERMETIC_ADMIN_DB"]) == gateway_test_state_dir / "admin.db"
    assert os.environ["AZIZAM_ADMIN_DB"] == os.environ["HERMETIC_ADMIN_DB"]
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"


def test_production_environment_names_cannot_leak_into_the_suite() -> None:
    allowed_gateway_names = {
        "AZIZAM_ADMIN_DB",
        "AZIZAM_CONFIG_PATH",
        "AZIZAM_KEY_ACTIVITY_LOG",
        "AZIZAM_MEDIA_JOBS_DB",
        "AZIZAM_METRICS_DB",
        "AZIZAM_RECEIPTS_DB",
        "AZIZAM_RESPONSES_DB",
    }
    leaked = {
        name
        for name in os.environ
        if name.startswith(("OPENAI_", "ANTHROPIC_", "AWS_", "DATABASE_"))
        or (name.startswith("AZIZAM_") and name not in allowed_gateway_names)
    }
    assert leaked == set()


@pytest.mark.parametrize(
    "family, host, port",
    [
        (socket.AF_INET, "127.0.0.1", 5000),
        (socket.AF_INET, "127.0.0.1", 5051),
        (socket.AF_INET, "127.0.0.1", 15000),
        (socket.AF_INET, "192.0.2.1", 443),
        (socket.AF_INET, "8.8.8.8", 53),
        (socket.AF_INET6, "::1", 5051),
        (socket.AF_INET6, "2001:db8::1", 443),
    ],
)
def test_live_and_egress_destinations_are_rejected_before_connect(
    family: socket.AddressFamily, host: str, port: int
) -> None:
    client = socket.socket(family, socket.SOCK_STREAM)
    try:
        with pytest.raises(pytest.fail.Exception, match=rf"{host}:{port}"):
            client.connect((host, port))
    finally:
        client.close()


def test_production_checkout_path_is_not_visible() -> None:
    production_root = Path(os.environ["HERMETIC_FORBIDDEN_PRODUCTION_PATH"])
    assert not production_root.exists()
    with pytest.raises(FileNotFoundError):
        (production_root / "data" / "admin.db").read_bytes()
    assert not (production_root / "data" / "admin.db-wal").exists()
    assert not (production_root / "cache").exists()


def test_host_pid_and_ipc_namespaces_are_not_visible() -> None:
    assert not Path(f"/proc/{os.environ['HERMETIC_HOST_RUNNER_PID']}").exists()
    assert os.readlink("/proc/1/ns/pid") != os.environ["HERMETIC_HOST_PID_NAMESPACE"]
    assert os.readlink("/proc/1/ns/ipc") != os.environ["HERMETIC_HOST_IPC_NAMESPACE"]


def test_interpreter_and_imports_are_inside_fresh_environment() -> None:
    validate_test_environment()
    assert Path(sys.executable).exists()


@pytest.mark.parametrize("command", [["systemctl", "--user", "restart", "azizam.service"], ["systemctl", "--user", "restart", "unrelated.service"], ["tmux", "kill-server"]])
def test_service_and_terminal_mutation_are_rejected_before_spawn(command: list[str]) -> None:
    with pytest.raises(pytest.fail.Exception, match="host-control command"):
        subprocess.run(command, check=False)


def test_process_signalling_is_rejected_before_effect() -> None:
    with pytest.raises(pytest.fail.Exception, match="process signalling"):
        os.kill(os.getpid(), signal.SIGTERM)


def test_ephemeral_fake_backend_is_allowed(test_owned_tcp_listener) -> None:
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(test_owned_tcp_listener.getsockname())
        connection, _ = test_owned_tcp_listener.accept()
        connection.close()
    finally:
        client.close()


def _wildcard_listener(family: socket.AddressFamily, host: str) -> socket.socket:
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.bind((host, 0))
    listener.listen()
    return listener


@pytest.mark.parametrize(
    "family, wildcard, reachable, remote",
    [
        (socket.AF_INET, "0.0.0.0", "127.0.0.1", "192.0.2.1"),
        (socket.AF_INET6, "::", "::1", "2001:db8::1"),
    ],
)
def test_a_wildcard_bound_port_is_reachable_on_loopback_only(
    family: socket.AddressFamily, wildcard: str, reachable: str, remote: str
) -> None:
    listener = _wildcard_listener(family, wildcard)
    port = listener.getsockname()[1]
    try:
        client = socket.socket(family, socket.SOCK_STREAM)
        try:
            client.connect((reachable, port))
        finally:
            client.close()
        remote_client = socket.socket(family, socket.SOCK_STREAM)
        try:
            with pytest.raises(pytest.fail.Exception, match=rf"{remote}:{port}"):
                remote_client.connect((remote, port))
            with pytest.raises(pytest.fail.Exception, match=rf"{remote}:{port}"):
                remote_client.connect_ex((remote, port))
        finally:
            remote_client.close()
    finally:
        listener.close()


def test_a_wildcard_bound_port_does_not_open_a_mapped_remote_address() -> None:
    listener = _wildcard_listener(socket.AF_INET6, "::")
    port = listener.getsockname()[1]
    client = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        with pytest.raises(pytest.fail.Exception, match="blocked connection"):
            client.connect(("::ffff:192.0.2.1", port))
    finally:
        client.close()
        listener.close()


def test_a_udp_send_cannot_leave_the_namespace() -> None:
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(OSError) as raised:
            sender.sendto(b"probe", ("192.0.2.1", 53))
    finally:
        sender.close()
    assert raised.value.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES}


def test_a_native_child_cannot_connect_outside_the_namespace(test_owned_tcp_listener) -> None:
    # bash opens the socket itself, so no Python hook runs: only the namespace stops it.
    def connect(host: str, port: int) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["/bin/bash", "-c", f"exec 3<>/dev/tcp/{host}/{port}"],
            capture_output=True,
            timeout=20,
        )

    assert connect("127.0.0.1", test_owned_tcp_listener.getsockname()[1]).returncode == 0
    outside = connect("192.0.2.1", 443)
    assert outside.returncode != 0
    assert b"192.0.2.1" in outside.stderr or b"unreachable" in outside.stderr.lower()


def test_the_runner_installed_the_guard_for_this_run() -> None:
    import conftest

    assert socket.socket.__name__ == "HermeticSocket"
    assert conftest._GUARD._installed
    assert conftest._GUARD._audit_installed


def test_the_guard_hooks_are_installed_and_removed_by_pytest_configure_and_unconfigure(monkeypatch) -> None:
    import conftest

    calls: list[str] = []

    class Recorder:
        def install(self) -> None:
            calls.append("install")

        def restore(self) -> None:
            calls.append("restore")

    monkeypatch.setattr(conftest, "_GUARD", Recorder())
    conftest.pytest_configure(None)
    conftest.pytest_unconfigure(None)
    assert calls == ["install", "restore"]


def test_restore_removes_the_socket_guard_and_forgets_its_listeners() -> None:
    from _hermetic import _REAL_SOCKET, HermeticGuard

    before = socket.socket
    guard = HermeticGuard()
    try:
        guard.install()
        assert socket.socket is not _REAL_SOCKET
        guard._allowed_tcp.add((socket.AF_INET, "127.0.0.1", 1))
        guard.restore()
        assert socket.socket is _REAL_SOCKET
        assert not guard._installed and not guard._allowed_tcp
    finally:
        socket.socket = before  # the run's own guard stays in place for later tests
