"""Run the documented first-run journey against disposable real processes.

No gateway internals are imported. State, config, environment, and (for Compose)
build context are fresh; authentication and registry edits use HTTP only.
"""
from contextlib import ExitStack
from pathlib import Path
import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

import httpx

from fake_engine import ANSWER, MODEL

REPO = Path(__file__).resolve().parents[2]


def reserve_port(stack):
    for port in range(18000, 19000):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            continue
        stack.callback(sock.close)
        return sock, port
    raise RuntimeError("No free test port in 18000–18999")


def expect(response, status):
    if response.status_code != status:
        # Response bodies can contain one-time secrets. Never print them.
        raise RuntimeError(f"{response.request.method} {response.request.url.path}: "
                           f"expected {status}, got {response.status_code}")
    return response


def wait_ready(client, path, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if client.get(path).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {path}")


def wait_file(path, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {path}")


def wait_litellm_ready(base_url, api_key, process, timeout=90):
    deadline = time.monotonic() + timeout
    headers = {"X-Litellm-Key": f"Bearer {api_key}"}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"LiteLLM sidecar exited with code {process.returncode}")
        try:
            response = httpx.get(f"{base_url}/v1/models", headers=headers, trust_env=False, timeout=5)
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for LiteLLM sidecar /v1/models")


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def snapshot(destination):
    # Copy only tracked source, with current edits. Never traverse ignored state
    # or operator files. This also prevents Docker COPY from including secrets.
    import shutil
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO).decode().split("\0")
    for name in paths:
        path = Path(name)
        if not name or path.parts[0] in {"data", ".git"}:
            continue
        if path.name.startswith(".env") or path.name == "proxy_config.yml":
            continue
        source = REPO / path
        if source.is_file() and not source.is_symlink():
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    shutil.copy2(REPO / "first-run.yml", destination / "first-run.yml")
    (destination / "data").mkdir()
    (destination / "tests/e2e").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "tests/e2e/fake_engine.py", destination / "tests/e2e/fake_engine.py")


def assert_fresh_setup_entry(client, compose):
    if not compose:
        setup = expect(client.get("/setup"), 200)
        assert '/setup/local-owner' in setup.text, "Fresh install has no owner claim form"
        print("PASS fresh setup renders the local owner claim form", flush=True)
        return

    setup = expect(client.get("/setup"), 303)
    assert setup.headers["location"] == "/ui/setup/", "Fresh setup did not redirect to exported UI"
    exported = expect(client.get("/ui/setup/"), 200)
    assert "text/html" in exported.headers.get("content-type", ""), "Exported setup page is not HTML"
    state = expect(client.get("/setup/api/state"), 200).json()
    assert state["owner_exists"] is False, "Fresh setup API state already has an owner"
    assert state["oidc_enabled"] is False, "Fresh first-run config unexpectedly enabled OIDC"
    assert isinstance(state["can_recover"], bool), "Fresh setup API state has invalid recovery capability"
    print("PASS exported setup redirect and fresh setup state", flush=True)


def assert_catalog_ready_after_chat(client):
    catalog = expect(client.get("/me/api/models/catalog"), 200).json()
    row = next((model for model in catalog["models"] if model["id"] == MODEL), None)
    assert row is not None, "Chat model is missing from the member models catalog"
    availability = row["availability"]
    source = availability.get("evidence", {}).get("source")
    assert availability["available"] is True, "Chat model is not available after successful chat"
    assert availability["status"] == "ready", "Chat model is not ready after successful chat"
    assert availability["measured"] is True, "Chat success has no measured availability evidence"
    assert source in {"health", "request", "status"}, "Chat model availability evidence has no live source"
    print(f"PASS member models catalog marks {MODEL} ready after chat via {source}", flush=True)


def run(compose):
    # All scratch files remain in this worktree, on disk rather than /tmp.
    scratch = REPO / "venv"
    scratch.mkdir(exist_ok=True)
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="fresh-instance-", dir=scratch)))
        source = root / "source"
        source.mkdir()
        snapshot(source)
        env = {"PATH": os.defpath, "HOME": str(root), "LANG": "C.UTF-8",
               "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "LITELLM_LOCAL_MODEL_COST_MAP": "True", "DO_NOT_TRACK": "1"}
        env["AI_GATEWAY_CONFIG_PATH"] = str(source / "first-run.yml")
        env["AI_GATEWAY_LITELLM_API_KEY"] = "sk-fresh-instance-test"
        env["LITELLM_MASTER_KEY"] = env["AI_GATEWAY_LITELLM_API_KEY"]
        gateway_socket, gateway_port = reserve_port(stack)
        engine_socket, engine_port = reserve_port(stack)
        substrate_socket, substrate_port = reserve_port(stack)
        if not compose:
            env["AI_GATEWAY_LITELLM_BASE_URL"] = f"http://127.0.0.1:{substrate_port}"
        log_path = root / "process.log"
        log = stack.enter_context(log_path.open("wb"))

        def failure_diagnostics():
            if sys.exc_info()[0] is not None:
                log.flush()
                # This log is from synthetic processes with a cleared environment;
                # redact even generated test keys before showing diagnostics.
                tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-35:])
                tail = re.sub(r"(?:opk_|oak_|sk-)[A-Za-z0-9_-]+", "[redacted]", tail)
                print(tail, file=sys.stderr)
        stack.callback(failure_diagnostics)

        def launch(command):
            process = subprocess.Popen(command, cwd=source, env=env, stdout=log,
                                       stderr=log, start_new_session=True)
            stack.callback(stop, process)
            return process

        if compose:
            project = root.name
            override = root / "compose.json"
            override.write_text(json.dumps({"services": {
                "gateway": {"build": {"context": str(source)}, "image": f"{project}-gateway",
                            "volumes": [f"{source / 'data'}:/app/data"], "restart": "no"},
                "litellm": {"volumes": [f"{source / 'data'}:/app/data"], "restart": "no",
                            "environment": {
                                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                                "DO_NOT_TRACK": "1",
                            }},
                "fake-engine": {"image": f"{project}-gateway", "command": ["python3", "/fake_engine.py",
                    "--host", "0.0.0.0", "--port", "18001"],
                    "volumes": [f"{source / 'tests/e2e/fake_engine.py'}:/fake_engine.py:ro"],
                    "restart": "no"}
            }}))
            env["OPENAI_PROXY_PORT"] = str(gateway_port)
            command = ["docker", "compose", "--project-name", project, "--project-directory", str(source),
                       "-f", str(source / "docker-compose.yml"), "-f", str(override)]

            def down():
                subprocess.run(command + ["down", "--volumes", "--remove-orphans", "--rmi", "local"],
                               env=env, stdout=log, stderr=log, check=True, timeout=90)
            stack.callback(down)
            print("Starting disposable Compose gateway, LiteLLM, and fake engine...", flush=True)
            subprocess.run(command + ["pull", "litellm"], env=env, stdout=log,
                           stderr=log, check=True, timeout=600)
            subprocess.run(command + ["build", "gateway"], env=env, stdout=log,
                           stderr=log, check=True, timeout=600)
            gateway_socket.close()
            subprocess.run(command + ["up", "--no-build", "--pull", "never", "-d"], env=env, stdout=log,
                           stderr=log, check=True, timeout=90)
            engine_url = "http://fake-engine:18001/v1"
        else:
            env["OPENAI_PROXY_PORT"] = str(gateway_port)
            gateway_socket.close()
            launch([sys.executable, "proxy.py"])
            engine_socket.close()
            launch([sys.executable, str(REPO / "tests/e2e/fake_engine.py"), "--port", str(engine_port)])
            engine_url = f"http://127.0.0.1:{engine_port}/v1"

        client = stack.enter_context(httpx.Client(base_url=f"http://127.0.0.1:{gateway_port}",
                                                 trust_env=False, timeout=10, follow_redirects=False))
        wait_ready(client, "/health")
        print("PASS fresh gateway health", flush=True)
        assert_fresh_setup_entry(client, compose)
        password = "Fresh-test-password-" + os.urandom(8).hex()
        expect(client.post("/setup/local-owner", data={"email": "owner@example.test",
            "password": password, "password_confirm": password}), 303)
        actor = expect(client.get("/admin/api/session"), 200).json()["actor"]
        assert "owner" in actor["roles"]
        csrf = actor["csrf_token"]
        assert csrf
        print("PASS owner claimed through /setup/local-owner", flush=True)
        page = expect(client.post("/setup/first-app", data={"app_name": "first-app",
                            "key_label": "Fresh test", "csrf_token": csrf}), 201)
        match = re.search(r"opk_[A-Za-z0-9_-]+", page.text)
        assert match, "Setup did not return the one-time runtime key"
        key = match.group()
        print("PASS runtime App key created through /setup/first-app (redacted)", flush=True)
        headers = {"x-ai-gateway-csrf": csrf, "x-ai-gateway-admin-action": "1"}
        registry = expect(client.get("/admin/api/operator/service-registry"), 200)
        headers["If-Match"] = registry.headers["etag"]
        created = expect(client.post("/admin/api/operator/services", headers=headers, json={
            "name": "First chat engine", "preset": "openai-compatible", "base_url": engine_url,
            "health_endpoint": "/v1/models",
            "capabilities": ["chat"], "models": [{"public": MODEL, "upstream": MODEL}]
        }), 201).json()["service_registry"]
        service = next(row for row in created["services"].values() if row["name"] == "First chat engine")
        assert service["config"]["model_policy_schema_version"] == 1, "New model mappings lack their policy schema"
        print("PASS engine registered through the documented admin API", flush=True)
        if not compose:
            with httpx.Client(base_url=engine_url, trust_env=False, timeout=5) as engine:
                wait_ready(engine, "/models")
        if not compose:
            # Use the same gateway-generated configuration as Compose.
            wait_file(source / "data/litellm.config.yaml")
            substrate_socket.close()
            sidecar = launch([str(Path(sys.executable).parent / "litellm"), "--config", "data/litellm.config.yaml",
                              "--host", "127.0.0.1", "--port", str(substrate_port)])
            wait_litellm_ready(env["AI_GATEWAY_LITELLM_BASE_URL"], env["AI_GATEWAY_LITELLM_API_KEY"], sidecar)
        deadline = time.monotonic() + 120
        while True:
            models = expect(client.get("/openai/v1/models", headers={"Authorization": f"Bearer {key}"}), 200).json()
            if MODEL in {model["id"] for model in models["data"]}:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Registered model missing from catalog after LiteLLM startup")
            time.sleep(1)
        print("PASS registered model visible in the authenticated catalog", flush=True)
        payload = {"model": MODEL, "messages": [{"role": "user", "content": "Hello"}], "stream": False}
        expect(client.post("/openai/v1/chat/completions", json=payload), 401)
        print("PASS chat rejects an unauthenticated client", flush=True)
        deadline = time.monotonic() + 120
        while True:
            response = client.post("/openai/v1/chat/completions", json=payload,
                                   headers={"Authorization": f"Bearer {key}"})
            if response.status_code == 200:
                break
            error = response.json().get("error", {})
            code = str(error.get("code", "unknown"))
            if not re.fullmatch(r"[a-z_]{1,80}", code):
                code = "upstream_error"
            if response.status_code not in {502, 503} or code != "backend_unavailable" or time.monotonic() >= deadline:
                raise RuntimeError(f"Chat failed with HTTP {response.status_code}, code {code}")
            time.sleep(1)
        content = response.json()["choices"][0]["message"]["content"]
        if content != ANSWER:
            raise RuntimeError("Chat content did not match the deterministic engine answer")
        print(f"PASS authenticated chat: {ANSWER}", flush=True)
        assert_catalog_ready_after_chat(client)
    print("PASS teardown: processes, Compose resources, and temporary state removed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", action="store_true", help="Test the shipped Docker Compose deployment")
    args = parser.parse_args()
    try:
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        run(args.compose)
    except KeyboardInterrupt:
        print("FAIL fresh-instance journey interrupted; teardown completed", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # No raw response/log/exception data: those can include generated keys.
        detail = str(exc) if isinstance(exc, (RuntimeError, AssertionError)) else "check the failing step above"
        print(f"FAIL fresh-instance journey ({type(exc).__name__}): {detail}", file=sys.stderr)
        sys.exit(1)
