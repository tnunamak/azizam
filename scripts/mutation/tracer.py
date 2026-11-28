"""pytest plugin for scripts/mutate: which tests execute which lines.

Load with `-p tracer` (scripts/mutation on PYTHONPATH).  MUTATION_TRACE_FILES
lists the traced source files (repo-relative, comma-separated), and
MUTATION_TRACE_OUT names a directory.  At session end each process (each
xdist worker, or the single process) writes one JSON file there:

    {"lines": {path: {line: [nodeid, ...]}},   lines each test executed
     "collect": {path: [line, ...]},          lines run outside any test
     "outcomes": {nodeid: "passed" | "failed" | "skipped"},
     "durations": {nodeid: seconds},
     "collect_errors": [nodeid, ...]}

It uses sys.monitoring (Python 3.12), so it needs no coverage package and
costs one callback per line location per test: each location is disabled
after its first event and re-enabled when the next test starts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_monitoring = sys.monitoring
_TOOL = _monitoring.COVERAGE_ID


class _Tracer:
    def __init__(self, files: list[str]) -> None:
        root = Path.cwd()
        self.paths = {str(root / f): f for f in files}
        self.paths.update({os.path.realpath(root / f): f for f in files})
        self.current: str | None = None
        self.lines: dict[str, dict[int, set[str]]] = {f: {} for f in files}
        self.collect: dict[str, set[int]] = {f: set() for f in files}
        self.outcomes: dict[str, str] = {}
        self.durations: dict[str, float] = {}
        self.collect_errors: list[str] = []

    def on_line(self, code, line):
        path = self.paths.get(code.co_filename)
        if path is not None:
            if self.current is None:
                self.collect[path].add(line)
            else:
                self.lines[path].setdefault(line, set()).add(self.current)
        return _monitoring.DISABLE

    def start(self) -> None:
        _monitoring.use_tool_id(_TOOL, "azizam-mutation-tracer")
        _monitoring.register_callback(_TOOL, _monitoring.events.LINE, self.on_line)
        _monitoring.set_events(_TOOL, _monitoring.events.LINE)

    def stop(self) -> None:
        _monitoring.set_events(_TOOL, 0)
        _monitoring.register_callback(_TOOL, _monitoring.events.LINE, None)
        _monitoring.free_tool_id(_TOOL)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.current = item.nodeid
        _monitoring.restart_events()
        yield
        self.current = None
        _monitoring.restart_events()

    def pytest_runtest_logreport(self, report):
        self.durations[report.nodeid] = self.durations.get(report.nodeid, 0.0) + report.duration
        if report.failed:
            self.outcomes[report.nodeid] = "failed"
        elif report.skipped and report.when != "teardown":
            self.outcomes.setdefault(report.nodeid, "skipped")
        elif report.when == "call":
            self.outcomes.setdefault(report.nodeid, "passed")

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors.append(report.nodeid)

    def write(self, out: Path, name: str) -> None:
        data = {
            "lines": {f: {str(n): sorted(ids) for n, ids in lines.items()} for f, lines in self.lines.items()},
            "collect": {f: sorted(lines) for f, lines in self.collect.items()},
            "outcomes": self.outcomes,
            "durations": self.durations,
            "collect_errors": self.collect_errors,
        }
        (out / f"{name}.json").write_text(json.dumps(data))


_tracer: _Tracer | None = None


def pytest_configure(config):
    global _tracer
    files = [f for f in os.environ.get("MUTATION_TRACE_FILES", "").split(",") if f]
    if not files or (config.getoption("numprocesses", None) and not hasattr(config, "workerinput")):
        return  # xdist controller: the workers trace
    _tracer = _Tracer(files)
    _tracer.start()
    config.pluginmanager.register(_tracer, "azizam-mutation-tracer")


def pytest_unconfigure(config):
    global _tracer
    if _tracer is None:
        return
    _tracer.stop()
    worker = getattr(config, "workerinput", {}).get("workerid", "main")
    _tracer.write(Path(os.environ["MUTATION_TRACE_OUT"]), worker)
    _tracer = None
