from engines import pixal3d_comfy_service as sidecar


def test_data_only_bindings():
    bindings = {'image': {'node': 'a', 'input': 'picture'},
                'resolution': {'node': 'b', 'input': 'size'},
                'output': {'node': 'c', 'input': 'prefix'}, 'resolutions': [64, 128],
                'seed': {'class_type': 'Sampler', 'input': 'noise'}}
    graph = {n: {'inputs': {}} for n in ('a', 'b', 'c')}
    graph['d'] = {'class_type': 'Sampler', 'inputs': {}}
    patched = sidecar.patch_prompt(graph, image_name='x', seed=3, resolution=64, prefix='y', bindings=bindings)
    assert patched['a']['inputs'] == {'picture': 'x'}
    assert patched['b']['inputs'] == {'size': 64}
    assert patched['c']['inputs'] == {'prefix': 'y'}
    assert patched['d']['inputs'] == {'noise': 3}
    assert sidecar.find_glb({'c': {'files': [{'filename': 'textured.glb'}]}}, bindings=bindings)['filename'] == 'textured.glb'


def test_resolution_choices_drive_http_admission(monkeypatch):
    import asyncio
    import httpx
    from unittest.mock import AsyncMock
    monkeypatch.setattr(sidecar.Pixal3DService, 'generate', AsyncMock(return_value=b'glb'))
    bindings = {**sidecar.BINDINGS, 'resolutions': [64]}
    app = sidecar.create_app('http://engine.test', prompt={}, bindings=bindings)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            allowed = await client.post('/generate', files={'image': ('x.png', b'picture')}, data={'resolution': '64'})
            denied = await client.post('/generate', files={'image': ('x.png', b'picture')}, data={'resolution': '512'})
        assert allowed.status_code == 200 and allowed.content == b'glb'
        assert denied.status_code == 422
        assert denied.json() == {'error': 'resolution must be one of [64]'}
    asyncio.run(run())
