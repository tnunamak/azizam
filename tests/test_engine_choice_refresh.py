import pytest

# Capture before autouse fixtures replace the public lookup during each test.
from gateway.engine_protocol.node_info import lookup as _real_lookup

from gateway.engine_protocol.choice_store import ChoiceStore
from gateway.engine_protocol.choice_refresh import DEFAULT_MAX_AGE, freshness, is_due, refresh_service

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


def test_overlapped_refresh_older_one_is_superseded(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    newer = refresh_service(store, 'svc', DECLS, fetch=fetcher(info(['b'])), now=200)
    older = refresh_service(store, 'svc', DECLS, fetch=fetcher(info(['a'])), now=100)
    assert newer == {('node', 'pick'): None}
    assert older == {('node', 'pick'): 'superseded by a newer snapshot'}
    snap = store.read('svc', 'node', 'pick')
    assert (snap.choices, snap.fetched_at) == (['b'], 200)
    assert store.read_failure('svc', 'node', 'pick') is None


def test_superseded_success_clears_no_failure(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    refresh_service(store, 'svc', DECLS, fetch=fetcher(info(['b'])), now=200)
    store.record_failure('svc', 'node', 'pick', 'later failure', at=250)
    refresh_service(store, 'svc', DECLS, fetch=fetcher(info(['a'])), now=100)
    assert store.read_failure('svc', 'node', 'pick')['at'] == 250


def test_failure_older_than_snapshot_is_reported_superseded(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    refresh_service(store, 'svc', DECLS, fetch=fetcher(info(['b'])), now=200)
    report = refresh_service(store, 'svc', DECLS, fetch=fetcher(RuntimeError('x')), now=100)
    assert report == {('node', 'pick'): 'superseded by a newer snapshot'}
    assert store.read_failure('svc', 'node', 'pick') is None


def fake_node_client(monkeypatch, status, payload, urls=None):
    import httpx
    from gateway.engine_protocol import node_info

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            if urls is not None:
                urls.append(url)
            assert kwargs['extensions'] == {'test_probe': True}
            return httpx.Response(status, json=payload, request=httpx.Request('GET', url))

    # The suite replaces lookup during fixture setup. Restore only this test's
    # real reader; monkeypatch teardown reinstates the suite's replacement.
    monkeypatch.setattr(node_info, 'lookup', _real_lookup)
    monkeypatch.setattr(node_info.engine_egress, 'probe', lambda: {'test_probe': True})
    monkeypatch.setattr(node_info.engine_egress, 'client', lambda **kw: Client())
    monkeypatch.setattr(node_info, '_CACHE', {})
    monkeypatch.setattr(node_info, '_FAILED_AT', {})
    monkeypatch.setattr(node_info, 'DISK_CACHE_DIR', None)
    return node_info


@pytest.mark.parametrize('disk_copy', [None, {'cached': True}], ids=['no-disk', 'disk'])
def test_fetch_fresh_503_raises_while_lookup_falls_back(monkeypatch, disk_copy):
    import httpx

    urls = []
    node_info = fake_node_client(monkeypatch, 503, {'node': {'live': True}}, urls)
    monkeypatch.setattr(node_info, '_disk_read', lambda node_type: disk_copy)
    with pytest.raises(httpx.HTTPStatusError):
        node_info.fetch_fresh('http://engine', 'node')
    assert node_info.lookup('http://engine', 'node') == disk_copy
    assert ('http://engine', 'node') not in node_info._CACHE
    assert ('http://engine', 'node') in node_info._FAILED_AT
    assert node_info.lookup('http://engine', 'node') == disk_copy
    assert len(urls) == 2  # The failure window prevents another lookup request.


def test_fetch_fresh_200_returns_definition_without_caching(monkeypatch):
    definition = {'input': {'required': {}}}
    node_info = fake_node_client(monkeypatch, 200, {'node': definition})
    disk_calls = []
    monkeypatch.setattr(node_info, '_disk_read', lambda *args: disk_calls.append(args))
    monkeypatch.setattr(node_info, '_disk_write', lambda *args: disk_calls.append(args))
    assert node_info.fetch_fresh('http://engine', 'node') == definition
    assert node_info._CACHE == {}
    assert node_info._FAILED_AT == {}
    assert disk_calls == []
    node_info._CACHE[('http://engine', 'node')] = {'old': True}
    assert node_info.fetch_fresh('http://engine', 'node') == definition
    assert node_info._CACHE[('http://engine', 'node')] == {'old': True}
    assert disk_calls == []


def test_lookup_and_fetch_fresh_use_same_quoted_url(monkeypatch):
    urls = []
    definition = {'input': {}}
    node_info = fake_node_client(monkeypatch, 200, {'A B/C': definition}, urls)
    assert node_info.lookup('http://engine', 'A B/C') == definition
    assert node_info.fetch_fresh('http://engine', 'A B/C') == definition
    assert urls == ['http://engine/object_info/A%20B%2FC'] * 2


def test_lookup_still_accepts_only_200(monkeypatch):
    definition = {'input': {}}
    node_info = fake_node_client(monkeypatch, 201, {'node': definition})
    assert node_info.fetch_fresh('http://engine', 'node') == definition
    assert node_info.lookup('http://engine', 'node') is None
    assert node_info._CACHE == {}


@pytest.mark.parametrize('payload', [{}, {'node': None}, {'node': []}], ids=['missing', 'null', 'list'])
def test_fetch_fresh_missing_definition_raises(monkeypatch, payload):
    node_info = fake_node_client(monkeypatch, 200, payload)
    with pytest.raises(ValueError):
        node_info.fetch_fresh('http://engine', 'node')


def test_freshness_fresh_snapshot_at_fetch_time(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    store.write('svc', 'node', 'pick', ['a', 'b'], fetched_at=100)
    before = (tmp_path / 'c.json').read_bytes()
    assert freshness(store, 'svc', DECLS, now=100) == [{
        'node_type': 'node', 'input': 'pick', 'fetched_at': 100,
        'age_s': 0, 'stale': False, 'choices_count': 2,
        'failure_reason': None, 'failed_at': None,
    }]
    assert (tmp_path / 'c.json').read_bytes() == before


@pytest.mark.parametrize('max_age', [50, DEFAULT_MAX_AGE], ids=['custom', 'default'])
def test_freshness_stale_at_exact_max_age(tmp_path, max_age):
    store = ChoiceStore(tmp_path / 'c.json')
    store.write('svc', 'node', 'pick', ['a'], fetched_at=100)
    now = 100 + max_age
    row = freshness(store, 'svc', DECLS, now=now, max_age=max_age)[0]
    assert row['stale'] is True
    assert row['stale'] == is_due(store, 'svc', 'node', 'pick', now=now, max_age=max_age)
    assert row['age_s'] == max_age
    assert row['fetched_at'] == 100
    assert row['choices_count'] == 1


def test_freshness_missing_snapshot_shows_failure(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    store.record_failure('svc', 'node', 'pick', 'refresh unavailable', at=90)
    assert freshness(store, 'svc', DECLS, now=100) == [{
        'node_type': 'node', 'input': 'pick', 'fetched_at': None,
        'age_s': None, 'stale': True, 'choices_count': None,
        'failure_reason': 'refresh unavailable', 'failed_at': 90,
    }]


def test_freshness_keeps_declaration_order_and_service(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    store.write('svc', 'z', 'second', ['a', 'b'], fetched_at=20)
    store.write('other', 'a', 'first', ['foreign'], fetched_at=30)
    declarations = [
        {'node_type': 'z', 'input': 'second'},
        {'node_type': 'a', 'input': 'first'},
        {'node_type': 'z', 'input': 'second'},
    ]
    rows = freshness(store, 'svc', declarations, now=20)
    assert [(row['node_type'], row['input']) for row in rows] == [
        ('z', 'second'), ('a', 'first'), ('z', 'second'),
    ]
    assert [row['choices_count'] for row in rows] == [2, None, 2]
    assert rows[1] == {
        'node_type': 'a', 'input': 'first', 'fetched_at': None,
        'age_s': None, 'stale': True, 'choices_count': None,
        'failure_reason': None, 'failed_at': None,
    }
    assert rows[0] == rows[2]
    assert freshness(store, 'svc', [], now=20) == []


def test_freshness_clamps_age_for_earlier_clock(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    store.write('svc', 'node', 'pick', ['a'], fetched_at=100)
    row = freshness(store, 'svc', DECLS, now=99)[0]
    assert row['fetched_at'] == 100
    assert row['age_s'] == 0
    assert row['stale'] == is_due(store, 'svc', 'node', 'pick', now=99)


def test_freshness_keeps_failure_older_than_snapshot(tmp_path, monkeypatch):
    store = ChoiceStore(tmp_path / 'c.json')
    store.write('svc', 'node', 'pick', ['a'], fetched_at=100)
    calls = []
    # A public reader can expose historical failure evidence alongside a newer snapshot.
    def read_failure(service, node_type, input_name):
        calls.append((service, node_type, input_name))
        return {'reason': 'previous refresh failed', 'at': 50}
    monkeypatch.setattr(store, 'read_failure', read_failure)
    row = freshness(store, 'svc', DECLS, now=100)[0]
    assert row['stale'] is False
    assert row['failure_reason'] == 'previous refresh failed'
    assert row['failed_at'] == 50
    assert calls == [('svc', 'node', 'pick')]


def test_freshness_reads_each_snapshot_once_during_replacement(tmp_path):
    store = ChoiceStore(tmp_path / 'c.json')
    declarations = [
        {'node_type': 'node', 'input': 'pick'},
        {'node_type': 'other', 'input': 'select'},
    ]
    old, fresh = {}, {}
    for index, declaration in enumerate(declarations):
        key = ('svc', declaration['node_type'], declaration['input'])
        store.write(*key, [f'old-{n}' for n in range(index + 1)], fetched_at=100 + index)
        old[key] = store.read(*key)
        store.write(*key, [f'new-{n}' for n in range(index + 3)], fetched_at=199)
        fresh[key] = store.read(*key)

    class ReplacingStore:
        def __init__(self):
            self.calls = []

        def read(self, service, node_type, input_name):
            key = (service, node_type, input_name)
            self.calls.append(key)
            return old[key] if self.calls.count(key) == 1 else fresh[key]

        def read_failure(self, service, node_type, input_name):
            return store.read_failure(service, node_type, input_name)

    replacing = ReplacingStore()
    rows = freshness(replacing, 'svc', declarations, now=200, max_age=50)
    assert rows == [
        {
            'node_type': declaration['node_type'], 'input': declaration['input'],
            'fetched_at': 100 + index, 'age_s': 100 - index,
            'stale': True, 'choices_count': index + 1,
            'failure_reason': None, 'failed_at': None,
        }
        for index, declaration in enumerate(declarations)
    ]
    assert replacing.calls == [('svc', 'node', 'pick'), ('svc', 'other', 'select')]
