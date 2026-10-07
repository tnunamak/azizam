"""Settings CAS and account-state regressions use only isolated fixture databases."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import accounts, admin_store
from gateway.admin_api import AdminContext, create_admin_router
from gateway.admin_runtime import AdminSQLiteStore, runtime_identity_for_app_credential_hash
from gateway.settings_api import create_settings_router


@pytest.fixture
def client_store(tmp_path):
    path = tmp_path / 'admin.db'
    admin_store.init_db(path)
    accounts.initialize(path)
    store = AdminSQLiteStore(path)
    actor = AdminContext(subject='fixture-owner', roles=frozenset({'owner'}), auth_method='bearer')
    app = FastAPI()
    app.include_router(create_admin_router(store, auth=lambda request: actor))
    app.include_router(create_settings_router(auth=lambda request: actor, admin_path=path, config_provider=lambda: {}))
    return TestClient(app), store, actor


@pytest.mark.parametrize('url,key,body', [
    ('/admin/api/settings/instance', 'instance_presentation', {'name': 'Original', 'base_url': '', 'theme_default': 'dark'}),
    ('/admin/api/oidc-config', 'oidc_provider_config', {'enabled': False, 'admin_emails': ['owner@example.test']}),
    ('/admin/api/operator/media-job-admission', 'media_job_admission_overrides', {'max_queue_depth': 3}),
])
def test_settings_require_revision_and_reject_stale_writes(client_store, url, key, body):
    client, store, _ = client_store
    assert client.put(url, json=body).status_code == 428
    first = client.put(url, json=body, headers={'If-Match': '0'})
    assert first.status_code == 200, first.text
    revision = store.setting_revision(key)
    assert first.json()['revision'] == revision
    assert revision != '0'
    if key == 'media_job_admission_overrides':
        assert client.get('/admin/api/operator/overrides').json()['overrides']['revisions'][key] == revision
    else:
        assert client.get(url).json()['revision'] == revision
    stale = client.put(url, json=body, headers={'If-Match': '0'})
    assert stale.status_code == 409
    assert stale.json()['error']['code'] == 'settings_conflict'
    assert store.setting_revision(key) == revision
    assert len(admin_store.list_audit(path=store.path)) == 1
    assert client.put(url, json=body, headers={'If-Match': revision}).status_code == 200
    assert store.setting_revision(key) != revision  # even identical same-second writes advance


def test_oidc_disable_cannot_strand_oidc_only_sign_in(client_store):
    client, store, _ = client_store
    admin_store.put_operator_setting('sign_in', {'mode': 'oidc'}, path=store.path)
    result = client.put('/admin/api/oidc-config', json={'enabled': False}, headers={'If-Match': '0'})
    assert result.status_code == 400
    assert 'at least one working sign-in method' in result.json()['error']['message']
    assert store.setting_revision('oidc_provider_config') == '0'
    admin_store.put_operator_setting('sign_in', {'mode': 'built-in'}, path=store.path)
    assert client.put('/admin/api/oidc-config', json={'enabled': False}, headers={'If-Match': '0'}).status_code == 200


def test_oidc_read_merge_race_is_checked_inside_write_transaction(client_store):
    client, store, _ = client_store
    def concurrent_change(candidate):
        admin_store.put_operator_setting('oidc_provider_config', {'enabled': False, 'admin_emails': ['new@example.test']}, path=store.path)
    store.oidc_config_validator = concurrent_change
    result = client.put('/admin/api/oidc-config', json={'enabled': False, 'admin_emails': ['stale@example.test']}, headers={'If-Match': '0'})
    assert result.status_code == 409
    assert store.get_oidc_config_setting_raw()['admin_emails'] == ['new@example.test']
