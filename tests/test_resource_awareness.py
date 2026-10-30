"""Automatic passive resource evidence through the real status build/read path."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest

import status_endpoint as status
from gateway.resource_awareness import ResourceObserver, age_resource_view
import test_status_build as _base


GPUS = [
    {"index": 0, "uuid": "card-a", "memory_total_mb": 10000, "memory_used_mb": 8000},
    {"index": 1, "uuid": "card-b", "memory_total_mb": 20000, "memory_used_mb": 3000},
]
SERVICES = {"alpha": {"host": "127.0.0.1"}, "beta": {}, "remote": {"host": "remote"}}
ATTR = {
    "compute_apps_complete": True,
    "service_pid_candidates": {"alpha": [11], "beta": [22]},
    "raw_compute_apps": [
        {"pid": 11, "gpu_uuid": "card-a", "used_mb": 4000},
        {"pid": 22, "gpu_uuid": "card-a", "used_mb": 1000},
        {"pid": 11, "gpu_uuid": "card-b", "used_mb": 2000},
        # External consumers must reduce free memory even without attribution.
        {"pid": 99, "gpu_uuid": "card-a", "used_mb": 2000},
    ],
}


def observe(observer=None, *, gpus=None, attribution=None, services=None, **kwargs):
    return (observer or ResourceObserver()).observe(
        GPUS if gpus is None else gpus, ATTR if attribution is None else attribution,
        SERVICES if services is None else services, now=kwargs.pop("now", 100),
        sampled_at=kwargs.pop("sampled_at", 99), **kwargs,
    )


def footprint(view, sid="alpha", card=0):
    return next(row for row in view["services"] if row["service_id"] == sid)["devices"][card]


def test_device_free_uses_all_consumers_and_services_remain_co_resident():
    view = observe()
    assert [d["free_mb"] for d in view["devices"]] == [2000, 17000]
    assert footprint(view)["resident_mb"] == 4000
    assert footprint(view, "beta")["resident_mb"] == 1000
    assert footprint(view, card=1)["resident_mb"] == 2000
    assert footprint(view, "remote")["resident_mb"] is None
    assert footprint(view, "beta", 1)["resident_mb"] is None
    assert "service_pid_candidates" not in json.dumps(view)


def test_high_water_is_scoped_to_unchanged_config_uuid_and_process_cohort():
    observer = ResourceObserver()
    observe(observer)
    smaller = copy.deepcopy(ATTR)
    smaller["raw_compute_apps"][0]["used_mb"] = 3000
    second = observe(observer, attribution=smaller, now=115, sampled_at=114)
    assert footprint(second)["resident_mb"] == 3000
    assert footprint(second)["observed_peak_mb"] == 4000
    assert footprint(second)["samples"] == 2
    configs = copy.deepcopy(SERVICES)
    configs["alpha"]["port"] = 1234
    changed = observe(observer, attribution=smaller, services=configs, now=130, sampled_at=129)
    assert footprint(changed)["observed_peak_mb"] == 3000
    assert footprint(changed)["samples"] == 1
    smaller["service_pid_candidates"]["alpha"] = [33]
    for app in smaller["raw_compute_apps"]:
        if app["pid"] == 11:
            app["pid"] = 33
    assert footprint(observe(observer, attribution=smaller))["samples"] == 1
    assert footprint(observe(ResourceObserver(), attribution=smaller))["samples"] == 1


@pytest.mark.parametrize("change,reason", [
    (lambda a: a["service_pid_candidates"].update(beta=[11]), "shared_process"),
    (lambda a: a.update(raw_compute_apps=[]), "not_observed"),
    (lambda a: a.update(compute_apps_complete=False), "inconsistent_process_sample"),
    (lambda a: a["raw_compute_apps"].append(dict(a["raw_compute_apps"][0])), "inconsistent_process_sample"),
    (lambda a: a["raw_compute_apps"][0].update(used_mb=-1), "inconsistent_process_sample"),
    (lambda a: a["raw_compute_apps"][0].update(used_mb=9000), "inconsistent_process_sample"),
])
def test_ambiguous_missing_partial_and_racing_samples_do_not_teach(change, reason):
    observer = ResourceObserver()
    observe(observer)
    attr = copy.deepcopy(ATTR)
    change(attr)
    bad = footprint(observe(observer, attribution=attr))
    assert bad["resident_mb"] is None
    assert bad["observed_peak_mb"] is None
    assert bad["reason"] == reason
    assert footprint(observe(observer))["samples"] == 1


def test_unresolved_process_discovery_is_not_reported_as_an_observed_cohort():
    attr = copy.deepcopy(ATTR)
    attr["service_pid_candidates"].pop("alpha")
    missing = footprint(observe(attribution=attr))
    assert missing["resident_mb"] is None
    assert missing["reason"] == "attribution_unavailable"
    attr["service_pid_candidates"]["alpha"] = []
    assert footprint(observe(attribution=attr))["reason"] == "attribution_unavailable"


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), 11000])
def test_invalid_device_usage_never_becomes_free_capacity(value):
    gpus = copy.deepcopy(GPUS)
    gpus[0]["memory_used_mb"] = value
    view = observe(gpus=gpus)
    assert view["devices"][0]["free_mb"] is None
    assert footprint(view)["resident_mb"] is None
    assert view["devices"][1]["free_mb"] == 17000


def test_device_identity_collision_and_missing_telemetry_are_unknown():
    assert all(d["free_mb"] is None for d in observe(gpus=[GPUS[0], GPUS[0]])["devices"])
    assert observe(gpus=[])["devices"] == []
    assert footprint(observe(trustworthy=False))["resident_mb"] is None
    assert footprint(observe(now=200))["resident_mb"] is None
    assert footprint(observe(now=98))["resident_mb"] is None


def test_cached_read_ages_without_mutating_or_relearning():
    view = observe()
    stale = age_resource_view(view, 60)
    assert stale["devices"][0]["free_mb"] is None
    assert footprint(stale)["resident_mb"] is None
    assert footprint(stale)["reason"] == "stale"
    assert footprint(stale)["observed_peak_mb"] == 4000  # explicitly historical
    assert view["devices"][0]["free_mb"] == 2000
    assert footprint(view)["samples"] == 1


def test_repeated_sample_is_not_new_evidence_and_idle_gap_resets_history():
    observer = ResourceObserver()
    observe(observer)
    assert footprint(observe(observer))["samples"] == 1
    smaller = copy.deepcopy(ATTR)
    smaller["raw_compute_apps"][0]["used_mb"] = 2000
    after_idle = observe(observer, attribution=smaller, now=200, sampled_at=199)
    assert footprint(after_idle)["samples"] == 1
    assert footprint(after_idle)["observed_peak_mb"] == 2000


def test_device_replacement_does_not_inherit_history():
    observer = ResourceObserver()
    observe(observer)
    gpus, attr = copy.deepcopy(GPUS), copy.deepcopy(ATTR)
    gpus[0]["uuid"] = "replacement"
    for app in attr["raw_compute_apps"]:
        if app["gpu_uuid"] == "card-a":
            app["gpu_uuid"] = "replacement"
    assert footprint(observe(observer, gpus=gpus, attribution=attr))["samples"] == 1


def test_pid_candidates_preserve_collisions_while_legacy_attribution_stays_exclusive(monkeypatch):
    config = {"backends": {
        sid: {"lifecycle": {"driver_config": {"unit": "shared.service"}}}
        for sid in ("alpha", "beta")
    }}
    monkeypatch.setattr(status, "_unit_cgroups", lambda units: {sid: "/shared" for sid in units})
    monkeypatch.setattr(status, "_cgroup_pids", lambda group: {11})
    monkeypatch.setattr(status, "_read_gpu_budget", lambda: None)
    # Exercise the real compute-app parser and attribution pipeline.
    monkeypatch.setattr(status, "_run", lambda *a, **k: (0, "11, card-a, 4000, process", ""))
    attr = status._vram_attribution(GPUS, config)
    assert attr["service_pids"] == {"alpha": [11]}
    assert attr["service_pid_candidates"] == {"alpha": [11], "beta": [11]}
    assert attr["compute_apps_complete"] is True
    view = observe(attribution=attr, services=config["backends"])
    assert footprint(view)["reason"] == "shared_process"
    assert footprint(view, "beta")["reason"] == "shared_process"


@pytest.mark.parametrize("rc,output", [(1, ""), (0, "11, card-a, 4000\nbad"), (0, "11, card-a, N/A")])
def test_failed_or_partial_compute_probe_is_explicit(monkeypatch, rc, output):
    monkeypatch.setattr(status, "_run", lambda *a, **k: (rc, output, ""))
    diagnostics = {}
    status._get_compute_apps(diagnostics=diagnostics)
    assert diagnostics == {"complete": False}


class TestStatusResourceIntegration(unittest.IsolatedAsyncioTestCase):
    setUp = _base.StatusBuildTests.setUp
    isolate_sections = _base.StatusBuildTests.isolate_sections

    async def test_builder_and_owner_status_response_expose_and_age_observations(self):
        import proxy

        self.isolate_sections()
        # Isolate all remaining host reads. The real builder/gather/cache path runs.
        for name, value in {
            "_build_models": {}, "_capabilities": [], "_image_capabilities": [],
            "_journal_xids": {}, "_uptime_since": None, "_kernel_recent_events": [],
            "_reboot_history": [], "_proxy_restart_count": 0, "_journal_disk_usage": 0,
            "_images_budget": {}, "_status_config_snapshot": {},
            "_status_auth_key_names": ([], {}), "_gpus_raw": GPUS, "_vram_attribution": ATTR,
        }.items():
            self.patches.enter_context(patch.object(status, name, return_value=value))
        self.patches.enter_context(patch.object(status, "_HOTSPOT_DIRS", []))
        self.patches.enter_context(patch.object(status, "_gpu_workloads", AsyncMock(return_value=[])))
        self.patches.enter_context(patch.object(status, "_RESOURCE_OBSERVER", ResourceObserver()))
        self.patches.enter_context(patch.object(status, "_SECTION_AT", {}))
        self.patches.enter_context(patch.object(status, "_SNAPSHOT_BUILD", None))
        clock = [100.0]
        self.patches.enter_context(patch.object(status, "_now", lambda: clock[0]))
        firehose = SimpleNamespace(
            snapshot_video_meta=lambda: {}, snapshot_per_key=lambda _: [],
            snapshot_window=lambda: {}, snapshot_recent_events=lambda _: [],
        )
        self.patches.enter_context(patch.object(status, "importlib", SimpleNamespace(import_module=lambda _: firehose)))
        payload = await status.build_status_payload({"backends": SERVICES})
        assert footprint(payload["resource_awareness"])["samples"] == 1
        status._store_snapshot(payload)
        fresh = await status.get_cached_status({})
        assert fresh["resource_awareness"]["devices"][0]["free_mb"] == 2000
        # A timed-out GPU source may retain a valid-looking old result. It must
        # not refresh or teach resource history even when the build itself is new.
        self.patches.enter_context(patch.object(status, "_gpus_raw", side_effect=TimeoutError()))
        clock[0] = 115
        timed_out = await status.build_status_payload({"backends": SERVICES})
        assert footprint(timed_out["resource_awareness"])["resident_mb"] is None
        assert timed_out["resource_awareness"]["devices"][0]["free_mb"] is None
        clock[0] = 161
        stale = await status.get_cached_status({})
        assert stale["resource_awareness"]["devices"][0]["free_mb"] is None
        assert footprint(stale["resource_awareness"])["reason"] == "stale"
        assert payload["resource_awareness"]["devices"][0]["free_mb"] == 2000
        self.patches.enter_context(patch.object(proxy, "ADMIN_AUTH", lambda request: None))
        self.patches.enter_context(patch.object(proxy, "_status_payload_with_protocol_issues", AsyncMock(side_effect=lambda p: p)))
        response = await proxy.v1_status(SimpleNamespace())
        assert response.status_code == 200
        assert json.loads(response.body)["resource_awareness"]["devices"][0]["free_mb"] is None
