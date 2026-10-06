#!/usr/bin/env python3
"""Gentle, credential-free exposure probes; discard all response bodies.

Run with --edge-ip from public DNS to avoid split-horizon LAN resolution.
Only empty invalid inference payloads and an MCP initialization are sent.
No sign-in attempts, account mutations, uploads, or valid generation requests.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import time


READS = (
    "/health", "/v1/models", "/anthropic/v1/models", "/api/tags",
    "/ollama/api/tags", "/sdapi/v1/sd-models", "/gateway/v1/capabilities",
    "/gateway/v1/receipts", "/me/api/session", "/me/api/usage/summary",
    "/me/api/media", "/admin/api/operator/service-registry", "/admin/api/people",
    "/typesafe/v1/models", "/status/raw", "/docs", "/openapi.json",
    "/showcase", "/showcase/api/status", "/ui/home/", "/ui/settings/", "/mcp",
    "/v1/assets/exposure-probe-missing/content",
    "/v1/images/exposure-probe-missing/content",
    "/v1/videos/exposure-probe-missing/content",
    "/.well-known/azizam", "/.well-known/skills",
)
WRITES = (
    "/v1/chat/completions", "/anthropic/v1/messages", "/api/generate",
    "/ollama/api/chat", "/sdapi/v1/txt2img", "/typesafe/v1/systemone",
    "/v1/decisions", "/gateway/v1/media/jobs",
)
SPOOFS = {
    "xff-lan": {"X-Forwarded-For": "192.168.1.20"},
    "xff-loopback": {"X-Forwarded-For": "127.0.0.1"},
    "xff-malformed": {"X-Forwarded-For": "192.168.1.20, garbage"},
    "cf-lan": {"CF-Connecting-IP": "192.168.1.20"},
    "forwarded-lan": {"Forwarded": "for=192.168.1.20;proto=https"},
}


def probe(host: str, ip: str, path: str, *, method="GET", headers=None, body=None):
    command = [
        "curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "8",
        "--resolve", f"{host}:443:{ip}", "--request", method,
        "--output", "/dev/null", "--write-out",
        '%{http_code} %{remote_ip} %{size_download} %{content_type}',
    ]
    for name, value in (headers or {}).items():
        command.extend(["--header", f"{name}: {value}"])
    if body is not None:
        command.extend(["--header", "Content-Type: application/json", "--data-raw", body])
    command.append(f"https://{host}{path}")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    fields = result.stdout.split(" ", 3)
    return {
        "host": host, "target_ip": ip, "method": method, "path": path,
        "headers": headers or {}, "curl_exit": result.returncode,
        "status": int(fields[0]) if fields and fields[0].isdigit() else 0,
        "remote_ip": fields[1] if len(fields) > 1 else "",
        "body_bytes": fields[2] if len(fields) > 2 else "",
        "content_type": fields[3] if len(fields) > 3 else "",
        "transport_error": result.stderr.strip()[:300] if result.returncode else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge-ip", required=True)
    parser.add_argument("--lan-ip", required=True)
    parser.add_argument("--host", action="append", required=True)
    args = parser.parse_args()
    print(json.dumps({"started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}), flush=True)
    for host in args.host:
        for label, ip in (("edge", args.edge_ip), ("lan-proxy", args.lan_ip)):
            cases = [(path, "GET", {}, None, "baseline") for path in READS]
            cases.extend((path, "POST", {}, "{}", "baseline") for path in WRITES)
            cases.append(("/mcp", "POST", {}, '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"exposure-review","version":"1"}}}', "baseline"))
            cases.append(("/v1/realtime", "GET", {
                "Connection": "Upgrade", "Upgrade": "websocket",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13",
            }, None, "websocket"))
            for spoof, headers in SPOOFS.items():
                cases.extend((path, "GET", headers, None, spoof) for path in (
                    "/v1/models", "/gateway/v1/capabilities", "/status/raw", "/typesafe/v1/models",
                ))
            for path, method, headers, body, case in cases:
                record = probe(host, ip, path, method=method, headers=headers, body=body)
                record.update(transport=label, case=case)
                print(json.dumps(record), flush=True)
                time.sleep(0.15)


if __name__ == "__main__":
    main()
