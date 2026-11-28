from gateway.admin_api import derive_admin_health_projection
from status_endpoint import _disk_pressure_reason


def test_unused_engine_outage_does_not_raise_active_health_alarm():
    health = derive_admin_health_projection(
        {"services": {
            "active": {"name": "Active", "config": {"models": [{"id": "fast"}]}},
            "old": {"name": "Legacy engine"},
        }},
        {"_relied_on_service_ids": ["active"], "backends": [
            {"service_id": "active", "live_status": "up"},
            {"service_id": "old", "live_status": "down"},
        ]},
        observation_available=True,
    )
    assert health["state"] == "ok"
    legacy = next(row for row in health["services"] if row["service_id"] == "old")
    assert legacy["in_use"] is False
    assert legacy["usage_label"] == "Not in use"
    assert legacy["observation"] == "down"
    assert legacy["tone"] == "neutral"


def test_relied_on_engine_outage_remains_actionable():
    health = derive_admin_health_projection(
        {"services": {"active": {"name": "Active"}}},
        {"_relied_on_service_ids": ["active"], "backends": [
            {"service_id": "active", "live_status": "down"},
        ]}, observation_available=True,
    )
    assert health["state"] == "bad"
    assert health["services"][0]["in_use"] is True


def test_disabled_relied_on_engine_is_neutral():
    health = derive_admin_health_projection(
        {"services": {"active": {"enabled": False}}},
        {"_relied_on_service_ids": ["active"]}, observation_available=True,
    )
    assert health["services"][0]["observation"] == "disabled"
    assert health["services"][0]["tone"] == "neutral"


def test_disk_warning_identifies_declared_model_volume_and_rounds_percent(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_dev=1))
    assert _disk_pressure_reason({"target": "/models", "pct_used": 96.1},
                                 {"model_routing": {"model_dir": "/models/llms"}}) == (
        "The models volume is almost full (96%). Free space so new models and outputs can be saved."
    )


def test_disk_warning_does_not_invent_models_volume(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_dev=1 if str(self) == "/logs" else 2))
    assert _disk_pressure_reason({"target": "/logs", "pct_used": 90},
                                 {"model_routing": {"model_dir": "/models"}}) == (
        "Disk (/logs) is almost full (90%). Free space on this volume so the gateway can continue to save files."
    )


def test_relied_on_route_proves_routability_without_registry_model_mapping():
    health = derive_admin_health_projection(
        {"services": {"active": {}}},
        {"_relied_on_service_ids": ["active"], "backends": [
            {"service_id": "active", "live_status": "up"},
            {"service_id": "unregistered", "live_status": "down"},
        ]}, observation_available=True,
    )
    assert health["state"] == "ok"
    assert health["services"][0]["routable"] is True
    assert health["services"][1]["in_use"] is False


def test_missing_model_path_keeps_disk_location(monkeypatch):
    from pathlib import Path
    def missing(self):
        raise OSError("missing path")
    monkeypatch.setattr(Path, "stat", missing)
    assert _disk_pressure_reason({"target": "/", "pct_used": 96},
                                 {"model_routing": {"model_dir": "/missing"}}).startswith("System disk (/)")
