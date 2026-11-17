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
