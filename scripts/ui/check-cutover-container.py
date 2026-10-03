#!/usr/bin/env python3
"""Check the packaged browser front door against a fresh, isolated container."""
from __future__ import annotations

import argparse
import http.cookiejar
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="vivid-fish-cutover-check")
    parser.add_argument("--port", type=int, default=18192)
    args = parser.parse_args()
    if not 18000 <= args.port <= 18999:
        parser.error("port must be between 18000 and 18999")
    root = Path(__file__).resolve().parents[2]
    state_root = root / ".ui-e2e-state"
    state_root.mkdir(exist_ok=True)
    name = f"cutover-check-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="container-", dir=state_root) as state:
        subprocess.run([
            "docker", "run", "--rm", "-d", "--name", name,
            "-p", f"127.0.0.1:{args.port}:5000",
            "--mount", f"type=bind,source={state},target=/app/data",
            "-e", "AI_GATEWAY_CONFIG_PATH=/app/first-run.yml",
            "-e", "AI_GATEWAY_LITELLM_BASE_URL=http://127.0.0.1:18999",
            args.image, "python3", "-m", "uvicorn", "proxy:app",
            "--host", "0.0.0.0", "--port", "5000",
        ], check=True, stdout=subprocess.DEVNULL)
        opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

        def request(path: str, data: bytes | None = None):
            headers = {"Accept": "text/html"}
            if data is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(
                f"http://127.0.0.1:{args.port}{path}", data=data, headers=headers
            )
            try:
                response = opener.open(req, timeout=10)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, response.headers, response.read()

        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if request("/health")[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError("Isolated container did not become ready")
            status, headers, _ = request("/?connect=container")
            assert status == 302 and headers["Location"] == "/ui/home/?connect=container"
            status, _, body = request("/ui/home/")
            assert status == 200 and b"/_next/" in body
            assert request("/ui/sign-in/")[0] == 200
            password = "Container journey password 2026"
            form = urllib.parse.urlencode({
                "email": "owner@example.test", "password": password,
                "password_confirm": password,
            }).encode()
            assert request("/setup/local-owner", form)[0] == 303
            status, headers, _ = request("/admin?from=container")
            assert status == 302 and headers["Location"] == "/ui/home/?from=container"
            status, _, body = request("/admin/api/session")
            assert status == 200 and "owner" in json.loads(body)["actor"]["roles"]
            assert request("/ui/status/")[0] == 200
            print("PASS: 7 fresh-container assertions (Home, sign-in, owner claim, admin query, session, Status)")
        finally:
            subprocess.run(["docker", "stop", name], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
