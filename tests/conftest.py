"""Keep pytest's gateway imports out of an operator's persistent state."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest


# Set AI_GATEWAY_TEST_STATE_DIR to retain pytest state somewhere else.  The
# default stays disk-backed rather than using the RAM-backed /tmp filesystem.
_STATE_ROOT = Path(os.environ.get("AI_GATEWAY_TEST_STATE_DIR", Path.home() / ".tmp"))
_STATE_ROOT.mkdir(parents=True, exist_ok=True)
_STATE_DIR = Path(tempfile.mkdtemp(prefix="vivid-fish-pytest-", dir=_STATE_ROOT))
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


def _cleanup_state_dir() -> None:
    shutil.rmtree(_STATE_DIR, ignore_errors=True)


atexit.register(_cleanup_state_dir)


@pytest.fixture(scope="session")
def gateway_test_state_dir() -> Path:
    return _STATE_DIR


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _cleanup_state_dir()
