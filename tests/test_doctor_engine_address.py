"""scripts/doctor.py takes the engine address from the service registry, not from an environment variable."""
from __future__ import annotations

from scripts import doctor
from tests.registry_test_helpers import seed_registry_document, write_service_registry_for_test


def _store(db, backends):
    write_service_registry_for_test(seed_registry_document({"backends": backends}), path=db)


def test_the_probe_address_is_the_comfyui_service_of_the_effective_registry(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    from gateway import admin_store
    admin_store.init_db(db)
    _store(db, {"comfyui": {"kind": "comfyui", "capabilities": ["images"], "host": "203.0.113.9", "port": 24680, "enabled": True}})
    monkeypatch.setenv("GRAPH_HOST", "http://203.0.113.1:1")  # a removed variable: it must change nothing
    assert doctor.default_comfy_url(db) == "http://203.0.113.9:24680"


def test_no_database_or_no_comfyui_service_means_no_probe_address_and_no_database_is_made(tmp_path):
    missing = tmp_path / "none.db"
    assert doctor.default_comfy_url(missing) is None and not missing.exists()
    from gateway import admin_store
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    _store(db, {"chat-engine": {"kind": "llama", "capabilities": ["chat"], "host": "203.0.113.5", "port": 1, "enabled": True}})
    assert doctor.default_comfy_url(db) is None


def test_an_ipv6_engine_address_is_bracketed_once(tmp_path):
    from urllib.parse import urlsplit

    from gateway import admin_store
    for host in ("2001:db8::1", "[2001:db8::1]"):
        db = tmp_path / f"admin-{len(host)}.db"
        admin_store.init_db(db)
        _store(db, {"comfyui": {"kind": "comfyui", "capabilities": ["images"], "host": host, "port": 24680, "enabled": True}})
        url = doctor.default_comfy_url(db)
        assert url == "http://[2001:db8::1]:24680"
        assert urlsplit(url).port == 24680
