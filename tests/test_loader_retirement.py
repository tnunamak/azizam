"""The old loader and the Tabby idle-unload loop are gone and must not return."""

import pytest

import proxy


@pytest.mark.parametrize("name", [
    "proxy_request", "load_model", "TABBY_LOAD_TIMEOUT", "MAX_LOAD_FAILURES",
    "LOAD_FAILURE_BACKOFF", "backend_for_host", "MODEL_REQUIRED_PATHS",
    "unload_model", "is_model_loaded", "maintenance_loop", "check_tabby_health",
    "get_tabby_model_config", "get_tabby_loaded_model_id",
    "currently_loaded_tabby_model", "TABBY_HOST", "TABBY_IDLE_TIMEOUT",
    "ADMIN_KEY", "API_KEY", "DEFAULT_MODEL", "CHECK_INTERVAL", "NO_ACTIVITY_PATHS",
])
def test_old_executor_and_loader_are_absent(name):
    assert not hasattr(proxy, name)


@pytest.mark.parametrize("name", [
    "model_loading", "load_event", "unload_event", "load_failure_count",
    "last_load_failure_time", "last_load_error", "reset_load_failures",
    "record_load_failure", "can_attempt_load", "model_unloading", "load_lock",
    "last_activity", "update_activity", "get_idle_seconds",
])
def test_loader_exclusive_state_is_absent(name):
    assert not hasattr(proxy.ProxyState(), name)
