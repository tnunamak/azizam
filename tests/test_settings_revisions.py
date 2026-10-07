"""Settings CAS and account-state regressions use only isolated fixture databases."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import accounts, accounts_api, admin_store
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
    app.include_router(accounts_api.create_account_router(
        path_provider=lambda: path, auth=lambda request: actor,
        session_reader=lambda request: None, cookie_provider=lambda: None,
        signing_key_provider=lambda: None,
        oidc_provider=lambda: {"enabled": bool((store.get_oidc_config_setting_raw() or {}).get("enabled"))},
    ))
    return TestClient(app), store, actor


@pytest.mark.parametrize('url,key,body', [
    ('/admin/api/settings/instance', 'instance_presentation', {'name': 'Original', 'base_url': '', 'theme_default': 'dark'}),
    ('/admin/api/oidc-config', 'oidc_provider_config', {'enabled': False, 'admin_emails': ['owner@example.test']}),
    ('/admin/api/operator/media-job-admission', 'media_job_admission_overrides', {'max_queue_depth': 3}),
    ('/admin/api/operator/sign-in', 'sign_in', {'mode': 'built-in'}),
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


def test_sign_in_and_provider_drafts_have_independent_revisions(client_store):
    client, store, _ = client_store
    provider_url = '/admin/api/oidc-config'
    mode_url = '/admin/api/operator/sign-in'
    provider_revision = client.get(provider_url).json()['revision']
    mode_revision = client.get(mode_url).json()['revision']
    provider_draft = {'enabled': False, 'admin_emails': ['draft@example.test']}

    mode_save = client.put(mode_url, json={'mode': 'built-in'}, headers={'If-Match': mode_revision})
    assert mode_save.status_code == 200, mode_save.text
    saved_mode_revision = mode_save.json()['revision']
    assert saved_mode_revision != mode_revision
    assert client.get(provider_url).json()['revision'] == provider_revision

    provider_save = client.put(provider_url, json=provider_draft, headers={'If-Match': provider_revision})
    assert provider_save.status_code == 200, provider_save.text
    saved_provider_revision = provider_save.json()['revision']
    assert saved_provider_revision != provider_revision
    assert client.get(mode_url).json()['revision'] == saved_mode_revision
    # A provider save must likewise leave an existing mode draft valid.
    assert client.put(mode_url, json={'mode': 'built-in'}, headers={'If-Match': saved_mode_revision}).status_code == 200

    stale_provider = client.put(provider_url, json={'enabled': False, 'admin_emails': ['stale@example.test']}, headers={'If-Match': provider_revision})
    assert stale_provider.status_code == 409
    assert stale_provider.json()['error']['code'] == 'settings_conflict'
    assert store.get_oidc_config_setting_raw()['admin_emails'] == ['draft@example.test']
    assert store.setting_revision('oidc_provider_config') == saved_provider_revision
    assert len(admin_store.list_audit(path=store.path)) == 3


@pytest.mark.parametrize('method', ['GET', 'PUT'])
@pytest.mark.parametrize('change_before_read', [False, True])
def test_oidc_response_retries_projection_race(client_store, method, change_before_read):
    _, store, actor = client_store
    admin_store.put_operator_setting('oidc_provider_config', {'enabled': False, 'admin_emails': ['original@example.test']}, path=store.path)
    initial_revision = store.setting_revision('oidc_provider_config')
    projections = 0

    def effective_config():
        nonlocal projections
        projections += 1
        def concurrent_change():
            admin_store.put_operator_setting('oidc_provider_config', {'enabled': False, 'admin_emails': ['concurrent@example.test']}, path=store.path)
        if projections == 1 and change_before_read:
            concurrent_change()
        config = {**store.get_oidc_config_setting(), 'source': 'operator-setting'}
        if projections == 1 and not change_before_read:
            concurrent_change()
        return config

    app = FastAPI()
    app.include_router(create_admin_router(store, auth=lambda request: actor, effective_oidc_config_provider=effective_config))
    with TestClient(app) as client:
        response = client.request(method, '/admin/api/oidc-config', json={'enabled': False, 'admin_emails': ['submitted@example.test']} if method == 'PUT' else None,
                                  headers={'If-Match': initial_revision})
    assert response.status_code == 200, response.text
    assert projections == 2
    assert response.json()['revision'] == store.setting_revision('oidc_provider_config')
    assert response.json()['oidc_config']['admin_emails'] == ['concurrent@example.test']
    assert response.json()['oidc_config']['source'] == 'operator-setting'
    assert len(admin_store.list_audit(path=store.path)) == (1 if method == 'PUT' else 0)


@pytest.mark.parametrize('method', ['GET', 'PUT'])
def test_oidc_response_bounds_projection_retries(client_store, method):
    _, store, actor = client_store
    projections = 0

    def effective_config():
        nonlocal projections
        projections += 1
        admin_store.put_operator_setting('oidc_provider_config', {'enabled': False}, path=store.path)
        return store.get_oidc_config_setting()

    app = FastAPI()
    app.include_router(create_admin_router(store, auth=lambda request: actor, effective_oidc_config_provider=effective_config))
    with TestClient(app) as client:
        response = client.request(method, '/admin/api/oidc-config', json={'enabled': False} if method == 'PUT' else None,
                                  headers={'If-Match': '0'})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'settings_snapshot_unavailable'
    assert response.headers['Retry-After'] == '1'
    assert projections == 3
    assert len(admin_store.list_audit(path=store.path)) == (1 if method == 'PUT' else 0)
