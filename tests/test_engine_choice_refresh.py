import pytest

# Capture before autouse fixtures replace the public lookup during each test.
from gateway.engine_protocol.node_info import lookup as _real_lookup

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
