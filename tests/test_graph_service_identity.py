"""The graph engine's service is selected by capability, per family; no fixed id names it.

Three families select on their own: ``images`` (and the image edit and identity work), ``videos`` and ``audio.generate``.
A record may carry any id, one record may serve all three, and three records may serve one each. An install that names
its service ``graph-one`` behaves as one that names it ``comfyui``. No engine is dialed here: the connection, lease and
health seams are spies.
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

import proxy
from gateway import service_registry
from tests.support.graph_service import GRAPH_CAPABILITIES, graph_record

RENAMED = "graph-one"
FAMILIES = ("images", "videos", "audio.generate")
ADDRESS_KEYS = ("GRAPH_HOST", "GRAPH_ENABLED", "GRAPH_TIMEOUT", "GRAPH_DEFAULT_MODEL", "GRAPH_WORKFLOWS_DIR",
                "GRAPH_VIDEO_TIMEOUT", "GRAPH_VIDEO_WORKFLOWS_DIR", "_GRAPH_BINDING")


def _record(port: int, **fields: Any) -> dict[str, Any]:
    return graph_record(host="203.0.113.7", port=port, **fields)


def _derive(backends: dict[str, Any]) -> dict[str, Any]:
    return proxy._derive_backend_constants({"backends": copy.deepcopy(backends)})


def _publish(monkeypatch: pytest.MonkeyPatch, backends: dict[str, Any]) -> dict[str, Any]:
    """The values *backends* derive, as the live snapshot's (the readers read the snapshot)."""
    derived = _derive(backends)
    for key in (*ADDRESS_KEYS, "GRAPH_SERVICES"):
        monkeypatch.setitem(proxy.runtime().derived, key, derived[key])
    return derived


def _projection(derived: dict[str, Any]) -> dict[str, Any]:
    return {key: derived[key] for key in ADDRESS_KEYS}


# --- derived values --------------------------------------------------------------------------------------------------

def test_a_renamed_record_derives_what_the_old_id_derived():
    fields = {"timeout": 77, "default_model": "m-one", "workflows_dir": "./w/images", "video_timeout": 88,
              "video_workflows_dir": "./w/videos"}
    renamed = _derive({RENAMED: _record(7890, **fields)})
    old = _derive({"comfyui": _record(7890, **fields)})
    assert _projection(renamed) == _projection(old)
    assert renamed["GRAPH_ENABLED"] is True and renamed["GRAPH_HOST"] == "http://203.0.113.7:7890"
    assert renamed["GRAPH_TIMEOUT"] == 77 and renamed["GRAPH_VIDEO_TIMEOUT"] == 88
    for family in FAMILIES:
        assert renamed["GRAPH_SERVICES"][family].service_id == RENAMED
        assert old["GRAPH_SERVICES"][family].service_id == "comfyui"


def test_no_record_declaring_a_family_leaves_it_off_with_no_id():
    derived = _derive({"someone": {"enabled": True, "host": "203.0.113.7", "port": 1, "capabilities": ["chat"]}})
    for family in FAMILIES:
        view = derived["GRAPH_SERVICES"][family]
        assert (view.service_id, view.host, view.enabled) == ("", "", False)
    assert derived["GRAPH_HOST"] == "" and derived["GRAPH_ENABLED"] is False


def test_a_family_is_served_only_by_a_record_that_declares_it():
    derived = _derive({
        "pictures": _record(7001, capabilities=["images", "generations"]),
        "motion": _record(7002, capabilities=["videos"]),
    })
    views = derived["GRAPH_SERVICES"]
    assert views["images"].service_id == "pictures" and views["videos"].service_id == "motion"
    assert views["audio.generate"].service_id == "" and views["audio.generate"].enabled is False


# --- selection ----------------------------------------------------------------------------------------------------------

def test_the_lowest_priority_record_wins_and_a_tie_goes_to_the_lower_id():
    backends = {
        "z-late": _record(7001, priority=30),
        "b-tie": _record(7002, priority=10),
        "a-tie": _record(7003, priority=10),
        "unranked": _record(7004),
    }
    views = _derive(backends)["GRAPH_SERVICES"]
    assert {views[family].service_id for family in FAMILIES} == {"a-tie"}
    assert views["images"].host == "http://203.0.113.7:7003"
    # A record with no priority sorts at 100: it loses to a ranked one and wins over none.
    assert _derive({"unranked": _record(7004), "z-late": _record(7001, priority=101)})["GRAPH_SERVICES"]["images"].service_id == "unranked"


def test_each_family_picks_its_own_winner():
    backends = {
        "first": _record(7001, priority=10, capabilities=["images"]),
        "second": _record(7002, priority=20, capabilities=["images", "videos"]),
        "third": _record(7003, priority=5, capabilities=["audio.generate"]),
    }
    views = _derive(backends)["GRAPH_SERVICES"]
    assert [views[family].service_id for family in FAMILIES] == ["first", "second", "third"]


@pytest.mark.parametrize("winner", [
    {"enabled": False},
    {"host": None, "port": None},
], ids=("disabled", "addressless"))
def test_a_winner_that_cannot_serve_stays_the_selection_with_no_runner_up(winner):
    backends = {
        "winner": {**_record(7001, priority=1), **winner},
        "runner-up": _record(7002, priority=2),
    }
    backends["winner"] = {key: value for key, value in backends["winner"].items() if value is not None}
    derived = _derive(backends)
    for family in FAMILIES:
        view = derived["GRAPH_SERVICES"][family]
        assert view.service_id == "winner" and view.enabled is False
    assert derived["GRAPH_ENABLED"] is False
    assert derived["GRAPH_HOST"] != "http://203.0.113.7:7002"


def test_an_addressless_winner_has_an_empty_host():
    derived = _derive({"winner": {"enabled": True, "capabilities": list(GRAPH_CAPABILITIES), "priority": 1},
                       "runner-up": _record(7002, priority=2)})
    assert derived["GRAPH_HOST"] == ""
    assert all(derived["GRAPH_SERVICES"][family].host == "" for family in FAMILIES)


# --- a candidate derives from itself ------------------------------------------------------------------------------------

def test_a_candidate_config_derives_from_itself_not_from_the_live_config(monkeypatch):
    monkeypatch.setitem(proxy.config, "backends", {"live-one": _record(9001, timeout=11)})
    candidate = {"candidate-one": _record(9002, timeout=22)}
    before = copy.deepcopy(candidate)
    derived = _derive(candidate)
    assert {derived["GRAPH_SERVICES"][family].service_id for family in FAMILIES} == {"candidate-one"}
    assert derived["GRAPH_HOST"] == "http://203.0.113.7:9002" and derived["GRAPH_TIMEOUT"] == 22
    assert candidate == before
    # And the other way round: a candidate with no graph record derives none while the live config has one.
    assert _derive({})["GRAPH_SERVICES"]["images"].service_id == ""


# --- one record per family ----------------------------------------------------------------------------------------------

def test_split_records_give_each_family_its_own_values():
    backends = {
        "pictures": _record(7001, capabilities=["images", "generations"], timeout=31, default_model="pic-model",
                            workflows_dir="./pictures", video_timeout=999, video_workflows_dir="./not-used"),
        "motion": _record(7002, capabilities=["videos"], timeout=41, default_model="mov-model",
                          workflows_dir="./not-used-either", video_timeout=61, video_workflows_dir="./motion"),
    }
    derived = _derive(backends)
    assert derived["GRAPH_HOST"] == "http://203.0.113.7:7001"
    assert (derived["GRAPH_TIMEOUT"], derived["GRAPH_DEFAULT_MODEL"], derived["GRAPH_WORKFLOWS_DIR"].name) == (31, "pic-model", "pictures")
    # The video values come from the record that serves videos, not the images record.
    assert (derived["GRAPH_VIDEO_TIMEOUT"], derived["GRAPH_VIDEO_WORKFLOWS_DIR"].name) == (61, "motion")
    assert derived["GRAPH_SERVICES"]["videos"].host == "http://203.0.113.7:7002"
    assert derived["GRAPH_SERVICES"]["images"].host == "http://203.0.113.7:7001"


def test_one_record_serving_all_three_families_projects_alike_under_any_id():
    record = _record(7890, timeout=55, default_model="m", workflows_dir="./a", video_timeout=66, video_workflows_dir="./b")
    by_id = {name: _projection(_derive({name: copy.deepcopy(record)})) for name in ("comfyui", RENAMED, "any other id")}
    assert by_id["comfyui"] == by_id[RENAMED] == by_id["any other id"]


# --- the seams -------------------------------------------------------------------------------------------------------

def test_the_adapter_and_connection_see_the_family_selected_service(monkeypatch):
    backends = {
        "pictures": _record(7001, capabilities=["images"]),
        "motion": _record(7002, capabilities=["videos"]),
        "sound": _record(7003, capabilities=["audio.generate"]),
    }
    _publish(monkeypatch, backends)
    seen: list[tuple[Any, str]] = []
    real = proxy.reviewed_adapters.adapter_for_backend

    def spy(record, capability):
        seen.append((record, capability))
        return real(record, capability)

    monkeypatch.setattr(proxy.reviewed_adapters, "adapter_for_backend", spy)
    for family, service_id in zip(FAMILIES, ("pictures", "motion", "sound")):
        seen.clear()
        proxy._graph_adapter(family)
        assert [(dict(record), capability) for record, capability in seen] == [(backends[service_id], family)]
        connection = proxy._graph_connection(capability=family, probe=True)
        assert connection.address == service_registry.engine_base_address(backends[service_id])
    assert proxy._graph_connection(capability="images", probe=True).address != proxy._graph_connection(capability="videos", probe=True).address


def test_a_request_connection_leases_the_selected_id_and_a_probe_needs_none(monkeypatch):
    backends = {"pictures": _record(7001, capabilities=["images"]), "motion": _record(7002, capabilities=["videos"])}
    _publish(monkeypatch, backends)
    leased: list[str] = []
    monkeypatch.setattr(proxy, "_require_egress_lease", leased.append)

    proxy._graph_connection(capability="images")
    proxy._graph_connection(capability="videos")
    assert leased == ["pictures", "motion"]

    proxy._graph_connection(capability="images", probe=True)
    proxy._graph_connection(capability="videos", probe=True)
    proxy._graph(object(), "images", probe=True)
    assert leased == ["pictures", "motion"]


def test_a_family_with_no_service_never_leases_an_empty_id(monkeypatch):
    _publish(monkeypatch, {"pictures": _record(7001, capabilities=["images"])})
    leased: list[str] = []
    monkeypatch.setattr(proxy, "_require_egress_lease", leased.append)
    with pytest.raises(RuntimeError):
        proxy._graph_connection(capability="audio.generate")
    assert leased == []


def test_the_connection_takes_its_default_timeout_from_the_family_service(monkeypatch):
    _publish(monkeypatch, {"pictures": _record(7001, capabilities=["images"], timeout=33)})
    monkeypatch.setattr(proxy, "_require_egress_lease", lambda _id: None)
    assert proxy._graph_connection(capability="images").timeouts.first_byte_s == 33
    assert proxy._graph_connection(5.0, capability="images").timeouts.first_byte_s == 5.0


# --- health ----------------------------------------------------------------------------------------------------------

def test_health_probes_the_family_selected_id(monkeypatch):
    _publish(monkeypatch, {"pictures": _record(7001, capabilities=["images"]), "motion": _record(7002, capabilities=["videos"])})
    probed: list[str] = []

    async def probe(service_id: str) -> bool:
        probed.append(service_id)
        return True

    monkeypatch.setattr(proxy, "check_service_health", probe)
    assert asyncio.run(proxy.check_graph_engine_health("images")) is True
    assert asyncio.run(proxy.check_graph_engine_health("videos")) is True
    assert probed == ["pictures", "motion"]


@pytest.mark.parametrize("backends", [
    {},
    {"off": _record(7001, enabled=False)},
    {"nowhere": {"enabled": True, "capabilities": list(GRAPH_CAPABILITIES)}},
], ids=("missing", "disabled", "addressless"))
def test_health_is_false_without_a_probe_when_the_family_service_cannot_serve(monkeypatch, backends):
    _publish(monkeypatch, backends)
    probed: list[str] = []

    async def probe(service_id: str) -> bool:
        probed.append(service_id)
        return True

    monkeypatch.setattr(proxy, "check_service_health", probe)
    for family in FAMILIES:
        assert asyncio.run(proxy.check_graph_engine_health(family)) is False
    assert probed == []


# --- nothing is captured at import -----------------------------------------------------------------------------------

def test_no_module_value_holds_the_service_id_or_its_record(monkeypatch):
    assert not hasattr(proxy, "_GRAPH_BACKEND_ID")
    _publish(monkeypatch, {"first-one": _record(7001)})
    assert proxy._graph_service_id("images") == "first-one"
    _publish(monkeypatch, {"second-one": _record(7002)})
    assert proxy._graph_service_id("images") == "second-one"
    assert proxy._graph_connection(capability="images", probe=True).address == "http://203.0.113.7:7002"


# --- a job goes to the service it was pinned to, with that service's own wait ---------------------------------------------

def _split_graph(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Two graph records on different addresses and timeouts: ``graph-img`` serves images, ``graph-vid`` serves the
    other two families. It is the live config too, because the runner finds the pinned record there."""
    backends = {
        "graph-img": _record(7001, capabilities=["images", "generations"], timeout=31),
        "graph-vid": _record(7002, capabilities=["videos", "audio.generate"], timeout=41),
    }
    _publish(monkeypatch, backends)
    monkeypatch.setitem(proxy.config, "backends", backends)
    monkeypatch.setattr(proxy, "_require_egress_lease", lambda _id: None)
    return backends


def test_the_runner_connects_to_the_service_the_job_was_pinned_to_with_that_services_wait(monkeypatch):
    backends = _split_graph(monkeypatch)
    host = proxy._graph_runner_host()
    key = proxy.DERIVED._GRAPH_BINDING
    for service_id, port, wait in (("graph-img", 7001, 31), ("graph-vid", 7002, 41)):
        found = host.engine(key, service_id)
        assert found is not None and found[0] == service_id
        connection = host.connection(found[0], found[1])
        assert connection.address == f"http://203.0.113.7:{port}"
        assert (connection.timeouts.first_byte_s, connection.timeouts.total_s) == (wait, wait)
    # A pin that names a service no longer configured is not run on another one.
    assert host.engine(key, "gone") is None


class _SpySession:
    def __init__(self, connection: Any, seen: list[tuple[str, list[str]]]) -> None:
        self.connection, self.seen = connection, seen

    async def delete_history(self, ids, *, headers):
        self.seen.append((self.connection.address, list(ids)))


class _SpyClient:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def test_deleting_a_video_forgets_its_history_on_the_service_it_ran_on(monkeypatch, tmp_path):
    from gateway import media_jobs
    from gateway.definition_store import DefinitionStore
    from gateway.job_envelope import build_job_envelope

    # The job ran on "old-vid"; since then "new-vid" took the videos family by priority.
    backends = {
        "new-vid": _record(7002, capabilities=["videos"], priority=1),
        "old-vid": _record(7003, capabilities=["videos"], priority=2),
    }
    _publish(monkeypatch, backends)
    monkeypatch.setitem(proxy.config, "backends", backends)
    monkeypatch.setattr(proxy, "_require_egress_lease", lambda _id: None)
    db = tmp_path / "jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    built = build_job_envelope(
        source_kind="workflow", source_id="wf", contract={"n": 1}, binding={"n": 1}, template=None, protocol_id="proto-x",
        adapter_id="adapter-x", interface_version=1, service_id="old-vid", origin="http://203.0.113.7:7003")
    job = media_jobs.create(media_jobs.MediaJob(intent="video.generate", request_id="r1", metadata={"protocol_job": "p1"}),
                            envelope=built, path=db)
    seen: list[tuple[str, list[str]]] = []
    headers_for: list[str] = []
    real = proxy.reviewed_adapters.adapter_for_backend
    monkeypatch.setattr(proxy.reviewed_adapters, "adapter_for_backend", lambda record, capability: type(
        "Adapter", (), {"attach": staticmethod(lambda connection, client: _SpySession(connection, seen)),
                        "label": real(record, capability).label})())
    monkeypatch.setattr(proxy, "configured_downstream_headers", lambda ctx, service: headers_for.append(service) or {})
    monkeypatch.setattr(proxy.engine_egress, "async_client", lambda **kwargs: _SpyClient())

    asyncio.run(proxy._forget_video_history(job.job_id, object()))
    assert seen == [("http://203.0.113.7:7003", ["p1"])]
    assert headers_for == ["old-vid"]
