"""No actual external connection or host process is needed to test the guard."""
import socket
import subprocess
import sys
from types import SimpleNamespace
import pytest

from scripts.ui.fixture_guard import check_event, install, parse_allowed_ports


def test_parse_declared_fixture_ports():
    assert parse_allowed_ports("18690, 18691,,18692") == {18690, 18691, 18692}


@pytest.mark.parametrize("raw", ["15000", "18690,5000", "18999,19000"])
def test_install_rejects_declared_ports_outside_fixture_range(raw):
    with pytest.raises(ValueError, match="18000–18999"):
        install({18080, *parse_allowed_ports(raw)})


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
        except* RuntimeError as errors:
            def leaves(error):
                if isinstance(error, BaseExceptionGroup):
                    for item in error.exceptions:
                        yield from leaves(item)
                else:
                    yield error
            assert all('Fixture blocked' in str(error) for error in leaves(errors))
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


def test_household_allows_only_the_speech_mp3_encode_between_temp_files(tmp_path):
    import tempfile
    from pathlib import Path

    temp = Path(tempfile.gettempdir())
    encode = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i",
              str(temp / "speech.wav"), "-codec:a", "libmp3lame", "-f", "mp3", str(temp / "speech.mp3")]
    media = tmp_path / "images"
    check_event("subprocess.Popen", ("ffmpeg", encode, None, None), {18080}, media)
    # Not without the household media root, not outside the temp dir, not another codec.
    with pytest.raises(RuntimeError, match="child process"):
        check_event("subprocess.Popen", ("ffmpeg", encode, None, None), {18080})
    elsewhere = encode[:6] + ["/etc/passwd.wav"] + encode[7:]
    with pytest.raises(RuntimeError, match="child process"):
        check_event("subprocess.Popen", ("ffmpeg", elsewhere, None, None), {18080}, media)
    other = encode[:7] + ["-codec:a", "pcm_s16le", "-f", "wav", str(temp / "x.wav")]
    with pytest.raises(RuntimeError, match="child process"):
        check_event("subprocess.Popen", ("ffmpeg", other, None, None), {18080}, media)


def test_household_allows_the_provenance_stamp_only_inside_its_state(tmp_path):
    work = tmp_path / "media_metadata"
    work.mkdir()
    media = tmp_path / "images"
    source = work / "token.in.mp3"
    source.write_bytes(b"fixture")
    stamp = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-map", "0",
             "-metadata", 'azizam_provenance={"kind":"audio"}', "-c", "copy",
             "-fflags", "+bitexact", "-f", "mp3", str(work / "token.out.mp3")]
    check_event("subprocess.Popen", ("ffmpeg", stamp, None, None), {18080}, media)
    with pytest.raises(RuntimeError, match="child process"):
        check_event("subprocess.Popen", ("ffmpeg", stamp, None, None), {18080})
    # Anything but -metadata pairs in the middle is refused.
    other = stamp[:9] + ["-filter_complex", "x=1"] + stamp[11:]
    with pytest.raises(RuntimeError, match="child process"):
        check_event("subprocess.Popen", ("ffmpeg", other, None, None), {18080}, media)
    outside = tmp_path.parent / "elsewhere.in.mp3"
    with pytest.raises(RuntimeError, match="child process"):
        check_event("subprocess.Popen", ("ffmpeg", stamp[:6] + [str(outside)] + stamp[7:], None, None), {18080}, media)
