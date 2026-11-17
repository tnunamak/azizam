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
    values = dict(margin_bytes=1000, background_margin_bytes=5000, wait_s=10.0, job_wait_s=1000.0, report_s=0.0, poll_s=2.0, settle_s=0.0, resident_ttl_s=600.0,
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
    assert (default.margin_bytes, default.wait_s, default.poll_s) == (12 * GIB, 300.0, 2.0)
    monkeypatch.setenv("AZIZAM_HOST_MEMORY_MARGIN_GB", "8")
    monkeypatch.setenv("AZIZAM_HOST_MEMORY_WAIT_S", "30")
    monkeypatch.setenv("AZIZAM_HOST_MEMORY_MODEL_ROOTS", str(tmp_path))
    changed = hma.load_policy()
    assert (changed.margin_bytes, changed.wait_s, changed.model_roots) == (8 * GIB, 30.0, (str(tmp_path),))


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

    made = hma.HostMemoryAdmission(policy(tmp_path, background_auth_kinds=("service_key",), settle_s=3.0), read_available=host,
                                   sleep=sleep, clock=clock)
    order = []

    async def ask(label, model, app_id):
        await made.admit(graph(model), hma.Ask(auth_kind=app_id, bounded=False))
        order.append((label, clock.now))

    async def go():
        background = asyncio.ensure_future(ask("background", "b.safetensors", "service_key"))   # asked first
        await asyncio.sleep(0)
        person = asyncio.ensure_future(ask("person", "a.safetensors", "app_key"))
        await asyncio.gather(background, person)

    run(go())
    assert [label for label, _ in order] == ["person", "background"]
    assert order[0][1] <= 6 and order[1][1] >= 40 - 2    # the person ran when memory freed; background after more freed
