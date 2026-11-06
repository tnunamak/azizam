"""Offline rename regressions: run with python3 -m unittest discover -s tests -p test_rename_review.py."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gateway import compat
from gateway.extensions import extract_extension_namespace
from typing import Any, Mapping, Sequence

# Extract the standalone generator without importing FastAPI or gateway stores.
_tree = ast.parse((Path(__file__).resolve().parents[1] / 'gateway/workflows_catalog.py').read_text())
_function = next(n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == '_python_example')
exec(compile(ast.Module(body=[_function], type_ignores=[]), 'workflow-example', 'exec'))

ROOT = Path(__file__).resolve().parents[1]


class RenameReviewTests(unittest.TestCase):
    def test_chat_wire_preserves_arbitrary_schemas_without_header_aliases(self):
        from gateway.backends.chat import build_chat_loopback_request
        schema = {'type': 'object', 'properties': {'vivid_fish_color': {'type': 'string'}},
                  'required': ['vivid_fish_color']}
        body = {'messages': [{'role': 'user', 'content': 'hello'}],
                'tools': [{'type': 'function', 'function': {'name': 'paint', 'parameters': schema}}],
                'response_format': {'type': 'json_schema', 'json_schema': schema},
                'metadata': {'vivid_fish_alias': False, 'azizam_alias': True}}
        result = build_chat_loopback_request(
            proxy_port=5199, body=body, internal_forward_header='x-azizam-internal-protocol-forward',
            internal_forward_secret='test', source_headers={'Authorization': 'Bearer test'},
            parent_request_id='offline', read_timeout=30,
        )
        self.assertEqual(result.body, json.dumps(body).encode('utf-8'))
        self.assertEqual(result.headers, {'content-type': 'application/json',
                                         'x-azizam-internal-protocol-forward': 'test',
                                         'authorization': 'Bearer test', 'x-azizam-request-id': 'offline'})

    def test_model_metadata_reads_only_new_owned_fields(self):
        from gateway.model_metadata import MetadataSources, annotate_entries
        old = {'id': 'old', 'vivid_fish_intent': 'image.generate'}
        new = {'id': 'new', 'azizam_intent': 'image.generate'}
        annotate_entries([old, new], MetadataSources())
        self.assertNotIn('type', old)
        self.assertEqual(new['type'], 'image')
        self.assertEqual(old, {'id': 'old', 'vivid_fish_intent': 'image.generate'})

    def test_model_list_has_no_alias_producer(self):
        tree = ast.parse((ROOT / 'proxy.py').read_text())
        route = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'list_models')
        self.assertFalse(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                             and n.func.id == 'emit_model_list_aliases' for n in ast.walk(route)))

    def test_workflow_namespace_keeps_caller_input_names(self):
        value = {'extensions': {'com.azizam.workflow': {'vivid_fish_color': 'blue'}}}
        self.assertEqual(extract_extension_namespace(value, 'com.azizam.workflow'), {'vivid_fish_color': 'blue'})
        self.assertEqual(extract_extension_namespace({'extensions': {'com.vividfish.workflow': {'metadata': 'full'}}}, 'com.azizam.workflow'), {})
        value['extensions']['com.vividfish.workflow'] = {'metadata': 'full'}
        self.assertEqual(extract_extension_namespace(value, 'com.azizam.workflow'), {'vivid_fish_color': 'blue'})
        self.assertEqual(len(value['extensions']), 2)

    def test_native_video_producer_emits_only_new_names(self):
        from gateway.video_media_jobs import video_generation_payload
        payload = video_generation_payload({'job_id': 'offline', 'status': 'queued', 'workflow': 'video.test'})
        for old, new in (('vivid_object', 'azizam_object'), ('vivid_status', 'azizam_status'),
                         ('vivid_fish_resolved_model', 'azizam_resolved_model')):
            self.assertIn(new, payload)
            self.assertNotIn(old, payload)

    def test_openapi_producers_emit_only_new_extension_keys(self):
        from gateway.discovery import WorkflowRegistry
        from gateway.workflow_openapi import augment_openapi, workflow_inputs_schema, _input_schema, _async_extension
        registry = WorkflowRegistry.from_directory(ROOT / 'manifests/workflows')
        record = registry.records()[0]
        schema = workflow_inputs_schema(record)
        self.assertEqual(schema['x-azizam-workflow-id'], record.id)
        self.assertNotIn('x-vividfish-workflow-id', schema)
        asset = _input_schema({'type': 'asset', 'enum': [{'vivid_fish_color': 'blue'}]}, form=False)
        self.assertTrue(asset['x-azizam-asset'])
        self.assertNotIn('x-vividfish-asset', asset)
        self.assertEqual(asset['enum'], [{'vivid_fish_color': 'blue'}])
        op = {}
        _async_extension(op, 'async', route='/offline')
        self.assertEqual(op, {'x-azizam-async': {'route': '/offline'}})
        spec = {'paths': {}}
        self.assertIs(augment_openapi(spec, []), spec)
        self.assertIn('x-azizam-workflows', spec)
        self.assertNotIn('x-vividfish-workflows', spec)

    def test_empty_env_uses_default(self):
        for name in ('AZIZAM_ADMIN_KEY', 'AZIZAM_API_KEY', 'AZIZAM_ADMIN_DB'):
            self.assertEqual(compat.get_env(name, 'default', environ={name: ''}), 'default')
            self.assertEqual(compat.get_env(name, environ={name: 'new'}), 'new')

    def test_registry_provider_payload_is_unchanged(self):
        from gateway.service_registry import normalize_service_registry
        payload = {'vivid_fish_alias': False, 'azizam_alias': True,
                   'schema': {'properties': {'vivid_fish_color': {'type': 'string'}}}}
        body = {'services': {'offline': {'config': {'provider_payload': payload}}}}
        result = normalize_service_registry(body)
        self.assertEqual(result['services']['offline']['config']['provider_payload'], payload)

    def test_store_env_readers(self):
        for module, name in (('metrics_store', 'METRICS'), ('responses_store', 'RESPONSES')):
            with patch.dict(os.environ, {f'AZIZAM_{name}_DB': '/tmp/offline-store.db'}):
                namespace = runpy.run_path(str(ROOT / (module + '.py')))
            self.assertEqual(namespace['DB_PATH'], Path('/tmp/offline-store.db'))

    def test_harness_env_readers(self):
        spec = importlib.util.spec_from_file_location('offline_harness_config', ROOT / 'tests/compat/harness/config.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        config = module.load_config({'AZIZAM_BASE_URL': 'http://example.invalid', 'AZIZAM_API_KEY': 'test', 'AZIZAM_TIMEOUT_S': '7'})
        self.assertEqual((config.base_url, config.api_key, config.request_timeout_s), ('http://example.invalid', 'test', 7))

    def test_python_examples_run_standalone(self):
        import requests
        class Reply:
            headers = {'content-type': 'application/json'}
            def raise_for_status(self): pass
            def json(self): return {}
        for mime in ('application/json', 'multipart/form-data'):
            example = _python_example('POST', 'http://example.invalid', content_type=mime,
                                      body={}, form={}, files=[], binary_response=False)
            self.assertNotIn('get_env', example)
            with patch.dict(os.environ, {'AZIZAM_API_KEY': 'offline'}, clear=True), patch.object(requests, 'request', return_value=Reply()) as request:
                exec(example, {})
                self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer offline')

    def test_doctor_auth_import_and_help(self):
        namespace = runpy.run_path(str(ROOT / 'scripts/doctor.py'))
        namespace['check_auth_env']()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/doctor.py'), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_smoke_direct_print_config(self):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH' and not k.startswith(('AZIZAM_', 'OPENAI_PROXY_', 'AI_GATEWAY_', 'VIVID_'))}
        with tempfile.TemporaryDirectory() as working_dir:
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/compat/smoke_protocol_frontends.py'), '--print-config'], cwd=working_dir, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
