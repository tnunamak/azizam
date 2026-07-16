"""Fail-closed process and network boundaries for hermetic pytest.

The Bubblewrap namespace is the primary protection for native children.  This
module supplies a precise Python-level error before test code can connect to a
configured endpoint or launch a host-control command.  Ephemeral listeners
created in this process remain usable by fake-backend and raw-ASGI tests.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

class HermeticEnvironmentError(RuntimeError):
    pass


def validate_test_environment() -> None:
    """The runner owns fresh provisioning; assert only process-local roots."""
    import sys
    from pathlib import Path
    root = Path(os.environ["VIVID_FISH_HERMETIC_REPO_ROOT"])
    if not root.is_dir() or not Path(sys.executable).exists():
        raise HermeticEnvironmentError("runner roots are unavailable")


_REAL_SOCKET = socket.socket
_REAL_CONNECT = _REAL_SOCKET.connect
_REAL_CONNECT_EX = _REAL_SOCKET.connect_ex
_INET_FAMILIES = frozenset((socket.AF_INET, socket.AF_INET6))
_FORBIDDEN_COMMANDS = frozenset(
    {"systemctl", "service", "tmux", "loginctl", "machinectl", "killall", "pkill"}
)


def _current_test() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "pytest collection")


def _destination(address: Any) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return repr(address)


class HermeticGuard:
    """Keep test effects inside the namespace and its unique state directory."""

    def __init__(self) -> None:
        self._allowed_tcp: set[tuple[int, str, int]] = set()
        self._allowed_unix: set[str] = set()
        self._installed = False
        self._audit_installed = False

    def install(self) -> None:
        if self._installed:
            return
        if not self._audit_installed:
            sys.addaudithook(self._audit)
            self._audit_installed = True
        socket.socket = self._guarded_socket_type()
        self._installed = True

    def restore(self) -> None:
        if self._installed:
            socket.socket = _REAL_SOCKET
            self._allowed_tcp.clear()
            self._allowed_unix.clear()
            self._installed = False

    def _guarded_socket_type(self) -> type[socket.socket]:
        guard = self

        class GuardedSocket(_REAL_SOCKET):
            def bind(self, address: Any) -> None:
                _REAL_SOCKET.bind(self, address)
                guard._remember_listener(self)

            def connect(self, address: Any) -> None:
                guard._check_destination(self.family, address)
                _REAL_CONNECT(self, address)

            def connect_ex(self, address: Any) -> int:
                guard._check_destination(self.family, address)
                return _REAL_CONNECT_EX(self, address)

        GuardedSocket.__name__ = "HermeticSocket"
        return GuardedSocket

    def _remember_listener(self, listener: socket.socket) -> None:
        if listener.family in _INET_FAMILIES:
            host, port = listener.getsockname()[:2]
            if isinstance(port, int) and port > 0:
                self._allowed_tcp.add((listener.family, str(host), port))
        elif listener.family == socket.AF_UNIX:
            path = listener.getsockname()
            state_dir = os.environ["VIVID_FISH_HERMETIC_STATE_DIR"]
            if isinstance(path, str) and Path(path).is_relative_to(state_dir):
                self._allowed_unix.add(path)

    def _check_destination(self, family: int, address: Any) -> None:
        if family in _INET_FAMILIES and isinstance(address, tuple) and len(address) >= 2:
            host, port = str(address[0]), address[1]
            allowed = (family, host, port) in self._allowed_tcp
            allowed = allowed or (family, "0.0.0.0", port) in self._allowed_tcp
            allowed = allowed or (family, "::", port) in self._allowed_tcp
            if allowed:
                return
        elif family == socket.AF_UNIX and isinstance(address, (str, bytes)):
            path = address.decode(errors="surrogateescape") if isinstance(address, bytes) else address
            if path in self._allowed_unix:
                return
        pytest.fail(
            "Hermetic pytest blocked connection to "
            f"{socket.AddressFamily(family).name} {_destination(address)} during {_current_test()}. "
            "Use ASGITransport/MockTransport or bind an ephemeral test listener."
        )

    def _audit(self, event: str, args: tuple[Any, ...]) -> None:
        if not self._installed:
            return
        if event == "socket.connect" and len(args) == 2:
            socket_instance, address = args
            self._check_destination(socket_instance.family, address)
        if event == "subprocess.Popen" and len(args) >= 2:
            executable, command = args[:2]
            target = executable
            if not target:
                target = command[0] if isinstance(command, (list, tuple)) else command
            name = Path(str(target)).name
            if name in _FORBIDDEN_COMMANDS:
                pytest.fail(
                    f"Hermetic pytest blocked host-control command {name!r} during {_current_test()}."
                )
        if event == "os.kill":
            pytest.fail(f"Hermetic pytest blocked process signalling during {_current_test()}.")


def assert_hermetic_environment() -> None:
    """Fail session startup before collection can import gateway runtime code."""
    required = (
        "VIVID_FISH_HERMETIC_TEST_NAMESPACE",
        "VIVID_FISH_HERMETIC_STATE_DIR",
        "VIVID_FISH_HERMETIC_FORBIDDEN_PRODUCTION_PATH",
        "VIVID_FISH_HERMETIC_HOST_RUNNER_PID",
        "VIVID_FISH_HERMETIC_HOST_PID_NAMESPACE",
        "VIVID_FISH_HERMETIC_HOST_IPC_NAMESPACE",
        "VIVID_FISH_HERMETIC_TEST_ENV_ROOT",
        "VIVID_FISH_HERMETIC_REPO_ROOT",
        "VIVID_FISH_HERMETIC_PYTHON_ROOT",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if os.environ.get("VIVID_FISH_HERMETIC_TEST_NAMESPACE") != "1" or missing:
        pytest.exit("pytest must run through scripts/run-hermetic-pytest.sh", returncode=2)
    state_dir = Path(os.environ["VIVID_FISH_HERMETIC_STATE_DIR"])
    if not state_dir.is_dir() or "/.tmp/" not in f"{state_dir}/":
        pytest.exit("pytest state must be a unique disk-backed directory below ~/.tmp", returncode=2)
    if Path(os.environ["VIVID_FISH_HERMETIC_FORBIDDEN_PRODUCTION_PATH"]).exists():
        pytest.exit("pytest namespace exposes the production checkout", returncode=2)
    if [name for _, name in socket.if_nameindex()] != ["lo"]:
        pytest.exit("pytest namespace must expose only loopback", returncode=2)
    with open("/proc/net/route", encoding="utf-8") as routes:
        if any(line.strip() for line in list(routes)[1:]):
            pytest.exit("pytest namespace must not have an IPv4 route", returncode=2)
    if Path(f"/proc/{os.environ['VIVID_FISH_HERMETIC_HOST_RUNNER_PID']}").exists():
        pytest.exit("pytest namespace exposes the host runner process", returncode=2)
    if os.readlink("/proc/1/ns/pid") == os.environ["VIVID_FISH_HERMETIC_HOST_PID_NAMESPACE"]:
        pytest.exit("pytest namespace shares the host PID namespace", returncode=2)
    if os.readlink("/proc/1/ns/ipc") == os.environ["VIVID_FISH_HERMETIC_HOST_IPC_NAMESPACE"]:
        pytest.exit("pytest namespace shares the host IPC namespace", returncode=2)
    try:
        validate_test_environment()
    except (HermeticEnvironmentError, KeyError, OSError, ValueError) as error:
        pytest.exit(f"pytest has an unapproved interpreter/import root: {error}", returncode=2)
