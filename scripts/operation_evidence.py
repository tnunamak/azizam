#!/usr/bin/env python3
"""Validate and canonicalize an offline operation evidence assertion.

This tool proves only that an operator-provided document has deterministic
bytes and an unexpired declared expiry.  It does not claim to observe a live
runtime or backend.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from gateway.operation_activation import OperationError, canonical_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="local JSON assertion to canonicalize")
    parser.add_argument("--overlay", type=Path, help="local declarative alias overlay JSON")
    parser.add_argument("--base-model-id", help="declared base model id for an activation package")
    args = parser.parse_args()
    try:
        document = json.loads(args.evidence.read_text(encoding="utf-8"))
        digest, canonical, expires_at = canonical_evidence(document)
        overlay = json.loads(args.overlay.read_text(encoding="utf-8")) if args.overlay else None
    except (OSError, json.JSONDecodeError, OperationError) as exc:
        parser.error(str(exc))
    package = {"format": "gateway-operation-activation-package/v1", "evidence": document, "evidence_digest": digest, "expires_at": expires_at, "claim": "operator assertion; not runtime attestation"}
    if overlay is not None:
        if not isinstance(overlay, dict) or not args.base_model_id:
            parser.error("--overlay requires an object and --base-model-id")
        package.update({"base_model_id": args.base_model_id, "overlay": overlay})
    print(json.dumps(package, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
