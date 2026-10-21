#!/usr/bin/env python3
"""Time every GET route of a running perf fixture gateway (scripts/perf/serve.py).

For each route under /me/api, /admin/api, /v1 (and the OpenAI/Anthropic/Ollama
twins) plus the discovery routes, the bench sends WARMUP + REPEATS sequential
requests on one keep-alive connection and records client p50/p95 (ms), payload
bytes, and the per-request work stamped by gateway_main.py: SQLite statements,
SQLite connections, spawned processes (minimum over repeats, which drops
background noise). Routes with path parameters get a real id read from a list
response; a route with no sample is reported as skipped, never silently passed.

    scripts/perf/api_bench.py --manifest STATE/perf-manifest.json --out result.json
"""
from __future__ import annotations

import argparse
import http.client
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

WARMUP = 1
REPEATS = 9
SLOW_MS = 2000  # a first request slower than this gets SLOW_REPEATS repeats, not REPEATS
SLOW_REPEATS = 3
PREFIXES = ("/me/api", "/admin/api", "/v1", "/openai/v1", "/anthropic/v1", "/ollama/api", "/api/",
            "/.well-known", "/llms.txt", "/health", "/gateway/v1", "/status/raw", "/sdapi/v1")
SKIP = {"/mcp", "/docs", "/redoc", "/openapi.json"}


class Session:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.conn = http.client.HTTPConnection(host, port, timeout=60)
        self.cookie = ""

    def get(self, path: str, extra: dict | None = None):
        headers = {"accept": "application/json"}
        if self.cookie:
            headers["cookie"] = self.cookie
        headers.update(extra or {})
        started = time.perf_counter()
        for attempt in (0, 1):
            try:
                self.conn.request("GET", path, headers=headers)
                response = self.conn.getresponse()
                body = response.read()
                break
            except (http.client.HTTPException, OSError):
                self.conn.close()
                self.conn = http.client.HTTPConnection(self.host, self.port, timeout=60)
                if attempt:
                    raise
        elapsed = (time.perf_counter() - started) * 1000
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body, elapsed

    def login(self, email: str, password: str) -> None:
        body = json.dumps({"email": email, "password": password})
        self.conn.request("POST", "/auth/api/login", body=body, headers={"content-type": "application/json"})
        response = self.conn.getresponse()
        response.read()
        if response.status != 200:
            raise SystemExit(f"login for {email} returned HTTP {response.status}")
        jar = [value.split(";", 1)[0] for key, value in response.getheaders() if key.lower() == "set-cookie"]
        self.cookie = "; ".join(jar)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def find_key(node, key: str, depth: int = 0):
    """First string value stored under `key` in a nested JSON value, searched breadth-wise."""
    if depth > 5:
        return None
    if isinstance(node, dict):
        if isinstance(node.get(key), str):
            return node[key]
        for value in node.values():
            found = find_key(value, key, depth + 1) if isinstance(value, (list, dict)) else None
            if found:
                return found
    elif isinstance(node, list):
        for item in node[:5]:
            found = find_key(item, key, depth + 1)
            if found:
                return found
    return None


# path parameter name -> (list route that holds a real value, key to read from its payload)
SAMPLE_SOURCES = {
    "asset_id": ("/me/api/media?limit=3", "asset_id"),
    "job_id": ("/me/api/jobs?limit=3", "job_id"),
    "request_id": ("/me/api/activity?limit=3", "request_id"),
    "profile_id": ("/admin/api/profiles", "id"),
    "provider_id": ("/admin/api/providers", "id"),
    "event_id": ("/admin/api/audit-events?limit=3", "id"),
    "link_id": ("/admin/api/invites", "id"),
    "credential_id": ("/admin/api/credentials", "id"),
    "revision": ("/admin/api/categories/history", "revision"),
}


def sample_pool(owner: Session) -> dict[str, str]:
    pool: dict[str, str] = {}
    for name, (path, key) in SAMPLE_SOURCES.items():
        status, _h, body, _ms = owner.get(path)
        if status != 200:
            continue
        try:
            found = find_key(json.loads(body), key)
        except ValueError:
            continue
        if found:
            pool[name] = found
    return pool


def fill(template: str, pool: dict[str, str]) -> str | None:
    missing = False

    def sub(match):
        nonlocal missing
        name = match.group(1).split(":")[0]
        value = pool.get(name)
        if value is None:
            missing = True
            return ""
        return urllib.parse.quote(value, safe="")

    path = re.sub(r"\{([^}]+)\}", sub, template)
    return None if missing else path


def run(manifest: dict) -> dict:
    host, port = "127.0.0.1", int(manifest["port"])
    owner, member, anon = Session(host, port), Session(host, port), Session(host, port)
    owner.login(manifest["owner_email"], manifest["password"])
    member.login(manifest["member_email"], manifest["password"])
    pool = sample_pool(owner)
    routes = json.loads(Path(manifest["routes_file"]).read_text(encoding="utf-8"))
    rows: list[dict] = []
    for template in routes:
        if template in SKIP or not template.startswith(PREFIXES):
            continue
        if "{rest:path}" in template:
            continue
        path = fill(template, pool)
        who = [("owner", owner)]
        if template.startswith("/me/api"):
            who.append(("member", member))
        if not template.startswith(("/me/api", "/admin/api")):
            who = [("anon", anon)]
        for label, session in who:
            row = {"route": template, "who": label}
            if path is None:
                rows.append({**row, "skipped": "no sample id"})
                continue
            samples, stamps = [], []
            status, size = 0, 0
            try:
                repeats = WARMUP + REPEATS
                index = -1
                while index + 1 < repeats:
                    index += 1
                    status, headers, body, ms = session.get(path)
                    if index == 0 and ms > SLOW_MS:
                        repeats = WARMUP + SLOW_REPEATS  # a multi-second route is already a finding
                    size = len(body)
                    if index >= WARMUP:
                        samples.append(ms)
                        stamps.append({k: float(headers.get(f"x-perf-{k}", 0)) for k in ("sql", "connects", "subproc", "ms", "cpu-ms")})
                    if status >= 400 and index == 0:
                        break
            except Exception as error:  # a hung or reset route is a finding, not a crash
                rows.append({**row, "error": f"{type(error).__name__}: {error}"})
                continue
            if status >= 400:
                rows.append({**row, "status": status, "skipped": f"HTTP {status}"})
                continue
            rows.append({
                **row, "status": status, "p50_ms": round(pct(samples, 0.5), 1), "p95_ms": round(pct(samples, 0.95), 1),
                "bytes": size, "sql": int(min(s["sql"] for s in stamps)), "connects": int(min(s["connects"] for s in stamps)),
                "subproc": int(min(s["subproc"] for s in stamps)), "server_ms": round(min(s["ms"] for s in stamps), 1),
                "cpu_ms": round(min(s["cpu-ms"] for s in stamps), 1),
            })
    return {"rows": rows, "pool": sorted(pool), "repeats": REPEATS}


def table(rows: list[dict], limit: int = 0) -> str:
    timed = sorted((r for r in rows if "p95_ms" in r), key=lambda r: -r["p95_ms"])
    lines = ["| route | who | p50 ms | p95 ms | cpu ms | bytes | sql | conns | procs |", "|---|---|--:|--:|--:|--:|--:|--:|--:|"]
    for r in timed[: limit or None]:
        lines.append(f"| {r['route']} | {r['who']} | {r['p50_ms']} | {r['p95_ms']} | {r.get('cpu_ms', '')} | {r['bytes']} | {r['sql']} | {r['connects']} | {r['subproc']} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--top", type=int, default=0)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = run(manifest)
    args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(table(result["rows"], args.top))
    for r in result["rows"]:
        if "skipped" in r or "error" in r:
            print(f"# {r['route']} ({r['who']}): {r.get('skipped') or r.get('error')}", file=sys.stderr)


if __name__ == "__main__":
    main()
