"""The VRAM observer for the exclusive mesh pool: one GPU's free and total memory, or a refusal. Nothing else."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from gateway import vram_observer
from gateway.exclusive_pool import MIB, ExclusivePoolSpec, observation_from_document, resolve_pointer


class FakeRun:
    """Stands in for subprocess.run. Records the argv and the timeout it was called with."""

    def __init__(self, stdout: str = "", returncode: int = 0, error: Exception | None = None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, *, capture_output, text, timeout, check):
        self.calls.append((list(argv), timeout))
        assert capture_output and text and not check
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


def test_reads_free_and_total_for_the_configured_gpu_in_bytes():
    run = FakeRun(stdout="5790, 24576\n")

    reading = vram_observer.read_gpu_memory(0, timeout_s=3.0, run=run)

    assert reading == vram_observer.GpuMemory(
        gpu_index=0, memory_free_bytes=5790 * MIB, memory_total_bytes=24576 * MIB,
    )
    argv, timeout = run.calls[0]
    assert argv == [
        "nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits", "-i", "0",
    ]
    assert timeout == 3.0


@pytest.mark.parametrize("failure", [
    {"returncode": 9, "stdout": ""},
    {"returncode": 0, "stdout": ""},
    {"returncode": 0, "stdout": "[N/A], 24576\n"},
    {"returncode": 0, "stdout": "5790\n"},
    {"returncode": 0, "stdout": "5790, 24576, 1\n"},
    {"returncode": 0, "stdout": "25000, 24576\n"},
    {"returncode": 0, "stdout": "-1, 24576\n"},
    {"returncode": 0, "stdout": "0, 0\n"},
    {"returncode": 0, "stdout": "5790.5, 24576\n"},
])
def test_refuses_any_answer_it_cannot_read_exactly(failure):
    run = FakeRun(stdout=failure["stdout"], returncode=failure["returncode"])

    with pytest.raises(vram_observer.ObserverUnavailable):
        vram_observer.read_gpu_memory(0, timeout_s=3.0, run=run)


def test_a_missing_or_hung_driver_is_a_refusal_not_a_guess():
    with pytest.raises(vram_observer.ObserverUnavailable):
        vram_observer.read_gpu_memory(0, timeout_s=3.0, run=FakeRun(error=FileNotFoundError("nvidia-smi")))
    with pytest.raises(vram_observer.ObserverUnavailable):
        vram_observer.read_gpu_memory(
            0, timeout_s=3.0, run=FakeRun(error=subprocess.TimeoutExpired("nvidia-smi", 3.0)),
        )


def test_the_reading_satisfies_the_pool_observer_contract_as_configured():
    # The committed pool declares these pointers; the observer's document must satisfy them exactly.
    spec = ExclusivePoolSpec(
        pool_id="gpu0", max_claim_bytes=MIB, reserve_bytes=MIB, retry_after_seconds=30, timeout_seconds=5,
        observer_service="vram-observer", observer_path="/vram",
        free_pointer="/memory_free_bytes", total_pointer="/memory_total_bytes",
    )
    reading = vram_observer.read_gpu_memory(0, timeout_s=3.0, run=FakeRun(stdout="5790, 24576\n"))

    observation = observation_from_document(vram_observer.document(reading), spec)

    assert observation.free_bytes == 5790 * MIB
    assert observation.total_bytes == 24576 * MIB
    assert resolve_pointer(vram_observer.document(reading), "/gpu_index") == 0


@pytest.fixture()
def observer_url():
    """Serve the observer on a loopback port with a fake driver, the way the module's entry point does."""
    run = FakeRun(stdout="5790, 24576\n")
    server = vram_observer.make_server("127.0.0.1", 0, gpu_index=0, timeout_s=3.0, run=run)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", run
    finally:
        server.shutdown()
        server.server_close()


def test_get_vram_answers_the_document_as_json(observer_url):
    base, _ = observer_url
    with urllib.request.urlopen(base + "/vram", timeout=5) as response:
        assert response.status == 200
        assert response.headers["content-type"].startswith("application/json")
        body = json.loads(response.read())
    assert body["gpu_index"] == 0
    assert body["memory_free_bytes"] == 5790 * MIB
    assert body["memory_total_bytes"] == 24576 * MIB


def test_get_health_is_ok_without_reading_the_driver(observer_url):
    base, run = observer_url
    with urllib.request.urlopen(base + "/health", timeout=5) as response:
        assert response.status == 200
    assert run.calls == []


def test_a_driver_failure_is_503_with_no_numbers(observer_url):
    base, run = observer_url
    run.stdout = "[N/A], [N/A]\n"
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(base + "/vram", timeout=5)
    assert caught.value.code == 503
    body = json.loads(caught.value.read())
    assert "memory_free_bytes" not in body
    assert "memory_total_bytes" not in body


def test_other_paths_and_methods_are_refused(observer_url):
    base, _ = observer_url
    with pytest.raises(urllib.error.HTTPError) as missing:
        urllib.request.urlopen(base + "/system_stats", timeout=5)
    assert missing.value.code == 404
    request = urllib.request.Request(base + "/vram", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as method:
        urllib.request.urlopen(request, timeout=5)
    assert method.value.code == 405


def test_the_server_binds_loopback_only():
    server = vram_observer.make_server("127.0.0.1", 0, gpu_index=0, timeout_s=3.0, run=FakeRun())
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_a_non_loopback_host_is_refused():
    with pytest.raises(ValueError):
        vram_observer.make_server("0.0.0.0", 0, gpu_index=0, timeout_s=3.0, run=FakeRun())
