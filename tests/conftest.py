"""Keep pytest's gateway imports out of an operator's persistent state."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from _hermetic import HermeticGuard, assert_hermetic_environment


# The runner creates exactly one unique, disk-backed state directory before
# pytest collection.  Refusing a direct pytest invocation prevents import-time
# gateway stores from falling back to a real operator database.
if os.environ.get("VIVID_FISH_HERMETIC_TEST_NAMESPACE") != "1":
    pytest.exit("pytest must run through scripts/run-hermetic-pytest.sh", returncode=2)
_STATE_DIR = Path(os.environ["AI_GATEWAY_TEST_STATE_DIR"])
_STORE_PATHS = {
    "AI_GATEWAY_ADMIN_DB": "admin.db",
    "AI_GATEWAY_METRICS_DB": "metrics.db",
    "AI_GATEWAY_RESPONSES_DB": "responses.db",
    "AI_GATEWAY_RECEIPTS_DB": "receipts.db",
    "AI_GATEWAY_MEDIA_JOBS_DB": "media_jobs.db",
}

# Test modules import proxy during collection.  These must be set while pytest
# imports this conftest, before that collection-time import can initialize or
# mutate the operator's default data/*.db files.
for _name, _filename in _STORE_PATHS.items():
    os.environ[_name] = str(_STATE_DIR / _filename)

_GUARD = HermeticGuard()


def pytest_sessionstart(session: pytest.Session) -> None:
    assert_hermetic_environment()


def pytest_configure(config: pytest.Config) -> None:
    _GUARD.install()


def pytest_unconfigure(config: pytest.Config) -> None:
    _GUARD.restore()


@pytest.fixture(autouse=True)
def _no_comfy_object_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting a ComfyUI prompt derives the embedded UI workflow from
    ComfyUI's /object_info. Tests must not reach for a live ComfyUI, so the
    lookup reports "unavailable" unless a test patches it."""
    from gateway import comfy_object_info

    monkeypatch.setattr(comfy_object_info, "lookup", lambda _host, _class_type: None)


@pytest.fixture(scope="session")
def gateway_test_state_dir() -> Path:
    return _STATE_DIR


@pytest.fixture
def test_owned_tcp_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        yield listener
    finally:
        listener.close()
