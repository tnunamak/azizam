"""Count the work a code block causes: SQLite statements, connections, processes.

A budget test runs one request at two data sizes and compares the counts. The
counts are exact and do not depend on host load, unlike wall-clock time.

    with count_work() as work:
        client.get("/me/api/usage/summary")
    assert work.statements <= 40 and work.processes == 0
"""
from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import threading
from dataclasses import dataclass, field
from unittest.mock import patch


@dataclass
class Work:
    statements: int = 0
    connections: int = 0
    processes: int = 0
    commands: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, name: str) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)


@contextlib.contextmanager
def count_work():
    work = Work()
    real_connect = sqlite3.connect
    real_popen = subprocess.Popen

    def connect(*args, **kwargs):
        work.add("connections")
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(lambda _statement: work.add("statements"))
        return connection

    class Popen(real_popen):
        def __init__(self, command, *args, **kwargs):
            work.add("processes")
            work.commands.append(str(command[0] if isinstance(command, (list, tuple)) else command))
            super().__init__(command, *args, **kwargs)

    # subprocess.run, check_output and asyncio's subprocess helpers all start a Popen.
    with patch.object(sqlite3, "connect", connect), patch.object(subprocess, "Popen", Popen):
        yield work
