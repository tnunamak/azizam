"""Speech and transcription engines on another computer, through the owner's dialog and the real routes (slice S7).

The owner adds an OpenAI-style speech or transcription server on a private address with the Add engine dialog. The check
reads a fake in process. The gateway then serves a request through the real route, the reviewed adapter and the remote
guard; only the last wire is a fake server, so the guard, the checked address and the allowed headers are the real ones.
Addresses are documentation-style (10.0.0 prefix).
"""

from __future__ import annotations

import io
import json
import wave

import httpx
import pytest

from gateway import admin_store, engine_check
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture
from tests.test_added_speech_engine_serves_speech import _app_key
from tests.test_engine_admin_routes import _edit
from tests.test_engine_definitions_speech import VOICES, _speech_server, _wav_long
from tests.test_engine_definitions_transcription import ASR_ID, ASR_PUBLIC, _clip, _transcription_server
from tests.test_remote_add_engine import LAN, NETWORK, _fake_wire, _jobs_home
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401 - gateway is a fixture

TOKEN = "t0k3n"
ALLOWED_HEADERS = {"host", "content-type", "content-length", "accept", "accept-encoding", "user-agent", "connection"}


@pytest.fixture(autouse=True)
def fixed_baseline(monkeypatch):
    monkeypatch.setattr(engine_check, "_baseline_token", lambda: TOKEN)


def _headers(client):
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def _stored(gateway, service_id):
    found = admin_store.get_service_registry_with_generation(path=gateway.path)
    return ((found or {}).get("value") or {}).get("services", {}).get(service_id)


def _add(owner, definition, name, address, check):
    return owner.post("/admin/api/operator/services", headers=_headers(owner), json={
        "definition": definition, "name": name,
        "values": {"address": address, "where": "network", "network_trusted": True},
        "capabilities": [row["id"] for row in check["capabilities"] if row["ticked"]], "models": check["models"]})


def _checked(owner, definition, address):
    checked = owner.post("/admin/api/engines/check", headers=csrf_for(owner),
                         json={"definition": definition, "values": {"address": address, "where": "network"}})
    assert checked.status_code == 200, checked.text
    check = checked.json()["check"]
    assert check["outcome"] == "ok", check["findings"]
    assert check["remote"]["network"] == NETWORK and check["remote"]["trust_needed"] is True
    return check


def _audio(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_wav_long(), headers={"content-type": "audio/wav"})


def test_a_speech_server_on_another_computer_serves_speech_through_the_guard(gateway, fake_upstreams, monkeypatch, tmp_path):
    _jobs_home(monkeypatch, tmp_path)
    host = f"{LAN}.6:8880"
    address = f"http://{host}"
    _speech_server(fake_upstreams, host=host)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    check = _checked(owner, "openai-speech", address)

    refused = owner.post("/admin/api/operator/services", headers=_headers(owner), json={
        "definition": "openai-speech", "name": "garage-voice", "values": {"address": address, "where": "network"},
        "capabilities": ["speech"], "models": check["models"]})
    assert refused.status_code == 400 and refused.json()["error"]["field"] == "network_trusted", refused.text
    assert _stored(gateway, "garage-voice") is None

    created = _add(owner, "openai-speech", "garage-voice", address, check)
    assert created.status_code == 201, created.text
    record = _stored(gateway, "garage-voice")
    block = record["config"]["connection"]["remote_dispatch"]
    assert block["enabled"] is True and block["transport"] == "private_plaintext" and block["private_cidrs"] == [NETWORK]
    assert block["tasks"] == ["speech"] and "shared_space" not in block
    assert record["config"]["connection"]["address"] == address
    assert [row["id"] for row in record["observations"]["voices"]] == VOICES

    seen: list[httpx.Request] = []

    def server(request):
        seen.append(request)
        return _audio(request)
    _fake_wire(fake_upstreams, host, server)

    spoken = owner.post("/v1/audio/speech", headers={"authorization": f"Bearer {key}"},
                        json={"model": "tts-1", "voice": "am_adam", "input": "hello", "response_format": "wav"})

    assert spoken.status_code == 200, spoken.text
    assert spoken.content[:4] == b"RIFF"
    [request] = [r for r in seen if r.url.path == "/v1/audio/speech"]
    assert json.loads(request.content)["voice"] == "am_adam"
    assert set(request.headers.keys()) <= ALLOWED_HEADERS
    assert request.headers.get("authorization") is None


def test_the_owner_turning_the_engine_off_stops_the_next_speech_call_before_any_request(gateway, fake_upstreams, monkeypatch, tmp_path):
    _jobs_home(monkeypatch, tmp_path)
    host = f"{LAN}.6:8880"
    _speech_server(fake_upstreams, host=host)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    check = _checked(owner, "openai-speech", f"http://{host}")
    assert _add(owner, "openai-speech", "garage-voice", f"http://{host}", check).status_code == 201
    seen: list[httpx.Request] = []
    _fake_wire(fake_upstreams, host, lambda request: (seen.append(request), _audio(request))[1])
    ask = {"model": "tts-1", "voice": "am_adam", "input": "hello", "response_format": "wav"}
    assert owner.post("/v1/audio/speech", headers={"authorization": f"Bearer {key}"}, json=ask).status_code == 200
    answered = len(seen)

    assert _edit(owner, "garage-voice", {"enabled": False}).status_code == 200

    stopped = owner.post("/v1/audio/speech", headers={"authorization": f"Bearer {key}"}, json=ask)
    assert stopped.status_code >= 400
    assert len(seen) == answered


def test_a_transcription_server_on_another_computer_transcribes_through_the_guard(gateway, fake_upstreams, monkeypatch, tmp_path):
    _jobs_home(monkeypatch, tmp_path)
    host = f"{LAN}.7:8000"
    address = f"http://{host}"
    _transcription_server(fake_upstreams, host=host)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    check = _checked(owner, "openai-transcription", address)
    created = _add(owner, "openai-transcription", "garage-ear", address, check)
    assert created.status_code == 201, created.text
    block = _stored(gateway, "garage-ear")["config"]["connection"]["remote_dispatch"]
    assert block["tasks"] == ["transcription"] and block["private_cidrs"] == [NETWORK]

    seen: list[httpx.Request] = []

    def server(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "hello from the other computer"})
    _fake_wire(fake_upstreams, host, server)

    heard = owner.post("/v1/audio/transcriptions", headers={"authorization": f"Bearer {key}"},
                       data={"model": ASR_PUBLIC, "response_format": "json"}, files={"file": ("clip.wav", _clip(), "audio/wav")})

    assert heard.status_code == 200, heard.text
    assert heard.json()["text"] == "hello from the other computer"
    [request] = [r for r in seen if r.url.path == "/v1/audio/transcriptions"]
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert ASR_ID.encode() in request.content
    assert set(request.headers.keys()) <= ALLOWED_HEADERS


def test_node_graph_engines_cannot_be_placed_on_another_computer(gateway):
    owner, *_ = owner_and_admin(gateway)
    refused = owner.post("/admin/api/engines/check", headers=csrf_for(owner), json={
        "definition": "workflow-image-engine",
        "values": {"address": f"http://{LAN}.5:8188", "where": "network", "network_trusted": True}})
    assert refused.status_code == 400 and "on this computer" in refused.json()["error"]["message"]
