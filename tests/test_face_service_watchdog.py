"""Idle accounting tests without loading models or starting the watchdog."""
import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def server(monkeypatch):
    core = types.ModuleType("facecore")
    core.KNOWN_MODELS = ("fixture",)
    core.ModelRootMissing = type("ModelRootMissing", (Exception,), {})
    core.analyze = lambda *args: []
    monkeypatch.setitem(sys.modules, "facecore", core)
    try:
        import numpy  # noqa: F401
    except ImportError:
        # The gateway suite has no numpy; the server only hands bytes to cv2.
        np_stub = types.ModuleType("numpy")
        np_stub.uint8 = "uint8"
        np_stub.frombuffer = lambda data, dtype: data
        monkeypatch.setitem(sys.modules, "numpy", np_stub)
    try:
        import cv2  # noqa: F401
    except ImportError:
        stub = types.ModuleType("cv2")
        stub.IMREAD_COLOR = 1
        stub.imdecode = lambda *args: None
        monkeypatch.setitem(sys.modules, "cv2", stub)
    path = Path(__file__).resolve().parents[1] / "services" / "face-service" / "server.py"
    spec = importlib.util.spec_from_file_location("watchdog_test_server", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setattr(sys, "argv", [str(path)])
    # Import creates a watchdog. Do not launch it; restore Thread.start before
    # TestClient creates its portal, so request execution uses real threads.
    with monkeypatch.context() as importing:
        importing.setattr(threading.Thread, "start", lambda self: None)
        spec.loader.exec_module(module)
    return module


def test_active_request_prevents_idle_exit(server):
    assert server.idle_exit_due(1000, 0, 1, 10) is False


def test_expired_idle_exits(server):
    assert server.idle_exit_due(1000, 0, 0, 10) is True


def test_recent_idle_does_not_exit(server):
    assert server.idle_exit_due(5, 0, 0, 10) is False


def test_zero_disables_idle_exit(server):
    assert server.idle_exit_due(1000, 0, 0, 0) is False


def test_analyze_failure_releases_active_request(server, monkeypatch):
    failure = RuntimeError("fixture analysis failed")
    calls = []
    def failing_analysis(*args):
        assert server._active_requests == 1
        calls.append(args)
        raise failure
    monkeypatch.setattr(server, "analyze_faces", failing_analysis)
    monkeypatch.setattr(server.cv2, "imdecode", lambda *args: types.SimpleNamespace(shape=(2, 3, 3)))
    before = server.time.monotonic()
    with TestClient(server.app, raise_server_exceptions=False) as client:
        response = client.post("/v1/faces/analyze", files={"image": ("input.bin", b"x")},
                               data={"model": "fixture"})
    assert response.status_code == 500
    assert len(calls) == 1
    assert server._active_requests == 0
    assert server._last_used >= before
