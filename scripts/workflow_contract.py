#!/usr/bin/env python3
"""Check or convert workflow documents (CODE_TO_DATA_S3, D3 and D12). Owners, package authors and agents use this one command.

    python scripts/workflow_contract.py check PATH... [--json]
    python scripts/workflow_contract.py convert FILE [-o OUTFILE]

``check`` reads each file (a directory means every ``*.yml`` and ``*.yaml`` in it). A schema 1 document is converted in
memory first and the result is checked as schema 2; a schema 2 document is checked as it is. It prints every error with its
path and code. Exit status: 0 when every document passes, 1 when one does not, 2 for a file that cannot be read.

``convert`` prints the schema 2 form of one schema 1 file (to ``-o OUTFILE`` if given; an existing file is never overwritten)
and the list of edits to standard error. It does not check the result; run ``check`` on it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.workflow_contract import check_contract  # noqa: E402
from gateway.workflow_convert import convert_1_to_2  # noqa: E402


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
        converted, notes = convert_1_to_2(document)
        report = check_contract(converted)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check documents against the schema 2 contract")
    check.add_argument("paths", nargs="+")
    check.add_argument("--json", action="store_true")
    convert = commands.add_parser("convert", help="print the schema 2 form of a schema 1 document")
    convert.add_argument("file")
    convert.add_argument("-o", "--out")
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args.paths, args.json)
    return run_convert(args.file, args.out)


if __name__ == "__main__":
    sys.exit(main())
