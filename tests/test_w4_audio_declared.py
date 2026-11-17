"""Same route inputs through the old audio body and declared operation delivery."""
import asyncio
import json
from pathlib import Path

import pytest

import proxy
from gateway import media_delivery, media_metadata
from tests import test_adapter_differential as base
from tests.support.adapter_differential_rig import GatewayRig
from tests.support import adapter_differential as wire


@pytest.mark.parametrize('case', ['success', 'delivery_failure', 'engine_failure', 'duration_default', 'normalization'])
def test_audio_old_and_declared_wire(tmp_path, case):
    traces = []
    for declared in (False, True):
        with GatewayRig('w4-audio-'+case, tmp_path / str(declared)) as rig:
            if case == 'engine_failure':
                from tests.test_adapter_differential_s7 import FAILED_HISTORY
                base._comfy(rig, base.comfy_exchanges(history=FAILED_HISTORY, strict=False))
            else:
                base._audio_comfy(rig)
            normalized = []
            if case == 'normalization':
                resolve = proxy._resolve_audio_workflow
                def with_peak(model):
                    name, config = resolve(model)
                    return name, {**config, 'normalize_peak_db': -1.0}
                rig.set(proxy, '_resolve_audio_workflow', with_peak)
                worker = proxy.private_tmp.run_worker
                async def observe_worker(function, *args, **kwargs):
                    normalized.append(function is proxy._peak_normalize_audio)
                    return await worker(function, *args, **kwargs)
                rig.set(proxy.private_tmp, 'run_worker', observe_worker)
            if case == 'delivery_failure':
                def refuse(*args, **kwargs):
                    raise media_metadata.MediaMetadataError('injected delivery rewrite failure')
                rig.set(media_delivery, 'deliver_bytes', refuse)
            if declared:
                rig.set(proxy, 'proxy_audio_generate', proxy.proxy_audio_generate_declared)
            body = {'model': 'tangoflux', 'prompt': 'a door creaks'}
            if case != 'duration_default':
                body['duration'] = 4
            answer = asyncio.run(rig.call('POST', '/v1/audio/generate', label=case, json_body=body))
            assert answer.status_code == (502 if case.endswith('failure') else 200), answer.text
            if case == 'normalization':
                assert normalized == [True]
            trace = json.loads(wire.dumps(rig.trace()))
            traces.append({key: trace[key] for key in ('caller', 'engine_requests', 'receipts')})
    expected = json.loads((Path(__file__).parent / 'fixtures/w4_audio_wire.json').read_text())[case]
    assert traces[0] == expected, wire.readable_diff(case, expected, traces[0])
    assert traces[0] == traces[1], wire.readable_diff(case, traces[0], traces[1])
