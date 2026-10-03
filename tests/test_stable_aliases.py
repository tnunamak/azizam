"""Two-layer model alias table: pure resolver, shipped config, and routes."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import stable_aliases as sa  # noqa: E402
from gateway.local_capabilities import build_alias_capabilities  # noqa: E402

SHIPPED = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
ALIASES = SHIPPED["model_aliases"]


def _all_up(_engine_id, _spec):
    return True


def _only(*deployed):
    return lambda engine_id, _spec: engine_id in deployed


TOY = {
    "chat": {
        "stable": {"fast": "default-fast", "default": "default", "quick": "fast"},
        "openai": [
            {"match": "gpt-oss*", "passthrough": True},
            {"match": "gpt-*-mini*", "to": "fast"},
            {"match": "gpt-*", "to": "default"},
        ],
    },
    "speech": {
        "engines": {
            "a": {"service": "tts", "model": "model-a"},
            "b": {"service": "tts-b", "model": "model-b"},
        },
        "stable": {"tts": ["a"], "tts-fast": ["b", "a"]},
        "openai": [{"match": "tts-1", "to": "tts-fast"}],
    },
}


class ResolverTests(unittest.TestCase):
    def test_unknown_name_is_not_an_alias(self):
        self.assertIsNone(sa.resolve_alias(TOY, "chat", "my-custom-model"))
        self.assertIsNone(sa.resolve_alias(TOY, "chat", ""))
        self.assertIsNone(sa.resolve_alias(TOY, "embeddings", "fast"))
        self.assertIsNone(sa.resolve_alias(None, "chat", "fast"))

    def test_stable_alias_and_chain(self):
        self.assertEqual(sa.resolve_alias(TOY, "chat", "fast").target, "default-fast")
        quick = sa.resolve_alias(TOY, "chat", "QUICK")
        self.assertEqual(quick.target, "default-fast")
        self.assertEqual(quick.layer, "stable")

    def test_identity_alias_resolves_to_itself(self):
        res = sa.resolve_alias(TOY, "chat", "default")
        self.assertEqual((res.target, res.layer), ("default", "stable"))

    def test_openai_patterns_first_match_wins_and_passthrough(self):
        self.assertEqual(sa.resolve_alias(TOY, "chat", "gpt-4o-mini").target, "default-fast")
        self.assertEqual(sa.resolve_alias(TOY, "chat", "gpt-4o").target, "default")
        self.assertEqual(sa.resolve_alias(TOY, "chat", "gpt-4o").layer, "openai")
        self.assertIsNone(sa.resolve_alias(TOY, "chat", "gpt-oss-120b"))

    def test_completions_share_chat_table(self):
        self.assertEqual(sa.resolve_alias(TOY, "completions", "gpt-4o").target, "default")

    def test_engine_list_picks_first_deployed_and_records_skips(self):
        res = sa.resolve_alias(TOY, "speech", "tts-fast", engine_available=_only("a"))
        self.assertEqual((res.engine, res.service, res.upstream_model), ("a", "tts", "model-a"))
        self.assertEqual(res.skipped_engines, ("b",))
        res = sa.resolve_alias(TOY, "speech", "tts-1", engine_available=_all_up)
        self.assertEqual((res.engine, res.layer), ("b", "openai"))

    def test_engine_id_is_a_valid_model_when_deployed(self):
        res = sa.resolve_alias(TOY, "speech", "b", engine_available=_all_up)
        self.assertEqual((res.engine, res.layer), ("b", "engine"))
        with self.assertRaises(sa.AliasUnavailableError) as ctx:
            sa.resolve_alias(TOY, "speech", "b", engine_available=_only("a"))
        self.assertEqual(ctx.exception.code, "model_not_deployed")

    def test_no_deployed_candidate_raises(self):
        with self.assertRaises(sa.AliasUnavailableError):
            sa.resolve_alias(TOY, "speech", "tts", engine_available=_only())

    def test_loop_is_reported_not_followed_forever(self):
        looped = {"chat": {"stable": {"a": "b", "b": "a"}}}
        self.assertTrue(any("loop" in p for p in sa.validate_aliases(looped)))
        with self.assertRaises(sa.AliasUnavailableError) as ctx:
            sa.resolve_alias(looped, "chat", "a")
        self.assertEqual(ctx.exception.code, "alias_misconfigured")

    def test_validate_catches_structural_mistakes(self):
        bad = {
            "chat": {"stable": {"x": ["engine"]}, "openai": [{"match": "gpt-*", "to": "nope"}]},
            "speech": {"engines": {"a": {"service": "s"}}, "stable": {"t": ["zzz"]}},
            "bogus": {},
        }
        problems = "\n".join(sa.validate_aliases(bad))
        for needle in ("engine list not allowed", "is not a stable alias", "missing model", "unknown engine", "unknown operation"):
            self.assertIn(needle, problems)

    def test_describe_reports_resolution_candidates_and_engine_state(self):
        view = sa.describe_aliases(TOY, engine_available=_only("a"))
        rows = {r["alias"]: r for r in view["speech"]["stable"]}
        self.assertEqual(rows["tts-fast"]["resolves_to"], "a")
        self.assertEqual(rows["tts-fast"]["candidates"], ["b", "a"])
        engines = {e["engine"]: e["deployed"] for e in view["speech"]["engines"]}
        self.assertEqual(engines, {"a": True, "b": False})
        self.assertEqual(view["chat"]["openai"][0], {"match": "gpt-oss*", "passthrough": True})

    def test_fold_mirrors_exact_names_into_legacy_tables(self):
        cfg = {
            "model_aliases": {
                "videos": {
                    "stable": {"video": "video-x"},
                    "openai": [{"match": "sora-2", "to": "video"}, {"match": "sora*", "to": "video"}],
                },
                "audio_generate": {"stable": {"sfx": "sfx-wf"}},
            },
            "videos": {"aliases": {"keep": "video-y"}},
            "audio": {"aliases": {}},
        }
        sa.fold_workflow_aliases(cfg)
        self.assertEqual(cfg["videos"]["aliases"], {"keep": "video-y", "video": "video-x", "sora-2": "video-x"})
        self.assertEqual(cfg["audio"]["aliases"], {"sfx": "sfx-wf"})


class ShippedConfigTests(unittest.TestCase):
    """The aliases in proxy_config.yml must point at things that exist."""

    def test_structure_is_valid(self):
        self.assertEqual(sa.validate_aliases(ALIASES), [])

    def test_every_documented_stable_name_exists(self):
        expected = {
            "chat": {"default", "fast", "coder", "vision", "rp"},
            "embeddings": {"embed"},
            "images": {"image", "image-fast", "image-uncensored", "image-anime"},
            "image_edits": {"image", "image-edit", "image-fast", "image-edit-fast"},
            "videos": {"video", "video-fast", "video-long"},
            "speech": {"tts", "tts-fast", "tts-quality"},
            "transcriptions": {"stt", "stt-fast", "stt-quality"},
            "audio_generate": {"music", "song", "sfx"},
        }
        for op, names in expected.items():
            self.assertTrue(names <= set(ALIASES[op]["stable"]), op)

    def test_chat_targets_are_roles_or_models(self):
        known = set(SHIPPED["roles"]) | {m["id"] for m in SHIPPED["models"]}
        for alias, target in sa.concrete_targets(ALIASES, "chat").items():
            self.assertIn(target, known, alias)

    def test_embedding_targets_exist(self):
        model_ids = {m["id"] for m in SHIPPED["backends"]["embeddings"]["models"]}
        for alias, target in sa.concrete_targets(ALIASES, "embeddings").items():
            self.assertIn(target, model_ids, alias)

    def test_image_targets_have_workflow_files(self):
        wf_dir = ROOT / "workflows" / "images"
        # Gateway-composed workflows have no ComfyUI file of their own; their manifest declares the model id.
        composed = {
            ((yaml.safe_load(p.read_text()).get("bindings") or {}).get("gateway") or {}).get("model_id")
            for p in (ROOT / "manifests" / "workflows").glob("*.yml")
        }
        for op, suffix in (("images", ""), ("image_edits", "")):
            for alias, target in sa.concrete_targets(ALIASES, op).items():
                self.assertTrue(
                    (wf_dir / f"{target}.json").exists() or target in composed, f"{op}:{alias}->{target}"
                )

    def test_image_stable_defaults_match_compat_defaults(self):
        defaults = SHIPPED["model_compatibility"]["defaults"]
        self.assertEqual(sa.concrete_targets(ALIASES, "images")["image"], defaults["images"])
        self.assertEqual(sa.concrete_targets(ALIASES, "image_edits")["image-edit"], defaults["image_edits"])
        self.assertEqual(sa.concrete_targets(ALIASES, "image_edits")["image"], defaults["image_edits"])

    def test_video_and_audio_targets_are_declared_workflows(self):
        for alias, target in sa.concrete_targets(ALIASES, "videos").items():
            self.assertIn(target, SHIPPED["videos"]["workflows"], alias)
        self.assertEqual(sa.concrete_targets(ALIASES, "videos")["video"], SHIPPED["videos"]["default_workflow"])
        for alias, target in sa.concrete_targets(ALIASES, "audio_generate").items():
            self.assertIn(target, SHIPPED["audio"]["workflows"], alias)

    def test_openai_names_resolve_for_every_endpoint(self):
        cases = {
            "chat": {
                "gpt-4o": "default", "gpt-4o-mini": "default-fast", "gpt-4.1": "default",
                "gpt-4.1-mini": "default-fast", "gpt-4.1-nano": "default-fast", "gpt-5": "default",
                "gpt-5-mini": "default-fast", "gpt-5-nano": "default-fast", "gpt-5.1": "default",
                "o3": "default", "o3-mini": "default-fast", "o4-mini": "default-fast",
                "gpt-3.5-turbo": "default-fast", "gpt-5-codex": "coder", "codex-mini-latest": "coder",
                "chatgpt-4o-latest": "default", "gpt-4o-2024-08-06": "default",
                "claude-sonnet-4-5": "default", "claude-haiku-4-5": "default-fast",
                "fast": "default-fast", "vision": "default",
            },
            "embeddings": {
                "text-embedding-3-small": "nomic-embed-text-v1.5",
                "text-embedding-3-large": "nomic-embed-text-v1.5",
                "text-embedding-ada-002": "nomic-embed-text-v1.5",
                "embed": "nomic-embed-text-v1.5",
            },
            "images": {"gpt-image-1": "krea2-turbo", "gpt-image-1-mini": "krea2-turbo", "gpt-image-1.5": "krea2-turbo",
                       "dall-e-3": "krea2-turbo", "dall-e-2": "krea2-turbo", "chatgpt-image-latest": "krea2-turbo",
                       "image": "krea2-turbo", "image-fast": "flux2-klein-4b",
                       "image-uncensored": "krea2-turbo-snofs", "image-anime": "anima-turbo"},
            "image_edits": {"gpt-image-1": "qwen-image-2.1-edit", "dall-e-2": "qwen-image-2.1-edit",
                            "image-edit": "qwen-image-2.1-edit", "image-edit-fast": "flux2-klein-4b-edit"},
            "videos": {"sora-2": "video-ltx-2.5-t2v", "sora-2-pro": "video-ltx-2.5-t2v",
                       "sora-2-2025-12-08": "video-ltx-2.5-t2v", "video": "video-ltx-2.5-t2v"},
            "audio_generate": {"music": "ace-step-1.5-xl-turbo", "song": "yue2-song", "sfx": "sa3-small-sfx"},
        }
        for op, names in cases.items():
            for name, want in names.items():
                res = sa.resolve_alias(ALIASES, op, name)
                self.assertIsNotNone(res, f"{op}:{name}")
                self.assertEqual(res.target, want, f"{op}:{name}")

    def test_speech_and_transcription_names_with_current_deployment(self):
        up = {"speech": _only("omnivoice"), "transcriptions": _only("parakeet")}
        for op, names in {
            "speech": ["tts-1", "tts-1-hd", "gpt-4o-mini-tts", "gpt-4o-tts", "tts", "tts-fast", "tts-quality", "omnivoice"],
            "transcriptions": ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe",
                               "stt", "stt-fast", "stt-quality", "parakeet"],
        }.items():
            for name in names:
                res = sa.resolve_alias(ALIASES, op, name, engine_available=up[op])
                self.assertIsNotNone(res, f"{op}:{name}")
                self.assertEqual(res.engine, {"speech": "omnivoice", "transcriptions": "parakeet"}[op])

    def test_diarize_names_resolve_only_to_the_diarize_engine(self):
        for name in ("gpt-4o-transcribe-diarize", "gpt-4o-transcribe-diarize-2026-01-01", "stt-diarize", "nemotron-diarize"):
            res = sa.resolve_alias(ALIASES, "transcriptions", name, engine_available=_all_up)
            self.assertEqual(res.engine, "nemotron-diarize", name)
            self.assertEqual(res.service, "stt-diarize", name)
        # No plain-text fallback: an engine without speakers would silently drop them.
        with self.assertRaises(sa.AliasUnavailableError) as ctx:
            sa.resolve_alias(ALIASES, "transcriptions", "gpt-4o-transcribe-diarize", engine_available=_only("parakeet"))
        self.assertEqual(ctx.exception.code, "model_not_deployed")
        # The generic gpt-4o transcribe names keep their own tier.
        res = sa.resolve_alias(ALIASES, "transcriptions", "gpt-4o-transcribe", engine_available=_all_up)
        self.assertEqual(res.engine, "parakeet-gpu")  # first of stt-quality; the CPU Parakeet is its fallback
        self.assertEqual([e for e, _ in res.fallbacks], ["parakeet", "canary"])

    def test_fast_tiers_split_when_their_engine_is_deployed(self):
        res = sa.resolve_alias(ALIASES, "speech", "tts-fast", engine_available=_all_up)
        self.assertEqual(res.engine, "supertonic")
        # Chatterbox Turbo is the default voice-cloning engine (Tim's blind listening test, 7/7).
        for name in ("tts", "tts-1", "tts-1-hd", "tts-quality", "gpt-4o-mini-tts"):
            res = sa.resolve_alias(ALIASES, "speech", name, engine_available=_all_up)
            self.assertEqual(res.engine, "chatterbox", name)
        # OmniVoice stays reachable by engine id and is the fallback behind Chatterbox.
        self.assertEqual(sa.resolve_alias(ALIASES, "speech", "omnivoice", engine_available=_all_up).engine, "omnivoice")
        fallback = sa.resolve_alias(ALIASES, "speech", "tts", engine_available=_only("omnivoice"))
        self.assertEqual(fallback.engine, "omnivoice")
        res = sa.resolve_alias(ALIASES, "speech", "kokoro", engine_available=_all_up)
        self.assertEqual(res.engine, "kokoro")
        res = sa.resolve_alias(ALIASES, "transcriptions", "stt-fast", engine_available=_all_up)
        self.assertEqual(res.engine, "nemotron-streaming")

    def test_gpt_oss_style_ids_stay_on_their_own_path(self):
        self.assertIsNone(sa.resolve_alias(ALIASES, "chat", "gpt-oss-20b"))

    def test_capabilities_expose_alias_resolution(self):
        view = sa.describe_aliases(ALIASES, engine_available=_only("omnivoice", "parakeet"))
        caps = build_alias_capabilities(view)
        by_model = {(c.intent, c.model): c for c in caps}
        fast = by_model[("chat.completions", "fast")]
        self.assertIn("resolves_to:default-fast", fast.tags)
        tts = by_model[("speech.synthesis", "tts-fast")]
        self.assertIn("resolves_to:omnivoice", tts.tags)
        self.assertIn("engine:omnivoice", tts.tags)
        self.assertEqual(tts.backend, "tts")


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(proxy.app)
        self.patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "_current_loaded_model_policy_decision", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _without_service(self, service_id):
        """The shipped config with one engine service removed (an undeployed engine)."""
        cfg = copy.deepcopy(proxy.config)
        del cfg["backends"][service_id]
        return patch.object(proxy, "config", cfg)

    def _chat_body_seen(self, path, payload):
        forward = AsyncMock(return_value=proxy.Response(content=b"{}", media_type="application/json"))
        with patch.object(proxy, "_forward_litellm_proxy_request", forward), patch.object(
            proxy, "_prepare_litellm_local_llm_request", AsyncMock(side_effect=lambda *a, **k: (None, k["body_json"]))
        ), patch.object(proxy, "_litellm_runtime_config") as runtime:
            runtime.return_value.enabled = True
            runtime.return_value.service_id = "litellm"
            with patch.object(proxy, "_service_availability_response", return_value=None):
                resp = self.client.post(path, json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        return forward.call_args.kwargs["body_json"]

    def test_chat_openai_name_is_rewritten_before_litellm(self):
        body = self._chat_body_seen(
            "/v1/chat/completions",
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2},
        )
        self.assertEqual(body["model"], "default-fast")
        self.assertEqual(body["temperature"], 0.2)

    def test_chat_concrete_and_unknown_names_are_untouched(self):
        for name in ("coder", "my-litellm-model"):
            body = self._chat_body_seen(
                "/v1/chat/completions", {"model": name, "messages": [{"role": "user", "content": "hi"}]}
            )
            self.assertEqual(body["model"], name)

    def test_embeddings_openai_name_is_rewritten(self):
        body = self._chat_body_seen(
            "/v1/embeddings", {"model": "text-embedding-3-small", "input": "x"}
        )
        self.assertEqual(body["model"], "nomic-embed-text-v1.5")

    def test_dry_run_endpoint_reports_resolution(self):
        resp = self.client.get("/gateway/v1/aliases/resolve", params={"operation": "chat", "model": "gpt-5"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["resolves_to"], "default")
        resp = self.client.get("/gateway/v1/aliases/resolve", params={"operation": "chat", "model": "coder-q5"})
        self.assertEqual(resp.json(), {"alias": False, "resolved": True, "requested": "coder-q5", "operation": "chat", "resolves_to": "coder-q5"})
        resp = self.client.get("/gateway/v1/aliases/resolve", params={"operation": "speech", "model": "supertonic"})
        self.assertEqual(resp.status_code, 200)
        with self._without_service("tts-supertonic"):
            resp = self.client.get("/gateway/v1/aliases/resolve", params={"operation": "speech", "model": "supertonic"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "model_not_deployed")

    def test_aliases_listing(self):
        resp = self.client.get("/gateway/v1/aliases", params={"operation": "speech"})
        self.assertEqual(resp.status_code, 200)
        speech = resp.json()["operations"]["speech"]
        rows = {r["alias"]: r for r in speech["stable"]}
        self.assertEqual(rows["tts"]["resolves_to"], "chatterbox")
        self.assertEqual(rows["tts"]["candidates"], ["chatterbox", "omnivoice"])
        self.assertEqual(rows["tts-fast"]["candidates"], ["supertonic", "chatterbox", "omnivoice"])
        engines = {e["engine"]: e["deployed"] for e in speech["engines"]}
        self.assertTrue(engines["omnivoice"])
        self.assertTrue(engines["supertonic"])
        self.assertEqual(rows["tts-fast"]["resolves_to"], "supertonic")
        self.assertEqual(rows["tts-quality"]["resolves_to"], "chatterbox")
        self.assertEqual(rows["tts-quality"]["candidates"], ["chatterbox", "omnivoice", "kokoro"])
        with self._without_service("tts-supertonic"):
            resp = self.client.get("/gateway/v1/aliases", params={"operation": "speech"})
        rows = {r["alias"]: r for r in resp.json()["operations"]["speech"]["stable"]}
        self.assertEqual(rows["tts-fast"]["resolves_to"], "chatterbox")
        self.assertEqual(rows["tts-fast"]["skipped_engines"], ["supertonic"])

    def test_models_entries_for_stable_aliases(self):
        entries = {e["id"]: e for e in proxy._stable_alias_model_entries()}
        self.assertEqual(entries["fast"]["vivid_fish_resolves_to"], {"chat": "default-fast"})
        self.assertEqual(
            entries["image"]["vivid_fish_resolves_to"],
            {"images": "krea2-turbo", "image_edits": "qwen-image-2.1-edit"},
        )
        self.assertEqual(entries["sfx"]["vivid_fish_resolves_to"], {"audio_generate": "sa3-small-sfx"})

    def test_media_resolution_helpers(self):
        res = proxy.resolve_model_for_operation("images", "gpt-image-1")
        self.assertEqual((res.effective_model, res.is_openai_alias), ("krea2-turbo", True))
        res = proxy.resolve_model_for_operation("image_edits", "image-edit-fast")
        self.assertEqual(res.effective_model, "flux2-klein-4b-edit")
        res = proxy.resolve_model_for_operation("videos", "video-fast")
        self.assertEqual(res.effective_model, "video-ltx-2.5-t2v")
        res = proxy.resolve_model_for_operation("images", "flux2-klein-4b")
        self.assertFalse(res.is_openai_alias)
        with self._without_service("tts-supertonic"), self.assertRaises(proxy.ModelCompatibilityError):
            proxy.resolve_model_for_operation("speech", "supertonic")
        with self.assertRaises(proxy.ModelCompatibilityError):
            proxy.resolve_model_for_operation("speech", "dall-e-3")

    def test_undeployed_engine_by_name_is_a_coded_400(self):
        with self._without_service("tts-supertonic"):
            resp = self.client.post(
                "/v1/audio/speech", json={"model": "supertonic", "voice": "nova", "input": "hi"}
            )
        self.assertEqual(resp.status_code, 400)
        error = resp.json()["error"]
        self.assertEqual((error["code"], error["param"]), ("model_not_deployed", "model"))

    def test_legacy_audio_and_video_tables_still_resolve(self):
        self.assertEqual(proxy._resolve_audio_workflow("sfx")[0], "sa3-small-sfx")
        self.assertEqual(proxy._resolve_audio_workflow("song")[0], "yue2-song")
        self.assertEqual(proxy._resolve_audio_workflow("ambient")[0], "sa3-small-sfx")
        self.assertEqual(proxy._resolve_video_workflow("sora-2")[0], "video-ltx-2.5-t2v")
        self.assertEqual(proxy._resolve_video_workflow("video-long")[0], "video-ltx-2.5-long")


class _FakeTTSClient:
    requests: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, headers=None, content=None):
        self.__class__.requests.append({"url": url, "body": json.loads(content)})
        return proxy.httpx.Response(200, content=b"wav", headers={"content-type": "audio/wav"})


class SpeechEngineRoutingTests(unittest.TestCase):
    """A second TTS engine, once its service exists, is reachable and gets its own voices."""

    def setUp(self):
        _FakeTTSClient.requests = []
        self.client = TestClient(proxy.app)
        cfg = copy.deepcopy(proxy.config)
        cfg["backends"]["tts-supertonic"] = {
            "name": "supertonic", "host": "127.0.0.1", "port": 8010, "capabilities": ["audio", "speech"],
        }
        self.patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "config", cfg),
            patch.object(proxy.httpx, "AsyncClient", _FakeTTSClient),
            patch.object(proxy, "_service_availability_response", return_value=None),
            patch.object(proxy, "_ensure_speech_service_ready", AsyncMock(return_value=None)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _speak(self, model, voice):
        resp = self.client.post(
            "/v1/audio/speech",
            json={"model": model, "voice": voice, "input": "hello", "response_format": "wav"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return _FakeTTSClient.requests[-1]

    def test_fast_alias_goes_to_the_deployed_fast_engine_with_mapped_voice(self):
        sent = self._speak("tts-fast", "Nova")
        self.assertEqual(sent["url"], "http://127.0.0.1:8010/v1/audio/speech")
        self.assertEqual(sent["body"]["model"], "supertonic")
        self.assertEqual(sent["body"]["voice"], "F2")

    def test_native_voice_and_unknown_voice(self):
        self.assertEqual(self._speak("supertonic", "M3")["body"]["voice"], "M3")
        self.assertEqual(self._speak("tts-fast", "no-such-voice")["body"]["voice"], "F1")

    def test_tts_alias_goes_to_chatterbox_with_identity_voices(self):
        sent = self._speak("tts", "nova")
        self.assertEqual(sent["url"], "http://127.0.0.1:8015/v1/audio/speech")
        self.assertEqual(sent["body"]["model"], "chatterbox-turbo")
        self.assertEqual(sent["body"]["voice"], "nova")

    def test_omnivoice_engine_id_still_reaches_omnivoice(self):
        sent = self._speak("omnivoice", "nova")
        self.assertEqual(sent["body"]["model"], "k2-fsa/OmniVoice")
        self.assertEqual(sent["body"]["voice"], "nova")
        self.assertNotIn(":8015", sent["url"])
        self.assertNotIn(":8010", sent["url"])


if __name__ == "__main__":
    unittest.main()
