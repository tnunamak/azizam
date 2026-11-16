"""Local-only deadline and process-group regressions; no gateway or model calls."""

from contextlib import contextmanager
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

HELPER = Path(__file__).with_name("codex_e2e_wait.sh")
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or any(shutil.which(tool) is None for tool in ("bash", "curl", "setsid")),
    reason="requires Linux proc, bash, curl and setsid",
)


@contextmanager
def listener(reply, delay=0):
    stop = threading.Event()
    peers = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.1)

    def serve():
        while not stop.is_set():
            try:
                peer, _ = server.accept()
            except socket.timeout:
                continue
            peers.append(peer)
            if reply:
                peer.settimeout(1)
                peer.recv(4096)
                if stop.wait(delay):
                    peer.close()
                    break
                try:
                    peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    # A deadline-aware client may already have closed.
                    pass
                peer.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}/health"
    finally:
        stop.set()
        thread.join(timeout=1)
        server.close()
        for peer in peers:
            peer.close()


def readiness(url, deadline):
    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; wait_ready "$2" "$3"', "_", str(HELPER), url, str(deadline)],
        capture_output=True, text=True, timeout=deadline + 3,
    )
    return result, time.monotonic() - started


def test_wait_ready_stalled_listener():
    with listener(False) as url:
        result, elapsed = readiness(url, 1)
    assert result.returncode == 1, result.stderr
    assert elapsed < 4


def test_wait_ready_success():
    with listener(True) as url:
        result, elapsed = readiness(url, 1)
    assert result.returncode == 0, result.stderr
    assert elapsed < 4


def test_wait_ready_rejects_late_success():
    with listener(True, delay=1.5) as url:
        result, elapsed = readiness(url, 1)
    assert result.returncode == 1, result.stderr
    assert elapsed < 4


def test_stop_group_surviving_kill_is_bounded():
    pid = "999999999"
    script = """
source "$1" || exit 2
kill() { return 0; }
wait() { echo unexpected_wait >&2; return 0; }
stop_group "$2" 1
"""
    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", script, "_", str(HELPER), pid],
        capture_output=True, text=True, timeout=6,
    )
    assert result.returncode == 1, result.stderr
    assert pid in result.stderr
    assert "unexpected_wait" not in result.stderr
    assert time.monotonic() - started < 6


def live_group(pid):
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            # comm can contain spaces or parentheses; fields follow the final ')'.
            fields = path.read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == pid and fields[0] != "Z":
            return True
    return False


def test_stop_group_term_ignoring_child(tmp_path):
    ready = tmp_path / "ready"
    script = """
source "$1" || exit 2
setsid bash -c 'trap "" TERM; echo "$$" > "$1"; while :; do sleep .1; done' _ "$2" &
pid=$!
trap 'stop_group "$pid" 0' EXIT
end=$((SECONDS + 2))
while [[ ! -s "$2" ]]; do
  (( SECONDS < end )) || exit 2
  sleep .05
done
stop_group "$pid" 1
trap - EXIT
"""
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["bash", "-c", script, "_", str(HELPER), str(ready)],
            capture_output=True, text=True, timeout=4,
        )
        assert result.returncode == 0, result.stderr
        pid = int(ready.read_text())
        end = started + 4
        while live_group(pid) and time.monotonic() < end:
            time.sleep(.05)
        assert not live_group(pid)
        assert time.monotonic() - started < 4
    finally:
        # Even a failed assertion uses the helper for signalling, never Python.
        if ready.exists():
            subprocess.run(
                ["bash", "-c", 'source "$1"; stop_group "$2" 0', "_", str(HELPER), ready.read_text().strip()],
                capture_output=True, timeout=3, check=False,
            )
