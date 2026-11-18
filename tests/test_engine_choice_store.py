from contextlib import contextmanager
import json

import pytest
import yaml

from gateway.engine_protocol.choice_store import ChoiceStore, choices_from_node_info, snapshot_choices


def test_fixture_round_trip_and_age(tmp_path):
    fixture = yaml.safe_load((__import__('pathlib').Path(__file__).parent / 'fixtures/engine_choices/input.yml').read_text())
    declaration = fixture['timesignature']['choices_from']
    store = ChoiceStore(tmp_path / 'choices.json')
    assert store.read('service-a', **declaration) is None
    store.write('service-a', **declaration, choices=['2', '4', '7'], fetched_at=100)
    result = ChoiceStore(store.path).read('service-a', **declaration)
    assert result.choices == ['2', '4', '7']
    assert not result.stale(now=109, max_age=10)
    assert result.stale(now=110, max_age=10)
    assert store.read('service-b', **declaration) is None


def test_invalid_refresh_keeps_last_good(tmp_path):
    store = ChoiceStore(tmp_path / 'choices.json')
    store.write('s', 'node', 'input', ['a'], fetched_at=1)
    for choices in ([], [1], ['a', 'a']):
        with pytest.raises(ValueError):
            store.write('s', 'node', 'input', choices, fetched_at=2)
    assert store.read('s', 'node', 'input').choices == ['a']


def test_schema_and_corruption_are_misses_and_not_overwritten(tmp_path):
    path = tmp_path / 'choices.json'
    store = ChoiceStore(path)
    for content in ('{', json.dumps({'version': 99, 'entries': []})):
        path.write_text(content)
        assert store.read('s', 'n', 'i') is None
        with pytest.raises(ValueError):
            store.write('s', 'n', 'i', ['x'], fetched_at=1)
        assert path.read_text() == content


@pytest.mark.parametrize("section", ["required", "optional"])
@pytest.mark.parametrize("entry", [
    [["last", "first", "middle"]],
    [["last", "first", "middle"], {"default": "first"}],
    ["COMBO", {"options": ["last", "first", "middle"]}],
])
def test_choices_from_node_info_preserves_full_order(section, entry):
    from copy import deepcopy
    from types import MappingProxyType

    before = deepcopy(entry)
    info = MappingProxyType({"input": MappingProxyType({
        section: MappingProxyType({"selection": entry}),
    })})
    choices = choices_from_node_info(info, "selection")
    assert choices == ["last", "first", "middle"]
    choices.append("new")
    assert entry == before


@pytest.mark.parametrize("info", [
    None, [], "private-value",
    {}, {"input": None}, {"input": []},
    {"input": {}},
    {"input": {"required": None}},
    {"input": {"optional": []}},
    {"input": {"required": {"other": [["private-value"]]}}},
    {"input": {"required": {"selection": None}}},
    {"input": {"required": {"selection": "private-value"}}},
    {"input": {"required": {"selection": []}}},
    {"input": {"required": {"selection": (["private-value"],)}}},
    {"input": {"required": {"selection": ["STRING"]}}},
    {"input": {"required": {"selection": [17]}}},
    {"input": {"required": {"selection": ["COMBO"]}}},
    {"input": {"required": {"selection": ["COMBO", None]}}},
    {"input": {"required": {"selection": ["COMBO", {}]}}},
    {"input": {"required": {"selection": ["COMBO", {"options": "private-value"}]}}},
])
def test_choices_from_node_info_rejects_invalid_shapes(info):
    with pytest.raises(ValueError) as failure:
        choices_from_node_info(info, "selection")
    assert "selection" in str(failure.value)
    assert "private-value" not in str(failure.value)


@pytest.mark.parametrize("section", ["required", "optional"])
@pytest.mark.parametrize("form", ["list", "combo"])
@pytest.mark.parametrize("options", [[], ["private-value", 1], [""], ["private-value", "private-value"], [None], [{}]])
def test_choices_from_node_info_rejects_invalid_options(section, form, options):
    entry = [options] if form == "list" else ["COMBO", {"options": options}]
    with pytest.raises(ValueError) as failure:
        choices_from_node_info({"input": {section: {"selection": entry}}}, "selection")
    assert "selection" in str(failure.value)
    assert "private-value" not in str(failure.value)


def test_failed_extraction_without_write_keeps_snapshot(tmp_path):
    store = ChoiceStore(tmp_path / "choices.json")
    store.write("service", "node", "selection", ["saved", "ordered"], fetched_at=10)
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        choices_from_node_info(
            {"input": {"optional": {"selection": ["COMBO", {"options": []}]}}},
            "selection",
        )
    assert store.path.read_bytes() == before
    snapshot = ChoiceStore(store.path).read("service", "node", "selection")
    assert snapshot.choices == ["saved", "ordered"]
    assert snapshot.fetched_at == 10


def _write_one(path, key):
    ChoiceStore(path).write('s', 'node', key, ['a'], fetched_at=1)


def test_threads_writing_different_keys_keep_both(tmp_path, monkeypatch):
    import threading
    import time
    from gateway.engine_protocol import choice_store

    path = tmp_path / 'choices.json'
    real_load = ChoiceStore._load

    def slow_load(self):
        entries = real_load(self)
        time.sleep(0.05)
        return entries

    monkeypatch.setattr(ChoiceStore, '_load', slow_load)
    keys = [f'k{i}' for i in range(6)]
    threads = [threading.Thread(target=_write_one, args=(path, key)) for key in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    monkeypatch.setattr(ChoiceStore, '_load', real_load)
    store = ChoiceStore(path)
    assert all(store.read('s', 'node', key) for key in keys)
    lock = path.with_name(path.name + '.lock')
    assert lock.exists() and (lock.stat().st_mode & 0o777) == 0o600
    assert choice_store is not None


def test_bounded_flock_accepts_only_exclusive_and_unlock(tmp_path):
    import fcntl

    with (tmp_path / "lock").open("w+b") as lock:
        bounded_flock(lock, fcntl.LOCK_EX, real_flock=fcntl.flock)
        try:
            for operation in (fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_SH):
                with pytest.raises(AssertionError, match=f"operation {operation}"):
                    bounded_flock(lock, operation, real_flock=fcntl.flock)
        finally:
            bounded_flock(lock, fcntl.LOCK_UN, real_flock=fcntl.flock)


def bounded_flock(fd, operation, *, real_flock):
    """Bound exclusive contention without changing the requested lock contract."""
    import fcntl
    import time

    if operation == fcntl.LOCK_UN:
        return real_flock(fd, operation)
    if operation != fcntl.LOCK_EX:
        raise AssertionError(f"unexpected flock operation {operation}")
    deadline = time.monotonic() + 5
    while True:
        try:
            return real_flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("choice-store lock remained held")
            time.sleep(min(0.01, remaining))


def _spawn_choice_writer(path, key, pipe):
    """Use the real kernel lock, but bound contention so failures need no signals."""
    import fcntl

    real_flock = fcntl.flock
    fcntl.flock = lambda fd, operation: bounded_flock(fd, operation, real_flock=real_flock)
    try:
        store = ChoiceStore(path)
        pipe.send('ready')
        store.write('s', 'node', key, ['child'], fetched_at=2)
        pipe.send('done')
    finally:
        fcntl.flock = real_flock
        pipe.close()


def test_other_process_lock_blocks_write(tmp_path):
    import fcntl
    import multiprocessing

    path = tmp_path / 'choices.json'
    store = ChoiceStore(path)
    store.write('s', 'node', 'parent', ['parent'], fetched_at=1)
    ctx = multiprocessing.get_context('spawn')
    receive, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_spawn_choice_writer, args=(path, 'child', send))
    started = False
    try:
        with path.with_name(path.name + '.lock').open('r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                proc.start()
                started = True
                send.close()
                assert receive.poll(10), "writer did not become ready"
                assert receive.recv() == 'ready'
                # Starting the clock after readiness avoids counting spawn latency.
                assert not receive.poll(1), "writer bypassed the sibling lock"
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        assert receive.poll(10), "writer did not finish after unlock"
        assert receive.recv() == 'done'
    finally:
        send.close()
        if started:
            proc.join(10)
        receive.close()
    assert proc.exitcode == 0
    proc.close()
    assert store.read('s', 'node', 'parent').choices == ['parent']
    assert store.read('s', 'node', 'child').choices == ['child']


def test_failed_write_releases_lock_for_other_process(tmp_path, monkeypatch):
    import multiprocessing

    path = tmp_path / 'choices.json'
    store = ChoiceStore(path)

    def fail_load():
        raise RuntimeError('injected locked load failure')

    # Patch this instance only; spawn imports a fresh module in the writer.
    with monkeypatch.context() as patch:
        patch.setattr(store, '_load', fail_load)
        with pytest.raises(RuntimeError, match='injected locked load failure'):
            store.write('s', 'node', 'failed', ['parent'], fetched_at=1)

    ctx = multiprocessing.get_context('spawn')
    receive, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_spawn_choice_writer, args=(path, 'after-failure', send))
    started = False
    try:
        proc.start()
        started = True
        send.close()
        assert receive.poll(10), "writer did not become ready"
        assert receive.recv() == 'ready'
        assert receive.poll(10), "failed write retained the sibling lock"
        assert receive.recv() == 'done'
    finally:
        send.close()
        if started:
            proc.join(10)
        receive.close()
    assert proc.exitcode == 0
    proc.close()
    assert store.read('s', 'node', 'after-failure').choices == ['child']
    assert store.read('s', 'node', 'failed') is None

def test_newest_fetch_wins(tmp_path):
    store = ChoiceStore(tmp_path / 'choices.json')
    assert store.write('s', 'node', 'k', ['new'], fetched_at=200) is True
    assert store.write('s', 'node', 'k', ['old'], fetched_at=100) is False
    snapshot = store.read('s', 'node', 'k')
    assert (snapshot.choices, snapshot.fetched_at) == (['new'], 200)


def test_equal_fetched_at_replaces(tmp_path):
    store = ChoiceStore(tmp_path / 'choices.json')
    assert store.write('s', 'node', 'k', ['first'], fetched_at=200) is True
    assert store.write('s', 'node', 'k', ['second'], fetched_at=200) is True
    assert store.read('s', 'node', 'k').choices == ['second']


def test_older_write_leaves_other_keys_alone(tmp_path):
    store = ChoiceStore(tmp_path / 'choices.json')
    store.write('s', 'node', 'k', ['new'], fetched_at=200)
    store.write('s', 'node', 'other', ['o'], fetched_at=50)
    assert store.write('s', 'node', 'k', ['old'], fetched_at=100) is False
    assert store.write('s', 'node', 'fresh', ['f'], fetched_at=10) is True
    assert store.read('s', 'node', 'other').choices == ['o']
    assert store.read('s', 'node', 'fresh').choices == ['f']
    assert store.read('s', 'node', 'k').choices == ['new']


def test_success_clears_only_failures_not_newer_than_snapshot(tmp_path):
    store = ChoiceStore(tmp_path / 'choices.json')
    store.record_failure('s', 'node', 'k', 'boom', at=200)
    assert store.write_clearing_failure('s', 'node', 'k', ['a'], fetched_at=200) is True
    assert store.read_failure('s', 'node', 'k') is None
    store.record_failure('s', 'node', 'k', 'boom', at=300)
    assert store.write_clearing_failure('s', 'node', 'k', ['b'], fetched_at=250) is True
    assert store.read_failure('s', 'node', 'k')['at'] == 300


def test_superseded_write_keeps_failure(tmp_path):
    store = ChoiceStore(tmp_path / 'choices.json')
    store.write('s', 'node', 'k', ['new'], fetched_at=200)
    store.record_failure('s', 'node', 'k', 'boom', at=250)
    assert store.write_clearing_failure('s', 'node', 'k', ['old'], fetched_at=100) is False
    assert store.read_failure('s', 'node', 'k')['at'] == 250
    assert store.read('s', 'node', 'k').choices == ['new']


def test_failure_older_than_snapshot_is_not_recorded(tmp_path):
    store = ChoiceStore(tmp_path / 'choices.json')
    store.write('s', 'node', 'k', ['a'], fetched_at=200)
    assert store.record_failure('s', 'node', 'k', 'boom', at=100) is False
    assert store.read_failure('s', 'node', 'k') is None
    assert store.record_failure('s', 'node', 'k', 'boom', at=200) is True
    assert store.read_failure('s', 'node', 'k')['at'] == 200


def test_failure_recorded_between_snapshot_and_clear_survives(tmp_path):
    """S stores at 200; F records at 201 right after S's first lock release; F must survive."""
    import threading

    class Hooked(ChoiceStore):
        def __init__(self, path):
            super().__init__(path)
            self.main = threading.get_ident()
            self.first_release = threading.Event()
            self.failure_done = threading.Event()

        @contextmanager
        def _locked(self):
            with super()._locked():
                yield
            if threading.get_ident() == self.main and not self.first_release.is_set():
                self.first_release.set()
                assert self.failure_done.wait(10)

    store = Hooked(tmp_path / 'choices.json')

    def failer():
        assert store.first_release.wait(10)
        store.record_failure('s', 'node', 'k', 'boom', at=201)
        store.failure_done.set()

    thread = threading.Thread(target=failer)
    thread.start()
    store.write_clearing_failure('s', 'node', 'k', ['a'], fetched_at=200)
    thread.join(10)
    assert store.read_failure('s', 'node', 'k')['at'] == 201


def _child(path, key, barrier, rounds, no_lock):
    import time
    from gateway.engine_protocol import choice_store

    real_load = ChoiceStore._load

    def slow_load(self):
        entries = real_load(self)
        time.sleep(0.02)
        return entries

    ChoiceStore._load = slow_load
    if no_lock:
        choice_store.fcntl.flock = lambda *args: None
    store = ChoiceStore(path)
    for round_ in range(rounds):
        barrier.wait(30)  # rendezvous outside the lock so every round overlaps
        store.write('s', 'node', key, [f'r{round_}'], fetched_at=round_)


def _run_children(path, *, no_lock, rounds=10, count=4):
    import multiprocessing

    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(count)
    procs = [ctx.Process(target=_child, args=(path, f'p{i}', barrier, rounds, no_lock))
             for i in range(count)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
    for proc in procs:
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
    assert [proc.exitcode for proc in procs] == [0] * count
    store = ChoiceStore(path)
    return {f'p{i}': store.read('s', 'node', f'p{i}') for i in range(count)}


def _lost_keys(final, rounds=10):
    return [key for key, snap in final.items() if snap is None or snap.choices != [f'r{rounds - 1}']]


def test_processes_writing_different_keys_keep_all(tmp_path):
    assert _lost_keys(_run_children(tmp_path / 'choices.json', no_lock=False)) == []


def test_processes_without_the_lock_lose_keys(tmp_path):
    """Negative control: the same run with flock stubbed out must lose a write."""
    assert _lost_keys(_run_children(tmp_path / 'choices.json', no_lock=True))


def test_snapshot_choices_includes_only_available_declared_inputs(tmp_path):
    from types import MappingProxyType

    store = ChoiceStore(tmp_path / "choices.json")
    store.write("service", "node", "selection", ["last", "first"], fetched_at=100)
    inputs = {
        "available": {"choices_from": MappingProxyType({"node_type": "node", "input": "selection"})},
        "missing": {"choices_from": {"node_type": "node", "input": "missing"}, "enum": ["fallback"]},
        "plain": {"enum": ["plain"]},
    }
    assert snapshot_choices(store, "service", inputs) == {"available": ["last", "first"]}


def test_snapshot_choices_keeps_stale_snapshot(tmp_path):
    import time

    store = ChoiceStore(tmp_path / "choices.json")
    store.write("service", "node", "selection", ["saved"], fetched_at=time.time() - 172800)
    assert store.read("service", "node", "selection").stale(now=time.time(), max_age=86400)
    inputs = {"value": {"choices_from": {"node_type": "node", "input": "selection"}}}
    assert snapshot_choices(store, "service", inputs) == {"value": ["saved"]}


def test_snapshot_choices_isolates_services(tmp_path):
    store = ChoiceStore(tmp_path / "choices.json")
    store.write("service-a", "node", "selection", ["saved"], fetched_at=100)
    inputs = {"value": {"choices_from": {"node_type": "node", "input": "selection"}}}
    assert snapshot_choices(store, "service-b", inputs) == {}


def test_snapshot_choices_skips_malformed_declarations_without_reading(tmp_path, monkeypatch):
    store = ChoiceStore(tmp_path / "choices.json")

    def unexpected_read(*args):
        pytest.fail("malformed declaration reached the store")

    monkeypatch.setattr(store, "read", unexpected_read)
    declarations = [
        None, [], "node", {},
        {"node_type": "node"}, {"input": "selection"},
        {"node_type": "", "input": "selection"},
        {"node_type": "node", "input": ""},
        {"node_type": 1, "input": "selection"},
        {"node_type": "node", "input": 1},
        {"node_type": None, "input": "selection"},
        {"node_type": "node", "input": None},
        {"node_type": "node", "input": "selection", "extra": "not-allowed"},
    ]
    inputs = {str(i): {"choices_from": declaration} for i, declaration in enumerate(declarations)}
    inputs["plain"] = {"enum": ["fallback"]}
    inputs["bad-spec"] = None
    assert snapshot_choices(store, "service", inputs) == {}


def test_snapshot_choices_copies_stored_lists(tmp_path, monkeypatch):
    store = ChoiceStore(tmp_path / "choices.json")
    store.write("service", "node", "selection", ["saved"], fetched_at=100)
    snapshot = store.read("service", "node", "selection")
    # Reuse the actual snapshot object so disk re-reading cannot hide aliasing.
    monkeypatch.setattr(store, "read", lambda *args: snapshot)
    inputs = {"value": {"choices_from": {"node_type": "node", "input": "selection"}}}
    first = snapshot_choices(store, "service", inputs)
    assert first["value"] is not snapshot.choices
    first["value"].append("changed")
    second = snapshot_choices(store, "service", inputs)
    assert second == {"value": ["saved"]}
    assert first is not second
    assert snapshot.choices == ["saved"]
