"""Provider names are public text, configurable without changing OIDC trust."""
from dataclasses import replace

import pytest

from gateway import admin_store
from gateway.admin_oidc import OIDCConfigError, load_oidc_config, validate_oidc_stored_override
from test_browser_signin_cutover import browser_gateway, config
from test_settings_revisions import client_store


@pytest.mark.parametrize("name", ["Household accounts", "Google", "River Library", "", "é" * 80])
def test_config_loads_public_display_name(name):
    assert load_oidc_config({"display_name": name}).display_name == name


INVALID_NAMES = ["a" * 81, "<b>Google</b>", "Name\n", "Name\t", "Name\x00", "Name\x7f", "Name\x85", "Name\u202e", "Name\u2028", 42]


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_config_rejects_invalid_display_name_even_when_disabled(name):
    with pytest.raises(OIDCConfigError):
        load_oidc_config({"enabled": False, "display_name": name})
    with pytest.raises(OIDCConfigError):
        validate_oidc_stored_override({"display_name": name})
    with pytest.raises(OIDCConfigError):
        replace(config(), display_name=name)


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_saving_an_invalid_name_is_refused_without_persisting(client_store, name):
    _, store, actor = client_store
    with pytest.raises(ValueError):
        store.update_oidc_config_setting(data={"display_name": name}, actor=actor)
    assert store.get_oidc_config_setting_raw() is None


def test_owner_can_save_read_and_clear_name(client_store):
    _, store, actor = client_store
    store.update_oidc_config_setting(data={"display_name": "Household accounts"}, actor=actor)
    assert store.get_oidc_config_setting()["display_name"] == "Household accounts"
    store.update_oidc_config_setting(data={"display_name": None}, actor=actor)
    assert store.get_oidc_config_setting_raw()["display_name"] == ""


@pytest.mark.parametrize("name,expected", [("Household accounts", "Household accounts"), ("", "single sign-on")])
def test_public_api_uses_resolved_display_name(browser_gateway, monkeypatch, name, expected):
    proxy, client, _ = browser_gateway
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", replace(config(), display_name=name))
    response = client.get("/auth/api/options")
    assert response.status_code == 200
    assert response.json()["oidc_label"] == expected


def test_env_name_and_database_override_reach_public_api(browser_gateway, monkeypatch):
    proxy, client, path = browser_gateway
    monkeypatch.setenv("AZIZAM_ADMIN_OIDC_DISPLAY_NAME", "Google")
    monkeypatch.setattr(proxy, "admin_cfg", {})
    bootstrap = proxy._bootstrap_admin_oidc_cfg()
    assert load_oidc_config(bootstrap, env={}).display_name == "Google"
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", replace(config(), display_name="Google"))
    assert client.get("/auth/api/options").json()["oidc_label"] == "Google"
    admin_store.put_operator_setting("oidc_provider_config", {"display_name": "River Library"}, path=path)
    assert client.get("/auth/api/options").json()["oidc_label"] == "River Library"
    admin_store.put_operator_setting("oidc_provider_config", {"display_name": ""}, path=path)
    assert client.get("/auth/api/options").json()["oidc_label"] == "single sign-on"
