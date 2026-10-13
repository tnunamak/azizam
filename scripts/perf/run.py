#!/usr/bin/env python3
"""Run the real-data performance harness end to end and judge it against budgets.toml.

    scripts/perf/run.py [--port 18740] [--out DIR] [--no-ui] [--counts-only] [--open-issue]

1. copies the live databases and media (read-only) into a fresh state directory under ~/.tmp
   and starts an instrumented gateway on PORT (and PORT+1 for the fake engine);
2. times every GET route (api_bench.py) and, unless --no-ui, loads every page (ui_bench.mjs);
3. stops the gateway, ranks what exceeds a budget (check.py) and writes result.json and
   report.md to --out;
4. with --open-issue, opens (or comments on) the open GitHub issue labelled `perf-red`.

Exit 0: within budget. 1: over budget. 2: the harness could not run (nothing was judged).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import api_bench  # noqa: E402
import check  # noqa: E402

LABEL = "perf-red"
TOP_IN_ISSUE = 15


def start_fixture(python: str, port: int, state: Path, source: Path, log: Path) -> tuple[subprocess.Popen, dict]:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    process = subprocess.Popen(
        [python, str(HERE / "serve.py"), "--port", str(port), "--state", str(state), "--source", str(source)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=log.open("ab"), text=True, start_new_session=True)
    line = process.stdout.readline()  # one JSON line when the gateway answers /health
    if not line.strip().startswith("{"):
        stop(process)
        raise RuntimeError(f"the fixture did not start; see {log}")
    return process, json.loads(line)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=20)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def report(violations: list[dict], sha: str, load: float) -> str:
    lines = [f"Performance budget check at {sha[:10]}, 1-minute load {load:.1f}.", ""]
    lines.append(check.table(violations) if violations else "Within budget.")
    return "\n".join(lines) + "\n"


def issue_body(violations: list[dict], sha: str, load: float, out: Path) -> str:
    shown = sorted(violations, key=lambda v: -v["over"])[:TOP_IN_ISSUE]
    more = f"\n\n{len(violations) - len(shown)} more are in the report." if len(violations) > len(shown) else ""
    return (f"{len(violations)} measurement(s) exceed `scripts/perf/budgets.toml` at `{sha[:10]}` "
            f"(1-minute load {load:.1f}).\n\n{check.table(shown)}{more}\n\n"
            f"Report and raw results: `{out}`\n\nReproduce: `scripts/perf/run.py`. "
            "Fix the code, not the budget; a route that cannot meet its limit yet needs an entry in "
            "`[api.routes]` or `[ui.pages]` with a reason.\n")


def open_issue(violations: list[dict], sha: str, load: float, out: Path, gh=None) -> str:
    """Open the perf-red issue, or comment on the open one. Returns what was done."""
    gh = gh or (lambda *args: subprocess.run(["gh", *args], capture_output=True, text=True))
    gh("label", "create", LABEL, "--color", "B60205", "--description", "A performance budget is exceeded")
    listing = gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "number", "--jq", ".[].number")
    body = issue_body(violations, sha, load, out)
    body_file = out / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")
    open_numbers = (listing.stdout or "").split()
    if open_numbers:
        gh("issue", "comment", open_numbers[0], "--body-file", str(body_file))
        return f"commented on #{open_numbers[0]}"
    gh("issue", "create", "--label", LABEL, "--title",
       f"perf-red: {len(violations)} measurement(s) over budget at {sha[:10]}", "--body-file", str(body_file))
    return "opened a new issue"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18740)
    parser.add_argument("--source", type=Path, default=Path.home() / "applications" / "azizam")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--python", default=str((REPO / "venv" / "bin" / "python") if (REPO / "venv/bin/python").exists() else sys.executable))
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--counts-only", action="store_true")
    parser.add_argument("--open-issue", action="store_true")
    args = parser.parse_args()
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    scratch = Path(os.environ.get("AZIZAM_CI_SCRATCH", Path.home() / ".tmp"))
    out = args.out or scratch / "azizam-perf-runs" / stamp
    out.mkdir(parents=True, exist_ok=True)
    state = scratch / f"azizam-perf-run-{args.port}"
    shutil.rmtree(state, ignore_errors=True)
    state.mkdir(parents=True)
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    load = os.getloadavg()[0]
    budgets = tomllib.loads((HERE / "budgets.toml").read_text(encoding="utf-8"))
    process = None
    try:
        process, manifest = start_fixture(args.python, args.port, state, args.source, out / "fixture.log")
        api = api_bench.run(manifest)
        (out / "api.json").write_text(json.dumps(api, indent=1), encoding="utf-8")
        ui = None
        if not args.no_ui:
            ui_file = out / "ui.json"
            done = subprocess.run(
                ["node", str(HERE / "ui_bench.mjs"), "--manifest", str(state / "perf-manifest.json"), "--out", str(ui_file)],
                cwd=REPO / "web", capture_output=True, text=True)
            (out / "ui.log").write_text(done.stdout + done.stderr, encoding="utf-8")
            if done.returncode:
                raise RuntimeError(f"ui_bench failed; see {out}/ui.log")
            ui = json.loads(ui_file.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001 - a harness failure is not a verdict on the code
        print(f"perf harness could not run: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    finally:
        if process is not None:
            stop(process)
        shutil.rmtree(state, ignore_errors=True)
    violations = check.api_violations(api["rows"], budgets["api"], args.counts_only)
    if ui is not None:
        violations += check.ui_violations(ui["rows"], budgets["ui"], args.counts_only)
    text = report(violations, sha, load)
    (out / "report.md").write_text(text, encoding="utf-8")
    (out / "result.json").write_text(json.dumps({"sha": sha, "load_1min": load, "violations": violations}, indent=1), encoding="utf-8")
    print(text)
    if violations and args.open_issue:
        print("issue:", open_issue(violations, sha, load, out))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
