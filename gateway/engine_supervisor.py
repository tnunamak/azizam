"""Stable group leader for one gateway-owned engine process.

The owner argv is passed as separate arguments, without a shell. The leader
stays alive after the engine exits so the gateway can still authenticate and
terminate the whole process group by PID plus kernel start time.

The leader also owns three things that must outlive the gateway:
- its own state file, written first and atomically, so a gateway crash right
  after the spawn cannot leave an untracked orphan;
- the engine's output pipe, so a gateway restart cannot break the engine's
  stdout (an adopted engine never sees SIGPIPE);
- the log: redacted, size-capped and rotated. A failing log write is dropped;
  the pump keeps draining so the engine never blocks on its own output.

It needs the standard library only: it runs as a script in a new session.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO, Iterable

_SECRET_NAME = re.compile(r"(?<![a-z])(key|token|secret|passw(?:or)?d|credential|auth)(?![a-z])", re.IGNORECASE)
_KNOWN_SECRET = re.compile(rb"\b(?:opk_|oak_)[A-Za-z0-9_-]+")
_LINE_BYTES = 4096
_SCRUBBER = None


def argv_secrets(argv: Iterable[str]) -> list[str]:
    """Values that follow a secret-looking flag, or sit in ``--flag=value`` / ``NAME=value``."""
    argv = list(argv)
    values: set[str] = set()
    for index, arg in enumerate(argv):
        if "=" in arg:
            name, _, value = arg.partition("=")
            if _SECRET_NAME.search(name):
                values.add(value)
        elif arg.startswith("-") and _SECRET_NAME.search(arg) and index + 1 < len(argv):
            following = argv[index + 1]
            if not following.startswith("-"):
                values.add(following)
    return sorted(value for value in values if len(value) >= 4)


def _forms_of(value: str) -> list[str]:
    """Every form of ``value`` the gateway's log scrubber removes (raw, URL-encoded, JSON-escaped, base64 and URL-safe base64,
    padded or not). The scrubber is loaded from its file: this script runs isolated (``-I``) and imports no package."""
    global _SCRUBBER
    try:
        if _SCRUBBER is None:
            spec = importlib.util.spec_from_file_location("azizam_log_scrub", Path(__file__).resolve().with_name("log_scrub.py"))
            _SCRUBBER = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
            spec.loader.exec_module(_SCRUBBER)                   # type: ignore[union-attr]
        return list(_SCRUBBER.forms(value))
    except Exception:  # noqa: BLE001 - without the scrubber the raw value is still removed
        return [value]


class LogPump:
    """Copy engine output to a redacted, size-capped, rotating log (the file is 0600: defence in depth, decision D45)."""

    def __init__(self, path: Path, *, limit: int, secrets: Iterable[str]) -> None:
        self.path = Path(path)
        self.limit = max(1024, int(limit))
        forms = {form for secret in secrets if len(secret) >= 4 for form in _forms_of(secret) if len(form) >= 4}
        self.secrets = sorted((form.encode() for form in forms), key=len, reverse=True)
        self._fd: int | None = None
        self._size = 0

    def run(self, source: BinaryIO) -> int:
        """Drain ``source`` to its end. Returns the number of lines read."""
        lines = 0
        while True:
            line = source.readline(_LINE_BYTES)
            if not line:
                return lines
            if len(line) == _LINE_BYTES and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = source.readline(_LINE_BYTES)
                line = b"[oversized log line omitted]\n"
            lines += 1
            try:
                self._write(self._redact(line)[: self.limit])
            except OSError:
                self._close()  # drop this line and try again on the next one

    def _redact(self, line: bytes) -> bytes:
        for secret in self.secrets:
            line = line.replace(secret, b"[redacted]")
        return _KNOWN_SECRET.sub(b"[redacted]", line)

    def _write(self, line: bytes) -> None:
        if self._fd is None:
            self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            os.fchmod(self._fd, 0o600)
            self._size = os.fstat(self._fd).st_size
        if self._size + len(line) > self.limit:
            self._close()
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            os.fchmod(self._fd, 0o600)
            self._size = 0
        os.write(self._fd, line)
        self._size += len(line)

    def _close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def _own_start_tick() -> str:
    stat = Path("/proc/self/stat").read_text()
    return stat[stat.rfind(")") + 2 :].split()[19]


def _write_state(path: Path, port: int, argv: list[str]) -> None:
    payload = {"pid": os.getpid(), "start_time": _own_start_tick(), "port": port,
               "argv_sha256": hashlib.sha256(json.dumps(argv).encode()).hexdigest()}
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return 2
    settings = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("AZIZAM_SUPERVISOR_")}
    names = [name for name in settings.get("AZIZAM_SUPERVISOR_REDACT_ENV", "").split(",") if name]
    secrets = [os.environ[name] for name in names if name in os.environ] + argv_secrets(argv)
    state_file = settings.get("AZIZAM_SUPERVISOR_STATE_FILE")
    try:
        if state_file:
            _write_state(Path(state_file), int(settings.get("AZIZAM_SUPERVISOR_PORT", "0")), argv)
    except (OSError, ValueError):
        return 3
    log_path = settings.get("AZIZAM_SUPERVISOR_LOG_FILE")
    try:
        child = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, shell=False,
            stdout=subprocess.PIPE if log_path else subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
    except OSError:
        return 127
    if log_path and child.stdout is not None:
        try:
            LogPump(Path(log_path), limit=int(settings.get("AZIZAM_SUPERVISOR_LOG_MAX_BYTES", "65536")),
                    secrets=secrets).run(child.stdout)
        except Exception:  # whatever went wrong, the engine must never block on its output
            while child.stdout.read(65536):
                pass
    child.wait()
    # Keep the group identity alive until the gateway kills the group. An
    # engine crash is discovered by the declared readiness observation.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
