from gateway import gpu_observation
import proxy
_RESOLVE_UUID = proxy._resolve_gpu0_uuid


def test_selected_device_follows_identity_and_order():
    rows = [{'index': 2, 'uuid': 'other', 'vram_free': 10}, {'index': 7, 'uuid': 'chosen', 'vram_free': 20}]
    selector = {'field': 'uuid', 'value': 'chosen'}
    assert gpu_observation.select_device(rows, selector)['vram_free'] == 20
    assert gpu_observation.select_device(rows[::-1], selector)['vram_free'] == 20
    assert gpu_observation.select_device(rows, {'field': 'index', 'value': 2})['vram_free'] == 10
    assert gpu_observation.select_device(rows, {'field': 'uuid', 'value': 'missing'}) is None


def test_stats_admission_and_uuid_use_service_descriptor(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import proxy
    descriptor = {'stats_selector': {'field': 'index', 'value': 7},
                  'host_selector': {'field': 'index', 'value': 7},
                  'uuid_probe': {'argv': ['fixture-observer', '--device-identities'],
                                 'fields': [{'key': 'index', 'type': 'int'}, {'key': 'uuid', 'type': 'str'}]}}
    monkeypatch.setattr(proxy, '_graph_service', lambda cap: SimpleNamespace(record={'config': {'inference_device': descriptor}}))
    reply = SimpleNamespace(status=200, json=lambda: {'devices': [
        {'index': 2, 'vram_free': 10, 'vram_total': 100}, {'index': 7, 'vram_free': 20, 'vram_total': 200}]})
    monkeypatch.setattr(proxy, '_graph', lambda *args, **kw: SimpleNamespace(stats=AsyncMock(return_value=reply)))
    assert asyncio.run(proxy.graph_vram_snapshot(None, 'test')) == (20, 200)
    argv = []
    def run(command, **kw):
        argv.append(command)
        return SimpleNamespace(returncode=0, stdout='2, other\n7, chosen\n', stderr='')
    monkeypatch.setattr(proxy.subprocess, 'run', run)
    assert _RESOLVE_UUID('test') == 'chosen'
    assert argv == [descriptor['uuid_probe']['argv']]


def test_identity_cache_tracks_effective_observer_and_selector():
    calls = []
    def run(argv):
        calls.append(argv)
        return 0, "1, first\n2, second\n", ""
    descriptor = {'stats_selector': {'field': 'index', 'value': 1},
                  'host_selector': {'field': 'index', 'value': 1},
                  'uuid_probe': {'argv': ['fixture-observer'], 'fields': [
                      {'key': 'index', 'type': 'int'}, {'key': 'uuid', 'type': 'str'}]}}
    record = {'config': {'inference_device': descriptor}}
    assert gpu_observation.inference_uuid(run, record) == 'first'
    assert gpu_observation.inference_uuid(run, record) == 'first'
    assert calls == [['fixture-observer']]
    descriptor['host_selector']['value'] = 2
    assert gpu_observation.inference_uuid(run, record) == 'second'
    assert calls == [['fixture-observer'], ['fixture-observer']]
