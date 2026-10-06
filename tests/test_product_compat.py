"""Rename contracts; can run directly without importing proxy or persistent stores."""
from __future__ import annotations

import asyncio
import ast
import importlib.util
import json
from pathlib import Path
import runpy
import tempfile
import subprocess
import base64
import unittest

from gateway.compat import (
    ENV_ALIASES, HEADER_ALIASES, ProductCompatibilityMiddleware, accept_aliases, emit_aliases,
    get_env, header_names, normalize_headers, emit_headers, emit_header_mapping,
)
from gateway import provenance

ROOT = Path(__file__).resolve().parents[1]


class ProductCompatibilityTests(unittest.TestCase):
    def test_every_old_env_is_read_with_new_precedence(self):
        for new, old_names in ENV_ALIASES.items():
            for old in old_names:
                with self.subTest(new=new, old=old):
                    self.assertEqual(get_env(new, environ={old: "old"}), "old")
                    self.assertEqual(get_env(old, environ={new: "new", old: "old"}), "new")
                    self.assertEqual(get_env(new, environ={new: "", old: "old"}), "")
                    self.assertEqual(get_env(new, "default", environ={}), "default")

    def test_all_fallbacks_follow_declared_order(self):
        for new, old in ENV_ALIASES.items():
            self.assertEqual(get_env(new, environ={name: name for name in old}), old[0])

    def test_non_product_environment_is_unchanged(self):
        self.assertEqual(get_env("OTHER", environ={"OTHER": "value"}), "value")
        self.assertEqual(get_env("OTHER", "default", environ={}), "default")

    def test_response_fields_have_identical_aliases(self):
        value = {"data": [{"azizam_alias": True, "azizam_resolves_to": {"chat": "m"}}],
                 "azizam_object": "video", "azizam_status": "running"}
        out = emit_aliases(value)
        self.assertEqual(out["data"][0]["azizam_resolves_to"], out["data"][0]["vivid_fish_resolves_to"])
        self.assertEqual(out["azizam_object"], out["vivid_object"])
        self.assertEqual(out["azizam_status"], out["vivid_status"])
        self.assertNotIn("vivid_fish_alias", value["data"][0])

    def test_old_response_fields_are_accepted_on_input(self):
        self.assertEqual(accept_aliases({"vivid_fish_alias": True}), {"azizam_alias": True})
        self.assertEqual(accept_aliases({"vivid_object": "video", "vivid_status": "done"}),
                         {"azizam_object": "video", "azizam_status": "done"})
        self.assertEqual(accept_aliases({"vivid_fish_alias": False, "azizam_alias": True}), {"azizam_alias": True})

    def test_workflow_namespaces_are_accepted(self):
        for namespace in ("com.azizam.workflow", "com.vividfish.workflow"):
            self.assertEqual(provenance.requested_metadata_mode({"extensions": {namespace: {"metadata": "full"}}}), "full")
        value = accept_aliases({"extensions": {"com.azizam.workflow": {"metadata": "none"},
                                              "com.vividfish.workflow": {"metadata": "full"}}})
        self.assertEqual(provenance.requested_metadata_mode(value), "none")
        self.assertNotIn("com.vividfish.workflow", emit_aliases(value)["extensions"])

    def test_provenance_reads_old_and_new_permanently(self):
        for key in ("azizam_provenance", "vividfish_provenance"):
            self.assertEqual(provenance.read_provenance_value({key: "record"}), "record")
            self.assertEqual(provenance.read_provenance_lines(key + ":record", ":"), "record")
            self.assertEqual(provenance.read_provenance_lines(key + "=record", "="), "record")
        self.assertEqual(provenance.read_provenance_value({"azizam_provenance": "new", "vividfish_provenance": "old"}), "new")
        self.assertEqual(provenance.PROVENANCE_KEY, "azizam_provenance")
        self.assertNotIn("vividfish_provenance", emit_aliases({"azizam_provenance": {}}))

    def test_headers_accept_old_with_new_precedence(self):
        names = list(HEADER_ALIASES)
        for new in names:
            for old in header_names(new)[1:]:
                self.assertIn((new.encode(), b"old"), normalize_headers([(old.encode(), b"old")]))
                out = normalize_headers([(old.encode(), b"old"), (new.encode(), b"new")])
                self.assertEqual(dict(out)[new.encode()], b"new")

    def test_response_headers_replace_stale_upstream_alias(self):
        headers = emit_headers([(b"x-ai-gateway-request-id", b"upstream"),
                                (b"x-azizam-request-id", b"gateway")])
        self.assertEqual(dict(headers)[b"x-ai-gateway-request-id"], b"gateway")
        self.assertEqual(dict(headers)[b"x-azizam-request-id"], b"gateway")
        self.assertEqual(emit_header_mapping({"X-Litellm-Key": "token"}), {"X-Litellm-Key": "token"})

    def test_old_upstream_response_gets_new_fields(self):
        out = emit_aliases({"vivid_fish_alias": True})
        self.assertEqual(out, {"azizam_alias": True, "vivid_fish_alias": True})

    def test_shell_env_fallback_and_precedence(self):
        command = 'source "$1"; [[ ${AZIZAM_PORT:-} == "$2" ]]'
        shell = str(ROOT / "scripts/azizam-env.sh")
        for env, expected in (({"OPENAI_PROXY_PORT": "5199"}, "5199"),
                              ({"OPENAI_PROXY_PORT": "5199", "AZIZAM_PORT": "5200"}, "5200"),
                              ({}, "")):
            result = subprocess.run(["bash", "-c", command, "bash", shell, expected],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_png_reader_accepts_both_embedded_keys(self):
        from gateway import media_metadata
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5XQAAAAASUVORK5CYII=")
        record = {"schema": "vividfish.provenance/1", "generator": "vividfish-gateway"}
        for key in provenance.PROVENANCE_KEYS:
            content = media_metadata.embed_png_text(png, {key: json.dumps(record)})
            raw = provenance.read_provenance_value(media_metadata.png_text_chunks(content))
            self.assertEqual(json.loads(raw), record)
            self.assertEqual(provenance.sanitize_provenance(record), record)

    def test_old_alias_emission_is_central(self):
        tree = ast.parse((ROOT / "gateway/compat.py").read_text())
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "emit_aliases"]
        self.assertEqual(len(functions), 1)
        for path in [ROOT / "proxy.py", * (ROOT / "gateway").rglob("*.py")]:
            if path.name == "compat.py":
                continue
            parsed = ast.parse(path.read_text())
            for node in ast.walk(parsed):
                if isinstance(node, ast.Dict):
                    for key in node.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            self.assertFalse(key.value.startswith("vivid_fish_"), str(path))

    def test_discovery_aliases_share_handlers(self):
        tree = ast.parse((ROOT / "proxy.py").read_text())
        routes = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                        routes[dec.args[0].value] = node.name
        self.assertEqual(routes["/.well-known/azizam"], routes["/.well-known/openai-proxy"])
        self.assertIn("/.well-known/ai-gateway", routes)
        skill = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "well_known_skill_file")
        self.assertTrue(any(isinstance(n, ast.Constant) and n.value == 'openai-proxy-gateway' for n in ast.walk(skill)))

    def test_safe_restart_config_dir_fallback(self):
        ns = runpy.run_path(str(ROOT / "scripts/safe-restart"))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            old = home / ".config/openai-proxy/admin-key"
            new = home / ".config/azizam/admin-key"
            old.parent.mkdir(parents=True)
            old.write_text("old")
            self.assertEqual(ns["resolve_token"]({"HOME": directory}), "old")
            new.parent.mkdir(parents=True)
            new.write_text("new")
            self.assertEqual(ns["resolve_token"]({"HOME": directory}), "new")
            self.assertEqual(ns["resolve_token"]({"HOME": directory, "VIVID_ADMIN_API_KEY": "legacy"}), "legacy")

    def test_openapi_extension_keys_accept_and_emit_both(self):
        for old, new in (("x-vividfish-workflows", "x-azizam-workflows"),
                         ("x-vividfish-workflow-id", "x-azizam-workflow-id"),
                         ("x-vividfish-asset", "x-azizam-asset")):
            self.assertEqual(accept_aliases({old: "value"}), {new: "value"})
            self.assertEqual(emit_aliases({new: "value"}), {new: "value", old: "value"})

    def test_status_restart_counter_falls_back_to_old_unit(self):
        from typing import Optional
        tree = ast.parse((ROOT / "status_endpoint.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_proxy_restart_count")
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return (0, "LoadState=not-found" if command[3] == "azizam.service" else "LoadState=loaded\nNRestarts=7", "")
        ns = {"_run": run, "Optional": Optional}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "status-counter", "exec"), ns)
        self.assertEqual(ns["_proxy_restart_count"](), 7)
        self.assertEqual([c[3] for c in calls], ["azizam.service", "openai-proxy.service"])

    def test_restart_unit_prefers_new_and_accepts_old(self):
        ns = runpy.run_path(str(ROOT / "scripts/safe-restart"))
        with tempfile.TemporaryDirectory() as directory:
            units = Path(directory) / ".config/systemd/user"
            units.mkdir(parents=True)
            (units / "openai-proxy.service").touch()
            self.assertEqual(ns["installed_gateway_unit"]({"HOME": directory}), "openai-proxy")
            (units / "azizam.service").touch()
            self.assertEqual(ns["installed_gateway_unit"]({"HOME": directory}), "azizam")

    def test_json_and_sse_wire_aliases(self):
        async def exercise(sse=False):
            seen = []
            sent = []
            incoming = [{"type": "http.request", "body": b'{"vivid_fish_alias":true}', "more_body": False}]
            async def receive():
                return incoming.pop(0) if incoming else {"type": "http.disconnect"}
            async def send(message):
                sent.append(message)
            async def app(scope, receive, send):
                seen.append(scope)
                seen.append(await receive())
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/event-stream" if sse else b"application/json"),
                                        (b"x-azizam-engine", b"engine")]})
                if sse:
                    for chunk in (b'data: {"azizam_', b'alias":true}\n\ndata: [DONE]\n\n'):
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                else:
                    await send({"type": "http.response.body", "body": b'{"azizam_alias":true}', "more_body": False})
            await ProductCompatibilityMiddleware(app)({"type": "http", "headers": [(b"content-type", b"application/json"), (b"x-vivid-engine", b"legacy")]}, receive, send)
            self.assertIn((b"x-azizam-engine", b"legacy"), seen[0]["headers"])
            self.assertEqual(json.loads(seen[1]["body"]), {"azizam_alias": True})
            self.assertIn((b"x-vivid-engine", b"engine"), sent[0]["headers"])
            body = b"".join(m.get("body", b"") for m in sent)
            self.assertIn(b'"vivid_fish_alias": true', body)
            if sse:
                self.assertIn(b"data: [DONE]\n\n", body)
            else:
                self.assertEqual(json.loads(body)["azizam_alias"], json.loads(body)["vivid_fish_alias"])
                self.assertEqual(int(dict(sent[0]["headers"])[b"content-length"]), len(body))
        asyncio.run(exercise())
        asyncio.run(exercise(True))


if __name__ == "__main__":
    unittest.main()
