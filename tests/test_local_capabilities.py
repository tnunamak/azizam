"""Tests for the pure local capability inventory module."""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import local_capabilities, model_metadata as mm  # noqa: E402
from gateway.capabilities import Capability, ResourceEstimate, summarize_capabilities  # noqa: E402
from tests.support import c2d_parity  # noqa: E402
from gateway.local_capabilities import (  # noqa: E402
    INTENT_AUDIO_GENERATE,
    INTENT_CHAT,
    INTENT_COMPLETIONS,
    INTENT_EMBEDDINGS,
    INTENT_SPEECH,
    INTENT_TRANSCRIPTIONS,
    INTENT_TRANSLATIONS,
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
            "capabilities": ["images", "generations", "videos", "audio.generate"],
            "workflows_dir": "./workflows/images",
            "default_model": "flux2-klein-4b",
        },
    },
    "roles": {
        "default": {
            "backend": "kobold",
            "model": "/secret/path/Qwen3.6-27B.gguf",
            "mmproj": "/secret/path/mmproj.gguf",
            "model_metadata": {"input_modalities": ["text", "image"]},
        },
        "vision": {
            "backend": "tabby",
            "model_metadata": {"input_modalities": ["text", "image"]},
        },
        "text-only-role": {
            "backend": "kobold",
            "model_metadata": {"input_modalities": ["text"]},
        },
    },
    "models": [
        {
            "id": "heretic-v2",
            "backend": "kobold",
            "path": "/secret/path/heretic.gguf",
            "mmproj": "/secret/path/mmproj.gguf",
            "model_metadata": {"input_modalities": ["text", "image"]},
        },
        {
            "id": "cydonia-24b",
            "backend": "kobold",
            "path": "/secret/path/cydonia.gguf",
            "mmproj": None,
            "model_metadata": {"input_modalities": ["text"]},
        },
    ],
    "videos": {
        "default_workflow": "video-ltx-2.5-t2v",
        "workflows": {
            "video-ltx-2.5-t2v": {
                "file": "workflows/videos/video-ltx-2.5-t2v.json",
                "max_seconds": 8,
            },
            "video-ltx-2.5-i2v": {
                "file": "workflows/videos/video-ltx-2.5-i2v.json",
            },
        },
        "aliases": {
            "ltx-2.5": "video-ltx-2.5-t2v",
            "ltx-2.5-i2v": "video-ltx-2.5-i2v",
            "sora-2": "video-ltx-2.5-t2v",
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


class ReadDeclaredInputModalitiesTest(unittest.TestCase):
    def test_absent_parent_or_field_is_undeclared(self):
        for record in ({}, {"model_metadata": {}}, {"model_metadata": {"tags": ["vision"]}}):
            with self.subTest(record=record):
                self.assertIsNone(mm.read_declared_input_modalities(record, "role:sample"))

    def test_explicit_null_input_is_invalid(self):
        with self.assertRaisesRegex(ValueError, "role:sample.model_metadata.input_modalities"):
            mm.read_declared_input_modalities(
                {"model_metadata": {"input_modalities": None}}, "role:sample"
            )

    def test_malformed_parent_is_invalid(self):
        for declaration in (None, False, 1, "text", [], ["text"]):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(
                ValueError, "role:sample.model_metadata: must be a mapping"
            ):
                mm.read_declared_input_modalities(
                    {"model_metadata": declaration}, "role:sample"
                )

    def test_non_list_input_is_invalid(self):
        for values in (False, 1, "text", {"text": True}, ("text",), {"text"}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "nonempty list"):
                mm.read_declared_input_modalities(
                    {"model_metadata": {"input_modalities": values}}, "model:sample"
                )

    def test_empty_input_list_is_invalid(self):
        with self.assertRaisesRegex(ValueError, "nonempty list"):
            mm.read_declared_input_modalities(
                {"model_metadata": {"input_modalities": []}}, "model:sample"
            )

    def test_unknown_input_is_invalid(self):
        with self.assertRaisesRegex(ValueError, "outside the input vocabulary"):
            mm.read_declared_input_modalities(
                {"model_metadata": {"input_modalities": ["unregistered-input"]}}, "model:sample"
            )

    def test_duplicate_input_is_invalid(self):
        with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
            mm.read_declared_input_modalities(
                {"model_metadata": {"input_modalities": ["text", "text"]}}, "model:sample"
            )

    def test_noncanonical_strings_are_invalid(self):
        for value in ("", "Text", "TEXT", " text", "text ", "\ttext"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "canonical strings"):
                mm.read_declared_input_modalities(
                    {"model_metadata": {"input_modalities": [value]}}, "model:sample"
                )

    def test_non_string_elements_are_invalid(self):
        for value in (None, False, 1, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "canonical strings"):
                mm.read_declared_input_modalities(
                    {"model_metadata": {"input_modalities": ["text", value]}}, "model:sample"
                )

    def test_result_follows_contract_order(self):
        values = list(reversed(mm.METADATA_CONTRACT.input_modalities))
        self.assertEqual(
            mm.read_declared_input_modalities(
                {"model_metadata": {"input_modalities": values}}, "model:sample"
            ),
            mm.METADATA_CONTRACT.input_modalities,
        )

    def test_known_audio_and_file_inputs_are_accepted(self):
        self.assertEqual(
            mm.read_declared_input_modalities(
                {"model_metadata": {"input_modalities": ["file", "audio"]}}, "model:sample"
            ),
            ("audio", "file"),
        )

    def test_reader_does_not_mutate_valid_or_invalid_records(self):
        for values in (["image", "text"], ["text", "text"], ["text", {}]):
            record = {"model_metadata": {"input_modalities": values, "tags": ["vision"]}}
            before = json.loads(json.dumps(record))
            with self.subTest(values=values):
                if values == ["image", "text"]:
                    self.assertEqual(mm.read_declared_input_modalities(record, "row:1"), ("text", "image"))
                else:
                    with self.assertRaises(ValueError):
                        mm.read_declared_input_modalities(record, "row:1")
                self.assertEqual(record, before)

    def test_errors_use_safe_row_identity_without_record_contents(self):
        private_path = "/private/operator/model.gguf"
        for declaration, field in (
            (private_path, "model_metadata"),
            ({"input_modalities": [private_path]}, "model_metadata.input_modalities"),
            ({"input_modalities": {"path": private_path}}, "model_metadata.input_modalities"),
        ):
            record = {
                "path": private_path,
                "description": "private operator note",
                "model_metadata": declaration,
            }
            with self.subTest(field=field), self.assertRaises(ValueError) as error:
                mm.read_declared_input_modalities(record, "models[3]")
            message = str(error.exception)
            self.assertTrue(message.startswith(f"models[3].{field}: "))
            self.assertNotIn(private_path, message)
            self.assertNotIn("private operator note", message)

    def test_text_only_declaration_is_complete_despite_other_signals(self):
        record = {
            "id": "vision",
            "backend": "vision",
            "capabilities": ["vision"],
            "mmproj": "/not-installed/projector.gguf",
            "model_metadata": {"input_modalities": ["text"], "tags": ["vision"]},
        }
        self.assertEqual(mm.read_declared_input_modalities(record, "role:vision"), ("text",))

    def test_alternate_contract_controls_membership_and_order(self):
        contract = dataclasses.replace(
            mm.METADATA_CONTRACT,
            input_modalities=("control", "sample", "blob"),
            scalar_input="control",
            input_priority=(),
            implications=(),
        )
        with patch.object(mm, "METADATA_CONTRACT", contract):
            self.assertEqual(
                mm.read_declared_input_modalities(
                    {"model_metadata": {"input_modalities": ["blob", "sample"]}}, "row:1"
                ),
                ("sample", "blob"),
            )
            with self.assertRaisesRegex(ValueError, "outside the input vocabulary"):
                mm.read_declared_input_modalities(
                    {"model_metadata": {"input_modalities": ["text"]}}, "row:1"
                )


class BuildLocalCapabilitiesTest(unittest.TestCase):
    def test_representative_declarations_and_emitted_inputs_are_exact(self):
        expected = [
            ("role:default", ("text", "image")),
            ("role:vision", ("text", "image")),
            ("role:text-only-role", ("text",)),
            ("heretic-v2", ("text", "image")),
            ("cydonia-24b", ("text",)),
        ]
        records = [
            (f"role:{name}", spec) for name, spec in REPRESENTATIVE_CONFIG["roles"].items()
        ] + [(spec["id"], spec) for spec in REPRESENTATIVE_CONFIG["models"]]
        self.assertEqual(
            [(identity, mm.read_declared_input_modalities(spec, identity)) for identity, spec in records],
            expected,
        )
        self.assertEqual(
            [(cap.model, cap.input_modalities) for cap in build_local_capabilities(REPRESENTATIVE_CONFIG)
             if cap.intent == INTENT_CHAT and cap.model is not None],
            expected,
        )

    def test_fixture_declarations_do_not_change_any_emitted_modalities(self):
        undeclared = json.loads(json.dumps(REPRESENTATIVE_CONFIG))
        for spec in [*undeclared["roles"].values(), *undeclared["models"]]:
            del spec["model_metadata"]

        def modalities(config):
            return [
                (cap.intent, cap.backend, cap.model, cap.workflow,
                 cap.input_modalities, cap.output_modalities)
                for cap in build_local_capabilities(config)
            ]

        self.assertEqual(modalities(REPRESENTATIVE_CONFIG), modalities(undeclared))

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

    def test_vision_role_gets_image_input_from_its_backend(self):
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

    def test_missing_local_model_path_is_unavailable(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        cydonia = next(c for c in caps if c.model == "cydonia-24b")
        self.assertFalse(cydonia.available)
        self.assertEqual(cydonia.availability["status"], "unavailable")
        self.assertIn("Model file is not installed.", cydonia.caveats)

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
        self.assertIn("response_format:wav", speech[0].tags)
        self.assertIn("response_format:mp3", speech[0].tags)
        for audio_format in ("opus", "aac", "flac", "pcm"):
            self.assertIn(f"response_format:{audio_format}", speech[0].tags)

    def test_stt_capability(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        transcription = _by_intent(caps, INTENT_TRANSCRIPTIONS)
        self.assertEqual(len(transcription), 1)
        self.assertEqual(transcription[0].backend, "stt")
        self.assertEqual(transcription[0].input_modalities, ("audio",))
        self.assertEqual(_by_intent(caps, INTENT_TRANSLATIONS), [])

    def test_stt_translation_capability_requires_backend_declaration(self):
        config = json.loads(json.dumps(REPRESENTATIVE_CONFIG))
        config["backends"]["stt"]["capabilities"].append("translations")

        caps = build_local_capabilities(config)
        translations = _by_intent(caps, INTENT_TRANSLATIONS)

        self.assertEqual(len(translations), 1)
        self.assertEqual(translations[0].backend, "stt")
        self.assertEqual(translations[0].input_modalities, ("audio",))

    def test_image_generate_and_edit(self):
        caps = build_local_capabilities(REPRESENTATIVE_CONFIG)
        gen = _by_intent(caps, "image.generate")
        edit = _by_intent(caps, "image.edit")
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
            {"video-ltx-2.5-t2v", "video-ltx-2.5-i2v"},
        )
        ltx = next(c for c in video if c.workflow == "video-ltx-2.5-t2v")
        joined = " ".join(ltx.caveats)
        self.assertIn("ltx-2.5", joined)
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
            "backends": {"comfyui": {"capabilities": ["videos"], "workflows_dir": "/nope"}},
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
            self.assertNotIn("workflows/videos/video-ltx-2.5-t2v.json", blob, audience)
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


# ---------------------------------------------------------------------------
# Parity golden (tests/fixtures/c2d_parity/local_capabilities.json): the full ordered
# Capability list for REPRESENTATIVE_CONFIG with default flags, as dataclasses.asdict
# per record, in Capability.sort_key order (the order is the contract: discovery sorts
# by it). Stubbed helper: gateway.model_files._is_installed (the one disk read behind
# model_file_availability and missing_model_file_caveat), once absent and once present,
# because REPRESENTATIVE_CONFIG names weight files that exist on no machine.
# The golden is written only by tests/fixtures/c2d_parity/capture.py.


def capture_local_capabilities() -> dict:
    out = {}
    for label, stub in (("files_absent", c2d_parity.model_files_absent), ("files_present", c2d_parity.model_files_present)):
        with stub():
            caps = sorted(build_local_capabilities(REPRESENTATIVE_CONFIG), key=lambda c: c.sort_key)
        out[label] = [dataclasses.asdict(cap) for cap in caps]
    return c2d_parity.as_golden(out)


@pytest.mark.parametrize("label", ["files_absent", "files_present"])
def test_local_capabilities_match_golden(label):
    golden = c2d_parity.load_golden("local_capabilities")
    actual = capture_local_capabilities()
    assert len(actual[label]) == len(golden[label]), "capability count changed"
    for index, (got, want) in enumerate(zip(actual[label], golden[label])):
        for field in sorted(set(got) | set(want)):
            assert got.get(field) == want.get(field), f"record {index} ({want['intent']}/{want['backend']}/{want['model']}) field {field}"


def test_local_capabilities_golden_has_no_paths_or_secrets():
    blob = c2d_parity.canonical(c2d_parity.load_golden("local_capabilities"))
    assert "/secret/" not in blob

# Workflow families bind only to a declared backend in the effective backend map.
@pytest.mark.parametrize(
    ("family", "intent"),
    [("videos", "video.generate"), ("audio", "audio.generate")],
)
def test_workflow_family_without_a_selected_service_has_no_capabilities(family, intent):
    config = {
        "backends": {},
        family: {"workflows": {"sample": {"file": "sample.json"}}},
    }

    assert _by_intent(build_local_capabilities(config), intent) == []


@pytest.mark.parametrize(
    ("family", "intent"),
    [("videos", "video.generate"), ("audio", "audio.generate")],
)
def test_workflow_family_uses_configured_backend(family, intent):
    config = {
        "backends": {
            "comfyui": {"name": "ComfyUI"},
            "other": {"name": "Another workflow backend"},
        },
        family: {
            "backend": "other",
            "workflows": {"sample": {"file": "sample.json"}},
        },
    }

    caps = _by_intent(build_local_capabilities(config), intent)
    assert len(caps) == 1
    assert caps[0].workflow == "sample"
    assert caps[0].backend == "other"


@pytest.mark.parametrize(
    ("family", "intent"),
    [("videos", "video.generate"), ("audio", "audio.generate")],
)
def test_workflow_family_configured_backend_must_be_present(family, intent):
    config = {
        "backends": {"comfyui": {"name": "ComfyUI"}},
        family: {
            "backend": "missing",
            "workflows": {"sample": {"file": "sample.json"}},
        },
    }

    assert _by_intent(build_local_capabilities(config), intent) == []


@pytest.mark.parametrize(
    ("family", "intent"),
    [("videos", "video.generate"), ("audio", "audio.generate")],
)
def test_workflow_family_uses_backend_present_only_in_override(family, intent):
    config = {
        "backends": {},
        family: {"workflows": {"sample": {"file": "sample.json"}}},
    }
    live_backends = {"comfyui": {"name": "Live ComfyUI", "capabilities": ["videos", "audio.generate"]}}

    caps = _by_intent(
        build_local_capabilities(config, backends=live_backends), intent
    )

    assert len(caps) == 1
    assert caps[0].workflow == "sample"
    assert caps[0].backend == "comfyui"
    assert config["backends"] == {}


def test_image_family_requires_a_selected_service_control():
    config = json.loads(json.dumps(REPRESENTATIVE_CONFIG))
    del config["backends"]["comfyui"]

    without_comfyui = build_local_capabilities(config)
    assert _by_intent(without_comfyui, "image.generate") == []
    assert _by_intent(without_comfyui, "image.edit") == []

    # Positive control rules out a fixture which never advertised images.
    with_comfyui = build_local_capabilities(REPRESENTATIVE_CONFIG)
    assert [c.model for c in _by_intent(with_comfyui, "image.generate")] == [
        "flux2-klein-4b",
    ]
    assert [c.model for c in _by_intent(with_comfyui, "image.edit")] == [
        "flux2-klein-4b-edit",
    ]


@pytest.mark.parametrize("family", ["videos", "audio", "images"])
@pytest.mark.parametrize("backend", [None, "", ["other"]])
def test_family_backend_rejects_malformed_declared_value(family, backend):
    config = {
        "backends": {"comfyui": {"capabilities": ["images", "videos", "audio.generate"], "default_model": "image-model"}},
        family: {
            "backend": backend,
            "workflows": {"sample": {"file": "sample.json"}},
        },
    }

    with pytest.raises(ValueError) as error:
        build_local_capabilities(config)

    assert str(error.value) == (
        f"{family}.backend must be a non-empty string; got {backend!r}"
    )


def test_image_family_uses_declared_backend_and_model_defaults():
    config = {
        "backends": {"other": {"name": "Other workflow backend"}},
        "images": {"backend": "other"},
        "model_compatibility": {
            "defaults": {"images": "image-model", "image_edits": "edit-model"},
        },
    }

    caps = build_local_capabilities(config)

    assert [(cap.intent, cap.backend, cap.model) for cap in caps] == [
        ("image.generate", "other", "image-model"),
        ("image.edit", "other", "edit-model"),
    ]


def test_image_family_reads_metadata_from_selected_backend():
    config = {
        "backends": {
            "comfyui": {"default_model": "wrong-backend-model"},
            "other": {
                "default_model": "selected-model",
                "workflows_dir": "./selected-workflows",
            },
        },
        "images": {"backend": "other"},
        "model_compatibility": {
            "defaults": {"images": "fallback-model", "image_edits": "edit-model"},
        },
    }

    caps = build_local_capabilities(config)

    assert [(cap.intent, cap.backend, cap.model, cap.caveats) for cap in caps] == [
        ("image.generate", "other", "selected-model", ()),
        ("image.edit", "other", "edit-model", ()),
    ]


def test_image_family_selected_backend_preserves_rows():
    config = json.loads(json.dumps(REPRESENTATIVE_CONFIG))
    assert "images" not in config
    caps = [
        cap for cap in build_local_capabilities(config)
        if cap.intent in ("image.generate", "image.edit")
    ]

    assert [
        (
            cap.intent, cap.backend, cap.model, cap.input_modalities,
            cap.output_modalities, cap.execution, cap.streaming,
            cap.resource_tier, cap.resources.gpu_required, cap.caveats,
        )
        for cap in caps
    ] == [
        (
            "image.generate", "comfyui", "flux2-klein-4b", ("text",),
            ("image",), "job", False, "batch", True, (),
        ),
        (
            "image.edit", "comfyui", "flux2-klein-4b-edit", ("text", "image"),
            ("image",), "job", False, "batch", True, (),
        ),
    ]

    # An empty record declares no capability, so there is no selected image service.
    config["backends"]["comfyui"] = {}
    assert _by_intent(build_local_capabilities(config), "image.generate") == []
    assert _by_intent(build_local_capabilities(config), "image.edit") == []


def test_image_family_missing_declared_backend_does_not_fall_back():
    config = json.loads(json.dumps(REPRESENTATIVE_CONFIG))
    config["images"] = {"backend": "missing"}

    caps = build_local_capabilities(config)

    assert _by_intent(caps, "image.generate") == []
    assert _by_intent(caps, "image.edit") == []


def test_image_family_uses_effective_backend_override():
    config = {
        "backends": {"other": {"default_model": "frozen-model"}},
        "images": {"backend": "other"},
        "model_compatibility": {
            "defaults": {"images": "image-model", "image_edits": "edit-model"},
        },
    }

    caps = build_local_capabilities(
        config, backends={"other": {"name": "Live workflow backend"}}
    )

    assert [(cap.intent, cap.backend, cap.model) for cap in caps] == [
        ("image.generate", "other", "image-model"),
        ("image.edit", "other", "edit-model"),
    ]
    assert build_local_capabilities(config, backends={}) == []
    assert config["backends"] == {"other": {"default_model": "frozen-model"}}


def _load_backend_rows_with_flag(tmp_path, flag):
    document = yaml.safe_load(
        (ROOT / "registry" / "local_capability_rows.yml").read_text(encoding="utf-8")
    )
    # Only required fields: no optional metadata or shipped backend row is needed.
    document["backend_rows"] = [{
        "group": "endpoints",
        "triggers": ["unfamiliar-trigger"],
        "flag": flag,
        "intent": "unfamiliar.intent",
        "inputs": ["unfamiliar-input"],
        "outputs": ["unfamiliar-output"],
        "streaming": False,
        "execution": "sync",
        "gpu_required": False,
    }]
    path = tmp_path / "local_capability_rows.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return local_capabilities.load_backend_capability_rows(path)


@pytest.mark.parametrize("flag", ["spectral_gate", "Particle-Override"])
def test_backend_row_uses_its_unfamiliar_flag(tmp_path, monkeypatch, flag):
    rows = _load_backend_rows_with_flag(tmp_path, flag)
    assert len(rows) == 1
    assert rows[0]["flag"] == flag
    monkeypatch.setattr(local_capabilities, "_BACKEND_ROWS", rows)
    config = {"backends": {"future-backend": {"capabilities": ["unfamiliar-trigger"]}}}

    assert build_local_capabilities(config, enabled_flags={flag: False}) == []
    enabled = build_local_capabilities(config, enabled_flags={flag: True})
    assert [
        (cap.intent, cap.input_modalities, cap.output_modalities) for cap in enabled
    ] == [("unfamiliar.intent", ("unfamiliar-input",), ("unfamiliar-output",))]
    assert build_local_capabilities(
        config, enabled_flags={flag: True, "audio": False}
    ) == enabled
    assert build_local_capabilities(config, enabled_flags={"audio": False}) == enabled
    assert build_local_capabilities(
        config, enabled_flags={flag: False, "audio": True}
    ) == []


@pytest.mark.parametrize("flag", ["spectral_gate", "Particle-Override"])
@pytest.mark.parametrize("enabled", [False, True])
def test_backend_row_flag_rename_preserves_output(tmp_path, monkeypatch, flag, enabled):
    config = {"backends": {"future-backend": {"capabilities": ["unfamiliar-trigger"]}}}
    monkeypatch.setattr(
        local_capabilities, "_BACKEND_ROWS", _load_backend_rows_with_flag(tmp_path, flag)
    )
    original = build_local_capabilities(config, enabled_flags={flag: enabled})

    renamed = f"renamed::{flag}"
    monkeypatch.setattr(
        local_capabilities, "_BACKEND_ROWS", _load_backend_rows_with_flag(tmp_path, renamed)
    )
    actual = build_local_capabilities(
        config, enabled_flags={renamed: enabled, flag: not enabled}
    )
    assert actual == original
    assert len(actual) == int(enabled)


@pytest.mark.parametrize(
    "flag",
    ["", " \t\n", None, False, 7, ["spectral_gate"], {"spectral_gate": True}],
    ids=["empty", "whitespace", "null", "boolean", "integer", "list", "mapping"],
)
def test_backend_row_flag_rejects_invalid_values(tmp_path, flag):
    with pytest.raises(ValueError) as error:
        _load_backend_rows_with_flag(tmp_path, flag)

    assert str(error.value) == (
        f"{tmp_path / 'local_capability_rows.yml'}: "
        "backend_rows row 1: flag must be a non-empty string"
    )


# Independent complete-record expectations for the catalog's empty-config projection.
# Keep this fixture shared with test_models_catalog; do not read expectations from
# the production backend-row table.
CATALOG_OLD_FLAGS = {
    "llm": True, "embeddings": True, "tts": True, "stt": True,
    "images": False, "videos": False, "audio": False,
}
CATALOG_BACKEND_TEMPLATES = (
    (("chat",), Capability(
        intent="chat.completions", backend="declared-service",
        input_modalities=("text",), output_modalities=("text",),
        execution="sync", streaming=True, resources=ResourceEstimate(gpu_required=True),
    )),
    (("completions",), Capability(
        intent="text.completions", backend="declared-service",
        input_modalities=("text",), output_modalities=("text",),
        execution="sync", streaming=True, resources=ResourceEstimate(gpu_required=True),
    )),
    (("embeddings",), Capability(
        intent="text.embeddings", backend="declared-service",
        input_modalities=("text",), output_modalities=("text",),
        execution="sync", streaming=False, resources=ResourceEstimate(gpu_required=False),
    )),
    (("speech", "tts"), Capability(
        intent="speech.synthesis", backend="declared-service",
        input_modalities=("text",), output_modalities=("audio",),
        execution="sync", streaming=True, resources=ResourceEstimate(gpu_required=True),
        capability="speech",
        tags=tuple(f"response_format:{value}" for value in ("wav", "mp3", "opus", "aac", "flac", "pcm")),
    )),
    (("transcriptions", "stt"), Capability(
        intent="speech.transcription", backend="declared-service",
        input_modalities=("audio",), output_modalities=("text",),
        execution="sync", streaming=False, resources=ResourceEstimate(gpu_required=True),
        action_label="Transcribe", recordable_inputs=("audio",),
    )),
    (("translations", "translation"), Capability(
        intent="speech.translation", backend="declared-service",
        input_modalities=("audio",), output_modalities=("text",),
        execution="sync", streaming=False, resources=ResourceEstimate(gpu_required=True),
        action_label="Translate", recordable_inputs=("audio",),
        caveats=("Only advertised when the local STT backend declares translation support.",),
    )),
)
CATALOG_TRIGGER_CASES = tuple(
    (trigger, expected)
    for triggers, expected in CATALOG_BACKEND_TEMPLATES
    for trigger in triggers
)
CATALOG_EMPTY_DECLARATIONS = (
    {}, {"capabilities": None}, {"capabilities": "chat"},
    {"capabilities": ("chat",)}, {"capabilities": {"chat": True}},
    {"capabilities": 7}, {"capabilities": []},
    {"capabilities": ["unknown-trigger"]}, {"capabilities": [None, 7, {}, []]},
)


@pytest.mark.parametrize(("trigger", "expected"), CATALOG_TRIGGER_CASES)
@pytest.mark.parametrize("normalized", [False, True])
def test_empty_config_backend_rows_match_complete_records(trigger, expected, normalized):
    tokens = [trigger.upper(), trigger, trigger.upper(), None, 7, "unknown-trigger"] if normalized else [trigger]
    backends = {"declared-service": {"capabilities": tokens}}
    default = build_local_capabilities({}, backends=backends)
    masked = build_local_capabilities({}, backends=backends, enabled_flags=CATALOG_OLD_FLAGS)

    assert [dataclasses.asdict(cap) for cap in default] == [dataclasses.asdict(expected)]
    assert [dataclasses.asdict(cap) for cap in masked] == [dataclasses.asdict(expected)]


@pytest.mark.parametrize("backend", CATALOG_EMPTY_DECLARATIONS)
def test_empty_config_backend_rows_require_a_declared_list(backend):
    # Isolate generic rows: graph selection follows the selector's iterable-capability contract.
    assert build_local_capabilities(
        {}, backends={"declared-service": backend}, enabled_flags=CATALOG_OLD_FLAGS,
    ) == []


def test_empty_config_backend_rows_preserve_vision_and_resource_fields():
    backends = {"declared-service": {
        "capabilities": ["CHAT", "VISION", "embeddings"],
        "resources": {"gpu_required": False, "vram_gb_estimate": 2.5,
                      "duration_class": "short", "max_parallel": 3},
    }}
    expected = [
        dataclasses.replace(CATALOG_BACKEND_TEMPLATES[0][1], input_modalities=("text", "image")),
        dataclasses.replace(CATALOG_BACKEND_TEMPLATES[2][1], resources=ResourceEstimate(
            gpu_required=False, vram_gb_estimate=2.5, duration_class="short", max_parallel=3,
        )),
    ]
    assert [dataclasses.asdict(cap) for cap in build_local_capabilities({}, backends=backends)] == [
        dataclasses.asdict(cap) for cap in expected
    ]


@pytest.mark.parametrize("backend_id", ["comfyui", "graph-one"])
@pytest.mark.parametrize("default_model", [None, "concrete-image-model"])
def test_empty_config_preserves_generic_rows_and_binds_images_after_service_rename(backend_id, default_model):
    backend = {
        "capabilities": [trigger for trigger, _expected in CATALOG_TRIGGER_CASES]
                        + ["images", "generations", "videos", "audio"],
        "workflows_dir": "./unused-workflows",
    }
    if default_model is not None:
        backend["default_model"] = default_model
    backends = {backend_id: backend}
    masked = build_local_capabilities({}, backends=backends, enabled_flags=CATALOG_OLD_FLAGS)
    default = build_local_capabilities({}, backends=backends)
    expected_generic = [
        dataclasses.replace(template, backend=backend_id)
        for _triggers, template in CATALOG_BACKEND_TEMPLATES
    ]
    assert [dataclasses.asdict(cap) for cap in masked] == [
        dataclasses.asdict(cap) for cap in expected_generic
    ]
    assert [dataclasses.asdict(cap) for cap in default if cap.model is None] == [
        dataclasses.asdict(cap) for cap in expected_generic
    ]
    expected_bound = []
    if default_model is not None:
        expected_bound = [Capability(
            intent="image.generate", backend=backend_id, model=default_model,
            input_modalities=("text",), output_modalities=("image",),
            execution="job", streaming=False, resource_tier="batch",
            resources=ResourceEstimate(gpu_required=True),
        )]
    assert [dataclasses.asdict(cap) for cap in default] == [
        dataclasses.asdict(cap) for cap in expected_generic + expected_bound
    ]


@pytest.mark.parametrize("use_override", [False, True])
def test_renamed_graph_service_preserves_complete_rows_under_its_id(use_override):
    config = json.loads(json.dumps(REPRESENTATIVE_CONFIG))
    expected = [
        dataclasses.replace(cap, backend="graph-one") if cap.backend == "comfyui" else cap
        for cap in build_local_capabilities(config)
    ]
    renamed = dict(config["backends"])
    renamed["graph-one"] = renamed.pop("comfyui")
    before = json.loads(json.dumps(config))
    if use_override:
        actual = build_local_capabilities(config, backends=renamed)
        assert config == before
    else:
        config["backends"] = renamed
        actual = build_local_capabilities(config)
    assert actual == expected
    assert len([cap for cap in actual if cap.backend == "graph-one"]) == 6
    assert not any(cap.backend == "comfyui" for cap in actual)


def _graph_family_config(backends):
    return {
        "backends": backends,
        **{
            section: json.loads(json.dumps(REPRESENTATIVE_CONFIG[section]))
            for section in ("videos", "audio", "model_compatibility")
        },
    }


def test_image_only_service_does_not_advertise_video_or_audio_workflows():
    config = _graph_family_config({
        "graph-one": {"capabilities": ["images"], "default_model": "image-model"},
    })
    caps = build_local_capabilities(config)
    assert [(cap.intent, cap.backend) for cap in caps] == [
        ("image.generate", "graph-one"), ("image.edit", "graph-one"),
    ]


def test_graph_families_select_their_own_capability():
    config = _graph_family_config({
        "still-one": {"capabilities": ["images"], "default_model": "image-model"},
        "motion-one": {"capabilities": ["videos"]},
        "sound-one": {"capabilities": [" AUDIO ", " GENERATE "]},
    })
    caps = build_local_capabilities(config)
    assert [(cap.intent, cap.backend) for cap in caps] == [
        ("image.generate", "still-one"), ("image.edit", "still-one"),
        ("video.generate", "motion-one"), ("video.generate", "motion-one"),
        ("audio.generate", "sound-one"), ("audio.generate", "sound-one"),
    ]


@pytest.mark.parametrize("winner_state", [
    {"enabled": False, "host": "127.0.0.1", "port": 18101},
    {},
], ids=["disabled", "addressless"])
def test_graph_inventory_keeps_the_selected_winner_without_falling_through(winner_state):
    graph = json.loads(json.dumps(REPRESENTATIVE_CONFIG["backends"]["comfyui"]))
    config = _graph_family_config({
        "runner-up": {**graph, "priority": 10, "enabled": True,
                      "host": "127.0.0.1", "port": 18102},
        "winner": {**graph, **winner_state, "priority": 0},
    })
    expected = [
        dataclasses.replace(cap, backend="winner")
        for cap in build_local_capabilities(_graph_family_config({"comfyui": graph}))
    ]
    assert len(expected) == 6
    assert build_local_capabilities(config) == expected
    # Inventory remains raw. Supplied family flags, not record.enabled, suppress rows.
    assert build_local_capabilities(
        config, enabled_flags={"images": False, "videos": False, "audio": False},
    ) == []


@pytest.mark.parametrize("family, intent", [
    ("images", "image.generate"), ("videos", "video.generate"), ("audio", "audio.generate"),
])
@pytest.mark.parametrize("pin_state", ["declared", "empty", "missing"])
def test_explicit_graph_family_pin_keeps_presence_and_no_fallthrough(family, intent, pin_state):
    backends = {
        "candidate": {"capabilities": ["images", "videos", "audio.generate"],
                      "default_model": "wrong-model", "priority": 0},
    }
    if pin_state != "missing":
        backends["pinned"] = {"default_model": "pinned-model"} if pin_state == "declared" else {}
    config = {
        "backends": backends,
        family: {"backend": "pinned", "workflows": {"sample": {"file": "sample.json"}}},
        "model_compatibility": {"defaults": {"images": "compat-model"}},
    }
    flags = {key: key == family for key in ("images", "videos", "audio")}
    caps = build_local_capabilities(config, enabled_flags=flags)
    if pin_state == "missing" or (family == "images" and pin_state == "empty"):
        assert caps == []
    else:
        assert len(caps) == 1
        assert (caps[0].intent, caps[0].backend) == (intent, "pinned")
        assert caps[0].model == ("pinned-model" if family == "images" else None)


@pytest.mark.parametrize("table, change, key, value, message", [
    ("image_defaults", "delete", "backend_capability", None, "missing fields"),
    ("image_defaults", "set", "default_backend", "comfyui", "unknown fields"),
    ("image_defaults", "set", "backend_capability", "", "non-empty string"),
    ("image_defaults", "set", "backend_capability", "  ", "non-empty string"),
    ("image_defaults", "set", "backend_capability", None, "non-empty string"),
    ("image_defaults", "set", "backend_capability", [], "non-empty string"),
    ("image_defaults", "set", "backend_capability", 7, "non-empty string"),
    ("image_defaults", "set", "backend_capability", False, "non-empty string"),
    ("workflow_defaults", "delete", "backend_capabilities", None, "missing fields"),
    ("workflow_defaults", "set", "default_backend", "comfyui", "unknown fields"),
    ("workflow_defaults", "set", "backend_capabilities", None, "must be a mapping"),
    ("workflow_defaults", "set", "backend_capabilities", [], "must be a mapping"),
    ("workflow_defaults", "set", "backend_capabilities", "videos", "must be a mapping"),
    ("workflow_defaults", "set", "backend_capabilities", {}, "missing fields"),
    ("workflow_defaults", "set", "backend_capabilities", {"videos": "videos"}, "missing fields"),
    ("workflow_defaults", "set", "backend_capabilities",
     {"videos": "videos", "audio": "audio.generate", "extra": "images"}, "unknown fields"),
    ("workflow_defaults", "set", "backend_capabilities",
     {"videos": " ", "audio": "audio.generate"}, "non-empty string"),
    ("workflow_defaults", "set", "backend_capabilities",
     {"videos": "videos", "audio": None}, "non-empty string"),
    ("workflow_defaults", "set", "backend_capabilities",
     {"videos": "videos", "audio": ["audio.generate"]}, "non-empty string"),
    ("workflow_defaults", "set", "backend_capabilities",
     {"videos": "videos", "audio": "audio.generate", 7: "images"}, "unknown fields"),
    ("workflow_defaults", "set", "backend_capabilities",
     {"videos": "videos", "audio": 7}, "non-empty string"),
])
def test_graph_selection_defaults_reject_invalid_schema(tmp_path, table, change, key, value, message):
    document = yaml.safe_load(
        (ROOT / "registry" / "local_capability_rows.yml").read_text(encoding="utf-8")
    )
    if change == "delete":
        del document[table][key]
    else:
        document[table][key] = value
    path = tmp_path / "invalid-selection-rows.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        local_capabilities.load_local_capability_image_rows(path)
    assert str(path) in str(error.value)
    assert f"{table} row 1" in str(error.value)
    assert message in str(error.value)


def test_unmasked_inventory_skips_non_list_capabilities_without_losing_valid_rows():
    config = {
        "videos": {"workflows": {"clip": {"file": "clip.json"}}},
        "audio": {"workflows": {"sound": {"file": "sound.json"}}},
    }
    backends = {
        "chat-good": {"capabilities": ["chat"]},
        "malformed": {"capabilities": 7},
    }
    # These are the default, unmasked graph flags. A malformed record must
    # not abort generic discovery or accidentally publish image/workflow rows.
    caps = build_local_capabilities(config, backends=backends)
    assert [(cap.intent, cap.backend) for cap in caps] == [
        ("chat.completions", "chat-good"),
    ]
    assert build_local_capabilities({}, backends={"malformed": {"capabilities": 7}}) == []


# Alias-operation rows drive the resolver vocabulary without changing capability output.
def _alias_rows_document():
    return yaml.safe_load(
        (ROOT / "registry" / "local_capability_rows.yml").read_text(encoding="utf-8")
    )


def _write_alias_rows_document(tmp_path, document):
    path = tmp_path / "local_capability_rows.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_alias_operation_constants_match_previous_literals():
    from gateway import stable_aliases

    # Independent oracle: the three declarations before the row-driven refactor.
    operations = (
        "chat",
        "embeddings",
        "images",
        "image_edits",
        "videos",
        "speech",
        "transcriptions",
        "audio_generate",
        "rerank",
        "decisions",
    )
    shared = {
        "completions": "chat",
        "translations": "transcriptions",
    }
    engines = ("speech", "transcriptions", "rerank", "decisions")
    assert type(stable_aliases.OPERATIONS) is tuple
    assert stable_aliases.OPERATIONS == operations
    assert type(stable_aliases._SECTION_FOR_OPERATION) is dict
    assert list(stable_aliases._SECTION_FOR_OPERATION.items()) == list(shared.items())
    assert type(stable_aliases.ENGINE_OPERATIONS) is tuple
    assert stable_aliases.ENGINE_OPERATIONS == engines


def test_alias_operation_accessor_is_cached_and_deeply_immutable():
    with patch.object(Path, "read_text", side_effect=AssertionError("unexpected reload")):
        rows = local_capabilities.alias_operation_rows()
        assert local_capabilities.alias_operation_rows() is rows
    assert type(rows) is tuple
    with pytest.raises(TypeError):
        rows[0]["name"] = "replacement"
    with pytest.raises(TypeError):
        rows[0]["shared_operations"][0] = "replacement"
    with pytest.raises(TypeError):
        rows[0]["input_modalities"][0] = "replacement"


def test_alias_operation_rows_preserve_capability_and_image_loader_apis():
    aliases, families = local_capabilities.load_local_capability_rows()
    assert tuple(aliases) == (
        "chat", "embeddings", "images", "image_edits", "videos",
        "speech", "transcriptions", "audio_generate",
    )
    assert aliases == local_capabilities._ALIAS_INTENTS
    assert families == local_capabilities._WORKFLOW_FAMILIES
    assert local_capabilities.load_local_capability_image_rows() == (
        local_capabilities._IMAGE_DEFAULTS,
        local_capabilities._IMAGE_ROWS,
        local_capabilities._WORKFLOW_DEFAULTS,
    )
    rows = local_capabilities.alias_operation_rows()
    assert rows[-2]["name"] == "rerank" and rows[-2]["intent"] is None
    assert rows[-1]["name"] == "decisions" and rows[-1]["intent"] == "decision.systemone"
    for row in rows[-2:]:
        assert row["publish_capability"] is False
        assert row["input_modalities"] is None
        assert row["output_modalities"] is None
        assert row["execution"] is None
    assert local_capabilities.build_alias_capabilities({
        name: {"stable": [{"alias": "sample", "resolves_to": "slot", "engine": "slot"}]}
        for name in ("rerank", "decisions")
    }) == []


@pytest.mark.parametrize("index, shared, message", [
    (1, ["completions"], "duplicate shared operation"),
    (0, ["completions", "completions"], "duplicate shared operation"),
    (0, ["embeddings"], "shared operation is a row name"),
    (1, ["chat"], "shared operation is a row name"),
    (0, ["chat"], "shared operation is a row name"),
], ids=["across-rows", "within-row", "later-row", "earlier-row", "own-row"])
def test_alias_rows_reject_shared_name_collisions(tmp_path, index, shared, message):
    document = _alias_rows_document()
    document["alias_operations"][index]["shared_operations"] = shared
    path = _write_alias_rows_document(tmp_path, document)
    with pytest.raises(ValueError, match=message) as error:
        local_capabilities.load_local_capability_rows(path)
    assert f"{path}: alias_operations row " in str(error.value)


@pytest.mark.parametrize("shared", [None, "completions", 1, [None], [""]])
def test_alias_rows_reject_malformed_shared_names(tmp_path, shared):
    document = _alias_rows_document()
    document["alias_operations"][0]["shared_operations"] = shared
    path = _write_alias_rows_document(tmp_path, document)
    with pytest.raises(ValueError, match="shared_operations must be a list of non-empty strings"):
        local_capabilities.load_local_capability_rows(path)


@pytest.mark.parametrize("engine", [None, 0, 1, "true", [], {}])
def test_alias_rows_reject_non_boolean_engine_flag(tmp_path, engine):
    document = _alias_rows_document()
    document["alias_operations"][0]["engine_operation"] = engine
    path = _write_alias_rows_document(tmp_path, document)
    with pytest.raises(ValueError, match="engine_operation must be a boolean"):
        local_capabilities.load_local_capability_rows(path)


@pytest.mark.parametrize("publish", [None, 1, "false"])
def test_alias_rows_reject_non_boolean_publication_flag(tmp_path, publish):
    document = _alias_rows_document()
    document["alias_operations"][0]["publish_capability"] = publish
    path = _write_alias_rows_document(tmp_path, document)
    with pytest.raises(ValueError, match="publish_capability must be a boolean"):
        local_capabilities.load_local_capability_rows(path)


@pytest.mark.parametrize("key", ["intent", "input_modalities", "output_modalities", "execution"])
def test_publishing_alias_rows_still_require_complete_metadata(tmp_path, key):
    document = _alias_rows_document()
    document["alias_operations"][0][key] = None
    path = _write_alias_rows_document(tmp_path, document)
    with pytest.raises(ValueError, match=f"{key} must be"):
        local_capabilities.load_local_capability_rows(path)


def test_alias_row_new_fields_have_backward_compatible_defaults(tmp_path):
    document = _alias_rows_document()
    document["alias_operations"] = document["alias_operations"][:8]
    for row in document["alias_operations"]:
        row.pop("shared_operations", None)
        row.pop("engine_operation", None)
        row.pop("publish_capability", None)
    path = _write_alias_rows_document(tmp_path, document)
    loaded = local_capabilities._load_local_capability_rows(path)
    assert loaded[0] == local_capabilities._ALIAS_INTENTS
    assert all(row["shared_operations"] == () for row in loaded[-1])
    assert all(row["engine_operation"] is False for row in loaded[-1])
    assert all(row["publish_capability"] is True for row in loaded[-1])


@pytest.mark.parametrize("engine", [False, True])
def test_new_alias_row_drives_all_three_collections(tmp_path, monkeypatch, engine):
    import importlib.util

    from gateway import stable_aliases

    document = _alias_rows_document()
    document["alias_operations"].append({
        "name": "unfamiliar-operation",
        "intent": "unfamiliar.intent",
        "input_modalities": ["unfamiliar-input"],
        "output_modalities": ["unfamiliar-output"],
        "execution": "sync",
        "shared_operations": ["unfamiliar-request"],
        "engine_operation": engine,
    })
    path = _write_alias_rows_document(tmp_path, document)
    loaded = local_capabilities._load_local_capability_rows(path)
    monkeypatch.setattr(local_capabilities, "_ALIAS_ROWS", loaded[-1])
    # Execute a fresh module against the loaded rows without replacing live classes.
    name = "gateway._test_row_stable_aliases"
    spec = importlib.util.spec_from_file_location(name, stable_aliases.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    with patch.object(Path, "read_text", side_effect=AssertionError("unexpected reload")):
        spec.loader.exec_module(module)
    assert module.OPERATIONS == (*stable_aliases.OPERATIONS, "unfamiliar-operation")
    assert list(module._SECTION_FOR_OPERATION.items()) == [
        *stable_aliases._SECTION_FOR_OPERATION.items(),
        ("unfamiliar-request", "unfamiliar-operation"),
    ]
    expected_engines = (
        (*stable_aliases.ENGINE_OPERATIONS, "unfamiliar-operation")
        if engine else stable_aliases.ENGINE_OPERATIONS
    )
    assert module.ENGINE_OPERATIONS == expected_engines
    assert module.section_for_operation("unfamiliar-request") == "unfamiliar-operation"
    assert loaded[0]["unfamiliar-operation"] == (
        "unfamiliar.intent", ("unfamiliar-input",), ("unfamiliar-output",), "sync", None,
    )


if __name__ == "__main__":
    unittest.main()
