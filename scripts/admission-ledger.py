#!/usr/bin/env python3
"""Inspect unknown resource tickets or settle one after explicit operator reconciliation.

Use the gateway interpreter. The ledger path is mandatory; this tool never selects
an instance or contacts an engine. `unknown` opens the ledger read-only. `settle`
requires the operator's observed postcondition and changes only an unknown ticket.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.arbitration.store import ArbitrationStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("unknown")
    settle = sub.add_parser("settle")
    settle.add_argument("request_id")
    settle.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    if not args.ledger.is_file():
        print(json.dumps({"error": "ledger does not exist"}), file=sys.stderr)
        return 2
    if args.command == "unknown":
        uri = f"file:{quote(str(args.ledger.resolve()))}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admission_tickets'").fetchone()
            rows = conn.execute("SELECT request_id, service_id, resources, reason FROM admission_tickets "
                                "WHERE phase='unknown' ORDER BY sequence").fetchall() if present else []
        print(json.dumps([{"request_id": row[0], "service_id": row[1], "resources": json.loads(row[2]), "reason": row[3]}
                          for row in rows]))
        return 0
    if not args.evidence.strip():
        parser.error("--evidence must name the observed upstream postcondition")
    store = ArbitrationStore(args.ledger)
    try:
        if not store.settle_unknown(args.request_id, evidence="operator: " + args.evidence, now=time.time()):
            print(json.dumps({"error": "ticket is not unknown"}), file=sys.stderr)
            return 2
        print(json.dumps({"request_id": args.request_id, "phase": "completed"}))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
