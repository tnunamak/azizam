"""Product retirement contracts; run without proxy or persistent stores."""
from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from typing import Optional

from gateway.compat import get_env
from gateway import provenance

ROOT = Path(__file__).resolve().parents[1]


class ProductCompatibilityTests(unittest.TestCase):
    def test_only_requested_environment_name_is_read(self):
        for old in ('AI_GATEWAY_API_KEY', 'OPENAI_PROXY_API_KEY', 'VIVID_API_KEY', 'AGW_API_KEY'):
            with self.subTest(old=old):
                self.assertEqual(get_env('AZIZAM_API_KEY', 'default', environ={old: 'old'}), 'default')
                self.assertEqual(get_env('AZIZAM_API_KEY', 'default', environ={'AZIZAM_API_KEY': '', old: 'old'}), 'default')
                self.assertEqual(get_env('AZIZAM_API_KEY', environ={'AZIZAM_API_KEY': 'new', old: 'old'}), 'new')

    def test_non_product_environment_is_unchanged(self):
        self.assertEqual(get_env('OTHER', environ={'OTHER': 'value'}), 'value')
        self.assertEqual(get_env('OTHER', 'default', environ={}), 'default')
        self.assertEqual(get_env('OTHER', 'default', environ={'OTHER': ''}), 'default')

    def test_only_new_workflow_namespace_is_read(self):
        self.assertEqual(provenance.requested_metadata_mode({'extensions': {'com.azizam.workflow': {'metadata': 'full'}}}), 'full')
        self.assertIsNone(provenance.requested_metadata_mode({'extensions': {'com.vividfish.workflow': {'metadata': 'full'}}}))
        value = {'extensions': {'com.azizam.workflow': {'metadata': 'none'}, 'com.vividfish.workflow': {'metadata': 'full'}}}
        self.assertEqual(provenance.requested_metadata_mode(value), 'none')
        self.assertEqual(len(value['extensions']), 2)

    def test_provenance_reads_old_and_new_permanently(self):
        for key in ('azizam_provenance', 'vividfish_provenance'):
            self.assertEqual(provenance.read_provenance_value({key: 'record'}), 'record')
            self.assertEqual(provenance.read_provenance_lines(key + ':record', ':'), 'record')
            self.assertEqual(provenance.read_provenance_lines(key + '=record', '='), 'record')
        self.assertEqual(provenance.read_provenance_value({'azizam_provenance': 'new', 'vividfish_provenance': 'old'}), 'new')
        self.assertEqual(provenance.PROVENANCE_KEY, 'azizam_provenance')

    def test_png_reader_accepts_both_embedded_keys(self):
        from gateway import media_metadata
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5XQAAAAASUVORK5CYII=')
        record = {'schema': 'vividfish.provenance/1', 'generator': 'azizam-gateway'}
        for key in provenance.PROVENANCE_KEYS:
            content = media_metadata.embed_png_text(png, {key: json.dumps(record)})
            raw = provenance.read_provenance_value(media_metadata.png_text_chunks(content))
            self.assertEqual(json.loads(raw), record)
            self.assertEqual(provenance.sanitize_provenance(record), record)

    def test_product_alias_helpers_and_middleware_are_removed(self):
        from gateway import compat
        self.assertEqual([name for name in vars(compat) if name.startswith(('emit_', 'accept_', 'canonical_'))], [])
        self.assertFalse(hasattr(compat, 'ProductCompatibilityMiddleware'))
        tree = ast.parse((ROOT / 'proxy.py').read_text())
        self.assertFalse(any(isinstance(n, ast.Name) and n.id == 'ProductCompatibilityMiddleware' for n in ast.walk(tree)))

    def test_discovery_only_registers_new_product_route(self):
        tree = ast.parse((ROOT / 'proxy.py').read_text())
        routes = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                        routes.add(dec.args[0].value)
        self.assertIn('/.well-known/azizam', routes)
        self.assertNotIn('/.well-known/openai-proxy', routes)
        self.assertNotIn('/.well-known/ai-gateway', routes)
        skill = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'well_known_skill_file')
        self.assertFalse(any(isinstance(n, ast.Constant) and n.value == 'openai-proxy-gateway' for n in ast.walk(skill)))

    def test_safe_restart_reads_only_new_config_directory(self):
        ns = runpy.run_path(str(ROOT / 'scripts/safe-restart'))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            old = home / '.config/openai-proxy/admin-key'
            new = home / '.config/azizam/admin-key'
            old.parent.mkdir(parents=True)
            old.write_text('old')
            self.assertIsNone(ns['resolve_token']({'HOME': directory}))
            new.parent.mkdir(parents=True)
            new.write_text('new')
            self.assertEqual(ns['resolve_token']({'HOME': directory}), 'new')
            self.assertEqual(ns['resolve_token']({'HOME': directory, 'VIVID_ADMIN_API_KEY': 'legacy'}), 'new')
            self.assertEqual(ns['resolve_token']({'HOME': directory, 'AZIZAM_ADMIN_KEY': 'canonical'}), 'canonical')

    def test_status_restart_counter_uses_only_new_unit(self):
        tree = ast.parse((ROOT / 'status_endpoint.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_proxy_restart_count')
        calls = []
        result = 'LoadState=loaded\nNRestarts=7'
        def run(command, **kwargs):
            calls.append(command)
            return 0, result, ''
        ns = {'_run': run, 'Optional': Optional}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'status-counter', 'exec'), ns)
        self.assertEqual(ns['_proxy_restart_count'](), 7)
        result = 'LoadState=not-found'
        self.assertIsNone(ns['_proxy_restart_count']())
        self.assertEqual([c[3] for c in calls], ['azizam.service', 'azizam.service'])


if __name__ == '__main__':
    unittest.main()
