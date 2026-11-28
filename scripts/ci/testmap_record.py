"""Record per-test repository dependencies for diff-selected pytest runs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import pytest


def _entry() -> dict:
    return {"functions": {}, "opened": set(), "listed": set(), "subprocess": False}


def _json_entry(entry: dict) -> dict:
    return {
        "functions": {path: sorted(names) for path, names in sorted(entry["functions"].items())},
        "opened": sorted(entry["opened"]),
        "listed": sorted(entry["listed"]),
        "subprocess": entry["subprocess"],
    }


# Imports, pytest's collection and assertion rewriting and traceback source lookups open and list files too. Those
# reads are not dependencies (the function record covers code that ran). A read counts only when the nearest caller
# that is either such machinery or repository code is repository code, as in a test that reads a file as text.
_MACHINERY = ("<frozen importlib", "/_pytest/", "/linecache.py", "/tokenize.py", "/pluggy/", "/xdist/", "/execnet/")


def _machinery_read(root: str) -> bool:
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if any(part in name for part in _MACHINERY):
            return True
        if name.startswith(root):
            return False
        frame = frame.f_back
    return True


class Recorder:
    def __init__(self, root: Path, out: Path):
        self.root = root.resolve()
        self.root_prefix = str(self.root) + os.sep
        self.out = out
        self.owner = "session"
        self.session = _entry()
        self.tests: dict[str, dict] = {}
        self.tool = None

    def entry(self):
        return self.session if self.owner == "session" else self.tests.setdefault(self.owner, _entry())

    def repo_path(self, value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        try:
            path = Path(os.fsdecode(value)).resolve()
            rel = path.relative_to(self.root)
        except (OSError, ValueError, TypeError):
            return None
        if any(part in {".git", "site-packages", "dist-packages"} for part in rel.parts):
            return None
        if any(part in {"venv", ".venv"} for part in rel.parts):
            return None
        return rel.as_posix() or "."

    def on_start(self, code, offset):
        if code.co_filename.startswith("<"):  # frozen modules, exec'd strings
            return sys.monitoring.DISABLE
        name = self.repo_path(code.co_filename)
        if name is not None:
            self.entry()["functions"].setdefault(name, set()).add(code.co_qualname)
        return sys.monitoring.DISABLE

    def audit(self, event, args):
        if event == "open":
            rel = self.repo_path(args[0]) if args else None
            if rel and not rel.endswith(".pyc") and not _machinery_read(self.root_prefix):
                self.entry()["opened"].add(rel)
        elif event in ("os.listdir", "os.scandir"):
            rel = self.repo_path(args[0] if args else ".")
            if rel is not None and not _machinery_read(self.root_prefix):
                self.entry()["listed"].add(rel)
        elif event == "subprocess.Popen":
            self.entry()["subprocess"] = True

    def start(self, config):
        for candidate in (3, 4):
            try:
                sys.monitoring.use_tool_id(candidate, "testmap_record")
                self.tool = candidate
                break
            except ValueError:
                continue
        if self.tool is None:
            raise RuntimeError("no free sys.monitoring tool id (3 or 4)")
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.PY_START, self.on_start)
        sys.monitoring.set_events(self.tool, sys.monitoring.events.PY_START)
        sys.addaudithook(self.audit)

    def set_owner(self, owner):
        if owner != self.owner:
            self.owner = owner
            sys.monitoring.restart_events()

    def finish(self):
        self.out.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "session": _json_entry(self.session),
                "tests": {name: _json_entry(entry) for name, entry in sorted(self.tests.items())}}
        target = self.out / f"shard-{os.getpid()}.json"
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, sort_keys=True) + "\n")
        temp.replace(target)


def pytest_configure(config):
    path = os.environ.get("TEST_MAP_DIR")
    if path:
        recorder = Recorder(Path(str(config.rootpath)), Path(path))
        config._testmap_recorder = recorder
        recorder.start(config)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    recorder = getattr(collector.config, "_testmap_recorder", None)
    if recorder is not None and isinstance(collector, pytest.Module):
        previous = recorder.owner
        recorder.set_owner(recorder.repo_path(collector.path) or "session")
        yield
        recorder.set_owner(previous)
    else:
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item):
    recorder = getattr(item.config, "_testmap_recorder", None)
    if recorder is not None:
        previous = recorder.owner
        recorder.set_owner(recorder.repo_path(item.path) or "session")
        yield
        recorder.set_owner(previous)
    else:
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    recorder = getattr(request.config, "_testmap_recorder", None)
    if recorder is None or fixturedef.scope not in {"session", "package"}:
        yield
        return
    previous = recorder.owner
    recorder.set_owner("session")
    yield
    recorder.set_owner(previous)
    finalizers = fixturedef._finalizers
    if finalizers:
        original = finalizers[-1]

        def finalize_in_session():
            prior = recorder.owner
            recorder.set_owner("session")
            try:
                return original()
            finally:
                recorder.set_owner(prior)

        finalizers[-1] = finalize_in_session


def pytest_sessionfinish(session, exitstatus):
    recorder = getattr(session.config, "_testmap_recorder", None)
    if recorder is not None:
        recorder.finish()
        sys.monitoring.set_events(recorder.tool, 0)
        sys.monitoring.free_tool_id(recorder.tool)


def _merge(directory: Path, base: str, output: Path):
    shards = sorted(directory.glob("shard-*.json"))
    if not shards:
        raise SystemExit("no shard-*.json files")
    session = _entry()
    tests = {}
    for shard in shards:
        data = json.loads(shard.read_text())
        for key, dest in [("session", session)]:
            _merge_entry(dest, data[key])
        for name, source in data["tests"].items():
            _merge_entry(tests.setdefault(name, _entry()), source)
    result = {"version": 1, "base": base, "session": _json_entry(session),
              "tests": {name: _json_entry(value) for name, value in sorted(tests.items())}}
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    temp.write_text(json.dumps(result, sort_keys=True) + "\n")
    temp.replace(output)


def _merge_entry(dest, source):
    for path, names in source["functions"].items():
        dest["functions"].setdefault(path, set()).update(names)
    dest["opened"].update(source["opened"])
    dest["listed"].update(source["listed"])
    dest["subprocess"] |= source["subprocess"]


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    merge = sub.add_parser("merge")
    merge.add_argument("--base", required=True)
    merge.add_argument("--out", required=True, type=Path)
    merge.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "merge":
        _merge(args.directory, args.base, args.out)


if __name__ == "__main__":
    main()
