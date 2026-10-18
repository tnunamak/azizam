"""Regression checks for the work avoided by exact subtree accounting."""
from types import SimpleNamespace

from gateway.engine_protocol import mapping
from gateway.engine_protocol.budgets import Budgets


def test_each_does_not_reserialize_copied_vectors(monkeypatch):
    vector = [i / 997 for i in range(768)]
    original = mapping._json_size
    visits = 0

    def size(value):
        nonlocal visits
        def contains(node):
            if node is vector:
                return True
            if type(node) is dict:
                return any(contains(v) for v in node.values())
            if type(node) is list:
                # Clones still contain the same immutable float identities.
                if len(node) == len(vector) and all(a is b for a, b in zip(node, vector)):
                    return True
                return any(contains(v) for v in node)
            return False
        visits += contains(value)
        return original(value)

    monkeypatch.setattr(mapping, '_json_size', size)
    rules = [{'each': {'from': '/body/vectors', 'to': '/data', 'map': [
        {'copy': {'from': '/item', 'to': '/embedding'}}]}}]
    result = mapping.build(rules, base='empty', roots=mapping.RESPONSE_ROOTS).apply(
        {'body': {'vectors': [vector, vector]}})
    assert result == {'data': [{'embedding': vector}, {'embedding': vector}]}
    assert visits <= 1


def test_each_batches_clock_reads(monkeypatch):
    calls = 0
    def clock():
        nonlocal calls
        calls += 1
        return 0.0
    monkeypatch.setattr(mapping, 'time', SimpleNamespace(perf_counter=clock))
    rules = [{'each': {'from': '/body/items', 'to': '/data', 'map': [
        {'copy': {'from': '/item', 'to': '/value'}}]}}]
    result = mapping.build(rules, base='empty', roots=mapping.RESPONSE_ROOTS,
                           budgets=Budgets(mapping_s=1)).apply({'body': {'items': list(range(1000))}})
    assert len(result['data']) == 1000
    assert calls < 200


def test_flat_numeric_array_accounting_uses_bounded_batches(monkeypatch):
    original = mapping._JsonFacts.measure
    floats = 0
    def measure(facts, value, level=1):
        nonlocal floats
        floats += type(value) is float
        return original(facts, value, level)
    monkeypatch.setattr(mapping._JsonFacts, 'measure', measure)
    facts = mapping._JsonFacts(Budgets())
    value = [i / 997 for i in range(768)]
    import json
    assert facts.measure(value) == (len(json.dumps(value, separators=(',', ':')).encode()), 1)
    assert floats == 0


def test_unused_nonserializable_source_values_are_not_new_refusals():
    rules = [{'each': {'from': '/body/items', 'to': '/data', 'map': []}}]
    result = mapping.build(rules, base='empty', roots=mapping.RESPONSE_ROOTS).apply(
        {'body': {'items': [0], 'unused': '\ud800'}})
    assert result == {'data': [{}]}


def test_signed_zero_sizes_remain_distinct():
    import pytest
    from gateway.engine_protocol.errors import BudgetExceeded
    rules = [{'each': {'from': '/body/items', 'to': '/out', 'map': [
        {'copy': {'from': '/item', 'to': '/value'}}]}}]
    with pytest.raises(BudgetExceeded) as raised:
        mapping.build(rules, base='empty', roots=mapping.RESPONSE_ROOTS,
                      budgets=Budgets(max_response_bytes=37)).apply({'body': {'items': [0.0, -0.0]}})
    assert raised.value.budget == 'max_response_bytes'


def test_scalar_sizes_use_exact_encoder_without_document_serialization(monkeypatch):
    import json
    values = ['é😀"\\\n', -0.0, 0.0, 1.234e100, float('nan'), float('inf'), -float('inf'), True, None, -999]
    expected = [len(json.dumps(v, ensure_ascii=False, separators=(',', ':')).encode()) for v in values]
    def no_document_serializer(*args, **kwargs):
        raise AssertionError('scalar sizing called the full document serializer')
    monkeypatch.setattr(mapping.json, 'dumps', no_document_serializer)
    assert [mapping._json_size(v) for v in values] == expected


def test_chunked_string_accounting_bounds_deadline_overshoot(monkeypatch):
    import pytest
    from gateway.engine_protocol.errors import BudgetExceeded
    elapsed = SimpleNamespace(now=0.0)
    monkeypatch.setattr(mapping, 'time', SimpleNamespace(perf_counter=lambda: elapsed.now))
    original = mapping._json_size
    def costly_string(value):
        if type(value) is str:
            elapsed.now += len(value) * 0.0000001
        return original(value)
    monkeypatch.setattr(mapping, '_json_size', costly_string)
    rules = [{'each': {'from': '/body/items', 'to': '/data', 'map': [
        {'copy': {'from': '/item', 'to': '/value'}}]}}]
    limit = .001
    with pytest.raises(BudgetExceeded) as raised:
        mapping.build(rules, base='empty', roots=mapping.RESPONSE_ROOTS,
                      budgets=Budgets(mapping_s=limit)).apply({'body': {'items': ['x' * 1_000_000]}})
    assert raised.value.budget == 'mapping_s'
    assert 0 < elapsed.now - limit < .005


def test_each_all_runtime_budgets_refuse_before_unbounded_growth():
    import time
    import pytest
    from gateway.engine_protocol.errors import BudgetExceeded
    rules = [{'each': {'from': '/body/items', 'to': '/data', 'map': [
        {'copy': {'from': '/item', 'to': '/value'}}]}}]
    deep = []
    for _ in range(5000):
        deep = [deep]
    cases = [
        (Budgets(max_selected=10), list(range(10000)), 'max_selected'),
        (Budgets(max_response_bytes=100), ['x' * 1000] * 10000, 'max_response_bytes'),
        (Budgets(max_json_depth=32), [deep], 'max_json_depth'),
        (Budgets(mapping_s=.001), [list(range(768)) for _ in range(10000)], 'mapping_s'),
    ]
    for budgets, items, name in cases:
        program = mapping.build(rules, base='empty', roots=mapping.RESPONSE_ROOTS, budgets=budgets)
        start = time.perf_counter()
        with pytest.raises(BudgetExceeded) as raised:
            program.apply({'body': {'items': items}})
        assert raised.value.budget == name
        assert time.perf_counter() - start < 1.0
