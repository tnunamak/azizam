"""The VRAM observer for an exclusive route pool: one GPU's free and total memory, read from nvidia-smi.

Served as ``GET /vram`` on loopback by ``python -m gateway.vram_observer``. It reads the driver and nothing else:
it keeps no history, makes no admission decision and holds no lock. Any answer it cannot read exactly is a
refusal (HTTP 503 with no numbers), never a guessed value. The pool treats that refusal as an unavailable observer.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

MIB = 1 << 20
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
QUERY = ["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"]


class ObserverUnavailable(Exception):
    """The driver did not answer with an exact reading, or did not answer in time."""


@dataclass(frozen=True)
class GpuMemory:
    gpu_index: int
    memory_free_bytes: int
    memory_total_bytes: int


def read_gpu_memory(gpu_index: int, *, timeout_s: float, run: Callable[..., Any] = subprocess.run) -> GpuMemory:
    """Ask the driver for one GPU's free and total memory. Raises ObserverUnavailable on any doubt."""
    try:
        completed = run([*QUERY, "-i", str(gpu_index)], capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ObserverUnavailable("the driver did not answer") from error
    if completed.returncode != 0:
        raise ObserverUnavailable("the driver reported an error")
    fields = [part.strip() for part in completed.stdout.strip().split(",")]
    if len(fields) != 2 or not all(field.isdigit() for field in fields):
        raise ObserverUnavailable("the driver answer is not two whole numbers")
    free_mib, total_mib = (int(field) for field in fields)
    if total_mib <= 0 or free_mib > total_mib:
        raise ObserverUnavailable("the driver answer is out of range")
    return GpuMemory(gpu_index=gpu_index, memory_free_bytes=free_mib * MIB, memory_total_bytes=total_mib * MIB)


def document(reading: GpuMemory) -> dict[str, Any]:
    return {
        "gpu_index": reading.gpu_index,
        "memory_free_bytes": reading.memory_free_bytes,
        "memory_total_bytes": reading.memory_total_bytes,
        "observed_at": time.time(),
    }


def make_server(host: str, port: int, *, gpu_index: int, timeout_s: float,
                run: Callable[..., Any] = subprocess.run) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("the VRAM observer serves loopback only")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            if self.path == "/health":
                return self._send(200, {"status": "ok"})
            if self.path != "/vram":
                return self._send(404, {"error": "not found"})
            try:
                reading = read_gpu_memory(gpu_index, timeout_s=timeout_s, run=run)
            except ObserverUnavailable as error:
                return self._send(503, {"error": str(error)})
            self._send(200, document(reading))

        def do_POST(self) -> None:  # noqa: N802
            self._send(405, {"error": "method not allowed"})

        do_PUT = do_DELETE = do_PATCH = do_POST

        def _send(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server's signature
            return

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve one GPU's VRAM facts to an exclusive route pool.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port, gpu_index=args.gpu, timeout_s=args.timeout)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
