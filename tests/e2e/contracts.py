"""Exercise product contracts over disposable gateway and fake engine HTTP.

Only loopback ports 18000–18999 and worktree-owned state are used. Lifecycle
observation is a fixture: no host service manager is called or engine started.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ui.fake_engine import app as engine_app


@engine_app.get("/system_stats")
def system_stats():
    return {"system": {"os": "fixture"}, "devices": []}


def reserve(stack):
    for port in range(18000, 19000):
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            listener.close()
            continue
        stack.callback(listener.close)
        return listener, port
    raise RuntimeError("No free fixture ports")


def stop(child):
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)


def expect(response, status=200):
    if response.status_code != status:
        # Never print payloads that can contain fixture one-time credentials.
        raise AssertionError(f"{response.request.method} {response.request.url.path}: expected {status}, got {response.status_code}")
    return response


def run():
    with ExitStack() as stack:
        scratch = ROOT / ".ui-e2e-state"
        scratch.mkdir(exist_ok=True)
        state = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="contracts-", dir=scratch)))
        gateway_socket, port = reserve(stack)
        engine_socket, engine_port = reserve(stack)
        config = state / "config.yml"
        # JSON is valid YAML. Known fixture integrations are operator-authored
        # data, including the explicit JSON-path health assertion.
        backends = {name: {"enabled": False, "host": "127.0.0.1", "port": engine_port}
                    for name in ("kobold", "tabby", "embeddings")}
        backends.update({
            "litellm": {"enabled": True, "host": "127.0.0.1", "port": engine_port,
                        "base_url": f"http://127.0.0.1:{engine_port}", "health_endpoint": "/health"},
            "comfyui": {"enabled": True, "host": "127.0.0.1", "port": engine_port,
                        "health_endpoint": "/system_stats", "health_success_path": "system"},
        })
        config.write_text(json.dumps({"proxy": {"host": "127.0.0.1", "port": port},
            "backends": backends, "admin": {"oidc": {"cookie": {"secure": False}}},
            "model_aliases": {"chat": {"stable": {name: "demo-chat" for name in ("fast", "default", "coder", "vision")}}},
            "images": {"cache_dir": str(state / "images"), "vram_estimates_file": str(state / "vram.json")}}))
        env = {"PATH": os.defpath, "HOME": str(state), "PYTHONPATH": str(ROOT),
               "PYTHONDONTWRITEBYTECODE": "1", "AI_GATEWAY_CONFIG_PATH": str(config),
               "AI_GATEWAY_GRACEFUL_SHUTDOWN_S": "1", "AI_GATEWAY_LITELLM_BASE_URL": f"http://127.0.0.1:{engine_port}"}
        for name, filename in {"AI_GATEWAY_ADMIN_DB": "admin.db", "AI_GATEWAY_METRICS_DB": "metrics.db",
            "AI_GATEWAY_RESPONSES_DB": "responses.db", "AI_GATEWAY_RECEIPTS_DB": "receipts.db",
            "AI_GATEWAY_MEDIA_JOBS_DB": "media_jobs.db"}.items():
            env[name] = str(state / filename)
        log = stack.enter_context((state / "process.log").open("wb"))
        engine_socket.close()
        engine = subprocess.Popen([sys.executable, str(Path(__file__)), "engine", str(engine_port)],
                                  cwd=state, env=env, stdout=log, stderr=log, start_new_session=True)
        stack.callback(stop, engine)
        gateway_socket.close()
        gateway = subprocess.Popen([sys.executable, str(Path(__file__)), "gateway", str(port)],
                                   cwd=state, env=env, stdout=log, stderr=log, start_new_session=True)
        stack.callback(stop, gateway)
        client = stack.enter_context(httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if gateway.poll() is not None or engine.poll() is not None:
                raise RuntimeError("Fixture process exited; log withheld because it can contain fixture keys")
            try:
                if client.get("/health").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.2)
        else:
            raise RuntimeError("Fixture readiness timed out")
        expect(client.post("/setup/local-owner", data={"email": "owner@example.test",
            "password": "Fixture contract password 2026", "password_confirm": "Fixture contract password 2026"}), 303)
        actor = expect(client.get("/admin/api/session")).json()["actor"]
        headers = {"x-ai-gateway-csrf": actor["csrf_token"]}

        def post(path, body, extra=None):
            response = client.post(path, json=body, headers={**headers, **(extra or {})})
            assert response.status_code in {200, 201}, f"POST {path}: HTTP {response.status_code}"
            return response.json()

        registry = expect(client.get("/admin/api/operator/service-registry"))
        service = post("/admin/api/operator/services", {"id": "fixture-chat", "name": "Fixture llama",
            "protocol": "openai-compatible", "base_url": f"http://127.0.0.1:{engine_port}",
            "health_endpoint": "/health", "capabilities": ["chat", "completions"],
            "gateway_lifecycle": True, "models": [{"public": "demo-chat", "upstream": "demo-chat"}],
            "model_policy_schema_version": 1, "enabled": True}, {"if-match": registry.headers["etag"]})
        # A lifecycle status fixture replaces host systemd for this process.
        for name, intent in (("decisions", "decisions"), ("realtime", "realtime"), ("rerank", "rerank"), ("face", "image.analyze")):
            registry = expect(client.get("/admin/api/operator/service-registry"))
            post("/admin/api/operator/services", {"id": f"fixture-{name}", "name": f"Fixture {name}",
                "protocol": "openai-compatible", "base_url": f"http://127.0.0.1:{engine_port}",
                "capabilities": [intent], "lifecycle": {"driver": "systemd", "service_name": f"fixture-{name}.service"},
                "enabled": True}, {"if-match": registry.headers["etag"]})
        post("/admin/api/config/reload", {})
        profile = post("/admin/api/profiles", {"name": "Contracts full local", "profile": {"id": "contracts-local"}})["profile"]
        user = expect(client.get("/admin/api/users")).json()["users"][0]
        app = post("/admin/api/apps", {"name": "Try verification", "kind": "personal", "owner_user_id": user["id"], "profile_id": profile["id"]})["app"]
        credential = post(f"/admin/api/apps/{app['id']}/keys", {"label": "Fixture Try key"})["credential"]
        key = credential["api_key"]
        auth = {"authorization": f"Bearer {key}"}
        # This is deliberately the first capability read, before owner Status.
        payload = expect(client.get("/gateway/v1/capabilities", headers=auth)).json()
        rows = payload["capabilities"]
        identifiers = [(row["intent"], row.get("model") or row.get("workflow")) for row in rows]
        assert len(identifiers) == len(set(identifiers)), "Duplicate caller-visible capability"
        chat = {row.get("model"): row for row in rows if row["intent"] == "chat.completions"}
        for name in ("fast", "default", "coder", "vision", "demo-chat"):
            assert chat[name]["availability"]["status"] == "ready", f"{name} did not inherit ready target"
        workflows = [row for row in rows if row["intent"] in {"image.generate", "video.generate", "audio.generate"} and row.get("workflow")]
        assert workflows, "No configured workflows found"
        assert all(row["availability"]["status"] == "ready" and row["availability"]["evidence"].get("observed_at_s") for row in workflows), json.dumps([
            {"intent": row["intent"], "model": row.get("model"), "workflow": row.get("workflow"),
             "availability": row["availability"]} for row in workflows if row["availability"]["status"] != "ready"])
        print(f"PASS cold discovery: {len(rows)} unique caller entries; chat aliases ready; {len(workflows)} media workflows have timestamped health", flush=True)
        report = subprocess.run([sys.executable, str(ROOT / "scripts/local/availability_report.py"), str(client.base_url).rstrip("/")],
            env={"PATH": os.defpath, "AI_GATEWAY_API_KEY": key}, capture_output=True, text=True, check=True)
        print(report.stdout, end="", flush=True)
        from pydantic import TypeAdapter
        from gateway.product_contracts import CapabilitiesResponse, MemberStatus, ModelsCatalog, SetupProgress
        TypeAdapter(CapabilitiesResponse).validate_python(payload)
        TypeAdapter(ModelsCatalog).validate_python(expect(client.get("/me/api/models/catalog")).json())
        member_status = expect(client.get("/me/api/status")).json()
        TypeAdapter(MemberStatus).validate_python(member_status)
        assert not any(field in json.dumps(member_status) for field in ("127.0.0.1", "fixture-chat", "service_name", "vram"))
        print("PASS generated contracts validate real discovery, Models, scoped Status responses", flush=True)
        request = expect(client.post("/v1/chat/completions", headers=auth,
            json={"model": "fast", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1})).json()
        assert request["choices"][0]["message"]["content"]
        progress = expect(client.get("/admin/api/setup-progress")).json()
        TypeAdapter(SetupProgress).validate_python(progress)
        assert not next(step for step in progress["steps"] if step["id"] == "connect_app")["done"]
        post("/admin/api/setup-evidence", {"event": "external_client_confirmed", "app_id": app["id"]}, {"x-ai-gateway-user-action": "1"})
        progress = expect(client.get("/admin/api/setup-progress")).json()
        assert next(step for step in progress["steps"] if step["id"] == "connect_app")["done"]
        print("PASS real internal request leaves connect_app incomplete; explicit owned-app confirmation completes it", flush=True)
        configured = "https://gateway.example.test"
        expect(client.put("/admin/api/settings/instance", headers=headers,
            json={"name": "Fixture gateway", "base_url": configured, "theme_default": "system"}))
        for endpoint in ("/.well-known/ai-gateway", "/.well-known/openai-proxy"):
            metadata = expect(client.get(endpoint, headers={"x-forwarded-host": "unrelated.test", "x-forwarded-proto": "http"})).json()
            assert metadata["base_url"] == configured and metadata["service"]["canonical_host"] == configured
            assert "compatibility_aliases" not in metadata["service"]
        assert configured in expect(client.get("/llms.txt")).text
        print("PASS Settings origin governs both well-known documents and llms.txt", flush=True)
    print("PASS disposable fixture servers stopped and state removed", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "engine":
        import uvicorn
        uvicorn.run(engine_app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
    elif len(sys.argv) > 1 and sys.argv[1] == "gateway":
        from gateway import lifecycle_drivers
        async def stopped(self, service):
            return lifecycle_drivers.ProcessState.STOPPED
        lifecycle_drivers.SystemdDriver.status = stopped
        import uvicorn
        uvicorn.run("proxy:app", host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
    else:
        run()
