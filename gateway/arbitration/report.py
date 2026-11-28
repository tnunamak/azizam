"""Read the shadow ledger as JSON: comparison counts and open operations.

    python -m gateway.arbitration.report [--db PATH]

PATH defaults to AZIZAM_ARBITRATION_DB, else data/arbitration.db. The file is opened read-only, in one read
transaction. A missing file or a file that is not a ledger of this schema is an error. Reading never creates,
migrates or writes a ledger (design §16 criterion 9).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gateway.arbitration.store import StoreSchemaError, default_path, read_snapshot

DESCRIPTION = "Read the shadow ledger as JSON: comparison counts and open operations."


def build_report(path: Path) -> dict:
    snapshot = read_snapshot(path)
    return {
        "db": str(path),
        "schema_version": snapshot.schema_version,
        "comparisons": snapshot.comparisons,
        "open_operations": [
            {
                "op_key": op.op_key,
                "resource_id": op.resource_id,
                "incarnation": op.incarnation,
                "generation": op.generation,
                "quantity": op.quantity,
                "unit": op.unit,
                "state": op.state,
                "execution_ended": op.execution_ended,
            }
            for op in snapshot.open_operations
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.arbitration.report", description=DESCRIPTION)
    parser.add_argument("--db", type=Path, default=None, help="ledger file (default: AZIZAM_ARBITRATION_DB or data/arbitration.db)")
    args = parser.parse_args(argv)
    path = args.db if args.db is not None else default_path()
    try:
        report = build_report(path)
    except (FileNotFoundError, StoreSchemaError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
