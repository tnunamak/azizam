"""Frozen pre-fix graphs from real image handlers, with offline upload/round fakes.

The baseline was captured at 68919a1e before changing prompt binding. Every
image.generate/image.edit manifest is included, including gateway compositions.
Only the FIBO graph may differ, and its exact permitted difference is asserted.
"""
import asyncio
import copy
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import proxy
import pytest
from fastapi import FastAPI
from gateway import identity_api
from gateway.manifests import load_workflow_manifest
from test_identity_api import FakeClient, GOOD, PHOTO
from test_image_route_receipts import _MultipartRequest, _MultipartUpload
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'tests/fixtures/image_manifest_prompt_baseline.json'
MANIFESTS = [
    (path, load_workflow_manifest(path))
    for path in sorted((ROOT / 'manifests/workflows').glob('*.yml'))
    if load_workflow_manifest(path)['intent'] in ('image.generate', 'image.edit')
]
PROMPT = 'Change the jacket to bright green'


@pytest.fixture
def image_round(monkeypatch, fake_upstreams):
    fake = fake_upstreams.asgi(load_scenario('comfyui/upload-ok-any'))
    for name, value in {
        'COMFY_HOST': fake.base_url, 'COMFY_ENABLED': True,
        '_select_edit_workflow': lambda model, **kwargs: model,
        '_dispatch_admission_response': Mock(return_value=None),
        '_service_availability_response': Mock(return_value=None),
        'configured_downstream_headers': Mock(return_value={}),
        'resolve_model_for_operation': Mock(return_value=SimpleNamespace(is_openai_alias=False)),
        '_workflow_target_policy_decision': Mock(return_value=None),
        'resolve_image_background_decision': AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False)),
        'get_vram_estimate': Mock(return_value=100),
        'graph_free_vram_bytes': AsyncMock(return_value=10**12),
        'compute_max_batch': Mock(return_value=1),
    }.items():
        monkeypatch.setattr(proxy, name, value)
    monkeypatch.setattr(proxy.media_delivery, 'current', lambda: None)
    monkeypatch.setattr(proxy.random, 'randint', lambda *args: 41)
    monkeypatch.setattr(proxy, '_build_image_response_payload',
                        lambda *args, **kwargs: proxy.Response('{}', media_type='application/json'))
    graphs = []

    async def run(client, workflow, *args, **kwargs):
        graphs.append(copy.deepcopy(workflow))
        return None, [GOOD]

    monkeypatch.setattr(proxy, '_graph_run_round', run)
    # Keep the identity pipeline real, substituting only face analysis and output storage.
    identity_app = FastAPI()
    graph, binding = proxy._identity_template()
    identity_api.register_identity_routes(identity_app, identity_api.IdentityApiDeps(
        authenticate=lambda *args: proxy.NO_AUTH_CONTEXT,
        enforce_policy=lambda *args: None,
        ensure_face_service=AsyncMock(return_value=None),
        face_client=lambda: FakeClient(),
        run_workflow=proxy._identity_run_workflow,
        template=lambda: (copy.deepcopy(graph), binding),
        store_image=lambda *args: {'b64_json': ''},
        models=('buffalo_l',),
    ))
    monkeypatch.setattr(proxy.app.state, 'identity_run_scene', identity_app.state.identity_run_scene)
    return graphs


def run_image(manifest):
    binding = manifest['bindings'].get('comfyui') or manifest['bindings']['gateway']
    model = binding.get('model_id') or Path(binding['workflow']).stem
    options = {'seed': 41} if 'seed' in manifest['inputs'] else {}
    if model == 'image-identity':
        options.update(attempts=1, refine='off', resolution=1024)
    fields = dict(model=model, prompt=PROMPT, size='1024x1024', n=1,
                  background='opaque', extensions={'com.azizam.workflow': options})
    if manifest['intent'] == 'image.generate':
        request = SimpleNamespace(json=AsyncMock(return_value=fields))
        return asyncio.run(proxy.proxy_graph_image_request(request, proxy.NO_AUTH_CONTEXT))
    items = [(key, json.dumps(value) if isinstance(value, dict) else str(value))
             for key, value in fields.items()]
    items.append(('image', _MultipartUpload('source.png', PHOTO)))
    if model == 'qwen-image-2.1-edit-multiref':
        items.append(('image[]', _MultipartUpload('second.png', PHOTO)))
    if (manifest.get('inputs', {}).get('mask') or {}).get('required'):
        from PIL import Image
        import io
        mask = io.BytesIO()
        Image.new('RGBA', (1280, 960), (0, 0, 0, 0)).save(mask, format='PNG')
        items.append(('mask', _MultipartUpload('mask.png', mask.getvalue())))
    return asyncio.run(proxy.proxy_graph_image_edit_request(_MultipartRequest(items), proxy.NO_AUTH_CONTEXT))


@pytest.mark.parametrize('path,manifest', MANIFESTS, ids=[p.stem for p, _ in MANIFESTS])
def test_every_image_manifest_matches_pre_fix_graph_bytes(path, manifest, image_round):
    response = run_image(manifest)
    assert response.status_code == 200, response.body
    assert image_round, path
    expected = json.loads(BASELINE.read_text())
    assert set(expected) == {p.name for p, _ in MANIFESTS}
    comparable = copy.deepcopy(image_round)
    if path.stem == 'fibo-2-edit-turbo':
        for graph in comparable:
            assert graph['6']['inputs']['prompt'] == PROMPT
            assert 'text' not in graph['6']['inputs']
            # Reverse exactly the intended correction before comparing every byte.
            graph['6']['inputs']['prompt'] = 'edit the image'
            graph['6']['inputs']['text'] = PROMPT
    serialized = json.dumps(comparable, separators=(',', ':'), ensure_ascii=True).encode()
    assert hashlib.sha256(serialized).hexdigest() == expected[path.name]


@pytest.mark.parametrize('intent', ['image.generate', 'image.edit'])
@pytest.mark.parametrize('mapping', ['multiple', 'absent', 'empty'])
def test_prompt_targets_and_legacy_fallback(intent, mapping, image_round, monkeypatch):
    manifest = copy.deepcopy(next(m for p, m in MANIFESTS if p.stem == 'fibo-2-edit-turbo'))
    workflow = json.loads((ROOT / manifest['bindings']['comfyui']['workflow']).read_text())
    manifest['intent'] = intent
    manifest['id'] = intent + '.fixture-prompt'
    workflow['legacy'] = {'class_type': 'Fixture', 'inputs': {'text': 'legacy default'}}
    workflow['_meta']['prompt_node'] = 'legacy'
    workflow['second'] = {'class_type': 'Fixture', 'inputs': {'instruction': 'second default'}}
    mapping_data = manifest['bindings']['comfyui']['map']
    if mapping == 'multiple':
        mapping_data['prompt'].append({'node': 'second', 'input': 'instruction'})
    elif mapping == 'empty':
        mapping_data['prompt'] = []
    else:
        del mapping_data['prompt']
    monkeypatch.setattr(proxy, 'load_graph_workflow', lambda model: copy.deepcopy(workflow))
    monkeypatch.setattr(proxy, '_find_image_manifest', lambda *args: manifest)
    response = run_image(manifest)
    assert response.status_code == 200, response.body
    assert len(image_round) == 1
    inputs = image_round[0]
    if mapping == 'multiple':
        assert inputs['6']['inputs']['prompt'] == PROMPT
        assert 'text' not in inputs['6']['inputs']
        assert inputs['second']['inputs']['instruction'] == PROMPT
        assert inputs['legacy']['inputs']['text'] == 'legacy default'
    else:
        assert inputs['legacy']['inputs']['text'] == PROMPT
        assert inputs['6']['inputs']['prompt'] == 'edit the image'
