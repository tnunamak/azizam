#!/usr/bin/env python3
"""Print each allowed capability's projected availability and evidence.

Use AZIZAM_API_KEY for a runtime App key. Only the public capability
discovery endpoint is read; this command never probes or starts engines.
"""

from __future__ import annotations

import sys as _compat_sys
from pathlib import Path as _CompatPath
_compat_sys.path.insert(0, str(_CompatPath(__file__).resolve().parents[2]))
from gateway.compat import get_env

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Any, Mapping


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="Gateway base URL, for example http://127.0.0.1:18080")
    parser.add_argument("--api-key", default=get_env("AZIZAM_API_KEY"), help="Bearer key; defaults to AZIZAM_API_KEY")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("base_url must be an HTTP(S) URL without credentials, query, or fragment")
    headers = {"accept": "application/json"}
    if args.api_key:
        headers["authorization"] = f"Bearer {args.api_key}"

    path = "/gateway/v1/capabilities"
    try:
        payload = _get_json(f"{base_url}{path}", headers)
    except RuntimeError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    rows = payload.get("capabilities")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        print(f"{path}: response has no valid capability list", file=sys.stderr)
        return 1
    print(path)
    if not rows:
        print("  No capabilities allowed or configured for this key.")
    for row in rows:
        print(_format_row(row))
    return 0


def _get_json(url: str, headers: Mapping[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("gateway request failed") from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("response was not a JSON object")
    return payload


def _format_row(row: Mapping[str, Any]) -> str:
    availability = row.get("availability") if isinstance(row.get("availability"), Mapping) else {}
    evidence = availability.get("evidence") if isinstance(availability.get("evidence"), Mapping) else {}
    label = " / ".join(str(value) for value in (row.get("intent"), row.get("model") or row.get("workflow")) if value) or "capability"
    state = availability.get("status") or row.get("status") or "unknown"
    reason = availability.get("reason") or row.get("reason") or "Not measured yet"
    source = evidence.get("source") or "none"
    observed = f" observed_at={evidence['observed_at_s']}" if evidence.get("observed_at_s") else ""
    receipt = f" receipt={evidence['request_id']}" if evidence.get("request_id") else ""
    return f"  {label}: {state} · {reason} · evidence={source}{observed}{receipt}"


if __name__ == "__main__":
    raise SystemExit(main())
