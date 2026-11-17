#!/usr/bin/env python3
"""Static modality inventory. Reports evidence, not dynamic reachability proof.

Scans handlers and shape methods without importing the gateway or touching stores.
Attribute calls and registry-selected runners require the explicit seams below.
"""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'proxy.py', *sorted((ROOT / 'gateway/frontend_shapes').glob('*.py')),
         ROOT / 'gateway/multipart_adapter.py', ROOT / 'gateway/node_graph_runner.py']
SEAMS = {
    '/v1/audio/speech': ('proxy.py:audio_speech', 'inline_tts.invoke; optional dispatch.Runtime.invoke', 'No: stream/raw relay and voice/fallback policy', 'test_adapter_differential_speech.py'),
    '/v1/audio/transcriptions': ('proxy.py:audio_transcriptions', 'chunked_stt.invoke', 'No: chunking, stream/deadline/cleanup ownership', 'test_adapter_differential_speech.py'),
    '/v1/audio/translations': ('proxy.py:audio_translations', 'chunked_stt.invoke', 'No: same STT lifecycle', 'test_adapter_differential_speech.py'),
    '/v1/images/generations': ('proxy.py:proxy_graph_image_request', 'NodeGraphRunner._rounds; optional _protocol_image_result', 'Partial: adaptive rounds, postprocessing and legacy response ownership', 'test_adapter_differential.py'),
    '/v1/images/edits': ('proxy.py:proxy_graph_image_edit_request', 'NodeGraphRunner._rounds', 'Partial: masks, upload/composite and postprocessing', 'test_adapter_differential_s7.py'),
    '/v1/images/variations': ('proxy.py:proxy_graph_image_variations_request', 'NodeGraphRunner._rounds', 'Partial: fixed prompt/denoise and postprocessing', 'test_adapter_differential_s7.py'),
    '/gateway/v1/media/jobs/image-generations': ('proxy.py:_run_image_media_job', 'workflow core / NodeGraphRunner._rounds', 'Partial: persisted request, retry and receipt lifecycle', 'test_adapter_differential_s7.py'),
    '/v1/audio/generate': ('proxy.py:proxy_audio_generate', 'NodeGraphRunner._rounds / legacy submit-history-view loop', 'Partial: normalize/strip/store and metering', 'test_adapter_differential.py'),
    '/gateway/v1/media/jobs': ('proxy.py:_dispatch_gateway_media_job_submission', 'modality workers OR declared_operation_job.run', 'Partial: shipped intents explicitly excluded by declared resolve', 'test_adapter_differential_s7.py'),
    '/v1/videos': ('proxy.py:proxy_graph_video_create', 'video create hand-off / NodeGraphRunner', 'Excluded: video create owner', 'test_adapter_differential_s7.py'),
    '/v1/videos/{video_id}/remix': ('proxy.py:proxy_graph_video_remix', 'video source / NodeGraphRunner', 'Partial: source references, video tracking', 'test_adapter_differential_s7.py'),
    '/v1/videos/edits': ('proxy.py:proxy_graph_video_edit', 'video source / NodeGraphRunner', 'Partial: source references, video tracking', 'test_adapter_differential_s7.py'),
    '/v1/videos/extensions': ('proxy.py:proxy_graph_video_extend', 'chain executor', 'No: segment chaining and progress/tracking', 'test_adapter_differential_s7.py'),
    '/v1/3d/generations': ('gateway/multipart_adapter.py:MultipartRunner.run', 'exclusive multipart runner', 'Yes: immediate typed bundle; preserve pool lease and Library provenance', 'test_adapter_differential_s7_mesh_rows.py'),
    '/v1/3d/generations/glb': ('gateway/multipart_adapter.py:MultipartRunner.run', 'exclusive multipart runner', 'Yes: same run, raw response shape', 'test_adapter_differential_s7_mesh_rows.py'),
}

def inventory():
    symbols = {}
    for path in FILES:
        tree = ast.parse(path.read_text())
        relative = str(path.relative_to(ROOT))
        def visit(nodes, prefix=''):
            for node in nodes:
                if isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + '.')
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    calls = sorted({ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)})
                    symbols[f'{relative}:{prefix}{node.name}'] = {'line': node.lineno, 'calls': calls}
        visit(tree.body)
    rows = []
    for route, (old, dispatch, ready, parity) in SEAMS.items():
        evidence = symbols[old]
        calls = evidence['calls']
        effects = [c for c in calls if any(word in c.lower() for word in
                   ('receipt', 'store', 'status', 'event', 'lease', 'metadata', 'policy', 'admission', 'meter', 'persist', 'delivery', 'strip', 'normalize'))]
        rows.append({'route': route, 'old_path': old, 'dispatch': dispatch, 'generic_ready': ready,
                     'existing_wire_test': parity, 'side_effect_calls': effects, 'line': evidence['line']})
    return {'routes': rows, 'symbols': symbols}

if __name__ == '__main__':
    print(json.dumps(inventory(), indent=2))
