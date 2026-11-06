"""A kept 3D-model request names what it ran, from the route's own data.

The mesh route has no ``model`` in its body, so the kept summary used to carry nothing History could title the
request with. The route now says which workflow it ran (its display name) and its capability, and the summary keeps
both; History titles the request from them. The route is the real one, behind the real capture middleware and a
real admin and media database; only auth, the observer and the downstream engine are the route tests' fakes.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from gateway import admin_store, media_jobs, request_history, request_history_capture, retention
from gateway.mesh_api import MESH_PATH
from tests.support.gateway import use_admin_db
from tests.test_request_history_capture import _kept_rows
from tests.test_mesh_route import GLB, Harness, PNG


def test_run_facts_keep_short_text_and_fill_the_capability_label():
    facts = request_history.run_facts({"workflow": "w1", "title": "  Name  ", "capability": "mesh", "extra": "x"})
    assert facts == {"workflow": "w1", "title": "Name", "capability": "mesh", "capability_label": "3D models"}
    assert request_history.run_facts(None) == {}
    assert request_history.run_facts({"title": 3, "capability": "unlisted-kind"}) == {"capability": "unlisted-kind"}


def test_a_summary_without_run_facts_reads_as_it_always_did():
    summary = request_history.summarize("/v1/chat/completions", {"model": "m"}, 200, 5)
    assert "run" not in summary


def test_a_kept_mesh_request_records_the_workflow_name_and_capability(monkeypatch, tmp_path):
    admin_db = use_admin_db(monkeypatch, tmp_path)
    person_id = admin_store.upsert_oidc_user("https://fixture.test", "mesh-person", path=admin_db)["id"]
    app_record = admin_store.create_app("Mesh app", kind="personal", owner_user_id=person_id,
                                        access_plan="person", path=admin_db)
    admin_store.put_prompt_memory_preference(
        person_id, remember=True,
        retention=retention.bundled().member_defaults(retention.REQUEST_HISTORY)["retention"],
        group=retention.GROUP_REQUEST_HISTORY, path=admin_db,
    )
    media_db = tmp_path / "media_jobs.db"
    media_jobs.init_db(media_db)

    harness = Harness()
    harness.store_output = lambda auth, output: "ast_" + "c" * 64
    app = harness.app()
    identity = SimpleNamespace(person_id=person_id, app_id=app_record["id"], auth_kind="app_key",
                               credential_id="credential-mesh")

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.request_history_identity = SimpleNamespace(identity=identity)
        return await call_next(request)

    app.add_middleware(request_history_capture.RequestHistoryMiddleware,
                       admin_path=lambda: admin_db, media_path=lambda: media_db)

    response = TestClient(app).post(MESH_PATH, data={}, files={"image": ("picture.png", PNG, "image/png")})
    assert response.status_code == 200, response.text
    assert response.content == GLB

    [row] = _kept_rows(media_db)
    summary = json.loads(row["summary_json"])
    assert summary["route"] == MESH_PATH
    assert summary["model"] is None
    assert summary["run"] == {
        "workflow": "mesh.generate.trellis-2",
        "title": "TRELLIS.2 image to 3D",
        "capability": "mesh",
        "capability_label": "3D models",
        # The library item the run made, so History can open it.
        "asset_id": "ast_" + "c" * 64,
    }
