import proxy


def test_decision_payload_data_and_model_transforms(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    sent = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, *, headers, json):
            sent.append(json)
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"remove_background":true}'}}]})
    target = proxy.CurrentLoadedLlmTarget(backend='test', host='http://engine.test', model='test')
    resolved = SimpleNamespace(base_transform_specs=[{'kind': 'param-map', 'from': 'chat_template_kwargs', 'to': 'controls'}], alias_overlay_specs=[])
    monkeypatch.setattr(proxy, '_first_servable_loaded_llm_target', AsyncMock(return_value=(target, None)))
    def resolve(model, *, service_id):
        assert model == 'test' and service_id == 'test'
        return resolved
    monkeypatch.setattr(proxy, '_resolve_request_target', resolve)
    monkeypatch.setattr(proxy, '_image_background_removal_enabled', lambda: True)
    monkeypatch.setattr(proxy, 'configured_downstream_headers', lambda *args: {})
    monkeypatch.setattr(proxy, '_transition_policy', lambda *args: SimpleNamespace(enabled=False))
    monkeypatch.setattr(proxy, '_require_egress_lease', lambda *args: None)
    monkeypatch.setattr(proxy, '_per_backend_transform_specs', lambda *args, **kw: [])
    monkeypatch.setattr(proxy.engine_egress, 'async_client', lambda **kw: Client())
    params = {'temperature': 0.7, 'max_tokens': 32, 'chat_template_kwargs': {'custom': True}}
    monkeypatch.setattr(proxy, '_images_group', lambda *args: {'decision_parameters': params})
    result = asyncio.run(proxy.decide_image_background_removal('portrait', auth_context=None))
    assert result.remove_background
    assert sent[0]['temperature'] == 0.7 and sent[0]['max_tokens'] == 32
    assert sent[0]['controls'] == {'custom': True}
    assert 'chat_template_kwargs' not in sent[0]
    assert params['chat_template_kwargs'] == {'custom': True}


def test_malformed_owner_transform_is_nonfatal(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    monkeypatch.setattr(proxy, '_first_servable_loaded_llm_target', AsyncMock(return_value=(
        proxy.CurrentLoadedLlmTarget(backend='chosen', host='http://engine.test', model='duplicate'), None)))
    monkeypatch.setattr(proxy, '_image_background_removal_enabled', lambda: True)
    monkeypatch.setattr(proxy, 'configured_downstream_headers', lambda *args: {})
    monkeypatch.setattr(proxy, '_transition_policy', lambda *args: SimpleNamespace(enabled=False))
    def malformed(*args, **kwargs):
        raise TypeError('malformed owner transform')
    monkeypatch.setattr(proxy, '_resolve_request_target', malformed)
    result = asyncio.run(proxy.decide_image_background_removal('portrait', auth_context=None))
    assert result.remove_background is False and result.source == 'llm_error'
