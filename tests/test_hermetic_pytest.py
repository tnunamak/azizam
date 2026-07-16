"""Focused proofs for the Wave 0 hermetic pytest boundary."""

from __future__ import annotations

import os
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys

import pytest

from _hermetic_environment import validate_test_environment


def test_state_config_and_environment_are_private_to_this_run(gateway_test_state_dir: Path) -> None:
    assert gateway_test_state_dir.is_relative_to(Path(os.environ["VIVID_FISH_HERMETIC_STATE_DIR"]))
    assert "/.tmp/" in f"{gateway_test_state_dir}/"
    assert Path(os.environ["AI_GATEWAY_CONFIG_PATH"]).parent == gateway_test_state_dir
    assert os.environ.get("OPENAI_API_KEY") is None
    assert os.environ.get("OPENAI_PROXY_ADMIN_DB") is None
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"


def test_production_environment_names_cannot_leak_into_the_suite() -> None:
    allowed_gateway_names = {
        "AI_GATEWAY_ADMIN_DB",
        "AI_GATEWAY_CONFIG_PATH",
        "AI_GATEWAY_MEDIA_JOBS_DB",
        "AI_GATEWAY_METRICS_DB",
        "AI_GATEWAY_RECEIPTS_DB",
        "AI_GATEWAY_RESPONSES_DB",
        "AI_GATEWAY_TEST_STATE_DIR",
    }
    leaked = {
        name
        for name in os.environ
        if name.startswith(("OPENAI_", "ANTHROPIC_", "AWS_", "DATABASE_"))
        or (name.startswith("AI_GATEWAY_") and name not in allowed_gateway_names)
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
    production_root = Path(os.environ["VIVID_FISH_HERMETIC_FORBIDDEN_PRODUCTION_PATH"])
    assert not production_root.exists()
    with pytest.raises(FileNotFoundError):
        (production_root / "data" / "admin.db").read_bytes()
    assert not (production_root / "data" / "admin.db-wal").exists()
    assert not (production_root / "cache").exists()


def test_host_pid_and_ipc_namespaces_are_not_visible() -> None:
    assert not Path(f"/proc/{os.environ['VIVID_FISH_HERMETIC_HOST_RUNNER_PID']}").exists()
    assert os.readlink("/proc/1/ns/pid") != os.environ["VIVID_FISH_HERMETIC_HOST_PID_NAMESPACE"]
    assert os.readlink("/proc/1/ns/ipc") != os.environ["VIVID_FISH_HERMETIC_HOST_IPC_NAMESPACE"]


def test_interpreter_and_imports_have_a_hash_locked_receipt() -> None:
    receipt = validate_test_environment()
    assert Path(sys.executable).is_relative_to(os.environ["VIVID_FISH_HERMETIC_TEST_ENV_ROOT"])
    assert receipt["lock_sha256"] == os.environ["VIVID_FISH_HERMETIC_LOCK_SHA256"]
    checkout = json.loads(
        Path(os.environ["VIVID_FISH_HERMETIC_CHECKOUT_RECEIPT"]).read_text(encoding="utf-8")
    )
    assert checkout["checkout_head"] == os.environ["VIVID_FISH_HERMETIC_CHECKOUT_HEAD"]
    assert checkout["checkout_diff_sha256"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.mark.parametrize("command", [["systemctl", "--user", "restart", "openai-proxy.service"], ["tmux", "kill-server"]])
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
