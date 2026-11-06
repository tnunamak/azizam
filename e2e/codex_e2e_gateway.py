"""Fixture-only entrypoint: the branch gateway with raw Responses capture."""

from __future__ import annotations

import json
import os
import sys
from hashlib import sha256
from pathlib import Path

from scripts.ui.fixture_guard import install, parse_allowed_ports


port, backend_port = map(int, sys.argv[1:])
install({port, backend_port, *parse_allowed_ports(os.environ.get("UI_E2E_ALLOWED_PORTS", ""))})

from gateway import admin_store
from gateway.runtime_auth import hash_api_key

db_path = Path(os.environ["AZIZAM_ADMIN_DB"])
admin_store.init_db(db_path)
profile = admin_store.create_profile(
    "Azizam e2e",
    {"id": "azizam-e2e", "protocols": ["openai"], "models": ["azizam-e2e"], "backends": ["litellm"]},
    path=db_path,
)
service = admin_store.ensure_service_account("azizam-e2e", path=db_path)
app_record = admin_store.create_app(
    "Azizam e2e", kind="service", owner_service_account_id=service["id"], default_profile_id=profile["id"], path=db_path,
)
admin_store.create_credential_hash(
    subject_kind="app", subject_id=app_record["id"], app_id=app_record["id"],
    hash=hash_api_key(os.environ["AZIZAM_E2E_DUMMY_KEY"]), prefix="azizam_e2e", profile_id=profile["id"], path=db_path,
)

import uvicorn
from proxy import app


@app.middleware("http")
async def capture_responses_request(request, call_next):
    if request.url.path == "/v1/responses":
        body = await request.body()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"invalid_json": True}
        response = await call_next(request)
        parsed["_azizam_e2e"] = {
            "authorization_hash": sha256(request.headers.get("authorization", "").encode()).hexdigest(),
            "status": response.status_code,
        }
        with Path(os.environ["CODEX_E2E_GATEWAY_LOG"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(parsed, sort_keys=True) + "\n")
        return response
    return await call_next(request)


uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
