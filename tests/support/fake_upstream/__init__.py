"""Scenario-driven fake upstreams, one per wire protocol (TEST_STRATEGY 4.6).

Use the ``fake_upstreams`` fixture: ``fake_upstreams.asgi(scenario)`` serves a
scenario in process (application semantics); ``fake_upstreams.loopback(...)``
serves it on a real 127.0.0.1 port (wire faults, timeouts). Both share
``fake_upstreams.clock``. Teardown fails the test on any unexpected call or
unconsumed required exchange, even when the gateway absorbed the 599.
"""

from .app import VIOLATION_STATUS, FakeUpstream, RecordedRequest
from .clock import VirtualClock
from .deadline import Deadlines, module_deadline, read_deadline
from .fixtures import FakeUpstreams, fake_upstreams, verify
from .known_bug import KnownBug, expect_fixed
from .protocols import PROTOCOLS
from .scenario import Scenario, ScenarioError, load_scenario, scenario_from_data
from .transport import HandlerTransport, HostRouter, StreamingASGITransport

__all__ = [
    "PROTOCOLS",
    "VIOLATION_STATUS",
    "Deadlines",
    "FakeUpstream",
    "FakeUpstreams",
    "HandlerTransport",
    "HostRouter",
    "KnownBug",
    "RecordedRequest",
    "Scenario",
    "ScenarioError",
    "StreamingASGITransport",
    "VirtualClock",
    "expect_fixed",
    "fake_upstreams",
    "load_scenario",
    "module_deadline",
    "read_deadline",
    "scenario_from_data",
    "verify",
]
