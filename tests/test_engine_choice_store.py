import json

import pytest
import yaml

from gateway.engine_protocol.choice_store import ChoiceStore, choices_from_node_info


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


def test_processes_writing_different_keys_keep_both(tmp_path):
    import multiprocessing

    path = tmp_path / 'choices.json'
    keys = [f'p{i}' for i in range(6)]
    ctx = multiprocessing.get_context('spawn')
    procs = [ctx.Process(target=_write_one, args=(path, key)) for key in keys]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    store = ChoiceStore(path)
    assert all(store.read('s', 'node', key) for key in keys)
