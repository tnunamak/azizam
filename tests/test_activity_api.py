"""Owner activity privacy, pagination, and receipt projection contracts."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, receipt_store
from gateway.activity_api import create_activity_router
from gateway.admin_api import AdminAuthError, AdminContext
from tests.support.access import service_app


@pytest.fixture
def activity(tmp_path):
    admin = tmp_path / 'admin.db'
    receipts = tmp_path / 'receipts.db'
    admin_store.init_db(admin)
    receipt_store.init_db(receipts)
    people = [admin_store.upsert_oidc_user('test', name, display_name=name, path=admin)
              for name in ('Alice', 'Bob')]
    apps = [admin_store.create_app(name, kind='personal', owner_user_id=person['id'], access_plan="person", path=admin)
            for name, person in zip(('Desktop', 'Phone'), people)]
    service_account = admin_store.ensure_service_account("svc_daisy", path=admin)
    service = service_app("Daisy", path=admin)
    def auth(request):
        role = request.headers.get('test-role', 'owner')
        if role == 'anonymous':
            raise AdminAuthError('Sign in', status_code=401)
        return AdminContext(subject='tester', roles=frozenset({role}))
    app = FastAPI()
    app.include_router(create_activity_router(auth=auth, admin_path=admin, receipts_path=receipts))
    return TestClient(app), receipts, people, apps, service


def seed(path, app, id):
    receipt_store.record({'request_id': id, 'intent': 'chat', 'status': 'succeeded',
                         'app_id': app['id'], 'metadata': {'private_note': 'never expose this'}}, created_at=1700000000, path=path)


def test_owner_reads_sanitized_receipts_and_stable_pages(activity):
    client, path, _, apps, _ = activity
    for id in ('a', 'b', 'c'):
        seed(path, apps[0], id)
    first = client.get('/admin/api/activity/requests?limit=2').json()
    assert [r['receipt']['request_id'] for r in first['records']] == ['c', 'b']
    assert first['records'][0]['person_label'] == 'Alice'
    assert first['records'][0]['app_label'] == 'Desktop'
    assert 'never expose this' not in json.dumps(first)
    second = client.get('/admin/api/activity/requests', params={'before': first['next_cursor'], 'limit': 2}).json()
    assert [r['receipt']['request_id'] for r in second['records']] == ['a']
    assert second['next_cursor'] is None


def test_person_filter_is_server_scoped_and_unknown_is_empty(activity):
    client, path, people, apps, _ = activity
    seed(path, apps[0], 'alice')
    seed(path, apps[1], 'bob')
    response = client.get('/admin/api/activity/requests', params={'person_id': people[1]['id']}).json()
    assert [r['receipt']['request_id'] for r in response['records']] == ['bob']
    assert client.get('/admin/api/activity/requests?person_id=missing').json()['records'] == []


def test_all_receipts_includes_nested_execution_receipts(activity):
    client, path, _, apps, _ = activity
    seed(path, apps[0], 'outer')
    receipt_store.record({'request_id': 'inner', 'parent_request_id': 'outer',
                         'intent': 'chat', 'status': 'succeeded', 'app_id': apps[0]['id']},
                         created_at=1700000001, path=path)
    records = client.get('/admin/api/activity/requests').json()['records']
    assert {row['receipt']['request_id'] for row in records} == {'inner', 'outer'}


@pytest.mark.parametrize(('role', 'status'), [('member', 403), ('admin', 403), ('anonymous', 401)])
def test_non_owners_cannot_read_any_receipts(activity, role, status):
    client, path, _, apps, _ = activity
    seed(path, apps[0], 'private')
    response = client.get('/admin/api/activity/requests', headers={'test-role': role})
    assert response.status_code == status
    assert 'private' not in response.text


def test_empty_and_invalid_cursor(activity):
    client, _, _, _, _ = activity
    assert client.get('/admin/api/activity/requests').json()['records'] == []
    assert client.get('/admin/api/activity/requests?before=broken').status_code == 400
    assert client.get('/admin/api/activity/requests?before=%5B1%5D').status_code == 400
    for cursor in ('[true, "id"]', '[NaN, "id"]', '[Infinity, "id"]'):
        assert client.get('/admin/api/activity/requests', params={'before': cursor}).status_code == 400
    assert client.get('/admin/api/activity/requests?limit=101').status_code == 422


def test_owner_activity_labels_and_filters_sources(activity):
    client, path, _, apps, service = activity
    seed(path, apps[0], 'alice')
    receipt_store.record({'request_id': 'service', 'intent': 'chat', 'status': 'succeeded',
                         'app_id': service['id']}, created_at=1700000001, path=path)
    receipt_store.record({'request_id': 'local', 'intent': 'chat', 'status': 'succeeded',
                         'app_id': None}, created_at=1700000002, path=path)

    all_rows = client.get('/admin/api/activity/requests').json()
    assert [row['receipt']['request_id'] for row in all_rows['records']] == ['local', 'service', 'alice']
    assert {'local-network', f"app:{service['id']}"} <= {source['id'] for source in all_rows['sources']}
    assert all_rows['records'][0]['source']['kind'] == 'local-network'
    assert all_rows['records'][0]['app_label'] == 'Local network · no key'
    assert all_rows['records'][1]['source']['label'] == 'Daisy'

    local = client.get('/admin/api/activity/requests?source=local-network').json()
    assert [row['receipt']['request_id'] for row in local['records']] == ['local']
    service_rows = client.get('/admin/api/activity/requests', params={'source': f"app:{service['id']}"}).json()
    assert [row['receipt']['request_id'] for row in service_rows['records']] == ['service']
