"""A synthetic engine that takes named files, for the declared-job file binding tests.

The engine is a plain ASGI app on the fake upstream router. It speaks the d0-smell wire plus ``POST /v1/refine``, which
takes multipart parts. It records exactly what it received (part name, file name, media type, bytes), so a test can
compare the bytes that reached the engine with the bytes the caller bound. Nothing here patches product code.
"""
from __future__ import annotations

import base64
import email.parser
import email.policy
import json
import time
from pathlib import Path

from tests.test_owner_engine_chain_smell import SmellEngine, _add_engine, _write_headers
from tests.test_signin_who_can_enter import csrf_for

DESCRIPTION = Path(__file__).resolve().parents[1] / "fixtures" / "engine_protocol" / "d0" / "smell-file.yml"
ENGINE_ADDRESS = "127.0.0.1:18302"
VECTOR = [0.25, -1.5, 3.0, 0.0]
# A real, tiny PNG and a real mono WAV with one silent sample: bytes the Library's own type measurement recognises.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1sAAAAASUVORK5CYII="
)
WAV = bytes.fromhex(
    "524946462600000057415645666d74201000000001000100401f0000803e00000200100064617461020000000000"
)


class FileEngine(SmellEngine):
    """Serves one result file for every job and records each multipart submit it gets."""

    def __init__(self) -> None:
        super().__init__()
        self.result = (PNG, "image/png")
        self.refines: list[dict[str, dict]] = []

    async def __call__(self, scope, receive, send):
        content, media_type = self.result
        if scope["method"] == "GET" and scope["path"] == "/v1/infer/smell-1/molecule":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", media_type.encode()), (b"content-length", str(len(content)).encode())]})
            await send({"type": "http.response.body", "body": content})
            return
        if scope["method"] == "POST" and scope["path"] == "/v1/refine":
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            headers = dict(scope["headers"])
            parsed = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + headers[b"content-type"] + b"\r\n\r\n" + body)
            parts = {}
            for part in parsed.iter_parts():
                parts[part.get_param("name", header="content-disposition")] = {
                    "filename": part.get_filename(), "content_type": part.get_content_type(),
                    "bytes": part.get_payload(decode=True)}
            self.refines.append(parts)
            reply = json.dumps({"job": {"id": "smell-1"}}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(reply)).encode())]})
            await send({"type": "http.response.body", "body": reply})
            return
        await super().__call__(scope, receive, send)


def start_engine(client, fake_upstreams) -> FileEngine:
    """Install the description, add the engine record and turn it on, through the owner routes the web UI uses."""
    installed = client.put(
        "/admin/api/engine-protocols/d0-smell-file", content=DESCRIPTION.read_text(encoding="utf-8"),
        headers={**csrf_for(client), "Content-Type": "application/yaml", "If-None-Match": "*"})
    assert installed.status_code == 201, installed.text
    engine = FileEngine()
    fake_upstreams.router.add(ENGINE_ADDRESS, engine)
    created = _add_engine(
        client, capabilities=["smell.infer", "smell.refine"],
        models=[{"id": "smell-1", "public_name": "smell-1"}],
        values={"address": f"http://{ENGINE_ADDRESS}", "dispatch": "protocol", "protocol_description": "d0-smell-file"})
    assert created.status_code == 201, created.text
    enabled = client.patch("/admin/api/operator/services/smell-box", headers=_write_headers(client), json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    return engine


def app_key(client, owner_id, plan_id, name: str = "smell-app") -> str:
    """A key for a service app. A second name makes a second app, which owns nothing the first one made."""
    app = client.post("/admin/api/apps", headers=csrf_for(client),
                      json={"name": name, "kind": "service", "access_plan": plan_id, "owner_user_id": owner_id})
    assert app.status_code == 201, app.text
    key = client.post(f"/admin/api/apps/{app.json()['app']['id']}/keys", headers=csrf_for(client), json={"display_name": "k"})
    assert key.status_code == 201, key.text
    return key.json()["credential"]["api_key"]


def headers(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + key}


def submit(public, key, body):
    return public.post("/gateway/v1/media/jobs", headers=headers(key), json=body)


def wait(public, key, submitted):
    job = None
    for _ in range(100):
        response = public.get("/gateway/v1/media/jobs/" + submitted["id"], headers=headers(key))
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError(job)


def make_asset(public, key) -> str:
    """Run an infer job whose result file lands in the key's Library; return that asset's id."""
    response = submit(public, key, {"intent": "smell.infer", "model": "smell-1", "input": {"vector": VECTOR}})
    assert response.status_code == 202, response.text
    job = wait(public, key, response.json())
    assert job["status"] == "succeeded", job
    (asset,) = job["assets"]
    return asset["asset_id"]


def refine(vector=VECTOR, **files):
    body = {"intent": "smell.refine", "model": "smell-1", "input": {"vector": vector}}
    if files:
        body["files"] = {name: {"asset_id": asset_id} for name, asset_id in files.items()}
    return body
