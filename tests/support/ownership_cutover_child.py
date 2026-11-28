"""The child processes of tests/test_ownership_cutover_verification.py: a real interpreter that the test pauses, kills or
lets finish. It imports only the cut-over (and the gateway for its own mode), not the test module: a kill test starts
one child per statement of the migration, and the test module's imports cost seconds per child."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import app_binding_cutover as cutover  # noqa: E402


class InjectedFailure(RuntimeError):
    pass


class ObservedConnection(sqlite3.Connection):
    """Pause/fail after a successful statement, outside sqlite's trace callback.

    Exceptions raised by trace callbacks are ignored by sqlite, so they cannot
    establish rollback. The hook also covers savepoints and guard self-tests.
    """

    hook = None
    statement_count = 0

    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if self.in_transaction and sql.strip().upper() != "COMMIT":
            self.statement_count += 1
            if self.hook is not None:
                self.hook(self.statement_count, sql)
        return result


def observed_connect(path, hook):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, factory=ObservedConnection)
    conn.row_factory = sqlite3.Row
    conn.hook = hook
    return conn


def _run_child(mode, path, args):
    if mode == "pause-statement":
        target = int(args[0])

        def pause(n, sql):
            if n == target:
                print("paused", flush=True)
                sys.stdin.readline()

        cutover._connect = lambda p: observed_connect(p, pause)
        cutover.apply(path)
        print("applied", flush=True)
    elif mode in ("pause-backup", "fail-backup"):
        class BackupConnection(sqlite3.Connection):
            def backup(self, target, **kwargs):
                def progress(status, remaining, total):
                    if remaining:
                        if mode == "fail-backup":
                            raise InjectedFailure("interrupted backup")
                        print("paused", flush=True)
                        sys.stdin.readline()

                return super().backup(target, pages=1, progress=progress)

        def connect(p):
            conn = sqlite3.connect(p, isolation_level=None, factory=BackupConnection)
            conn.row_factory = sqlite3.Row
            return conn

        cutover._connect = connect
        cutover.apply(path)
    elif mode == "pause-report":
        def report(*args):
            print("paused", flush=True)
            sys.stdin.readline()
            return []

        cutover._report = report
        cutover.apply(path)
    elif mode == "gateway":
        from contextlib import asynccontextmanager, redirect_stdout
        from fastapi.testclient import TestClient
        with redirect_stdout(sys.stderr):
            import proxy

        proxy.ADMIN_STORE.path = path
        take_lock = proxy.gateway_lifetime.hold

        def hold(p):
            print("requesting-lifetime-lock", flush=True)
            take_lock(p)

        proxy.gateway_lifetime.hold = hold

        @asynccontextmanager
        async def serving(app):
            # Engines/background workers are outside this lock test. This is the
            # actual production lifespan's transition into its serving body.
            yield

        proxy._serve = serving

        print("entering-lifespan", flush=True)
        with TestClient(proxy.app) as client:
            assert client.get("/auth/api/options").status_code == 200
            print("serving", flush=True)
            sys.stdin.readline()
    else:
        raise AssertionError(mode)


if __name__ == "__main__":
    _run_child(sys.argv[1], Path(sys.argv[2]), sys.argv[3:])
