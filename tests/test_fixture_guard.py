"""No actual external connection or host process is needed to test the guard."""
import socket
from types import SimpleNamespace
import pytest

from scripts.ui.fixture_guard import check_event


@pytest.mark.parametrize("address", [("127.0.0.1", 5051), ("127.0.0.1", 5092),
                                     ("192.0.2.1", 18080), ("127.0.0.1", 18081)])
def test_fixture_rejects_all_undeclared_destinations(address):
    with pytest.raises(RuntimeError, match="outside its declared loopback ports"):
        check_event("socket.connect", (SimpleNamespace(family=socket.AF_INET), address), {18080})


def test_fixture_allows_only_its_declared_listener():
    check_event("socket.connect", (SimpleNamespace(family=socket.AF_INET), ("127.0.0.1", 18080)), {18080})


@pytest.mark.parametrize("command", ["systemctl", "docker", "tmux", "pgrep"])
def test_fixture_blocks_host_control_and_inventory(command):
    with pytest.raises(RuntimeError, match="host-control"):
        check_event("subprocess.Popen", (f"/usr/bin/{command}", [command], None, None), {18080})


def test_fixture_blocks_unix_sockets_and_shell_wrappers():
    with pytest.raises(RuntimeError, match="outside its declared loopback ports"):
        check_event("socket.connect", (SimpleNamespace(family=socket.AF_UNIX), "/run/docker.sock"), {18080})
    for event, args in [("os.system", (b"systemctl status",)),
                        ("os.posix_spawn", ("/bin/sh", ["sh", "-c", "systemctl status"], {})),
                        ("os.exec", ("/bin/sh", ["sh", "-c", "systemctl status"], {}))]:
        with pytest.raises(RuntimeError, match="host-control"):
            check_event(event, args, {18080})
