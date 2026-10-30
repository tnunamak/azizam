"""The mesh row probes its trellis and observer live. A test that lists capabilities stands the harness's fake
upstream in for that probe, so the test never reaches a real service (the hermetic guard would refuse it)."""
from __future__ import annotations

from unittest.mock import patch

import proxy
from tests.test_mesh_route import Harness


def fake_mesh_pool():
    """Patch ``proxy.MESH_DEPS`` with the harness deps: a healthy pool answered by the fake transport."""
    return patch.object(proxy, "MESH_DEPS", Harness().deps())
