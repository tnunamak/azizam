from pathlib import Path
import yaml
from gateway import capability_ids


def test_route_controls_inner_hop_without_code(tmp_path):
    path = tmp_path / 'frontends.yml'
    path.write_text(yaml.safe_dump({'records': [{'registered_routes': [
        {'canonical': '/v1/new', 'inner_hop': {'endpoint': '/new', 'capability': 'new', 'intent': 'new.run', 'resource_class': 'batch'}}]}]}))
    endpoints, admission = capability_ids.load_inner_hop_routes(path)
    assert endpoints == {'new': ('/new',)}
    assert admission == {'/new': ('new.run', 'batch')}


def test_inner_hop_shipped_parity():
    assert capability_ids.INNER_HOP_ENDPOINTS == {'chat': ('/chat/completions', '/completions'), 'embed': ('/embeddings',)}
    assert capability_ids.INNER_HOP_ADMISSION == {'/chat/completions': ('chat.generate', 'chat'), '/completions': ('chat.generate', 'chat'), '/embeddings': ('text.embeddings', 'embeddings')}


def test_paid_route_policy_matrix_and_receipt_identity(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from gateway import provider_execution, route_registry
    import test_provider_execution as oracle
    async def run():
        harness = oracle.ProviderExecutionTests()
        harness.setUp()
        try:
            for allowed in (True, False):
                facts = {'canonical': '/v1/chat/completions',
                         'azizam_policy': {'protocol': 'openai.chat', 'intent': 'custom.allowed' if allowed else 'custom.denied'}}
                monkeypatch.setattr(route_registry, 'registered_route_records', lambda **kw: [facts])
                seen = []
                def decide(identity, request, decision):
                    seen.append((request.protocol, request.intent))
                    return SimpleNamespace(denied=not allowed, reasons=('denied by test policy',))
                monkeypatch.setattr(provider_execution.category_enforcement, 'apply', decide)
                response, send = await harness._execute()
                assert response.status_code == (200 if allowed else 403)
                assert send.await_count == int(allowed)
                expected = ('openai.chat', facts['azizam_policy']['intent'])
                assert seen == [expected]
                receipt = harness.receipts[-1]
                assert (receipt.protocol, receipt.intent) == expected
        finally:
            harness.tearDown()
    asyncio.run(run())
