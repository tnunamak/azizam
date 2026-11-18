"""Host-memory admission: a prompt whose models are not resident waits until the host has room for them."""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway import client_errors, host_memory_admission as hma
from gateway import media_jobs
from gateway.engine_protocol import node_graph
from gateway.engine_protocol.types import CanonicalRequest, Connection, Timeouts

GIB = 1 << 30
BUSY_MESSAGE = "The host's memory is busy with other work. Try again soon."


def policy(root, **over):
    values = dict(margin_bytes_by_kind={"personal": 1000, "service": 5000}, wait_s=10.0, job_wait_s=1000.0, report_s=0.0, poll_s=2.0, settle_s=0.0, resident_ttl_s=600.0,
                  model_extensions=(".safetensors", ".gguf"), model_roots=(str(root),))
    values.update(over)
    return hma.HostMemoryPolicy(**values)


def weights(root, name, size):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)


def graph(*names):
    return {str(i): {"class_type": "Loader", "inputs": {"file": name, "steps": 20}} for i, name in enumerate(names, 1)}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def admission(root, readings, clock=None, **over):
    clock = clock or Clock()
    seen = iter(readings)
    last = {"v": None}

    def reader():
        try:
            last["v"] = next(seen)
        except StopIteration:
            pass
        return last["v"]

    made = hma.HostMemoryAdmission(policy(root, **over), read_available=reader, sleep=clock.sleep, clock=clock)
    return made, clock


def hma_env_dir():
    return hma.safe_load_strict(hma._DECLARATION.read_text())["engine_dir_env"]


def run(coro):
    return asyncio.run(coro)


def test_enough_memory_admits_at_once(tmp_path):
    weights(tmp_path, "checkpoints/big.safetensors", 10_000)
    made, clock = admission(tmp_path, [11_000])
    run(made.admit(graph("big.safetensors")))
    assert clock.slept == []


def test_too_little_then_enough_admits_after_waiting(tmp_path):
    weights(tmp_path, "checkpoints/big.safetensors", 10_000)
    made, clock = admission(tmp_path, [5_000, 8_000, 11_000])
    run(made.admit(graph("big.safetensors")))
    assert clock.slept == [2.0, 2.0]


def test_too_little_until_the_limit_gives_the_busy_answer(tmp_path):
    weights(tmp_path, "checkpoints/big.safetensors", 10_000)
    made, clock = admission(tmp_path, [5_000])
    with pytest.raises(hma.HostMemoryBusy) as busy:
        run(made.admit(graph("big.safetensors")))
    error = busy.value
    assert (error.code, error.category, error.retryable) == ("resource_unavailable", "capacity", True)
    assert error.client_message == BUSY_MESSAGE
    assert clock.now >= 10.0


def test_the_sync_answer_has_an_exact_status_code_and_message():
    response = hma.busy_response()
    body = json.loads(response.body)
    assert response.status_code == 503
    assert body["error"]["code"] == "resource_unavailable"
    assert body["error"]["message"] == BUSY_MESSAGE
    assert "resource_unavailable" in client_errors.ERROR_CODES


def test_resident_models_skip_the_wait_until_the_engine_frees_them(tmp_path):
    weights(tmp_path, "big.safetensors", 10_000)
    made, clock = admission(tmp_path, [50_000, 1, 1, 1])
    run(made.admit(graph("big.safetensors")))          # loads it
    run(made.admit(graph("big.safetensors")))          # resident: no wait although the host is full
    assert clock.slept == []
    made.forget_all()                                  # the engine freed its models
    with pytest.raises(hma.HostMemoryBusy):
        run(made.admit(graph("big.safetensors")))


def test_a_resident_entry_expires(tmp_path):
    weights(tmp_path, "big.safetensors", 10_000)
    made, clock = admission(tmp_path, [50_000, 1], resident_ttl_s=100.0)
    run(made.admit(graph("big.safetensors")))
    clock.now += 101
    with pytest.raises(hma.HostMemoryBusy):
        run(made.admit(graph("big.safetensors")))


def test_only_models_that_are_not_resident_count(tmp_path):
    weights(tmp_path, "a.safetensors", 10_000)
    weights(tmp_path, "b.safetensors", 4_000)
    made, clock = admission(tmp_path, [50_000, 5_000])
    run(made.admit(graph("a.safetensors")))
    run(made.admit(graph("a.safetensors", "b.safetensors")))   # b needs 4000 + 1000 margin: 5000 is enough
    assert clock.slept == []


def test_unknown_size_is_not_counted_and_a_prompt_with_no_known_size_is_admitted_unread(tmp_path):
    reads = []

    def reader():
        reads.append(1)
        return 1

    made = hma.HostMemoryAdmission(policy(tmp_path), read_available=reader)
    run(made.admit(graph("missing.safetensors")))
    assert reads == []
    weights(tmp_path, "known.safetensors", 10_000)
    sized = admission(tmp_path, [5_000])[0]
    with pytest.raises(hma.HostMemoryBusy):                     # the missing file adds nothing; the known one still gates
        run(sized.admit(graph("known.safetensors", "missing.safetensors")))


def test_a_host_memory_reading_that_fails_admits(tmp_path):
    weights(tmp_path, "big.safetensors", 10_000)
    made = hma.HostMemoryAdmission(policy(tmp_path), read_available=lambda: None)
    run(made.admit(graph("big.safetensors")))


def test_an_admitted_load_is_reserved_so_a_second_prompt_waits(tmp_path):
    weights(tmp_path, "a.safetensors", 10_000)
    weights(tmp_path, "b.safetensors", 10_000)
    made, clock = admission(tmp_path, [11_000, 11_000, 11_000, 22_000], settle_s=3.0)
    run(made.admit(graph("a.safetensors")))
    run(made.admit(graph("b.safetensors")))
    assert clock.slept == [2.0, 2.0]                            # b waited for a's reservation to settle, then for room


def test_models_are_found_in_a_folder_under_a_root_and_models_of_lists_each_once(tmp_path):
    weights(tmp_path, "unet/x.safetensors", 7)
    made = hma.HostMemoryAdmission(policy(tmp_path), read_available=lambda: 10**12)
    assert hma.models_of(graph("x.safetensors", "x.safetensors", "y.png"), (".safetensors",)) == ["x.safetensors"]
    assert made.size_of("x.safetensors") == 7
    assert made.size_of("../x.safetensors") is None


def test_the_policy_reads_its_defaults_from_the_registry_and_env_overrides_them(monkeypatch, tmp_path):
    monkeypatch.delenv(hma_env_dir(), raising=False)
    for name in ("AZIZAM_HOST_MEMORY_MARGIN_GB", "AZIZAM_HOST_MEMORY_WAIT_S", "AZIZAM_HOST_MEMORY_MODEL_ROOTS"):
        monkeypatch.delenv(name, raising=False)
    default = hma.load_policy()
    assert (default.margin_bytes_by_kind, default.wait_s, default.poll_s) == ({"personal": 12 * GIB, "service": 20 * GIB}, 300.0, 2.0)
    monkeypatch.setenv("AZIZAM_HOST_MEMORY_MARGIN_GB", "8")
    monkeypatch.setenv("AZIZAM_HOST_MEMORY_WAIT_S", "30")
    monkeypatch.setenv("AZIZAM_HOST_MEMORY_MODEL_ROOTS", str(tmp_path))
    changed = hma.load_policy()
    assert (changed.margin_bytes_by_kind["personal"], changed.wait_s, changed.model_roots) == (8 * GIB, 30.0, (str(tmp_path),))


def test_roots_come_from_the_engine_directory_and_its_extra_model_paths(monkeypatch, tmp_path):
    other = tmp_path / "shared"
    other.mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "extra_model_paths.yaml").write_text(f"diffusion:\n    base_path: {other}/\n    unet: unet/\n")
    monkeypatch.setenv(hma_env_dir(), str(tmp_path))
    monkeypatch.delenv("AZIZAM_HOST_MEMORY_MODEL_ROOTS", raising=False)
    assert hma.load_policy().model_roots == (str(tmp_path / "models"), str(other))


def test_the_adapter_admits_the_prompt_it_is_about_to_send(tmp_path):
    weights(tmp_path, "big.safetensors", 10_000)
    made, _clock = admission(tmp_path, [1])
    adapter = node_graph.NodeGraphAdapter(admission=made)
    conn = Connection(address="http://graph.test:1", timeouts=Timeouts(connect_s=1, first_byte_s=1, total_s=1))
    request = CanonicalRequest("graph.submit", body={"prompt": graph("big.safetensors")})
    with pytest.raises(hma.HostMemoryBusy):
        run(adapter.admit(conn, "graph.submit", request))


class Host:
    """A host whose free memory is a function of the fake clock."""

    def __init__(self, clock, schedule):
        self.clock, self.schedule = clock, schedule

    def __call__(self):
        return [value for at, value in self.schedule if at <= self.clock.now][-1]


def test_a_persons_job_waits_shows_the_waiting_state_then_runs(tmp_path):
    db = tmp_path / "jobs.db"
    media_jobs.init_db(db)
    job = media_jobs.create(media_jobs.MediaJob(intent="video.generate"), path=db)
    weights(tmp_path, "big.safetensors", 10_000)
    clock = Clock()
    made = hma.HostMemoryAdmission(policy(tmp_path), read_available=Host(clock, [(0, 3_000), (6, 30_000)]),
                                   sleep=clock.sleep, clock=clock)
    ran = []

    def report(state):
        media_jobs.append_event(job.job_id, "attempt", hma.wait_event(state), path=db)

    async def go():
        await made.admit(graph("big.safetensors"), hma.Ask(bounded=False, on_wait=report))
        ran.append(clock.now)

    run(go())
    assert ran == [6.0]                                   # no failure although the limit for a request (10 s) is near
    seen = [e["payload"] for e in media_jobs.events(job.job_id, audience="client", path=db)]
    waiting = [p for p in seen if p.get("phase") == hma.WAITING_PHASE]
    assert waiting and waiting[0] == {"phase": "waiting_for_memory", "status": "running", "needed_gb": 0.0, "available_gb": 0.0}
    assert seen[-1] == {"phase": "running", "status": "running"}


def test_the_wait_state_carries_the_gb_needed_and_available():
    state = hma.WaitState(needed_bytes=int(34.5 * GIB), available_bytes=int(16.25 * GIB), waited_s=3.0)
    assert hma.wait_event(state) == {"phase": "waiting_for_memory", "status": "running", "needed_gb": 34.5, "available_gb": 16.2}


def test_a_job_that_waits_longer_than_a_request_may_is_not_refused_but_a_request_is(tmp_path):
    weights(tmp_path, "big.safetensors", 10_000)
    clock = Clock()
    host = Host(clock, [(0, 3_000), (500, 30_000)])
    made = hma.HostMemoryAdmission(policy(tmp_path), read_available=host, sleep=clock.sleep, clock=clock)
    run(made.admit(graph("big.safetensors"), hma.Ask(bounded=False)))
    assert clock.now == 500.0
    other = hma.HostMemoryAdmission(policy(tmp_path), read_available=host, sleep=clock.sleep, clock=clock)
    clock.now = 0.0
    with pytest.raises(hma.HostMemoryBusy):
        run(other.admit(graph("big.safetensors"), hma.Ask(bounded=True)))


def test_a_service_key_request_waits_behind_a_person_and_uses_the_larger_margin(tmp_path):
    weights(tmp_path, "a.safetensors", 10_000)
    weights(tmp_path, "b.safetensors", 10_000)
    clock = Clock()
    # 12 000 covers a person (10 000 + 1 000 margin) but not background work (10 000 + 5 000 margin).
    host = Host(clock, [(0, 3_000), (4, 12_000), (40, 60_000)])

    async def sleep(seconds):
        clock.now += seconds
        await asyncio.sleep(0)

    made = hma.HostMemoryAdmission(policy(tmp_path, settle_s=3.0), read_available=host,
                                   sleep=sleep, clock=clock)
    order = []

    async def ask(label, model, auth_kind):
        await made.admit(graph(model), hma.Ask(app_kind=hma.app_kind_of(auth_kind), bounded=False))
        order.append((label, clock.now))

    async def go():
        background = asyncio.ensure_future(ask("background", "b.safetensors", "service_key"))   # asked first
        await asyncio.sleep(0)
        person = asyncio.ensure_future(ask("person", "a.safetensors", "app_key"))
        await asyncio.gather(background, person)

    run(go())
    assert [label for label, _ in order] == ["person", "background"]
    assert 4 <= order[0][1] < 40 <= order[1][1]    # the person ran when 12 000 freed; background only when 60 000 did


# -- the margins and waits are an owner setting over the shipped file ---------------------------------------------------

SAVED = {"margin_gb": {"personal": 2, "service": 30}, "wait_s": 20, "job_wait_s": 500}


def save(path, value):
    from gateway import admin_store
    admin_store.put_operator_setting_with_audit(
        hma.SETTING_KEY, value, updated_by="tester", audit_action="operator.host_memory.update", audit_actor_kind="user",
        audit_actor_id="tester", audit_actor_label="tester", audit_target_kind="operator_setting",
        audit_target_id=hma.SETTING_KEY, path=path)


@pytest.fixture
def admin_db(tmp_path):
    from gateway import admin_store
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    return path


def test_with_no_saved_setting_the_shipped_defaults_apply(admin_db):
    assert hma.stored_setting(admin_db) is None
    shipped = hma.HostMemoryPolicy()
    assert hma.effective(shipped, hma.stored_setting(admin_db)) is shipped
    assert hma.setting_view(shipped) == {"margin_gb": {"personal": 12.0, "service": 20.0}, "wait_s": 300.0, "job_wait_s": 7200.0}


def test_the_saved_setting_wins_over_the_shipped_defaults(admin_db):
    save(admin_db, SAVED)
    made = hma.effective(hma.HostMemoryPolicy(), hma.stored_setting(admin_db))
    assert made.margin_bytes_by_kind == {"personal": 2 * GIB, "service": 30 * GIB}
    assert (made.wait_s, made.job_wait_s) == (20.0, 500.0)
    assert made.poll_s == 2.0      # what the setting does not hold stays as shipped


@pytest.mark.parametrize("bad", [
    {"margin_gb": {"personal": -1, "service": 20}, "wait_s": 1, "job_wait_s": 2},
    {"margin_gb": {"personal": 12, "service": float("nan")}, "wait_s": 1, "job_wait_s": 2},
    {"margin_gb": {"personal": True, "service": 20}, "wait_s": 1, "job_wait_s": 2},
    {"margin_gb": {"personal": "12", "service": 20}, "wait_s": 1, "job_wait_s": 2},
    {"margin_gb": {"personal": 12}, "wait_s": 1, "job_wait_s": 2},
    {"margin_gb": {"personal": 12, "service": 20, "robot": 3}, "wait_s": 1, "job_wait_s": 2},
    {"margin_gb": {"personal": 12, "service": 20}, "wait_s": -1, "job_wait_s": 2},
    {"margin_gb": {"personal": 12, "service": 20}, "wait_s": 600, "job_wait_s": 300},
    {"margin_gb": {"personal": 12, "service": 20}, "wait_s": 1},
    {"margin_gb": 12, "wait_s": 1, "job_wait_s": 2},
])
def test_an_invalid_setting_is_refused(bad):
    with pytest.raises(hma.InvalidHostMemorySetting):
        hma.validate_setting(bad)


def test_a_damaged_saved_row_falls_back_to_the_shipped_defaults(admin_db):
    save(admin_db, {"margin_gb": {"personal": -5, "service": 1}, "wait_s": 1, "job_wait_s": 2})
    assert hma.stored_setting(admin_db) is None


def test_a_service_app_uses_the_service_margin_from_the_saved_setting(tmp_path, admin_db):
    weights(tmp_path, "a.safetensors", 10 * GIB)
    save(admin_db, {"margin_gb": {"personal": 2, "service": 30}, "wait_s": 5, "job_wait_s": 500})
    assert hma.app_kind_of("service_key") == "service"
    assert [hma.app_kind_of(k) for k in ("session", "app_key", "mcp_grant", "trusted_address", None)] == ["personal"] * 5
    # 15 GiB free: a person needs 10 + 2, a service App needs 10 + 30.
    def build():
        clock = Clock()
        return hma.HostMemoryAdmission(policy(tmp_path, margin_bytes_by_kind={"personal": 99 * GIB, "service": 99 * GIB}),
                                       read_available=lambda: 15 * GIB, sleep=clock.sleep, clock=clock,
                                       saved_setting=lambda: hma.stored_setting(admin_db))
    run(build().admit(graph("a.safetensors"), hma.Ask(app_kind="personal")))
    with pytest.raises(hma.HostMemoryBusy):
        run(build().admit(graph("a.safetensors"), hma.Ask(app_kind="service")))


def test_a_save_applies_to_the_next_admission_without_a_restart(tmp_path, admin_db):
    weights(tmp_path, "a.safetensors", 10 * GIB)
    clock = Clock()
    made = hma.HostMemoryAdmission(policy(tmp_path), read_available=lambda: 15 * GIB, sleep=clock.sleep, clock=clock,
                                   saved_setting=lambda: hma.stored_setting(admin_db))
    save(admin_db, {"margin_gb": {"personal": 10, "service": 10}, "wait_s": 5, "job_wait_s": 500})
    with pytest.raises(hma.HostMemoryBusy):
        run(made.admit(graph("a.safetensors"), hma.Ask()))
    save(admin_db, {"margin_gb": {"personal": 1, "service": 10}, "wait_s": 5, "job_wait_s": 500})
    run(made.admit(graph("a.safetensors"), hma.Ask()))


def test_the_app_kind_of_a_caller_is_read_from_its_identity():
    from types import SimpleNamespace
    assert hma.app_kind_of_caller(SimpleNamespace(identity=SimpleNamespace(auth_kind="service_key"))) == "service"
    assert hma.app_kind_of_caller(SimpleNamespace(identity=SimpleNamespace(auth_kind="session"))) == "personal"
    # A credential kind on the context itself is not where requests and jobs carry it; None (a lost caller) has a person.
    assert hma.app_kind_of_caller(SimpleNamespace(auth_kind="service_key", identity=None)) == "personal"
    assert hma.app_kind_of_caller(None) == "personal"


def test_graph_runner_uses_injected_admission_when_shared_is_off(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from gateway.node_graph_runner import NodeGraphRunner

    weights(tmp_path, "big.safetensors", 10_000)
    made, clock = admission(tmp_path, [1, 11_000], settle_s=30)
    monkeypatch.setattr(hma, "shared", lambda: hma.HostMemoryAdmission(hma.HostMemoryPolicy()))
    adapter = node_graph.NodeGraphAdapter(admission=made)
    ctx = SimpleNamespace(caller=None, job_id="job")
    monkeypatch.setattr(media_jobs, "append_event", lambda *args, **kwargs: None)
    runner = NodeGraphRunner("graph", "graph", None)
    run(runner._wait_for_host_memory(ctx, adapter, {"prompt": graph("big.safetensors")}))
    assert clock.slept == [2.0]
    assert made._reserved
