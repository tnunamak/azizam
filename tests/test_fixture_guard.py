"""No actual external connection or host process is needed to test the guard."""
import socket
import subprocess
import sys
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
    with pytest.raises(RuntimeError, match="child process"):
        check_event("subprocess.Popen", (f"/usr/bin/{command}", [command], None, None), {18080})


def test_fixture_blocks_unix_sockets_and_shell_wrappers():
    with pytest.raises(RuntimeError, match="outside its declared loopback ports"):
        check_event("socket.connect", (SimpleNamespace(family=socket.AF_UNIX), "/run/docker.sock"), {18080})
    for event, args in [("os.system", (b"systemctl status",)),
                        ("os.posix_spawn", ("/bin/sh", ["sh", "-c", "systemctl status"], {})),
                        ("os.exec", ("/bin/sh", ["sh", "-c", "systemctl status"], {}))]:
        with pytest.raises(RuntimeError, match="child process"):
            check_event(event, args, {18080})


def test_guard_blocks_ipv6_and_datagrams():
    with pytest.raises(RuntimeError, match="outside its declared loopback ports"):
        check_event("socket.connect", (SimpleNamespace(family=socket.AF_INET6), ("::1", 18080)), {18080})
    with pytest.raises(RuntimeError, match="outside its declared loopback ports"):
        check_event("socket.sendto", (SimpleNamespace(family=socket.AF_INET), ("127.0.0.1", 5051)), {18080})
    with pytest.raises(RuntimeError, match="name resolution"):
        check_event("socket.getaddrinfo", ("external.example", 18080, 0, 0, 0), {18080})


def test_real_async_httpx_connection_is_blocked_before_listener(test_owned_tcp_listener):
    listener = test_owned_tcp_listener
    port = listener.getsockname()[1]
    code = '''
import asyncio, httpx, sys
from scripts.ui.fixture_guard import install
install({18080})
async def probe():
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            await client.get('http://127.0.0.1:' + sys.argv[1])
        except RuntimeError as exc:
            assert 'Fixture blocked' in str(exc)
            print('connection blocked')
        else:
            raise AssertionError('Forbidden connection succeeded')
asyncio.run(probe())
'''
    result = subprocess.run([sys.executable, "-c", code, str(port)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "connection blocked"
    listener.settimeout(.1)
    with pytest.raises(TimeoutError):
        listener.accept()
