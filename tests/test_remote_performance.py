"""Real SQLite change detection and bounded worker costs at engine I/O."""
import asyncio
import sqlite3

import httpx
import pytest

from gateway import admin_store, engine_egress
from gateway.engine_protocol.errors import ProtocolError
from tests.support.remote_protocol import case


@pytest.mark.parametrize('failed_read', [False, True])
@pytest.mark.parametrize('remote', [False, True])
def test_legacy_unchanged_registry_needs_no_worker_and_retains_denials(tmp_path, monkeypatch, failed_read, remote):
    path = tmp_path / 'registry.db'
    admin_store.init_db(path)
    registry, _, _, _, _, _ = case()
    monkeypatch.setattr(engine_egress, '_live', engine_egress._live)
    engine_egress.install(engine_egress.project(registry['services'] if remote else {}),
                          registry_path=path, registry_generation=admin_store.service_registry_generation(path=path))
    reader = engine_egress._live.registry_reader
    connection = reader._connection
    queries, sends = [], []
    connection.set_trace_callback(queries.append)
    if failed_read:
        def fail():
            raise sqlite3.OperationalError('synthetic generation read failure')
        monkeypatch.setattr(reader, 'version', fail)
    def worker(*args, **kwargs):
        pytest.fail('unchanged or unreadable legacy registry submitted a worker')
    monkeypatch.setattr(asyncio, 'to_thread', worker)
    def send(request):
        sends.append(request.url)
        return httpx.Response(200, content=b'local result')
    async def run():
        async with engine_egress.async_client(transport=httpx.MockTransport(send)) as client:
            if remote:
                with pytest.raises(ProtocolError):
                    await client.get('https://engine.example/health')
            else:
                for _ in range(3):
                    assert (await client.get('http://localhost:59124/health')).content == b'local result'
    try:
        asyncio.run(run())
        assert len(sends) == (0 if remote else 3)
        assert reader._connection is connection
        assert all(query == 'PRAGMA data_version' for query in queries)
    finally:
        connection.set_trace_callback(None)
