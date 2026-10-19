"""A recording forward proxy between a gateway and an engine.

The gateway sends it each piece it cuts; it passes the request to the engine unchanged and notes the piece's
duration. The runner reads the notes to learn where the gateway cut, whatever version of the gateway ran, so the
check needs nothing from the gateway beyond its normal route.
"""
from __future__ import annotations

import http.client
import io
import json
import re
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_BOUNDARY = re.compile(rb"boundary=([^;\s]+)")


def piece_seconds(body: bytes, content_type: bytes) -> float | None:
    """Duration of the WAV file part in a multipart body, or None."""
    match = _BOUNDARY.search(content_type)
    start = body.find(b"RIFF")
    if match is None or start < 0:
        return None
    end = body.rfind(b"\r\n--" + match.group(1), start)
    try:
        with wave.open(io.BytesIO(body[start:end if end > 0 else len(body)])) as handle:
            return handle.getnframes() / handle.getframerate()
    except (wave.Error, EOFError):
        return None


class Tap:
    def __init__(self, port: int, engine_host: str, engine_port: int) -> None:
        self.notes: list[dict] = []
        self._lock = threading.Lock()
        tap = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                return None

            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                started = time.monotonic()
                connection = http.client.HTTPConnection(engine_host, engine_port, timeout=900)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "connection"}}
                connection.request(self.command, self.path, body=body or None, headers=headers)
                reply = connection.getresponse()
                data = reply.read()
                connection.close()
                note = {"seconds": piece_seconds(body, self.headers.get("Content-Type", "").encode()),
                        "status": reply.status, "wall": time.monotonic() - started}
                if self.command == "POST":
                    with tap._lock:
                        tap.notes.append(note)
                self.send_response(reply.status)
                for key, value in reply.getheaders():
                    if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _forward

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def take(self) -> list[dict]:
        with self._lock:
            notes, self.notes = self.notes, []
        return notes

    def dump(self) -> str:
        return json.dumps(self.notes)
