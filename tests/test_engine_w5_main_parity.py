"""Compare request bytes and compiled fragments with the main revision in the fixture README."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from starlette.requests import Request

import proxy
from gateway import service_registry
from gateway.litellm_substrate import compile_litellm_config_from_registry
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401

FIXTURES = Path(__file__).parent / 'fixtures' / 'engine_w5'
LIVE = json.loads((FIXTURES / 'live-shape.json').read_text())


@pytest.mark.parametrize('shape', ['seed', 'live'])
def test_unedited_registry_compiles_to_the_main_bytes(shape):
    registry = LIVE if shape == 'live' else service_registry.build_service_registry_seed(
        yaml.safe_load((Path(__file__).parents[1] / 'proxy_config.yml').read_text()))
    actual = (json.dumps(compile_litellm_config_from_registry(registry), indent=2) + '\n').encode()
    assert actual == (FIXTURES / f'{shape}.main-compiled.json').read_bytes()


@pytest.mark.parametrize('stream', [False, True])
def test_live_shaped_hop_keeps_main_url_headers_body_and_read_timeout(monkeypatch, stream):
    captured = []
    class Captured(BaseException): pass
    def capture(**kwargs):
        captured.append(kwargs)
        raise Captured()
    monkeypatch.setattr(proxy, 'LlmExecutionRequest', capture)
    monkeypatch.setattr(proxy, '_dispatch_admission_response', lambda *args, **kwargs: None)
    monkeypatch.setattr(proxy, 'UPSTREAM_READ_TIMEOUT', 1800.0)
    monkeypatch.setattr(proxy.ADMIN_STORE, 'get_service_registry', lambda: copy.deepcopy(LIVE))
    namespace = dict(vars(proxy))
    exec(compile((FIXTURES / 'main_hop.py').read_text(), 'main_hop.py', 'exec'), namespace)
    body = {'model': 'default', 'messages': [{'role': 'user', 'content': 'a test with é'}], 'stream': stream}
    for forward in (namespace['_forward_internal_litellm_local_upstream'], proxy._forward_internal_litellm_local_upstream):
        with pytest.raises(Captured):
            asyncio.run(forward(Request({'type': 'http', 'headers': []}), backend='kobold',
                                upstream_base='http://127.0.0.1:5051', body=copy.deepcopy(body),
                                activation=SimpleNamespace(backend_model='default'), target_model='default', lease=None))
    assert captured[0] == captured[1]
    assert captured[1]['read_timeout'] == 1800


@pytest.mark.parametrize('service_id', list(LIVE['services']))
def test_each_live_shaped_resource_probe_keeps_main_url_and_headers(monkeypatch, service_id, fake_upstreams):
    saved = LIVE['services'][service_id]
    config = service_registry.runtime_backend_from_service(saved)
    # Main's resource probe builder is unchanged. Its transport sent no headers.
    expected_url = proxy._service_resource_probe_url(config)
    from urllib.parse import urlsplit
    destination = urlsplit(expected_url)
    fake = fake_upstreams.asgi(scenario_from_data({
        'protocol': 'openai-compatible', 'name': f'legacy-health-{service_id}',
        'provenance': {'source': 'hand-written', 'reference': 'main resource probe transport, see fixture README'},
        'exchanges': [{'id': 'health', 'match': {'method': 'GET', 'path': destination.path,
                        'headers': {'authorization': {'$absent': True}}},
                       'response': {'status': 200, 'json': {'ok': True}}}],
    }), host=destination.netloc)
    monkeypatch.setenv('W5_TEST_LITELLM_KEY', 'invented-test-key')
    assert asyncio.run(proxy._probe_service_resource_fact(config)) == {'status': 'available'}
    [sent] = fake.calls('health')
    assert sent.path == destination.path
    assert sent.headers['host'] == destination.netloc
