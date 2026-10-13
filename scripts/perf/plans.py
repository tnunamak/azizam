#!/usr/bin/env python3
"""Find queries whose plan reads a whole big table: the next full scan before it is slow.

Needs a fixture started with PERF_PLANS=1 (scripts/perf/serve.py) that has served the requests of
interest (run api_bench.py against it first). Every distinct statement the gateway ran is
explained against the fixture's own copy of the database, and any plan that

  * SCANs a table of at least --rows rows without a covering/search index, or
  * SEARCHes an index and then sorts (USE TEMP B-TREE FOR ORDER BY) over a table of that size

is printed with the table size.

    PERF_PLANS=1 scripts/perf/serve.py --port 18740 &   # prints the ready line; S is its "state" path
    scripts/perf/api_bench.py --manifest S/perf-manifest.json --out /dev/null
    scripts/perf/plans.py --manifest S/perf-manifest.json [--rows 2000]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_bench import Session  # noqa: E402

TABLE = re.compile(r"(?:SCAN|SEARCH)\s+(?:TABLE\s+)?(\w+)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=2000)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    session = Session("127.0.0.1", int(manifest["port"]))
    status, _h, body, _ms = session.get("/__perf/statements")
    if status != 200:
        raise SystemExit("start the fixture with PERF_PLANS=1")
    found = 0
    sizes: dict[tuple[str, str], int] = {}
    for database, statement in json.loads(body):
        if not statement.upper().startswith(("SELECT", "WITH", "UPDATE", "DELETE")) or not Path(database).is_file():
            continue
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
                plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + statement)]
                for line in plan:
                    match = TABLE.search(line)
                    if not match:
                        continue
                    table = match.group(1)
                    key = (database, table)
                    if key not in sizes:
                        try:
                            sizes[key] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                        except sqlite3.Error:
                            sizes[key] = 0
                    scan = line.startswith("SCAN") and "USING" not in line.replace("USING INTEGER", "")
                    sort = any("TEMP B-TREE FOR ORDER BY" in other for other in plan) and line.startswith("SEARCH")
                    if sizes[key] >= args.rows and (scan or sort):
                        print(f"{sizes[key]:>8} rows  {'SCAN' if scan else 'SORT'}  {Path(database).name}.{table}: {statement[:150]}")
                        found += 1
        except sqlite3.Error:
            continue
    print(f"{found} plan(s) read a large table whole", file=sys.stderr)


if __name__ == "__main__":
    main()
