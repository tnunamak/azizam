"""Tests for the pure local capability inventory module."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.capabilities import summarize_capabilities  # noqa: E402
from gateway.local_capabilities import (  # noqa: E402
    INTENT_AUDIO_GENERATE,
    INTENT_CHAT,
    INTENT_COMPLETIONS,
    INTENT_EMBEDDINGS,
    INTENT_IMAGE_EDIT,
    INTENT_IMAGE_GENERATE,
    INTENT_SPEECH,
    INTENT_TRANSCRIPTIONS,
    INTENT_VIDEO_GENERATE,
    build_local_capabilities,
)


REPRESENTATIVE_CONFIG: dict = {
    "backends": {
        "tabby": {
            "name": "TabbyAPI (Qwen3-VL)",
            "capabilities": ["vision", "chat", "completions"],
        },
        "kobold": {
            "name": "llama-turbo",
            "capabilities": ["chat", "completions"],
        },
        "tts": {
            "name": "voxtral-int4-tts",
            "capabilities": ["audio", "speech"],
        },
        "stt": {
            "name": "parakeet-stt",
            "capabilities": ["audio", "transcriptions"],
        },
        "embeddings": {
            "name": "nomic-embed-text-v1.5 CPU",
            "capabilities": ["embeddings"],
            "resources": {"gpu_required": False, "vram_gb_estimate": 0},
        },
        "comfyui": {
            "name": "ComfyUI",
            "capabilities": ["images", "generations", "videos"],
            "workflows_dir": "./workflows/images",
            "default_model": "flux2-klein-4b",
        },
    },
    "roles": {
        "default": {
            "backend": "kobold",
            "model": "/secret/path/Qwen3.6-27B.gguf",
            "mmproj": "/secret/path/mmproj.gguf",
        },
        "vision": {
            "backend": "tabby",
        },
        "text-only-role": {
            "backend": "kobold",
        },
    },
    "models": [
        {
            "id": "heretic-v2",
            "backend": "kobold",
            "path": "/secret/path/heretic.gguf",
            "mmproj": "/secret/path/mmproj.gguf",
        },
        {
            "id": "cydonia-24b",
            "backend": "kobold",
            "path": "/secret/path/cydonia.gguf",
            "mmproj": None,
        },
    ],
    "videos": {
        "default_workflow": "video-ltx-2.3-distilled",
        "workflows": {
            "video-ltx-2.3-distilled": {
                "file": "workflows/video-ltx-2.3-distilled.json",
                "max_seconds": 8,
            },
            "video-wan-2.2-ti2v-5b-q8": {
                "file": "workflows/video-wan-2.2.json",
            },
        },
        "aliases": {
            "ltx-2.3": "video-ltx-2.3-distilled",
            "wan-2.2": "video-wan-2.2-ti2v-5b-q8",
            "sora-2": "video-ltx-2.3-distilled",
        },
    },
    "audio": {
        "default_workflow": "ace-step-1.5-xl-turbo",
        "workflows": {
            "ace-step-1.5-xl-turbo": {
                "file": "workflows/audio/ace-step.json",
            },
            "tangoflux": {
                "file": "workflows/audio/tangoflux.json",
            },
        },
        "aliases": {
            "sfx": "tangoflux",
            "music": "ace-step-1.5-xl-turbo",
        },
    },
    "model_compatibility": {
        "defaults": {
            "images": "flux2-klein-4b",
            "image_edits": "flux2-klein-4b-edit",
        },
    },
}


def _by_intent(caps, intent):
    return [c for c in caps if c.intent == intent]


class BuildLocalCapabilitiesTest(unittest.TestCase):
    def test_llm_roles_emit_chat_capabilities(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        chat = _by_intent(caps, INTENT_CHAT)
        models = {c.model for c in chat if c.model}
        self.assertIn("role:default", models)
        self.assertIn("role:vision", models)
        self.assertIn("heretic-v2", models)
        self.assertIn("cydonia-24b", models)

    def test_mmproj_implies_vision_input(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        default_role = next(
            c for c in caps if c.intent == INTENT_CHAT and c.model == "role:default"
        )
        self.assertIn("image", default_role.input_modalities)

    def test_vision_role_name_implies_vision(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        vision_role = next(
            c for c in caps if c.intent == INTENT_CHAT and c.model == "role:vision"
        )
        self.assertIn("image", vision_role.input_modalities)

    def test_no_mmproj_text_only(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        cydonia = next(c for c in caps if c.model == "cydonia-24b")
        # cydonia is bound to kobold, which doesn't have vision capability
        self.assertEqual(cydonia.input_modalities, ("text",))

    def test_backend_vision_capability_propagates(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        # role:vision is bound to tabby which has 'vision' in capabilities;
        # also catch the generic chat capability for tabby
        tabby_chat = [
            c for c in caps if c.intent == INTENT_CHAT and c.backend == "tabby" and c.model is None
        ]
        self.assertTrue(tabby_chat)
        self.assertIn("image", tabby_chat[0].input_modalities)

    def test_openai_endpoints_emit_chat_and_completions(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        completions = _by_intent(caps, INTENT_COMPLETIONS)
        backends = {c.backend for c in completions}
        self.assertEqual(backends, {"tabby", "kobold"})

    def test_embeddings_capability_can_be_cpu_only(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        embeddings = _by_intent(caps, INTENT_EMBEDDINGS)
        self.assertEqual(len(embeddings), 1)
        self.assertEqual(embeddings[0].backend, "embeddings")
        self.assertFalse(embeddings[0].resources.gpu_required)
        self.assertEqual(embeddings[0].resources.vram_gb_estimate, 0)

    def test_tts_capability(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        speech = _by_intent(caps, INTENT_SPEECH)
        self.assertEqual(len(speech), 1)
        self.assertEqual(speech[0].backend, "tts")
        self.assertEqual(speech[0].output_modalities, ("audio",))

    def test_stt_capability(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        transcription = _by_intent(caps, INTENT_TRANSCRIPTIONS)
        self.assertEqual(len(transcription), 1)
        self.assertEqual(transcription[0].backend, "stt")
        self.assertEqual(transcription[0].input_modalities, ("audio",))

    def test_image_generate_and_edit(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        gen = _by_intent(caps, INTENT_IMAGE_GENERATE)
        edit = _by_intent(caps, INTENT_IMAGE_EDIT)
        self.assertEqual(len(gen), 1)
        self.assertEqual(gen[0].model, "flux2-klein-4b")
        self.assertEqual(gen[0].execution, "job")
        self.assertEqual(len(edit), 1)
        self.assertEqual(edit[0].model, "flux2-klein-4b-edit")
        self.assertIn("image", edit[0].input_modalities)

    def test_video_workflows_emit_capabilities_with_aliases(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        video = _by_intent(caps, INTENT_VIDEO_GENERATE)
        ids = {c.workflow for c in video}
        self.assertEqual(
            ids,
            {"video-ltx-2.3-distilled", "video-wan-2.2-ti2v-5b-q8"},
        )
        ltx = next(c for c in video if c.workflow == "video-ltx-2.3-distilled")
        joined = " ".join(ltx.caveats)
        self.assertIn("ltx-2.3", joined)
        self.assertIn("sora-2", joined)

    def test_audio_workflows_emit_capabilities(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        audio = _by_intent(caps, INTENT_AUDIO_GENERATE)
        ids = {c.workflow for c in audio}
        self.assertEqual(ids, {"ace-step-1.5-xl-turbo", "tangoflux"})
        tango = next(c for c in audio if c.workflow == "tangoflux")
        self.assertEqual(tango.output_modalities, ("audio",))
        self.assertTrue(any("sfx" in cav for cav in tango.caveats))

    def test_lifecycle_and_health_default_unknown(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        for c in caps:
            self.assertEqual(c.lifecycle, "unknown")
            self.assertEqual(c.health, "unknown")

    def test_flags_disable_families(self):
        caps = build_local_capabilities(
            REPRESENTATIVE_CONFIG,
            enabled_flags={
                "llm": False,
                "embeddings": False,
                "tts": False,
                "stt": False,
                "images": False,
                "videos": False,
                "audio": False,
            },
        )
        self.assertEqual(caps, [])

    def test_unknown_backend_is_called_out(self):
        config = {
            "roles": {
                "orphan": {"backend": "phantom"},
            },
        }
        caps = build_local_capabilities(config)
        orphan = next(c for c in caps if c.model == "role:orphan")
        self.assertTrue(any("phantom" in cav for cav in orphan.caveats))

    def test_purity_does_not_touch_filesystem(self):
        # Reference a workflow file that obviously does not exist; module must
        # still return a capability without raising or attempting to open it.
        config = {
            "backends": {"comfyui": {"workflows_dir": "/nope"}},
            "videos": {
                "workflows": {
                    "phantom": {"file": "/does/not/exist.json"},
                },
            },
        }
        caps = build_local_capabilities(config)
        phantom = next(c for c in caps if c.workflow == "phantom")
        self.assertEqual(phantom.intent, INTENT_VIDEO_GENERATE)


class SummarySafetyTest(unittest.TestCase):
    def test_no_paths_or_secrets_leak_in_any_audience(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        for audience in ("client", "admin", "internal"):
            payload = summarize_capabilities(caps, audience)
            blob = json.dumps(payload)
            self.assertNotIn("/secret/path", blob, audience)
            self.assertNotIn("Qwen3.6-27B.gguf", blob, audience)
            self.assertNotIn("mmproj.gguf", blob, audience)
            self.assertNotIn("heretic.gguf", blob, audience)
            self.assertNotIn("cydonia.gguf", blob, audience)
            # workflow JSON paths must not be surfaced in summaries
            self.assertNotIn("workflows/video-ltx-2.3-distilled.json", blob, audience)
            self.assertNotIn("workflows/audio/ace-step.json", blob, audience)
            # workflows_dir path should not be advertised
            self.assertNotIn("./workflows/images", blob, audience)

    def test_client_summary_excludes_backend_identity(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        payload = summarize_capabilities(caps, "client")
        for entry in payload["capabilities"]:
            self.assertNotIn("backend", entry)
            self.assertNotIn("policy_sources", entry)
            self.assertNotIn("policy_reasons", entry)


if __name__ == "__main__":
    unittest.main()
