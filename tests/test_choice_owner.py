"""Owner access to engine-derived choice snapshots."""
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway.workflow_owner_api import register_workflow_owner_routes


def test_owner_can_list_choice_freshness():
    entry = SimpleNamespace(id='demo', contract={'inputs': {'pick': {
        'choices_from': {'node_type': 'Node', 'input': 'pick'}}}}, bindings={'default': object()})
    store = SimpleNamespace(snapshot=lambda: SimpleNamespace(workflows={'demo': entry}))
    router = APIRouter(prefix='/admin/api')
    register_workflow_owner_routes(
        router, store=store, guard=lambda request: object(), read_json=lambda request: {},
        resolve_identity=lambda entry, binding_key=None: SimpleNamespace(service_id='svc', origin='http://203.0.113.10:8188'))
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get('/admin/api/workflows/choices')
    assert response.status_code == 200
    assert response.json()['choices'][0]['state'] == 'missing'
