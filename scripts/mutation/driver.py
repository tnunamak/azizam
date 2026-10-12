"""Mutation engine behind scripts/mutate (docs/design/TEST_STRATEGY.md 4.5).

Runs inside the hermetic sandbox (`run-hermetic-pytest.sh --python-script`),
in the read-only checkout, and writes the JSON report to stdout.  Progress goes
to stderr.

For each module:
1. Baseline: run the test universe on clean code with the line tracer.  The
   tests that execute the module are its selection.  If any of them fails, or
   collection fails, the module is refused and gets no score.
2. Each mutant runs, with a hard time limit, against the tests that execute
   its lines in the module's own test files (narrow).  A survivor is re-run
   against every test that executes those lines (wide).  A mutant on a line
   that runs only at import uses the module's own tests, then (wide) every
   test that executes the module and quotes the mutated literal.
3. Each kill is confirmed: the killing test, run alone, fails on the mutant
   and passes on clean code.  A kill that does not reproduce alone is an
   `error`, never a kill.
4. tests/mutation/survivors.toml classifies survivors.  `equivalent` turns a
   survivor into status `equivalent`; a classified mutant that is now killed
   is reported as stale.

Score = killed / (generated - equivalent); killed counts timeouts.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutgen  # noqa: E402

PLUGIN_DIR = str(Path(__file__).resolve().parent)
# pytest's short summary: FAILED/ERROR <nodeid>, or SUBFAILED(<params>) <nodeid>.
KILLER = re.compile(r"^(?:FAILED|ERROR|SUBFAILED\(.*?\)) (\S+\.py\S*)", re.M)
COPY_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "node_modules")


class Refused(Exception):
    """The baseline is not green, so no score may be computed."""


def log(message: str) -> None:
    print(f"mutate: {message}", file=sys.stderr, flush=True)


def load_config(root: Path) -> tuple[dict, dict]:
    modules = tomllib.loads((root / "tests/mutation/modules.toml").read_text())["module"]
    survivors_path = root / "tests/mutation/survivors.toml"
    entries = tomllib.loads(survivors_path.read_text()).get("mutant", []) if survivors_path.exists() else []
    return modules, {e["id"]: e for e in entries}


class Engine:
    """One sandboxed mutation session over a checkout at `root`."""

    def __init__(self, root: Path, work: Path, *, workers: int, pytest_cmd: list[str],
                 base_env: dict[str, str] | None = None, min_limit: float = 60.0,
                 max_limit: float = 1800.0, wide: bool = True) -> None:
        self.root = root
        self.work = work
        self.workers = workers
        self.pytest_cmd = pytest_cmd
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.min_limit = min_limit
        self.max_limit = max_limit
        self.wide = wide
        self.counter = 0
        self.lock = threading.Lock()
        work.mkdir(parents=True, exist_ok=True)
        self.base = work / "base"
        shutil.copytree(root, self.base, ignore=COPY_IGNORE, symlinks=True)
        self.slots: list[Path] = []
        for index in range(workers):
            slot = work / f"w{index}"
            shutil.copytree(self.base, slot, symlinks=True)
            self.slots.append(slot)
        self.free = list(self.slots)
        self.cond = threading.Condition()

    # ---- process plumbing -------------------------------------------------

    def _env(self, checkout: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
        with self.lock:
            self.counter += 1
            state = self.work / "state" / str(self.counter)
        state.mkdir(parents=True)
        env = dict(self.base_env)
        env.pop("AZIZAM_ADMIN_DB", None)
        if "HERMETIC_TEST_STATE_DIR" in env:
            env.update(HERMETIC_TEST_STATE_DIR=str(state), HERMETIC_ADMIN_DB=str(state / "admin.db"),
                       HERMETIC_REPO_ROOT=str(checkout))
        env["PYTHONPATH"] = PLUGIN_DIR
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(extra or {})
        return env

    def pytest(self, checkout: Path, args: list[str], *, limit: float,
               extra_env: dict[str, str] | None = None) -> tuple[int | None, str]:
        """Run pytest in `checkout`; return (exit code or None on timeout, output)."""
        env = self._env(checkout, extra_env)
        proc = subprocess.Popen([*self.pytest_cmd, *args], cwd=checkout, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=limit)
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            return None, out or ""
        finally:
            shutil.rmtree(Path(env.get("HERMETIC_TEST_STATE_DIR", "/nonexistent")), ignore_errors=True)

    def run_ids(self, checkout: Path, ids: list[str], *, limit: float, stop_first: bool = True) -> tuple[int | None, str]:
        with self.lock:
            self.counter += 1
            argfile = self.work / f"args-{self.counter}.txt"
        argfile.write_text("".join(f"{i}\n" for i in ids))
        try:
            return self.pytest(checkout, [*(["-x"] if stop_first else []), "-q", "-rfE", f"@{argfile}"], limit=limit)
        finally:
            argfile.unlink(missing_ok=True)

    def acquire(self) -> Path:
        with self.cond:
            while not self.free:
                self.cond.wait()
            return self.free.pop()

    def release(self, slot: Path) -> None:
        with self.cond:
            self.free.append(slot)
            self.cond.notify()

    # ---- baseline ---------------------------------------------------------

    def baseline(self, files: list[str], universe: list[str]) -> dict:
        """Run `universe` on clean code with the tracer over `files`."""
        trace = self.work / "trace"
        shutil.rmtree(trace, ignore_errors=True)
        trace.mkdir()
        args = ["-q", "-p", "tracer", "-n", str(self.workers), *universe] if self.workers > 1 \
            else ["-q", "-p", "tracer", *universe]
        started = time.monotonic()
        rc, out = self.pytest(self.base, args, limit=self.max_limit * 4, extra_env={
            "MUTATION_TRACE_FILES": ",".join(files), "MUTATION_TRACE_OUT": str(trace)})
        merged: dict = {"lines": {f: {} for f in files}, "collect": {f: set() for f in files},
                        "outcomes": {}, "durations": {}, "collect_errors": [], "rc": rc,
                        "seconds": time.monotonic() - started, "tail": out.strip().splitlines()[-15:]}
        for part in trace.glob("*.json"):
            data = json.loads(part.read_text())
            for f, lines in data["lines"].items():
                for line, ids in lines.items():
                    merged["lines"][f].setdefault(int(line), set()).update(ids)
            for f, lines in data["collect"].items():
                merged["collect"][f].update(lines)
            merged["outcomes"].update(data["outcomes"])
            merged["durations"].update(data["durations"])
            merged["collect_errors"] += data["collect_errors"]
        if rc is None or rc not in (0, 1) or merged["collect_errors"] or not merged["outcomes"]:
            raise Refused(f"baseline run did not complete (exit {rc}; collection errors "
                          f"{merged['collect_errors'][:5]}): " + " | ".join(merged["tail"][-5:]))
        return merged

    # ---- one module ---------------------------------------------------------

    def module(self, name: str, spec: dict, base: dict, survivors: dict, only: set[str] | None) -> dict:
        path = spec["path"]
        functions = spec.get("functions")
        src, mutants = mutgen.generate(str(self.base / path), path, functions)
        if only is not None:
            mutants = [m for m in mutants if m["id"] in only]
        line_tests: dict[int, set[str]] = base["lines"][path]
        collect_lines: set[int] = base["collect"][path]
        if functions is not None:
            line_tests = self._scope_lines(src, functions, line_tests)
        selected = set().union(*line_tests.values()) if line_tests else set()
        failing = sorted(i for i in selected if base["outcomes"].get(i) == "failed")
        report = {"module": name, "path": path, "scope": spec.get("scope", "whole module"),
                  "baseline": {"selected": len(selected), "failed": failing}}
        if failing:
            report["refused"] = f"{len(failing)} selected tests fail on clean code"
            return report
        if not selected:
            report["refused"] = "no test executes this module"
            return report
        own = [re.compile(p) for p in spec.get("own_tests", [rf"^tests/test_{name}\b"])]
        primary = {i for i in selected if any(p.search(i) for p in own)}
        quoting_cache: dict[str, set[str]] = {}

        def quoting(literal: str) -> set[str]:
            if literal not in quoting_cache:
                files = {i.split("::")[0] for i in selected}
                hits = {f for f in files if literal in (self.base / f).read_text(errors="replace")}
                quoting_cache[literal] = {i for i in selected if i.split("::")[0] in hits}
            return quoting_cache[literal]

        def selection(m: dict) -> tuple[list[str], list[str]] | None:
            ids: set[str] = set()
            for line in range(m["line"], m["end_line"] + 1):
                ids |= line_tests.get(line, set())
            if ids:
                narrow = ids & primary or ids
                return sorted(narrow), sorted(ids)
            if any(line in collect_lines for line in range(m["line"], m["end_line"] + 1)):
                literal = m["old"].strip("'\"")
                wide = primary | (quoting(literal) if len(literal) >= 3 else set())
                return sorted(primary or selected), sorted(wide or selected)
            return None

        def limit_for(ids: list[str]) -> float:
            return min(self.max_limit, self.min_limit + 3 * sum(base["durations"].get(i, 1.0) for i in ids))

        def run(m: dict) -> dict:
            res = {k: m[k] for k in ("id", "line", "func", "kind", "old", "new")}
            sel = selection(m)
            if sel is None:
                res.update(status="no-cov", tests=0)
                return res
            narrow, wide = sel
            slot = self.acquire()
            target = slot / path
            started = time.monotonic()
            try:
                target.write_text(mutgen.apply(src, m))
                phases = [("narrow", narrow)]
                if self.wide and set(wide) != set(narrow):
                    phases.append(("wide", wide))
                for phase, ids in phases:
                    rc, out = self.run_ids(slot, ids, limit=limit_for(ids))
                    res.update(phase=phase, tests=len(ids))
                    if rc is None:
                        res["status"] = "timeout"
                        break
                    if rc == 0:
                        res["status"] = "survived"
                        continue
                    killers = KILLER.findall(out)
                    if rc in (1, 2) and killers:
                        res.update(status="killed", killers=killers[:3])
                        break
                    res.update(status="error", detail=f"pytest exit {rc}: " + " | ".join(out.strip().splitlines()[-3:]))
                    break
                if res.get("status") == "killed":
                    self._confirm_on_mutant(slot, res)
            finally:
                target.write_text(src)
                self.release(slot)
            res["seconds"] = round(time.monotonic() - started, 1)
            return res

        started = time.monotonic()
        results = []
        with ThreadPoolExecutor(self.workers) as pool:
            for index, res in enumerate(pool.map(run, mutants), 1):
                results.append(res)
                if index % 25 == 0 or index == len(mutants):
                    log(f"{name}: {index}/{len(mutants)} mutants, {time.monotonic() - started:.0f}s")
        self._confirm_on_clean(results)
        self._classify(results, survivors)
        report.update(self._summary(results, survivors, path))
        report["mutants"] = results
        report["seconds"] = round(time.monotonic() - started, 1)
        return report

    def _scope_lines(self, src: str, functions: list[str], line_tests: dict) -> dict:
        keep: set[int] = set()
        for node in ast.parse(src).body:
            if getattr(node, "name", None) in functions:
                keep.update(range(node.lineno, node.end_lineno + 1))
        return {n: ids for n, ids in line_tests.items() if n in keep}

    def _confirm_on_mutant(self, slot: Path, res: dict) -> None:
        """A kill counts only if the first killer, run alone, fails on the mutant."""
        killer = res["killers"][0]
        if "::" not in killer:  # collection error of a whole file: it cannot pass alone
            return
        rc, _ = self.run_ids(slot, [killer], limit=self.min_limit * 3)
        if rc == 0:
            res.update(status="error", detail=f"{killer} fails only in the combined run, not alone")

    def _confirm_on_clean(self, results: list[dict]) -> None:
        """Every killer must pass alone on clean code."""
        killers = sorted({r["killers"][0] for r in results if r.get("status") == "killed" and "::" in r["killers"][0]})

        def clean(killer: str) -> tuple[str, int | None]:
            slot = self.acquire()
            try:
                return killer, self.run_ids(slot, [killer], limit=self.min_limit * 3)[0]
            finally:
                self.release(slot)

        with ThreadPoolExecutor(self.workers) as pool:
            bad = {k for k, rc in pool.map(clean, killers) if rc != 0}
        for r in results:
            if r.get("status") == "killed" and r["killers"][0] in bad:
                r.update(status="error", detail=f"{r['killers'][0]} fails alone on clean code")

    @staticmethod
    def _classify(results: list[dict], survivors: dict) -> None:
        for r in results:
            entry = survivors.get(r["id"])
            if entry is None:
                continue
            r["class"] = entry["class"]
            r["reason"] = entry.get("reason", "")
            if entry.get("killed_by"):
                r["killed_by"] = entry["killed_by"]
            if r["status"] == "survived" and entry["class"] == "equivalent":
                r["status"] = "equivalent"

    @staticmethod
    def _summary(results: list[dict], survivors: dict, path: str) -> dict:
        counts = {s: 0 for s in ("killed", "timeout", "survived", "no-cov", "equivalent", "error")}
        for r in results:
            counts[r["status"]] += 1
        open_survivors = [r for r in results if r["status"] in ("survived", "no-cov")]
        unproductive = sum(1 for r in open_survivors if r.get("class") == "unproductive")
        unclassified = [r["id"] for r in open_survivors if "class" not in r]
        stale = [r["id"] for r in results if r.get("class") and r["status"] in ("killed", "timeout")]
        generated = len(results)
        denominator = generated - counts["equivalent"]
        detected = counts["killed"] + counts["timeout"]
        ids = {r["id"] for r in results}
        missing = sorted(i for i in survivors if i.startswith(f"{path}:") and i not in ids)
        return {"generated": generated, "counts": counts, "unproductive": unproductive,
                "score": round(detected / denominator, 4) if denominator else None,
                "unclassified": unclassified, "stale_classifications": stale,
                "classifications_without_mutant": missing}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="scripts/mutate")
    ap.add_argument("modules", nargs="+", help="names from tests/mutation/modules.toml, or 'all'")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--ids", help="file with mutant IDs to run (one per line); others are skipped")
    ap.add_argument("--tests", nargs="*", default=None, help="test paths for the baseline (default: all)")
    ap.add_argument("--no-wide", action="store_true", help="skip the wide re-run of survivors")
    ap.add_argument("--min-limit", type=float, default=60.0, help="per-mutant time limit floor, seconds")
    ap.add_argument("--max-limit", type=float, default=1800.0, help="per-mutant time limit ceiling, seconds")
    ap.add_argument("--sha", default="")
    a = ap.parse_args(argv)
    root = Path.cwd()
    modules, survivors = load_config(root)
    names = list(modules) if a.modules == ["all"] else a.modules
    unknown = [n for n in names if n not in modules]
    if unknown:
        log(f"unknown modules {unknown}; known: {sorted(modules)}")
        return 2
    only = None
    if a.ids:
        only = {line.strip() for line in Path(a.ids).read_text().splitlines() if line.strip()}
    work = Path(os.environ["HERMETIC_STATE_DIR"]) / "tmp" / "mutate"
    engine = Engine(root, work, workers=a.workers, pytest_cmd=[sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "xdist"],
                    min_limit=a.min_limit, max_limit=a.max_limit, wide=not a.no_wide)
    report = {"tool": "scripts/mutate", "sha": a.sha, "workers": a.workers, "modules": []}
    files = [modules[n]["path"] for n in names]
    log(f"baseline over {files} ({'all tests' if a.tests is None else a.tests})")
    try:
        base = engine.baseline(files, [] if a.tests is None else a.tests)
    except Refused as error:
        report["refused"] = str(error)
        print(json.dumps(report, indent=1))
        log(f"REFUSED: {error}")
        return 3
    report["baseline"] = {"seconds": round(base["seconds"], 1), "tests": len(base["outcomes"]),
                          "failed_anywhere": sorted(i for i, o in base["outcomes"].items() if o == "failed")}
    rc = 0
    for name in names:
        log(f"{name}: generating and running mutants")
        result = engine.module(name, modules[name], base, survivors, only)
        report["modules"].append(result)
        if "refused" in result:
            log(f"{name}: REFUSED: {result['refused']} {result['baseline']['failed'][:5]}")
            rc = 3
        else:
            log(f"{name}: {result['counts']} score={result['score']}")
            if result["counts"]["error"]:
                rc = rc or 4
    print(json.dumps(report, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
