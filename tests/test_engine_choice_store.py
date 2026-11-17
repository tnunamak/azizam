import json

import pytest
import yaml

from gateway.engine_protocol.choice_store import ChoiceStore


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
