"""The status probes run off the event loop, once per scope, and a slow section cannot hold the page.

On uvloop a spawn forks the whole gateway on the loop thread, so every systemd probe blocked
every request for as long as the fork took: about 45 ms per probe at 680 MB resident, and 13
probes in one cold Home read. These tests fix where the work runs and how many calls it makes.
None of them is a wall-clock bound, so host load cannot flip them.
"""
import asyncio
import threading

import proxy
import status_endpoint
from gateway import lifecycle_drivers

STATES = {"a.service": "active", "b.service": "inactive", "c.service": "activating", "d.service": "failed"}


def _systemd_config(units: list[str]) -> dict:
    return {"backends": {
        f"svc-{unit}": {"name": unit, "lifecycle": {"driver": "systemd", "driver_config": {"unit": unit, "scope": "user"}}}
        for unit in units
    }}


def _units(argv: list[str]) -> list[str]:
    return argv[argv.index("--") + 1:] if "--" in argv else argv[-1:]


def _answer(argv: list[str]) -> bytes:
    return "".join(f"{STATES.get(unit, 'inactive')}\n" for unit in _units(argv)).encode()


def _record_probes(monkeypatch) -> list[tuple[str, int, list[str]]]:
    """Record every systemctl probe: where it was started (the loop thread or a worker) and its argv."""
    calls: list[tuple[str, int, list[str]]] = []

    class Done:
        returncode = 0

        def __init__(self, out: bytes) -> None:
            self.out = out

        async def communicate(self):
            return self.out, b""

    async def spawn_on_loop(*argv, **kwargs):
        calls.append(("loop", threading.get_ident(), list(argv)))
        return Done(_answer(list(argv)))

    def run_on_worker(argv):
        calls.append(("worker", threading.get_ident(), list(argv)))
        return _answer(list(argv))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_on_loop)
    monkeypatch.setattr(lifecycle_drivers, "_run_probe", run_on_worker, raising=False)
    return calls


def test_systemd_probes_never_start_on_the_event_loop_thread(monkeypatch):
    calls = _record_probes(monkeypatch)
    rows = asyncio.run(status_endpoint._backend_grid(_systemd_config(["a.service", "b.service", "c.service"])))
    assert len(rows) == 3, "the grid did not answer for every systemd engine"
    on_loop = [argv for where, _thread, argv in calls if where == "loop"]
    assert on_loop == [], f"systemctl was started on the event loop thread: {on_loop}"
    assert any(where == "worker" for where, _thread, _argv in calls)


def test_every_systemd_unit_in_a_scope_is_answered_by_one_systemctl_call(monkeypatch):
    calls = _record_probes(monkeypatch)
    units = ["a.service", "b.service", "c.service", "d.service"]
    rows = {row["service_id"]: row for row in asyncio.run(status_endpoint._backend_grid(_systemd_config(units)))}
    assert [argv for _where, _thread, argv in calls] == [["systemctl", "--user", "is-active", "--", *units]]
    assert rows["svc-b.service"]["live_status"] == "stopped"
    assert rows["svc-c.service"]["live_status"] == "starting"
    assert rows["svc-d.service"]["live_status"] == "down"


def test_independent_status_sections_run_at_the_same_time():
    barrier = threading.Barrier(2, timeout=10)  # both sections must be waiting together, or one times out

    async def run():
        errors: dict[str, str] = {}
        results = await status_endpoint.gather_sections(
            {"left": asyncio.to_thread(barrier.wait), "right": asyncio.to_thread(barrier.wait)},
            errors=errors, stale=[])
        return results, errors

    results, errors = asyncio.run(run())
    assert set(results) == {"left", "right"} and errors == {}


def test_a_section_past_its_budget_is_stale_and_does_not_hold_the_others():
    release = threading.Event()

    async def run():
        errors: dict[str, str] = {}
        stale: list[str] = []
        results = await asyncio.wait_for(status_endpoint.gather_sections(
            {"slow": asyncio.to_thread(release.wait, 10), "fast": asyncio.sleep(0, result="answer")},
            errors=errors, stale=stale, budgets={"slow": 0.05, "fast": 5.0}), 15)
        release.set()  # let the worker finish before the event loop shuts its executor down
        return results, errors, stale

    results, errors, stale = asyncio.run(run())
    assert results["fast"] == "answer"
    assert stale == ["slow"] and errors["slow"].startswith("stale")


def test_a_section_that_misses_its_budget_shows_its_last_answer_marked_stale():
    name = "test-last-answer-section"

    async def run():
        await status_endpoint.gather_sections({name: asyncio.sleep(0, result="fresh")}, errors={}, stale=[])
        release = threading.Event()
        errors: dict[str, str] = {}
        stale: list[str] = []
        results = await status_endpoint.gather_sections(
            {name: asyncio.to_thread(release.wait, 10)}, errors=errors, stale=stale, budgets={name: 0.05})
        release.set()
        return results, errors, stale

    results, errors, stale = asyncio.run(run())
    assert results[name] == "fresh" and stale == [name] and errors[name].startswith("stale")


def test_an_availability_failure_is_logged_as_one_line_with_only_its_type(monkeypatch, capsys):
    async def status_without_probes(_config):
        return {}

    async def same_payload(payload):
        return payload

    def registry_fails():
        raise ValueError("/var/lib/azizam/admin.db is locked")  # a path and a message: neither may reach the log

    monkeypatch.setattr(proxy.status_endpoint, "get_cached_status", status_without_probes)
    monkeypatch.setattr(proxy, "_status_payload_with_protocol_issues", same_payload)
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", registry_fails)
    payload = asyncio.run(proxy._status_payload_with_availability())
    lines = [line for line in capsys.readouterr().out.splitlines() if "status availability" in line]
    assert len(lines) == 1 and "ValueError" in lines[0]
    assert "/var/lib" not in lines[0] and "locked" not in lines[0]
    assert payload == {}
