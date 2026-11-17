"""Remote authority regressions from the independent security review."""
import asyncio

import pytest

from gateway.engine_protocol.errors import ProtocolError
from tests.support.remote_protocol import SocketRecorder, case


def test_remote_legacy_switch_refuses_before_client_or_credential(monkeypatch, caplog):
    from gateway.engine_control import EngineControlRuntime
    from gateway.swap_drain import SwapDrain
    from gateway import engine_control

    registry, _, _, _, _, _ = case()
    service = registry['services']['remote-box']
    service['protocol_operations'] = {'select': {'method': 'POST', 'path': '/select'}}
    selection = {'mode': 'switch', 'switch': {'call': 'select', 'args': {}, 'observe_selected': 'chosen'}}
    service['config']['offerings'][0]['selection'] = selection
    service['offerings'] = service['config']['offerings']
    recorder = SocketRecorder(monkeypatch)
    attempts = []
    def unbound(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('unbound client or credential lookup')
    monkeypatch.setattr(engine_control, 'credential_headers', unbound)
    runtime = EngineControlRuntime(service, drain=SwapDrain(), lock_for=lambda _: asyncio.Lock(), client_factory=unbound)
    async def check():
        for call in (lambda: runtime.invoke('select', {}), lambda: runtime.select('remote-model', selection), runtime.load):
            with pytest.raises(ProtocolError) as error:
                await call()
            assert error.value.code == 'protocol_dispatch_refused'
    asyncio.run(check())
    assert not attempts and not recorder.connections and not recorder.writes
    assert 'synthetic-control-review' not in caplog.text


@pytest.mark.parametrize('proxy_env', [False, True])
def test_remote_legacy_health_refuses_before_client_or_credential(monkeypatch, proxy_env):
    from gateway import lifecycle_drivers as drivers
    from gateway.engine_observations import ObservationReader
    registry, _, _, _, _, _ = case()
    service = registry['services']['remote-box']
    service['health_endpoint'] = '/health'
    recorder = SocketRecorder(monkeypatch)
    if proxy_env:
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            monkeypatch.setenv(name, 'http://proxy.invalid:8888')
    def unbound(*args, **kwargs):
        raise AssertionError('unbound health client or credential lookup')
    monkeypatch.setattr(drivers.engine_egress, 'async_client', unbound)
    monkeypatch.setattr(drivers, '_health_request', unbound)
    async def check():
        with pytest.raises(ProtocolError):
            await drivers._poll_health(service, .1)
        with pytest.raises(ProtocolError):
            await drivers.ExternalDriver().ensure_ready(service, .1)
        with pytest.raises(ProtocolError):
            await ObservationReader(client_factory=unbound).read(service, 'process_ready')
        with pytest.raises(ProtocolError):
            drivers.driver_for_service('remote-box', {'remote-box': service})
    asyncio.run(check())
    assert not recorder.connections and not recorder.writes
