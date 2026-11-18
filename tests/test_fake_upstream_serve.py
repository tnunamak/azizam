"""In-process lifecycle checks for the scheduled loopback script."""
import importlib.util
import io
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.support.fake_upstream.scenario import PROTOCOLS, scenario_from_data


@pytest.fixture
def script():
    path = Path(__file__).resolve().parents[1] / "scripts/scheduled/fake_upstream_serve.py"
    spec = importlib.util.spec_from_file_location("scheduled_fake_upstream_serve", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scenario_path(tmp_path):
    data = {
        "protocol": next(iter(PROTOCOLS)),
        "name": "serve-lifecycle",
        "provenance": {"source": "hand-written", "reference": "in-process lifecycle test"},
        "exchanges": [],
    }
    scenario_from_data(data)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running(script, scenario_path, problems_path, port=None, stopped=False):
    stop = threading.Event()
    if stopped:
        stop.set()
    out = io.StringIO()
    results, errors = [], []
    port = free_port() if port is None else port

    def target():
        try:
            results.append(script.run(port, scenario_path, problems_path, stop, out))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        yield stop, out, results, errors, thread, port
    finally:
        stop.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "run did not stop within 10 seconds"
        if errors:
            raise errors[0]


def await_port(out, thread, port):
    deadline = time.monotonic() + 10
    while f"PORT {port}\n" not in out.getvalue():
        assert thread.is_alive(), "run exited before announcing its port"
        assert time.monotonic() < deadline, "no PORT line within 10 seconds"
        time.sleep(0.01)


def test_bind_failure_has_no_port_line(script, scenario_path, tmp_path, capsys):
    problems = tmp_path / "problems.json"
    problems.write_text("[]", encoding="utf-8")
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        with running(script, scenario_path, problems, held.getsockname()[1], stopped=True) as state:
            _, out, results, _, thread, _ = state
            thread.join(timeout=10)
        assert results == [1]
        assert "PORT " not in out.getvalue()
        assert not problems.exists()
    assert capsys.readouterr().err.strip()


def test_stale_problems_removed_before_port(script, scenario_path, tmp_path):
    problems = tmp_path / "problems.json"
    problems.write_text("[]", encoding="utf-8")
    with running(script, scenario_path, problems) as state:
        _, out, _, _, thread, port = state
        await_port(out, thread, port)
        assert not problems.exists()


def test_stop_writes_valid_problem_list(script, scenario_path, tmp_path):
    problems = tmp_path / "problems.json"
    with running(script, scenario_path, problems) as state:
        stop, out, results, _, thread, port = state
        await_port(out, thread, port)
        stop.set()
    assert results == [0]
    assert isinstance(json.loads(problems.read_text(encoding="utf-8")), list)


def test_problem_write_uses_same_directory_replace(script, scenario_path, tmp_path, monkeypatch):
    problems = tmp_path / "problems.json"
    replacements = []
    real_replace = script.os.replace

    def record_replace(source, target):
        source, target = Path(source), Path(target)
        assert source != target
        assert source.parent == target.parent == problems.parent
        assert not target.exists()
        assert isinstance(json.loads(source.read_text(encoding="utf-8")), list)
        replacements.append((source, target))
        return real_replace(source, target)

    monkeypatch.setattr(script.os, "replace", record_replace)
    with running(script, scenario_path, problems) as state:
        _, out, results, _, thread, port = state
        await_port(out, thread, port)
    assert results == [0]
    assert len(replacements) == 1
    assert replacements[0][1] == problems
    assert not replacements[0][0].exists()
    assert isinstance(json.loads(problems.read_text(encoding="utf-8")), list)
