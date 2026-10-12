"""Ratchet: test files that fake an HTTP upstream on their own (TEST_STRATEGY 6).

New upstream fakes belong in tests/support/fake_upstream/. This count may
only fall; when a migration lowers it, lower the ceiling in the same change.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_TESTS = Path(__file__).parent
_FK = _TESTS / "support" / "fake_upstream"
# Mechanisms that stand in for an upstream over HTTP. A FastAPI() app that
# only hosts a gateway router for TestClient is the gateway under test, not a fake.
_MECHANISMS = {
    "own_server": re.compile(
        r"HTTPServer|BaseHTTPRequestHandler|uvicorn\.(Server|Config|run)\b|asyncio\.start_server|socketserver|web\.Application\("
    ),
    "mock_transport": re.compile(r"httpx\.MockTransport|\bMockTransport\("),
    "patched_async_client": re.compile(
        r"patch\([^)\n]*httpx\.AsyncClient|setattr\([^)\n]*httpx[^)\n]*AsyncClient|setattr\([^)\n]*['\"]AsyncClient['\"]|patch\.object\(httpx"
    ),
}

# Files that match a mechanism but serve real gateway code, not a fake upstream.
_NOT_FAKES = {
    "tests/compat/python/macro_ux_fixture.py": "serves the real proxy app over uvicorn for the UX fixture",
    "tests/test_decision_engine_server.py": "serves the real decision engine Handler under test",
    "tests/test_provider_missing_keys_live.py": "serves the real provider router over TCP",
    "tests/test_route_policy_inventory.py": "patches AsyncClient as a no-network tripwire",
}


def ad_hoc_fake_files() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(_TESTS.rglob("*.py")):
        name = path.relative_to(_TESTS.parent).as_posix()
        if path.is_relative_to(_FK) or path == Path(__file__) or path.name == "_hermetic.py" or name in _NOT_FAKES:
            continue
        text = path.read_text(encoding="utf-8")
        kinds = [kind for kind, pattern in _MECHANISMS.items() if pattern.search(text)]
        if kinds:
            found[name] = kinds
    return found


def test_ad_hoc_upstream_fakes_only_fall() -> None:
    ceiling = tomllib.loads((_TESTS / "budgets.toml").read_text(encoding="utf-8"))["ratchets"]["ad_hoc_upstream_fake_files"]
    found = ad_hoc_fake_files()
    listing = "\n".join(f"  {name}: {', '.join(kinds)}" for name, kinds in found.items())
    assert len(found) <= ceiling, (
        f"{len(found)} test files fake an upstream on their own (ceiling {ceiling}). "
        f"Serve the upstream from tests/support/fake_upstream/ instead:\n{listing}"
    )
    assert len(found) == ceiling, (
        f"The count fell to {len(found)}: lower [ratchets] ad_hoc_upstream_fake_files "
        f"in tests/budgets.toml from {ceiling} to {len(found)}.\n{listing}"
    )
