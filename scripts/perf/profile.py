#!/usr/bin/env python3
"""Where does a request spend its time? Sample the gateway's threads while repeating it.

Needs a fixture started with PERF_PROFILE=1 (scripts/perf/serve.py):

    PERF_PROFILE=1 scripts/perf/serve.py --port 18730 &
    scripts/perf/profile.py --manifest STATE/perf-manifest.json /me/api/home [--member] [--repeat 25]

Prints the repo functions with the most samples (inclusive: the function or anything
it called was running) and the busiest leaf calls. Sample counts are 4 ms ticks.
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
    parser.add_argument("--repeat", type=int, default=25)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("path")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    session = Session("127.0.0.1", int(manifest["port"]))
    session.login(manifest["member_email" if args.member else "owner_email"], manifest["password"])
    session.get(args.path)
    session.get("/__perf/prof?reset=1")
    total = 0.0
    for _ in range(args.repeat):
        total += session.get(args.path)[3]
    status, _h, body, _ms = session.get("/__perf/prof?reset=1")
    if status != 200:
        raise SystemExit("start the fixture with PERF_PROFILE=1")
    data = json.loads(body)
    print(f"{args.path}: {total / args.repeat:.0f} ms per request, {data['samples'] / args.repeat:.0f} ticks per request")
    print("-- inclusive (ticks per request)")
    for name, count in data["inclusive"][: args.top]:
        print(f"{count / args.repeat:7.1f}  {name}")
    print("-- leaf")
    for name, count in data["leaf"][:15]:
        print(f"{count / args.repeat:7.1f}  {name}")


if __name__ == "__main__":
    main()
