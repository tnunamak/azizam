#!/usr/bin/env python3
"""Save the bodies of a fixed set of GET responses, or compare two saves.

The proof behind "behavior-preserving" (start the second server with `serve.py --clone STATE_OF_THE_FIRST`): run the same requests against the same
copied data before and after a change and compare the bytes.

    snapshot.py save --manifest M --out DIR [--paths FILE]
    snapshot.py diff DIR_BEFORE DIR_AFTER

A path file holds one `owner|member|anon  /path?query` per line. Volatile
fields (signed URL signatures and expiries, "now"-based times, the port) are replaced
by a fixed token and JSON keys are sorted before saving, so a difference is a real one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_bench import Session  # noqa: E402

DEFAULT_PATHS = """\
owner /me/api/usage/summary
owner /me/api/usage/summary?person=all
owner /me/api/usage/summary?person=local-network
owner /me/api/usage/summary?source=local-network&person=all
member /me/api/usage/summary
owner /me/api/media?limit=60
member /me/api/media?limit=60
owner /me/api/jobs
owner /me/api/activity
owner /me/api/home
member /me/api/home
owner /me/api/status
member /me/api/status
owner /me/api/models/catalog
owner /me/api/workflows
owner /admin/api/overview
owner /admin/api/status
owner /admin/api/setup-progress
anon /v1/models
anon /gateway/v1/capabilities
anon /gateway/v1/receipts
anon /gateway/v1/media/jobs
anon /v1/videos
"""
VOLATILE = [
    (re.compile(r"(sig|signature|expires|exp|token)=[^&\"\\]+"), r"\1=X"),
    (re.compile(r'"(observed_at_s|last_success_at_s|last_attempt_at_s|checked_at|generated_at|server_time|latency_ms|age_s|created)":\s*[0-9.]+'), r'"\1": 0'),
    (re.compile(r'"csrf_token":\s*"[^"]*"'), '"csrf_token": "X"'),
]


def normalise(body: bytes) -> bytes:
    text = body.decode("utf-8", "replace")
    text = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:PORT", text)
    try:  # key order follows set iteration order, which differs between processes
        text = json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
    except ValueError:
        pass
    for pattern, repl in VOLATILE:
        text = pattern.sub(repl, text)
    return text.encode()


def save(args) -> None:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    lines = (args.paths.read_text() if args.paths else DEFAULT_PATHS).splitlines()
    sessions = {name: Session("127.0.0.1", int(manifest["port"])) for name in ("owner", "member", "anon")}
    sessions["owner"].login(manifest["owner_email"], manifest["password"])
    sessions["member"].login(manifest["member_email"], manifest["password"])
    args.out.mkdir(parents=True, exist_ok=True)
    index = {}
    for line in filter(None, (l.strip() for l in lines)):
        who, path = line.split(None, 1)
        status, _h, body, _ms = sessions[who].get(path)
        name = hashlib.sha1(f"{who} {path}".encode()).hexdigest()[:12]
        (args.out / f"{name}.body").write_bytes(normalise(body))
        index[f"{who} {path}"] = {"file": name, "status": status}
    (args.out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"saved {len(index)} responses to {args.out}")


def diff(args) -> None:
    before = json.loads((args.before / "index.json").read_text())
    after = json.loads((args.after / "index.json").read_text())
    bad = 0
    for key, a in before.items():
        b = after.get(key)
        same = (b is not None and a["status"] == b["status"]
                and (args.before / f"{a['file']}.body").read_bytes() == (args.after / f"{b['file']}.body").read_bytes())
        print(("same   " if same else "DIFFER ") + f"HTTP {a['status']} {key}")
        bad += not same
    raise SystemExit(1 if bad else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(required=True)
    s = sub.add_parser("save")
    s.add_argument("--manifest", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--paths", type=Path)
    s.set_defaults(run=save)
    d = sub.add_parser("diff")
    d.add_argument("before", type=Path)
    d.add_argument("after", type=Path)
    d.set_defaults(run=diff)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
