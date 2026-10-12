"""Focused proofs for the Wave 0 hermetic pytest boundary."""

from __future__ import annotations

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
