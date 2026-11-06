"""Argument handling, the result file and the exit code. Exit 0: every assertion passed. 1: a step or assertion
failed, or an unexpected error stopped the run. 2: an input is wrong and nothing was sent (a manifest, a body, a
sha256, the key file, or a timing ratio in request mode)."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence

import httpx

from .manifest import InputError, load_cases
from .runner import Proof

SCHEMA = "transition-proof/1"
# RFC 6750 bearer token (token68). A key outside this set (a quote or a backslash, for one) could come back
# JSON-escaped in a gateway reply, and the escaped text would no longer match the key for redaction.
_TOKEN = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transition-proof",
        description="Park a domain, wake it, and prove the cached prefix survived, over the gateway's HTTP surface.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--cases", required=True, type=Path, help="case manifest (JSON)")
    parser.add_argument("--owner-key-file", type=Path, help="file holding the owner key; required unless --dry-run")
    parser.add_argument("--model", required=True, help="the alias to call")
    parser.add_argument("--out", required=True, type=Path, help="result JSON")
    parser.add_argument("--wake-mode", choices=("post", "request"), default="post",
                        help="post: POST wake, then send the follow-up. request: the follow-up triggers the wake")
    parser.add_argument("--dry-run", action="store_true", help="validate the manifest and bodies; send nothing")
    parser.add_argument("--run-id", default=None, help="stamped into idempotency keys (default: random)")
    parser.add_argument("--poll-interval-s", type=float, default=1.0)
    parser.add_argument("--poll-timeout-s", type=float, default=600.0, help="per operation")
    parser.add_argument("--request-timeout-s", type=float, default=900.0, help="per chat request")
    return parser


def _read_key(path: Optional[Path]) -> str:
    if path is None:
        raise InputError("--owner-key-file is required unless --dry-run")
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise InputError(f"cannot read the owner key file: {error.strerror}") from None
    if not key:
        raise InputError("the owner key file is empty")
    if not _TOKEN.match(key):
        # An inner line break or space would go into the Authorization header, and the HTTP library quotes a
        # refused header value in its error. Never send such a key, and never name any part of it.
        raise InputError("the owner key file must hold one bearer token (RFC 6750): letters, digits and "
                         "the characters . _ ~ + / -, with optional = at the end")
    return key


def _write(out: Path, result: dict[str, Any], key: Optional[str]) -> None:
    """The result never carries the key: it is replaced wherever it appears, even inside a quoted reply."""
    text = json.dumps(result, indent=2, sort_keys=False)
    if key:
        text = text.replace(key, "[redacted]")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + ".part")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, out)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    run_id = args.run_id or uuid.uuid4().hex[:12]
    result: dict[str, Any] = {
        "schema": SCHEMA, "status": "failed", "dry_run": args.dry_run, "run_id": run_id, "base_url": args.base_url,
        "domain": args.domain, "model": args.model, "wake_mode": args.wake_mode, "started_at": _now(), "cases": [],
    }
    key: Optional[str] = None
    try:
        cases = load_cases(args.cases)
        if args.wake_mode == "request" and any(c.cache.max_timing_ratio is not None for c in cases):
            raise InputError("max_timing_ratio cannot be used with --wake-mode request: the follow-up time includes "
                             "the wake, so its ratio to the cold time is not meaningful")
        if args.dry_run:
            if args.owner_key_file is not None:
                _read_key(args.owner_key_file)
            result["cases"] = [{"label": c.label, "file": c.file, "body_sha256": c.sha256,
                                "messages": len(c.body["messages"]), "cache_check": vars(c.cache),
                                "estimated_prompt_tokens": c.estimated_prompt_tokens} for c in cases]
            result["status"] = "validated"
            return _finish(args.out, result, key, 0)
        key = _read_key(args.owner_key_file)
    except InputError as error:
        result.update(status="invalid_input", error=str(error))
        print(f"transition-proof: {error}", file=sys.stderr)
        return _finish(args.out, result, key, 2)

    # Overwrite any earlier result first: if this run dies before its own result is written, the file must not
    # keep an old "passed".
    _write(args.out, {**result, "status": "running"}, key)
    timeout = httpx.Timeout(args.request_timeout_s, connect=10.0)
    try:
        with httpx.Client(base_url=args.base_url.rstrip("/"), headers={"Authorization": f"Bearer {key}"},
                          timeout=timeout) as client:
            proof = Proof(client, domain=args.domain, model=args.model, wake_mode=args.wake_mode, run_id=run_id,
                          poll_interval_s=args.poll_interval_s, poll_timeout_s=args.poll_timeout_s, secret=key)
            for case in cases:
                record = proof.run_case(case)
                result["cases"].append(record)
                if record["status"] != "passed":
                    # A held gate or a lost reply makes the next case meaningless; report the rest as not run.
                    result["cases"].extend({"label": later.label, "status": "not_run"}
                                           for later in cases[len(result["cases"]):])
                    break
    except Exception as error:  # noqa: BLE001 - only the type is kept: a message or traceback could carry data
        result.update(status="failed", error=f"unexpected {type(error).__name__}")
        print(f"transition-proof: unexpected {type(error).__name__}", file=sys.stderr)
        return _finish(args.out, result, key, 1)
    assertions = [a for c in result["cases"] for a in c.get("assertions", [])]
    failed = [a for a in assertions if not a["passed"]]
    result["assertions_total"], result["assertions_failed"] = len(assertions), len(failed)
    result["status"] = "failed" if failed or any(c["status"] != "passed" for c in result["cases"]) else "passed"
    for record in result["cases"]:
        for item in record.get("assertions", []):
            print(f"{'PASS' if item['passed'] else 'FAIL'} {record['label']} {item['name']}")
    print(f"transition-proof: {result['status']} ({len(failed)} of {len(assertions)} assertions failed) -> {args.out}")
    return _finish(args.out, result, key, 0 if result["status"] == "passed" else 1)


def _finish(out: Path, result: dict[str, Any], key: Optional[str], code: int) -> int:
    result["finished_at"] = _now()
    result["exit_code"] = code
    _write(out, result, key)
    return code
