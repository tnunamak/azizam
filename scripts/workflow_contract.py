#!/usr/bin/env python3
"""Check or convert workflow documents (CODE_TO_DATA_S3, D3 and D12). Owners, package authors and agents use this one command.

    python scripts/workflow_contract.py check PATH... [--json]
    python scripts/workflow_contract.py convert FILE [-o OUTFILE]
    python scripts/workflow_contract.py diff-effective --before GIT_REF --after WORK_TREE [--json]

``check`` reads each file (a directory means every ``*.yml`` and ``*.yaml`` in it). A schema 1 document is converted in
memory first and the result is checked as schema 2; a schema 2 document is checked as it is. It prints every error with its
path and code. Exit status: 0 when every document passes, 1 when one does not, 2 for a file that cannot be read.

``convert`` prints the schema 2 form of one schema 1 file (to ``-o OUTFILE`` if given; an existing file is never overwritten)
and the list of edits to standard error. It does not check the result; run ``check`` on it.

``diff-effective`` is the migration's equality check: it compares what the code at GIT_REF makes of its bundled workflows
with what the code in WORK_TREE makes of its own (projections, compiled engine requests for a fixed input corpus with
generated values fixed, binding content against the converter's edits, services and protocol descriptions) and prints each
difference, with its reason when ``scripts/workflow_equality_allowed.yml`` names it. Exit status: 0 when every difference is
named, 1 when one is not.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.workflow_convert import convert_1_to_2, convert_and_check  # noqa: E402
from gateway.workflow_equality import diff_effective  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
# What the code of a ref needs to answer: its packages, its data, its top-level modules.
REF_PATHS = ("gateway", "registry", "manifests", "workflows", "extensions")


def _files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        found.extend(sorted([*path.glob("*.yml"), *path.glob("*.yaml")]) if path.is_dir() else [path])
    return found


def _load(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} must hold a mapping")
    return document


def run_check(paths: list[str], as_json: bool) -> int:
    status = 0
    results = []
    for path in _files(paths):
        try:
            document = _load(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            print(f"{path}: cannot read: {error}", file=sys.stderr)
            return 2
        converted, notes, report = convert_and_check(document)
        results.append({"file": str(path), "converted": bool(notes), "errors": report.as_list()})
        if not report.ok:
            status = 1
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            how = "converted from schema 1, " if result["converted"] else ""
            print(f"{result['file']}: {how}{len(result['errors'])} errors")
            for error in result["errors"]:
                print(f"  {error['pointer'] or '/'}  {error['code']}  {error['message']}")
    return status


def run_convert(file: str, out: str | None) -> int:
    try:
        document = _load(Path(file))
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"{file}: cannot read: {error}", file=sys.stderr)
        return 2
    converted, notes = convert_1_to_2(document)
    text = yaml.safe_dump(converted, sort_keys=False, allow_unicode=True)
    if out:
        target = Path(out)
        if target.exists():
            print(f"{out} already exists; it is not overwritten", file=sys.stderr)
            return 2
        target.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for note in notes:
        print(f"{'.'.join(map(str, note.path))}: {note.operation}: {note.reason}", file=sys.stderr)
    return 0


def extract_ref(ref: str, into: Path) -> Path:
    """The code and data of a git ref, in a new directory under ``into``."""
    names = subprocess.run(["git", "-C", str(REPO), "ls-tree", "--name-only", ref], capture_output=True, text=True, check=True).stdout.split()
    wanted = [name for name in names if name in REF_PATHS or (name.endswith(".py") and "/" not in name)]
    target = into / "before"
    target.mkdir()
    archive = subprocess.Popen(["git", "-C", str(REPO), "archive", ref, "--", *wanted], stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(target)], stdin=archive.stdout, check=True)
    if archive.wait() != 0:
        raise RuntimeError(f"git archive {ref} failed")
    return target


def run_diff_effective(before: str, after: str, as_json: bool) -> int:
    scratch = Path(tempfile.mkdtemp(prefix="effective-"))
    try:
        result = diff_effective(extract_ref(before, scratch), Path(after).resolve(), scratch=scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if as_json:
        print(json.dumps({"ok": result.ok, "compared": result.compared,
                          "unexplained": [d.__dict__ for d in result.unexplained],
                          "explained": [{**d.__dict__, "reason": reason} for d, reason in result.explained]}, indent=2, default=str))
        return 0 if result.ok else 1
    print(f"compared: {', '.join(f'{name} {count}' for name, count in result.compared.items())}")
    reasons: dict[str, list[str]] = {}
    for diff, reason in result.explained:
        reasons.setdefault(reason, []).append(diff.workflow)
    for reason, workflows in reasons.items():
        print(f"named difference x{len(workflows)} ({len(set(workflows))} workflows): {reason}")
    for diff in result.unexplained:
        print(f"UNEXPLAINED {diff.line()}")
    print("PASS: every difference is named" if result.ok else f"FAIL: {len(result.unexplained)} differences are not named")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check documents against the schema 2 contract")
    check.add_argument("paths", nargs="+")
    check.add_argument("--json", action="store_true")
    convert = commands.add_parser("convert", help="print the schema 2 form of a schema 1 document")
    convert.add_argument("file")
    convert.add_argument("-o", "--out")
    effective = commands.add_parser("diff-effective", help="compare the effective registry of a git ref and a work tree")
    effective.add_argument("--before", required=True, help="a git ref")
    effective.add_argument("--after", required=True, help="a work tree")
    effective.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "diff-effective":
        return run_diff_effective(args.before, args.after, args.json)
    if args.command == "check":
        return run_check(args.paths, args.json)
    return run_convert(args.file, args.out)


if __name__ == "__main__":
    sys.exit(main())
