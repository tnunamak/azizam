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
    _, store, actor = client_store
    admin_store.put_operator_setting('sign_in', {'mode': 'oidc'}, path=store.path)
    with pytest.raises(ValueError, match='at least one working sign-in method'):
        store.update_oidc_config_setting(data={'enabled': False}, actor=actor)
    assert store.setting_revision('oidc_provider_config') == '0'
    admin_store.put_operator_setting('sign_in', {'mode': 'built-in'}, path=store.path)
    store.update_oidc_config_setting(data={'enabled': False}, actor=actor)
    assert store.setting_revision('oidc_provider_config') != '0'


def test_oidc_save_is_checked_against_the_revision_the_caller_read(client_store):
    _, store, actor = client_store
    store.update_oidc_config_setting(data={'enabled': False, 'client_id': 'first'}, actor=actor, expected_revision='0')
    with pytest.raises(admin_store.SettingsConflictError):
        store.update_oidc_config_setting(data={'enabled': False, 'client_id': 'stale'}, actor=actor, expected_revision='0')
    assert store.get_oidc_config_setting_raw()['client_id'] == 'first'


def test_oidc_read_merge_race_is_checked_inside_write_transaction(client_store):
    _, store, actor = client_store
    def concurrent_change(candidate):
        admin_store.put_operator_setting('oidc_provider_config', {'enabled': False, 'client_id': 'new@example.test'}, path=store.path)
    store.oidc_config_validator = concurrent_change
    with pytest.raises(admin_store.SettingsConflictError):
        store.update_oidc_config_setting(data={'enabled': False, 'client_id': 'stale@example.test'}, actor=actor)
    assert store.get_oidc_config_setting_raw()['client_id'] == 'new@example.test'
