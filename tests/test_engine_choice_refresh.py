import pytest

from gateway.engine_protocol.choice_store import ChoiceStore
from gateway.engine_protocol.choice_refresh import is_due, refresh_service

DECLS = [{'node_type': 'node', 'input': 'pick'}]


def info(options):
    return {'input': {'required': {'pick': [options]}}}


def fetcher(result, calls=None):
    def fetch(service, node_type):
        if calls is not None:
            calls.append((service, node_type))
        if isinstance(result, Exception):
            raise result
        return result
    return fetch


def test_success_replaces_old_entry(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    store.write('svc', 'node', 'pick', ['old'], fetched_at=1)
    calls = []
    report = refresh_service(store, 'svc', DECLS, fetch=fetcher(info(['a', 'b']), calls), now=500)
    snap = store.read('svc', 'node', 'pick')
    assert (snap.choices, snap.fetched_at) == (['a', 'b'], 500)
    assert report == {('node', 'pick'): None}
    assert calls == [('svc', 'node')]
    assert store.read_failure('svc', 'node', 'pick') is None


@pytest.mark.parametrize('result', [RuntimeError('engine down'), info([]), {'input': {}}, None])
def test_failed_fetch_keeps_stale_choices_and_reports_failure(tmp_path, result):
    store = ChoiceStore(tmp_path / 'c.json')
    store.write('svc', 'node', 'pick', ['old'], fetched_at=1)
    report = refresh_service(store, 'svc', DECLS, fetch=fetcher(result), now=10**6)
    assert store.read('svc', 'node', 'pick').choices == ['old']
    assert store.read('svc', 'node', 'pick').fetched_at == 1
    failure = store.read_failure('svc', 'node', 'pick')
    assert failure['at'] == 10**6 and failure['reason']
    assert report[('node', 'pick')] == failure['reason']


def test_failure_with_no_entry_writes_no_choices(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    refresh_service(store, 'svc', DECLS, fetch=fetcher(RuntimeError('x')), now=5)
    assert store.read('svc', 'node', 'pick') is None
    assert store.read_failure('svc', 'node', 'pick')['at'] == 5


def test_success_clears_earlier_failure(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    refresh_service(store, 'svc', DECLS, fetch=fetcher(RuntimeError('x')), now=5)
    refresh_service(store, 'svc', DECLS, fetch=fetcher(info(['a'])), now=6)
    assert store.read_failure('svc', 'node', 'pick') is None


def test_is_due(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    assert is_due(store, 'svc', 'node', 'pick', now=0)
    store.write('svc', 'node', 'pick', ['a'], fetched_at=100)
    assert not is_due(store, 'svc', 'node', 'pick', now=100 + 24 * 3600 - 1)
    assert is_due(store, 'svc', 'node', 'pick', now=100 + 24 * 3600)
    assert is_due(store, 'svc', 'node', 'pick', now=150, max_age=50)
    assert not is_due(store, 'svc', 'node', 'pick', now=149, max_age=50)


def test_fetch_fresh_raises_instead_of_falling_back(monkeypatch):
    from gateway.engine_protocol import node_info

    class Boom:
        def __enter__(self):
            raise OSError('down')

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(node_info.engine_egress, 'client', lambda **kw: Boom())
    monkeypatch.setitem(node_info._CACHE, ('h', 'node'), {'cached': True})
    with pytest.raises(OSError):
        node_info.fetch_fresh('h', 'node')
