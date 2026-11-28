"""Shared HTTP audio workflow engine for manifest and normalization tests."""
from contextlib import contextmanager
import json
from unittest.mock import patch

import httpx

from .transport import HandlerTransport


@contextmanager
def audio_engine(captured, *, content=b'RIFFfake-wave', content_type='audio/wav',
                 prompt_id='audio-prompt', filename='out.wav', discover_outputs=False):
    client = httpx.AsyncClient

    def reply(request):
        if request.method == 'POST' and request.url.path.endswith('/prompt'):
            captured['post_url'] = str(request.url)
            captured['payload'] = json.loads(request.content)
            return httpx.Response(200, json={'prompt_id': prompt_id})
        if '/history/' in request.url.path:
            if discover_outputs:
                saved = {'SaveAudio': 'audio', 'SaveAudioMP3': 'audio', 'TangoFluxVAEDecodeAndPlay': 'audios'}
                outputs = {node_id: {saved[node['class_type']]: [{'filename': filename, 'type': 'output'}]}
                           for node_id, node in captured['payload']['prompt'].items() if node.get('class_type') in saved}
            else:
                node, key = captured.get('produced', ('3', 'audios'))
                outputs = {node: {key: [{'filename': filename, 'type': 'output'}]}}
            return httpx.Response(200, json={prompt_id: {'status': {'status_str': 'success'}, 'outputs': outputs}})
        if request.url.path.endswith('/view'):
            return httpx.Response(200, content=content, headers={'content-type': content_type})
        return httpx.Response(404)

    def factory(*args, **kwargs):
        return client(*args, **{**kwargs, 'transport': HandlerTransport(reply)})

    with patch.object(httpx, 'AsyncClient', factory):
        yield captured
