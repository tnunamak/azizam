"""The ``fake_upstreams`` pytest fixture and its fail-closed teardown."""

from __future__ import annotations

import itertools
from typing import Iterator

import pytest

from .app import FakeUpstream
from .clock import VirtualClock
from .loopback import LoopbackServer
from .scenario import Scenario
from .transport import HostRouter


class FakeUpstreams:
    """Every fake a test registers, on one virtual clock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = VirtualClock()
        self._monkeypatch = monkeypatch
        self._router: HostRouter | None = None
        self._fakes: list[FakeUpstream] = []
        self._servers: list[LoopbackServer] = []
        self._ids = itertools.count(1)

    @property
    def router(self) -> HostRouter:
        if self._router is None:
            self._router = HostRouter(self._monkeypatch)
        return self._router

    def asgi(self, scenario: Scenario, *, host: str = "") -> FakeUpstream:
        """Serve ``scenario`` in process at ``http://<host>`` (application semantics)."""
        host = host or f"{scenario.protocol.name}-{next(self._ids)}.fk.test"
        fake = FakeUpstream(scenario, self.clock, name=f"{scenario.name}@{host}", base_url=f"http://{host}")
        self.router.add(host, fake)
        self._fakes.append(fake)
        return fake

    def loopback(self, scenario: Scenario) -> FakeUpstream:
        """Serve ``scenario`` on a 127.0.0.1 port (wire faults and timeouts)."""
        fake = FakeUpstream(scenario, self.clock, name=scenario.name)
        server = LoopbackServer(fake).start()
        self._servers.append(server)
        fake.base_url = server.base_url
        fake.name = f"{scenario.name}@{server.base_url}"
        self._fakes.append(fake)
        return fake

    def problems(self) -> list[str]:
        return [problem for fake in self._fakes for problem in fake.problems()]

    def close(self) -> None:
        self.clock.close()
        for server in self._servers:
            server.stop()
        self._servers.clear()


def verify(fakes: FakeUpstreams) -> None:
    """Fail the test on any latched violation or unconsumed required exchange."""
    problems = fakes.problems()
    if problems:
        pytest.fail("fake upstream contract broken:\n" + "\n".join(problems), pytrace=False)


@pytest.fixture
def fake_upstreams(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeUpstreams]:
    fakes = FakeUpstreams(monkeypatch)
    try:
        yield fakes
    finally:
        fakes.close()
    verify(fakes)
