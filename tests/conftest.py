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
if os.environ.get("HERMETIC_TEST_NAMESPACE") != "1":
    pytest.exit("pytest must run through scripts/run-hermetic-pytest.sh", returncode=2)
_STATE_DIR = Path(os.environ["HERMETIC_TEST_STATE_DIR"])

# These production overrides must be absent before we install test-owned paths.
# The runner supplies the admin path through its neutral, private contract.
# pytest-xdist workers inherit the controller's test-owned value, which is the
# runner's own admin path and so cannot name an operator database.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
for _name in ("AZIZAM_ADMIN_DB",):
    if _name in os.environ and not (_WORKER and os.environ[_name] == os.environ["HERMETIC_ADMIN_DB"]):
        pytest.exit(f"production environment variable leaked into pytest: {_name}", returncode=2)
# Workers share the run's namespace, so each owns a private subdirectory for
# its import-time stores.  Serial runs keep the run root.
if _WORKER:
    if Path(os.environ["HERMETIC_ADMIN_DB"]) != _STATE_DIR / "admin.db":
        pytest.exit("pytest admin database must belong to this run's state directory", returncode=2)
    _STATE_DIR = _STATE_DIR / f"xdist-{_WORKER}"
    _STATE_DIR.mkdir(exist_ok=True)
    os.environ["HERMETIC_TEST_STATE_DIR"] = str(_STATE_DIR)
    os.environ["HERMETIC_ADMIN_DB"] = str(_STATE_DIR / "admin.db")
_ADMIN_DB = Path(os.environ["HERMETIC_ADMIN_DB"])
if _ADMIN_DB != _STATE_DIR / "admin.db":
    pytest.exit("pytest admin database must belong to this run's state directory", returncode=2)
_STORE_PATHS = {
    "AZIZAM_ADMIN_DB": "admin.db",
    "AZIZAM_METRICS_DB": "metrics.db",
    "AZIZAM_RESPONSES_DB": "responses.db",
    "AZIZAM_RECEIPTS_DB": "receipts.db",
    "AZIZAM_MEDIA_JOBS_DB": "media_jobs.db",
    "AZIZAM_KEY_ACTIVITY_LOG": "key_activity.jsonl",
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
