"""Offline rename regressions: run with python3 -m unittest discover -s tests -p test_rename_review.py."""
import asyncio
import ast
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest.mock import patch

from gateway import compat
from gateway.extensions import extract_extension_namespace
from typing import Any, Mapping

# Extract the standalone generator without importing FastAPI or gateway stores.
_tree = ast.parse((Path(__file__).resolve().parents[1] / 'gateway/workflows_catalog.py').read_text())
_function = next(n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == '_python_example')
exec(compile(ast.Module(body=[_function], type_ignores=[]), 'workflow-example', 'exec'))

ROOT = Path(__file__).resolve().parents[1]


class RenameReviewTests(unittest.TestCase):
    def _chat_wire(self, *, check_request, mime):
        schema = {'type': 'object', 'properties': {'vivid_fish_color': {'type': 'string'}},
                  'required': ['vivid_fish_color']}
        body = json.dumps({'messages': [{'role': 'user', 'content': 'hello'}],
                           'tools': [{'type': 'function', 'function': {'name': 'paint', 'parameters': schema}}],
                           'response_format': {'type': 'json_schema', 'json_schema': schema},
                           'metadata': {'vivid_fish_alias': False, 'azizam_alias': True}}).encode()
        async def exercise():
            sent = []
            async def receive():
                return {'type': 'http.request', 'body': body, 'more_body': False}
            async def send(message):
                sent.append(message)
            async def app(scope, receive, send):
                if check_request:
                    self.assertEqual((await receive())['body'], body)
                await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', mime)]})
                output = body if mime == b'application/json' else b'data: ' + body + b'\n\n'
                await send({'type': 'http.response.body', 'body': output[:19], 'more_body': True})
                await send({'type': 'http.response.body', 'body': output[19:], 'more_body': False})
                return output
            expected = await compat.ProductCompatibilityMiddleware(app)(
                {'type': 'http', 'path': '/v1/chat/completions', 'headers': [(b'content-type', b'application/json')]}, receive, send)
            self.assertEqual(b''.join(m.get('body', b'') for m in sent), expected)
        asyncio.run(exercise())

    def test_chat_request_schema_is_byte_identical(self):
        self._chat_wire(check_request=True, mime=b'application/json')

    def test_chat_response_schema_is_byte_identical(self):
        self._chat_wire(check_request=False, mime=b'application/json')

    def test_chat_sse_schema_is_byte_identical(self):
        self._chat_wire(check_request=False, mime=b'text/event-stream')

    def test_collision_preserves_both(self):
        arbitrary = {'vivid_fish_count': 1, 'azizam_count': 2}
        self.assertEqual(compat.emit_aliases(arbitrary), arbitrary)
        value = {'vivid_fish_alias': False, 'azizam_alias': True}
        self.assertEqual(compat.emit_aliases(value), value)
        self.assertTrue(compat.read_field(value, 'azizam_alias'))

    def test_nested_values_are_never_visited(self):
        value = {'azizam_alias': True}
        for _ in range(12):
            value = {'child': value}
        with patch.object(compat, 'emit_aliases', wraps=compat.emit_aliases) as emit:
            output = emit(value)
            self.assertEqual(emit.call_count, 1)
        self.assertIs(output['child'], value['child'])
        with patch.object(compat, 'accept_aliases', wraps=compat.accept_aliases) as accept:
            accept(value)
            self.assertEqual(accept.call_count, 1)

    def test_model_list_callsite_emits_gateway_aliases(self):
        tree = ast.parse((ROOT / 'proxy.py').read_text())
        route = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'list_models')
        self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                            and n.func.id == 'emit_model_list_aliases' for n in ast.walk(route)))
        entry = {'azizam_alias': True, 'metadata': {'vivid_fish_alias': False}}
        output = compat.emit_model_list_aliases({'data': [entry], 'azizam_model_access': 'current_loaded'})
        self.assertTrue(output['data'][0]['vivid_fish_alias'])
        self.assertEqual(output['vivid_fish_model_access'], 'current_loaded')
        self.assertIs(output['data'][0]['metadata'], entry['metadata'])

    def test_workflow_namespace_keeps_caller_input_names(self):
        for name in ('com.vividfish.workflow', 'com.azizam.workflow'):
            value = {'extensions': {name: {'vivid_fish_color': 'blue'}}}
            self.assertEqual(extract_extension_namespace(value, 'com.azizam.workflow'), {'vivid_fish_color': 'blue'})
        value = {'extensions': {'com.azizam.workflow': {'metadata': 'none'},
                                'com.vividfish.workflow': {'metadata': 'full'}}}
        self.assertEqual(extract_extension_namespace(value, 'com.azizam.workflow'), {'metadata': 'none'})
        self.assertEqual(len(value['extensions']), 2)

    def test_native_video_producer_emits_aliases(self):
        from gateway.video_media_jobs import video_generation_payload
        payload = video_generation_payload({'job_id': 'offline', 'status': 'queued', 'workflow': 'video.test'})
        for old, new in (('vivid_object', 'azizam_object'), ('vivid_status', 'azizam_status'),
                         ('vivid_fish_resolved_model', 'azizam_resolved_model')):
            self.assertEqual(payload[old], payload[new])

    def test_openapi_producers_emit_shallow_aliases_in_place(self):
        from gateway.discovery import WorkflowRegistry
        from gateway.workflow_openapi import augment_openapi, workflow_inputs_schema, _input_schema, _async_extension
        registry = WorkflowRegistry.from_directory(ROOT / 'manifests/workflows')
        record = registry.records()[0]
        schema = workflow_inputs_schema(record)
        self.assertEqual(schema['x-vividfish-workflow-id'], schema['x-azizam-workflow-id'])
        asset = _input_schema({'type': 'asset', 'enum': [{'vivid_fish_color': 'blue'}]}, form=False)
        self.assertEqual(asset['x-vividfish-asset'], asset['x-azizam-asset'])
        self.assertEqual(asset['enum'], [{'vivid_fish_color': 'blue'}])
        op = {}
        _async_extension(op, 'async', route='/offline')
        self.assertEqual(op['x-vividfish-async'], op['x-azizam-async'])
        spec = {'paths': {}}
        self.assertIs(augment_openapi(spec, []), spec)
        self.assertEqual(spec['x-vividfish-workflows'], spec['x-azizam-workflows'])

    def test_empty_env_falls_through(self):
        for name in ('AZIZAM_ADMIN_KEY', 'AZIZAM_API_KEY', 'AZIZAM_ADMIN_DB'):
            old = compat.ENV_ALIASES[name]
            self.assertEqual(compat.get_env(name, environ={name: '', old[0]: 'fallback'}), 'fallback')
            self.assertEqual(compat.get_env(name, environ={name: 'new', old[0]: 'old'}), 'new')
        self.assertEqual(compat.get_env('AZIZAM_ADMIN_KEY', environ={'AI_GATEWAY_ADMIN_KEY': '', 'VIVID_ADMIN_API_KEY': 'legacy'}), 'legacy')

    def test_registry_provider_payload_is_unchanged(self):
        from gateway.service_registry import normalize_service_registry
        payload = {'vivid_fish_alias': False, 'azizam_alias': True,
                   'schema': {'properties': {'vivid_fish_color': {'type': 'string'}}}}
        body = {'services': {'offline': {'config': {'provider_payload': payload}}}}
        result = normalize_service_registry(body)
        self.assertEqual(result['services']['offline']['config']['provider_payload'], payload)

    def test_store_env_readers(self):
        for module, name in (('metrics_store', 'METRICS'), ('responses_store', 'RESPONSES')):
            with patch.dict(os.environ, {f'AZIZAM_{name}_DB': '', f'AI_GATEWAY_{name}_DB': '/tmp/offline-store.db'}):
                namespace = runpy.run_path(str(ROOT / (module + '.py')))
            self.assertEqual(namespace['DB_PATH'], Path('/tmp/offline-store.db'))

    def test_harness_env_readers(self):
        spec = importlib.util.spec_from_file_location('offline_harness_config', ROOT / 'tests/compat/harness/config.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        config = module.load_config({'OPENAI_PROXY_BASE_URL': 'http://example.invalid', 'OPENAI_PROXY_API_KEY': 'test', 'OPENAI_PROXY_TIMEOUT_S': '7'})
        self.assertEqual((config.base_url, config.api_key, config.request_timeout_s), ('http://example.invalid', 'test', 7))

    def test_python_examples_run_standalone(self):
        import requests
        class Reply:
            headers = {'content-type': 'application/json'}
            def raise_for_status(self): pass
            def json(self): return {}
        for mime in ('application/json', 'multipart/form-data'):
            example = _python_example('POST', 'http://example.invalid', content_type=mime,
                                      body={}, form={}, files={}, binary_response=False)
            self.assertNotIn('get_env', example)
            with patch.dict(os.environ, {'AZIZAM_API_KEY': '', 'AI_GATEWAY_API_KEY': '', 'OPENAI_PROXY_API_KEY': 'offline'}, clear=True), patch.object(requests, 'request', return_value=Reply()) as request:
                exec(example, {})
                self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer offline')

    def test_doctor_auth_import_and_help(self):
        namespace = runpy.run_path(str(ROOT / 'scripts/doctor.py'))
        namespace['check_auth_env']()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/doctor.py'), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_smoke_direct_print_config(self):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH' and not k.startswith(('AZIZAM_', 'OPENAI_PROXY_', 'AI_GATEWAY_', 'VIVID_'))}
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/compat/smoke_protocol_frontends.py'), '--print-config'], cwd='/tmp', env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
