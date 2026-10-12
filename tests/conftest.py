"""Keep pytest's gateway imports out of an operator's persistent state."""

from __future__ import annotations

import os
import socket
import tempfile
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
def host_disk_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A private directory on the host disk, for the few tests whose subject is
    a real-disk property (fsync, free space, a device boundary).  `tmp_path` is
    RAM under the hermetic runner (docs/design/TEST_STRATEGY.md 4.4)."""
    root = os.environ.get("HERMETIC_HOST_DISK_DIR")
    if not root:
        pytest.fail("host_disk_dir needs the hermetic runner: HERMETIC_HOST_DISK_DIR is unset")
    return Path(tempfile.mkdtemp(prefix="case-", dir=root))


@pytest.fixture
def test_owned_tcp_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        yield listener
    finally:
        listener.close()


# Hypothesis profiles (docs/design/TEST_STRATEGY.md 4.3). The hermetic runner
# forwards HYPOTHESIS_PROFILE into the sandbox; the gate uses "gate". Both
# profiles derandomize, so a run at one commit always tries the same examples
# (a property failure is never a flake), and keep no example database:
# shrunk counterexamples live in the test file as explicit @example cases.
os.environ.setdefault("HYPOTHESIS_STORAGE_DIRECTORY", str(_STATE_DIR / "hypothesis"))
from hypothesis import HealthCheck, settings as _hypothesis_settings  # noqa: E402

_HYPOTHESIS_COMMON = dict(derandomize=True, database=None, deadline=None,
                          suppress_health_check=(HealthCheck.too_slow,))
_hypothesis_settings.register_profile("gate", max_examples=100, **_HYPOTHESIS_COMMON)
_hypothesis_settings.register_profile("nightly", max_examples=2000, **_HYPOTHESIS_COMMON)
_HYPOTHESIS_PROFILE = os.environ.get("HYPOTHESIS_PROFILE", "gate")
if _HYPOTHESIS_PROFILE not in {"gate", "nightly"}:
    pytest.exit(f"unknown HYPOTHESIS_PROFILE {_HYPOTHESIS_PROFILE!r}; use gate or nightly", returncode=2)
_hypothesis_settings.load_profile(_HYPOTHESIS_PROFILE)
