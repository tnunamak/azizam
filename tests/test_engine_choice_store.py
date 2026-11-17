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


def test_two_instances_serialize_writes(tmp_path, monkeypatch):
    import threading

    from gateway.engine_protocol import choice_store

    path = tmp_path / 'choices.json'
    first, second = ChoiceStore(path), ChoiceStore(path)
    first_waiting = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    errors = []
    real_write = choice_store.private_io.atomic_write_text

    def delayed_write(path, content):
        if threading.current_thread() is writer_a:
            first_waiting.set()
            if not release_first.wait(10):
                raise AssertionError('first writer was not released')
        return real_write(path, content)

    monkeypatch.setattr(choice_store.private_io, 'atomic_write_text', delayed_write)

    def write(store, service, choices, started=None):
        try:
            if started is not None:
                started.set()
            store.write(service, 'node', 'input', choices, fetched_at=1)
        except BaseException as exc:
            errors.append(exc)

    writer_a = threading.Thread(target=write, args=(first, 'a', ['choice-a']), daemon=True)
    writer_b = threading.Thread(
        target=write, args=(second, 'b', ['choice-b'], second_started), daemon=True)
    writer_a.start()
    try:
        assert first_waiting.wait(5), 'first writer did not reach atomic replacement'
        writer_b.start()
        assert second_started.wait(5), 'second writer did not start'
        writer_b.join(timeout=0.2)
    finally:
        release_first.set()
        writer_a.join(timeout=5)
        if writer_b.ident is not None:
            writer_b.join(timeout=5)
    assert not writer_a.is_alive() and not writer_b.is_alive(), 'writers did not finish'
    assert not errors
    assert first.read('a', 'node', 'input').choices == ['choice-a']
    assert second.read('b', 'node', 'input').choices == ['choice-b']


def test_write_creates_private_sibling_lock(tmp_path):
    import stat

    store = ChoiceStore(tmp_path / 'new' / 'choices.json')
    store.write('s', 'node', 'input', ['a'], fetched_at=1)
    lock = store.path.with_name(store.path.name + '.lock')
    assert lock.is_file()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert store.read('s', 'node', 'input').choices == ['a']
