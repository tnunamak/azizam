"""The /showcase page: route, access rules, examples index and section rules."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import showcase  # noqa: E402

STATIC = ROOT / "showcase"


def _app(examples: Path, *, deny: bool = False, status=None) -> TestClient:
    def authenticate(request, path):
        return Response("no", status_code=401) if deny else object()

    async def provider():
        return status or {"engines": [], "comfyui": {"running": 0, "pending": 0}, "llm": {"processing": False}}

    app = FastAPI()
    app.include_router(showcase.build_showcase_router(
        static_dir=STATIC, examples_dir=examples, authenticate=authenticate, status_provider=provider))
    return TestClient(app)


def _write_example(root: Path, ex_id: str, meta: dict, files: dict[str, bytes] | None = None) -> None:
    d = root / ex_id
    d.mkdir(parents=True)
    (d / "example.json").write_text(json.dumps(meta), encoding="utf-8")
    for name, data in (files or {}).items():
        (d / name).write_bytes(data)


class ShowcaseRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.examples = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_static_assets_and_types(self) -> None:
        c = _app(self.examples)
        self.assertIn("javascript", c.get("/showcase/static/app.js").headers["content-type"])
        self.assertIn("javascript", c.get("/showcase/static/core.js").headers["content-type"])
        self.assertIn("text/css", c.get("/showcase/static/showcase.css").headers["content-type"])
        self.assertEqual(c.get("/showcase/static/sections.json").status_code, 200)

    def test_page_at_slash_path(self) -> None:
        c = _app(self.examples)
        page = c.get("/showcase/")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(page.headers["cache-control"], "no-cache")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertIn("<title>", page.text)
        head = c.head("/showcase/")
        self.assertEqual((head.status_code, head.content), (200, b""))
        for method in ("GET", "HEAD"):
            moved = c.request(method, "/showcase?a=1&b=2", follow_redirects=False)
            self.assertEqual((moved.status_code, moved.headers["location"]), (308, "/showcase/?a=1&b=2"))

    def test_page_missing_is_404(self) -> None:
        app = FastAPI()
        app.include_router(showcase.build_showcase_router(static_dir=self.examples, examples_dir=self.examples))
        self.assertEqual(TestClient(app).get("/showcase/").status_code, 404)

    def test_static_rejects_unknown_and_traversal(self) -> None:
        c = _app(self.examples)
        self.assertEqual(c.get("/showcase/static/nope.js").status_code, 404)
        self.assertEqual(c.get("/showcase/static/..%2Fproxy.py").status_code, 404)
        self.assertEqual(c.get("/showcase/static/index.html").status_code, 404)

    def test_page_uses_no_external_origins(self) -> None:
        for name in ("index.html", "app.js", "core.js", "showcase.css"):
            text = (STATIC / name).read_text(encoding="utf-8")
            # Plain <a> links to the docs are navigation; anything the page loads stays same-origin.
            for url in re.findall(r"""(?:\bsrc=|<link\b[^>]*\bhref=)["'](https?://[^"']+)""", text):
                self.fail(f"{name} loads external resource {url}")
            self.assertNotIn("@import url(http", text)

    def test_examples_index_lists_valid_examples_only(self) -> None:
        _write_example(self.examples, "tts-fast", {"card": "tts", "seconds": 1.2}, {"speech.mp3": b"ID3"})
        _write_example(self.examples, "broken", {"card": "x"})
        (self.examples / "broken" / "example.json").write_text("{not json", encoding="utf-8")
        (self.examples / "stray-dir").mkdir()
        (self.examples / "Bad Name").mkdir()
        c = _app(self.examples)
        data = c.get("/showcase/examples/index.json").json()["data"]
        self.assertEqual([e["id"] for e in data], ["tts-fast"])
        self.assertEqual(data[0]["base"], "/showcase/examples/tts-fast/")
        r = c.get("/showcase/examples/tts-fast/speech.mp3")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "audio/mpeg"))
        self.assertEqual(c.get("/showcase/examples/tts-fast/missing.png").status_code, 404)
        self.assertEqual(c.get("/showcase/examples/..%2F..%2Fproxy.py/x").status_code, 404)

    def test_examples_index_empty_when_dir_missing(self) -> None:
        c = _app(self.examples / "absent")
        self.assertEqual(c.get("/showcase/examples/index.json").json(), {"object": "list", "data": []})

    def test_status_goes_through_auth(self) -> None:
        self.assertEqual(_app(self.examples, deny=True).get("/showcase/api/status").status_code, 401)
        ok = _app(self.examples).get("/showcase/api/status")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["comfyui"], {"running": 0, "pending": 0})
        self.assertEqual(ok.headers["cache-control"], "no-store")

    def test_public_path_rules(self) -> None:
        pub = showcase.is_public_showcase_path
        for p in ("/showcase", "/showcase/", "/showcase/static/app.js", "/showcase/examples/index.json", "/showcase/examples/a/b.png"):
            self.assertTrue(pub(p), p)
        for p in ("/showcase/api/status", "/showcaseX", "/v1/models", "/gateway/v1/workflows"):
            self.assertFalse(pub(p), p)


class ShowcaseGatewayWiringTests(unittest.TestCase):
    def test_gateway_treats_page_as_public_and_status_as_private(self) -> None:
        import proxy
        self.assertTrue(proxy.is_public_path("/showcase"))
        self.assertTrue(proxy.is_public_path("/showcase/static/app.js"))
        self.assertFalse(proxy.is_public_path("/showcase/api/status"))
        # The gateway serves the static page itself; the web app has no showcase page.
        client = TestClient(proxy.app)
        response = client.get("/showcase?x=1", follow_redirects=False)
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers["location"], "/showcase/?x=1")
        page = client.get("/showcase/")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-cache")
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")
        self.assertEqual(page.headers["content-security-policy"], showcase.SHOWCASE_CSP)
        self.assertEqual(client.post("/showcase/").status_code, 404)
        self.assertEqual(client.get("/showcase/examples/index.json").status_code, 200)


class SectionRulesTests(unittest.TestCase):
    """Mirror of core.js classifyWorkflow, used only to guard the data."""

    def setUp(self) -> None:
        self.sections = json.loads((STATIC / "sections.json").read_text())["sections"]

    @staticmethod
    def _short(wf_id: str) -> str:
        return wf_id.split(".", 2)[2]

    def _classify(self, wf_id: str, intent: str) -> str | None:
        sid = self._short(wf_id)
        for s in self.sections:
            m = s.get("match")
            if s.get("fallback") or not m:
                continue
            if m.get("intent") and m["intent"] != intent:
                continue
            if m.get("id") and not re.search(m["id"], sid):
                continue
            if m.get("not_id") and re.search(m["not_id"], sid):
                continue
            return s["id"]
        return None

    def test_every_shipped_manifest_lands_in_an_explicit_section(self) -> None:
        manifests = sorted((ROOT / "manifests" / "workflows").glob("*.y*ml"))
        self.assertTrue(manifests)
        for path in manifests:
            doc = yaml.safe_load(path.read_text())
            self.assertIsNotNone(self._classify(doc["id"], doc["intent"]), f"{doc['id']} has no showcase section")

    def test_section_ids_unique_and_one_fallback(self) -> None:
        ids = [s["id"] for s in self.sections]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sum(1 for s in self.sections if s.get("fallback")), 1)

    def test_modality_order(self) -> None:
        ids = [s["id"] for s in self.sections]
        order = ["chat", "images", "image-edits", "video-create", "speech", "transcription", "music", "songs", "sfx", "embeddings", "protocols"]
        self.assertEqual([i for i in ids if i in order], order)


class ExampleScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = str(ROOT / "scripts" / "build-showcase-examples")
        loader = importlib.machinery.SourceFileLoader("build_showcase_examples", path)
        spec = importlib.util.spec_from_loader("build_showcase_examples", loader)
        cls.mod = importlib.util.module_from_spec(spec)
        sys.modules["build_showcase_examples"] = cls.mod
        loader.exec_module(cls.mod)

    def test_idle_requires_every_probe_clear(self) -> None:
        f = self.mod.idle_reasons
        idle = dict(working=0, comfy_queue={"queue_running": [], "queue_pending": []}, llm_slots=[{"is_processing": False}])
        self.assertEqual(f(**idle), [])
        self.assertTrue(f(**{**idle, "working": 1}))
        self.assertTrue(f(**{**idle, "comfy_queue": {"queue_running": [1], "queue_pending": []}}))
        self.assertTrue(f(**{**idle, "comfy_queue": {"queue_running": [], "queue_pending": [1]}}))
        self.assertTrue(f(**{**idle, "llm_slots": [{"is_processing": True}]}))

    def test_unreadable_probes_count_as_busy_but_missing_pane_as_idle(self) -> None:
        f = self.mod.idle_reasons
        self.assertTrue(f(working=0, comfy_queue=None, llm_slots=[]))
        self.assertTrue(f(working=0, comfy_queue={}, llm_slots=None))
        self.assertEqual(f(working=None, comfy_queue={}, llm_slots=[]), [])

    def test_selection_runs_cheap_first_and_video_last(self) -> None:
        chosen = self.mod.select(None, None)
        groups = [e["group"] for e in chosen]
        self.assertEqual(groups, sorted(groups, key=self.mod.GROUP_ORDER.index))
        self.assertEqual(groups[0], "cheap")
        self.assertEqual(groups[-1], "video")
        ids = [e["id"] for e in chosen]
        self.assertEqual(len(ids), len(set(ids)))

    def test_workflow_card_ids_disambiguate_intents(self) -> None:
        w = self.mod.workflow_card_id
        self.assertEqual(w("images", "flux2-klein-4b"), "image.generate.flux2-klein-4b")
        self.assertEqual(w("edits", "flux2-klein-4b"), "image.edit.flux2-klein-4b")
        self.assertEqual(w("video", "rife-interpolate"), "video.edit.rife-interpolate")
        self.assertEqual(w("video", "ltx-2.5-t2v"), "video.generate.ltx-2.5-t2v")
        self.assertEqual(w("cheap", "tts"), "tts")

    def test_save_is_atomic_and_skip_marker_is_example_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.mod.Ctx(gw=None, out=Path(tmp))
            ctx.group = "audio"
            ctx.save({"id": "x", "card": "tangoflux"}, {"output.flac": b"fLaC"})
            meta = json.loads((Path(tmp) / "x" / "example.json").read_text())
            self.assertEqual(meta["card"], "audio.generate.tangoflux")
            self.assertTrue((Path(tmp) / "x" / "output.flac").is_file())
            self.assertFalse(list((Path(tmp) / "x").glob("*.tmp")))

    def test_published_run_time_excludes_idle_wait_and_lookups(self) -> None:
        class Clock:
            """A virtual clock: sleeping advances it, nothing else does."""

            def __init__(self) -> None:
                self.now = 1000.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.now += seconds

        clock = Clock()

        class SlowGuard:
            def wait(self, label: str) -> None:
                clock.sleep(0.3)

        class Http:
            def request(self, method, url, **kw):
                clock.sleep(0.05)  # the only time the published run should include
                return httpx.Response(200, json={}, request=httpx.Request(method, url))

        with patch.object(self.mod, "time", clock):
            gw = self.mod.Gateway("http://127.0.0.1:9", guard=SlowGuard())
            gw.http = Http()
            gw.resolve = lambda operation, model: None
            gw.display_name = lambda model: clock.sleep(0.2)
            _, seconds = self.mod.timed(lambda: gw.request("POST", "/v1/embeddings", json={"model": "embed"}))
        self.assertEqual(seconds, 0.05)
        self.assertEqual(gw.recorded_requests[0]["requested_model"], "embed")

    def test_main_skips_present_examples_without_calling_the_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for e in self.mod.EXAMPLES:
                _write_example(Path(tmp), e["id"], {"card": e["card"]})
            rc = self.mod.main(["--out", tmp, "--no-idle-check", "--base", "http://127.0.0.1:9"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
