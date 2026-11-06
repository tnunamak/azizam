#!/usr/bin/env python3
"""Show which code a request's SQLite statements and processes come from.

Needs a fixture started with PERF_ATTRIBUTE=1 (scripts/perf/serve.py):

    PERF_ATTRIBUTE=1 scripts/perf/serve.py --port 18810 &
    scripts/perf/attribute.py --manifest STATE/perf-manifest.json /me/api/usage/summary [--member]

Prints the 25 largest groups as `count  kind 'statement' @ file:line:function < caller < caller`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_bench import Session  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--member", action="store_true")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("path")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    session = Session("127.0.0.1", int(manifest["port"]))
    session.login(manifest["member_email" if args.member else "owner_email"], manifest["password"])
    session.get("/__perf/where?reset=1")
    status, headers, body, ms = session.get(args.path)
    _s, _h, where, _ms = session.get("/__perf/where?reset=1")
    print(f"HTTP {status}  {ms:.0f} ms  {len(body)} bytes  sql={headers.get('x-perf-sql')} "
          f"conns={headers.get('x-perf-connects')} procs={headers.get('x-perf-subproc')}")
    if _s != 200:
        raise SystemExit("start the fixture with PERF_ATTRIBUTE=1")
    for chain, count in json.loads(where)[: args.top]:
        print(f"{count:6d}  {chain}")


if __name__ == "__main__":
    main()
