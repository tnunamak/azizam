#!/usr/bin/env python3
"""Serve one fake-upstream scenario on a loopback port until SIGTERM.

Usage: fake_upstream_serve.py PORT SCENARIO.json PROBLEMS.json

The gateway fixture guard (scripts/ui/fixture_guard.py) only allows 18xxx
ports, so the port is given, not drawn by the OS. Prints `PORT <n>` once it listens. On SIGTERM it writes the contract problems
(a request no exchange expects, or a required exchange never called) to
PROBLEMS.json as a list and exits 0; the caller decides what they mean.
The server and scenario format are tests/support/fake_upstream (TEST_STRATEGY.md 4.6).
"""
from __future__ import annotations

import asyncio
import json
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support.fake_upstream import FakeUpstream, VirtualClock, scenario_from_data  # noqa: E402
from tests.support.fake_upstream.loopback import LoopbackServer  # noqa: E402


class FixedPortServer(LoopbackServer):
    """LoopbackServer on a chosen port. FK binds port 0 and has no port option, so
    this repeats its startup (tests/support/fake_upstream/loopback.py `_run`)."""

    def __init__(self, app, port: int) -> None:
        super().__init__(app)
        self._wanted_port = port

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._server = self._loop.run_until_complete(
                asyncio.start_server(self._handle, "127.0.0.1", self._wanted_port))
            self.port = self._server.sockets[0].getsockname()[1]
        except BaseException as error:  # noqa: BLE001 - reported to start()
            self._error = error
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()


def main() -> None:
    port = int(sys.argv[1])
    scenario_path, problems_path = map(Path, sys.argv[2:])
    scenario = scenario_from_data(json.loads(scenario_path.read_text(encoding="utf-8")), source=str(scenario_path))
    clock = VirtualClock()
    fake = FakeUpstream(scenario, clock, name=scenario.name)
    server = FixedPortServer(fake, port).start()
    fake.base_url = server.base_url
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(f"PORT {server.port}", flush=True)
    stop.wait()
    clock.close()
    server.stop()
    problems_path.write_text(json.dumps(fake.problems()), encoding="utf-8")


if __name__ == "__main__":
    main()
