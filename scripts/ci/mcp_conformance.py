#!/usr/bin/env python3
"""Run the official MCP conformance suite against Azizam's MCP server and
authorization server.

The suite (@modelcontextprotocol/conformance, pinned in tests/conformance)
connects to a running server. This script serves the production protocol code,
gateway/mcp_server.py's router and gateway/mcp_oauth.py's authorization server,
on a loopback port it picks, with stub dependencies: no gateway, no models, no
operator data. Then it runs:

* the server requirement sets for 2026-07-28 and 2025-11-25, each against the
  expected-failures baseline in tests/conformance/. The baseline lists only
  scenarios for features Azizam does not offer (prompts, resources, sampling,
  elicitation, tasks, logging, completion) or the suite's own diagnostic test
  tools. A new failure, or a listed one that starts passing, fails the run.
* the authorization-server scenarios (metadata, authorization code grant).
  The suite waits for a person to approve in a browser; this script plays that
  person against the real consent endpoints.

The suite cannot send an Authorization header, so the fixture's MCP endpoint
accepts every request. Authentication is covered by tests/test_mcp_oauth*.py.

Usage: python scripts/ci/mcp_conformance.py [--output DIR]
Exit status is 0 only when every requirement set matches its baseline and both
authorization scenarios pass.
"""
from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from gateway import mcp_oauth, mcp_server  # noqa: E402
from gateway.mcp_oauth_store import Store  # noqa: E402

SUITE_DIR = ROOT / "tests" / "conformance"
SUITE = SUITE_DIR / "node_modules" / ".bin" / "conformance"
REVISIONS = ("2026-07-28", "2025-11-25")
PERSON = "usr_conformance"
SESSION_HEADER = "x-conformance-person"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_app(base: str, db: Path) -> FastAPI:
    app = FastAPI()

    async def no_core(request, method, path, **kwargs):
        return httpx.Response(404, json={"error": {"message": "no core in the conformance fixture",
                                                   "code": "not_found"}})

    mcp_server.register_mcp_routes(app, mcp_server.McpDeps(
        authenticate=lambda request, path: SimpleNamespace(identity=None),
        call=no_core, catalog=lambda: [], aliases=lambda: {},
        store_asset=lambda *args: {}, read_asset=lambda *args: None,
        public_base_url=lambda request: base, server_version="conformance",
        allowed_origins=lambda request: [base],
        authorization_servers=lambda request: [base], scopes=(mcp_oauth.SCOPE,),
    ))

    async def no_documents(url: str):
        raise OSError("the conformance fixture fetches no client metadata documents")

    oauth = mcp_oauth.McpOAuth(mcp_oauth.McpOAuthDeps(
        public_base_url=lambda request: base,
        session=lambda request: request.headers.get(SESSION_HEADER),
        person=lambda session: {"id": session, "email": "conformance@example.invalid"} if session == PERSON else None,
        check_action=lambda request, session: None,
        person_can_connect=lambda person_id: person_id == PERSON,
        plan=lambda person_id: {"name": "Conformance"},
        client_ip=lambda request: "127.0.0.1",
        fetch_client_document=no_documents,
        store=Store(lambda: db),
    ))
    oauth.register(app)
    return app


class Fixture:
    def __init__(self, db: Path) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(build_app(self.base, db), host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "Fixture":
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("the conformance fixture did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def run_suite(args: list[str], *, log: Path) -> int:
    with log.open("w") as out:
        return subprocess.run([str(SUITE), *args], cwd=SUITE_DIR, stdout=out, stderr=subprocess.STDOUT,
                              timeout=600).returncode


def run_server_revision(fixture: Fixture, revision: str, output: Path) -> int:
    log = output / f"server-{revision}.log"
    code = run_suite(["server", "--url", f"{fixture.base}/mcp", "--requirements", revision,
                      "--expected-failures", str(SUITE_DIR / f"expected-failures.{revision}.yml"),
                      "-o", str(output / f"server-{revision}")], log=log)
    print(f"== server {revision}: exit {code}")
    print("\n".join(line for line in log.read_text().splitlines()
                    if line.startswith(("Total:", "✗", "Unexpected", "Stale", "Expected", "Not scored"))))
    return code


def approve(authorize_url: str, base: str) -> None:
    """Play the person: open the authorization URL, approve, follow the redirect."""
    with httpx.Client(follow_redirects=False, timeout=30) as browser:
        start = browser.get(authorize_url)
        if start.status_code != 302 or not start.headers["location"].startswith(mcp_oauth.CONSENT_PAGE):
            raise RuntimeError(f"authorization did not reach consent: {start.status_code} {start.text[:200]}")
        request_id = parse_qs(urlsplit(start.headers["location"]).query)["request"][0]
        decision = browser.post(f"{base}/me/api/mcp/requests/{request_id}", json={"decision": "allow"},
                                headers={SESSION_HEADER: PERSON})
        decision.raise_for_status()
        browser.get(decision.json()["redirect"])  # the suite's callback listener


def run_authorization(fixture: Fixture, output: Path) -> int:
    callback_port = free_port()
    redirect = f"http://127.0.0.1:{callback_port}/callback"
    registered = httpx.post(f"{fixture.base}/oauth/register", json={
        "client_name": "MCP conformance", "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"]}, timeout=30)
    registered.raise_for_status()
    # One run: the code-grant scenario reads the endpoints the metadata
    # scenario fetched in the same run.
    args = [str(SUITE), "authorization", "--url", fixture.base, "--client-id", registered.json()["client_id"],
            "--resource", f"{fixture.base}/mcp", "--port", str(callback_port), "-o", str(output / "authorization")]
    proc = subprocess.Popen(args, cwd=SUITE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        found = re.search(r"(https?://\S+/oauth/authorize\?\S+)", line)
        if found:
            threading.Thread(target=approve, args=(found.group(1), fixture.base), daemon=True).start()
    code = proc.wait(timeout=600)
    (output / "authorization.log").write_text("".join(lines))
    totals = [re.search(r"Total: (\d+) passed, (\d+) failed", line) for line in lines]
    total = next((m for m in totals if m), None)
    passed = code == 0 and total is not None and int(total.group(1)) > 0 and total.group(2) == "0"
    print(f"== authorization server: {'pass' if passed else 'FAIL'} (exit {code};"
          f" {total.group(0) if total else 'no summary'})")
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, help="keep logs and result JSON here")
    options = parser.parse_args()
    if not SUITE.exists():
        print(f"error: {SUITE} is missing; run npm ci in {SUITE_DIR}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="azizam-mcp-conformance.") as tmp:
        output = options.output or Path(tmp) / "out"
        output.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with Fixture(Path(tmp) / "admin.db") as fixture:
            codes = [run_server_revision(fixture, revision, output) for revision in REVISIONS]
            codes.append(run_authorization(fixture, output))
        print(f"== mcp conformance: {'PASS' if not any(codes) else 'FAIL'} in {time.monotonic() - started:.1f}s"
              f" (logs: {output})")
        return 1 if any(codes) else 0


if __name__ == "__main__":
    sys.exit(main())
