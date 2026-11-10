"""Tests for the pure capability model."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from tests.support.access import access, plan_for  # noqa: E402
from gateway.capabilities import (  # noqa: E402
    UNREVIEWED_WORKFLOW_CAVEAT,
    Capability,
    ResourceEstimate,
    _derive_input_modalities,
    capability_from_workflow_record,
    capability_with_policy_view,
    summarize_capabilities,
    summarize_capability,
    unavailable_capability,
)


from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.manifests import Provenance  # noqa: E402
from tests.support.workflow_docs import STANDARD_FACTS  # noqa: E402
from gateway.policy import ALLOWED, DENIED, PolicyDecision, PolicyRequest, evaluate


CHAT_WORKFLOW_YAML = textwrap.dedent(
    """
    schema_version: 2
    version: "0.1.0"
    id: image.edit.controlnet.example
    intent: image.edit
    capability: image
    inputs:
      prompt:
        type: string
        required: true
      source_image:
        type: asset
        accepts: [image/png]
        required: true
      denoise:
        type: number
        minimum: 0
        maximum: 1
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/example-controlnet.json
        map:
          prompt:
            - node: "6"
              input: text
    """
).strip()


UNTRUSTED_WORKFLOW_YAML = textwrap.dedent(
    """
    schema_version: 2
    version: "0.1.0"
    id: image.generate.untrusted.example
    intent: image.generate
    capability: image
    inputs:
      prompt:
        type: string
        required: true
    outputs:
      image:
        media_type: image/png
    bindings:
      a1111:
        endpoint: /sdapi/v1/txt2img
        map:
          prompt:
            - path: prompt
      comfyui:
        workflow: workflows/images/community.json
        map:
          prompt:
            - node: "6"
              input: text
    """
).strip()


def _record(yaml_text: str, *, trusted: bool = True, source: str = "local"):
    """Provenance and facts are the record's, never the document's (CODE_TO_DATA_S3, S3.5a)."""
    manifest = yaml.safe_load(yaml_text)
    registry = WorkflowRegistry()
    return registry.add_manifest(manifest, source_path="fixture.yml", provenance=Provenance(trusted, source), facts=STANDARD_FACTS)


class CapabilityConstructionTests(unittest.TestCase):
    def test_required_fields_and_defaults(self) -> None:
        cap = Capability(intent="chat.generate", backend="llama.cpp.gpu1")

        self.assertEqual(cap.intent, "chat.generate")
        self.assertEqual(cap.backend, "llama.cpp.gpu1")
        self.assertIsNone(cap.model)
        self.assertIsNone(cap.workflow)
        self.assertEqual(cap.execution, "sync")
        self.assertEqual(cap.lifecycle, "unknown")
        self.assertEqual(cap.health, "unknown")
        self.assertFalse(cap.streaming)
        self.assertEqual(cap.exposure, "default")
        self.assertEqual(cap.trust_tier, "stable")
        self.assertEqual(cap.resource_tier, "interactive")
        self.assertEqual(cap.tags, ())
        self.assertTrue(cap.policy_available)
        self.assertTrue(cap.available)
        self.assertEqual(cap.caveats, ())

    def test_rejects_unknown_execution_mode(self) -> None:
        with self.assertRaises(ValueError):
            Capability(intent="x", backend="b", execution="forever")

    def test_rejects_unknown_lifecycle_and_health(self) -> None:
        with self.assertRaises(ValueError):
            Capability(intent="x", backend="b", lifecycle="ablaze")
        with self.assertRaises(ValueError):
            Capability(intent="x", backend="b", health="haunted")

    def test_rejects_unknown_metadata_values(self) -> None:
        with self.assertRaises(ValueError):
            Capability(intent="x", backend="b", exposure="public")
        with self.assertRaises(ValueError):
            Capability(intent="x", backend="b", trust_tier="trusted")
        with self.assertRaises(ValueError):
            Capability(intent="x", backend="b", resource_tier="slow")
        with self.assertRaises(ValueError):
            Capability(intent="x", backend="b", tags="coder")

    def test_dataclass_is_frozen_and_hashable(self) -> None:
        cap = Capability(intent="x", backend="b")
        with self.assertRaises(Exception):
            cap.intent = "y"  # type: ignore[misc]
        self.assertEqual({cap, cap}, {cap})


class AvailabilityTests(unittest.TestCase):
    def test_stopped_lifecycle_is_unavailable(self) -> None:
        cap = Capability(intent="x", backend="b", lifecycle="stopped")
        self.assertFalse(cap.available)

    def test_unhealthy_is_unavailable(self) -> None:
        cap = Capability(intent="x", backend="b", health="unhealthy")
        self.assertFalse(cap.available)

    def test_policy_denied_is_unavailable(self) -> None:
        cap = Capability(intent="x", backend="b", policy_available=False)
        self.assertFalse(cap.available)

    def test_unknown_states_are_treated_as_available(self) -> None:
        # Discovery must not hide capabilities just because a probe hasn't
        # run yet. The router applies live data later.
        cap = Capability(intent="x", backend="b")
        self.assertTrue(cap.available)

    def test_explicit_unavailable_projection_wins_over_unknown_states(self) -> None:
        cap = Capability(
            intent="x",
            backend="b",
            availability={
                "available": False,
                "status": "unavailable",
                "reason": "Model file is not installed.",
            },
        )
        self.assertFalse(cap.available)


class CaveatsTests(unittest.TestCase):
    def test_with_caveats_appends_unique(self) -> None:
        cap = Capability(intent="x", backend="b", caveats=("warm-up slow",))
        cap2 = cap.with_caveats("warm-up slow", "tool-use partial")
        self.assertEqual(cap2.caveats, ("warm-up slow", "tool-use partial"))

    def test_with_caveats_ignores_empty(self) -> None:
        cap = Capability(intent="x", backend="b")
        self.assertEqual(cap.with_caveats("", None or "").caveats, ())


class SummarizeCapabilityTests(unittest.TestCase):
    def _cap(self) -> Capability:
        return Capability(
            intent="image.edit",
            backend="comfyui.local",
            workflow="image.edit.controlnet.example",
            input_modalities=("text", "image"),
            output_modalities=("image",),
            execution="job",
            streaming=False,
            exposure="opt_in",
            trust_tier="experimental",
            resource_tier="batch",
            tags=("controlnet",),
            lifecycle="warm",
            health="healthy",
            policy_available=True,
            resources=ResourceEstimate(
                gpu_required=True, vram_gb_estimate=12, duration_class="medium"
            ),
            caveats=(UNREVIEWED_WORKFLOW_CAVEAT,),
            policy_sources=("profile:image-agent", "policy:local-only"),
            policy_reasons=("workflow permitted", "no cloud fallback"),
        )

    def test_client_summary_omits_backend_and_policy(self) -> None:
        client = summarize_capability(self._cap(), audience="client")

        self.assertEqual(client["intent"], "image.edit")
        self.assertEqual(client["execution"], "job")
        self.assertEqual(client["input_modalities"], ["text", "image"])
        self.assertEqual(client["output_modalities"], ["image"])
        self.assertEqual(client["workflow"], "image.edit.controlnet.example")
        self.assertEqual(client["exposure"], "opt_in")
        self.assertEqual(client["trust_tier"], "experimental")
        self.assertEqual(client["resource_tier"], "batch")
        self.assertEqual(client["tags"], ["controlnet"])
        self.assertEqual(client["available"], True)
        self.assertEqual(client["caveats"], [UNREVIEWED_WORKFLOW_CAVEAT])

        for forbidden in (
            "backend",
            "lifecycle",
            "health",
            "resources",
            "policy_available",
            "policy_sources",
            "policy_reasons",
        ):
            self.assertNotIn(forbidden, client, f"client summary leaked {forbidden}")

    def test_admin_summary_includes_backend_resources_and_sources(self) -> None:
        admin = summarize_capability(self._cap(), audience="admin")

        self.assertEqual(admin["backend"], "comfyui.local")
        self.assertEqual(admin["lifecycle"], "warm")
        self.assertEqual(admin["health"], "healthy")
        self.assertTrue(admin["policy_available"])
        self.assertEqual(admin["resources"]["gpu_required"], True)
        self.assertEqual(admin["resources"]["vram_gb_estimate"], 12)
        self.assertEqual(admin["resources"]["duration_class"], "medium")
        self.assertEqual(
            admin["policy_sources"], ["profile:image-agent", "policy:local-only"]
        )
        # Admin must NOT see raw reasons (those are policy-internal prose).
        self.assertNotIn("policy_reasons", admin)

    def test_internal_summary_adds_policy_reasons(self) -> None:
        internal = summarize_capability(self._cap(), audience="internal")
        self.assertEqual(
            internal["policy_reasons"], ["workflow permitted", "no cloud fallback"]
        )
        self.assertEqual(internal["backend"], "comfyui.local")

    def test_unknown_audience_rejected(self) -> None:
        with self.assertRaises(ValueError):
            summarize_capability(self._cap(), audience="public")

    def test_summary_drops_optional_identity_when_unset(self) -> None:
        cap = Capability(intent="chat.generate", backend="llama.cpp.gpu1")
        client = summarize_capability(cap, audience="client")
        self.assertNotIn("model", client)
        self.assertNotIn("workflow", client)


class SummarizeCapabilitiesOrderingTests(unittest.TestCase):
    def test_capabilities_sorted_deterministically(self) -> None:
        caps = [
            Capability(intent="image.edit", backend="comfyui.local", workflow="w.b"),
            Capability(intent="image.edit", backend="comfyui.local", workflow="w.a"),
            Capability(intent="chat.generate", backend="llama.cpp", model="qwen"),
            Capability(intent="chat.generate", backend="llama.cpp", model="phi"),
            Capability(intent="image.edit", backend="a1111.local"),
        ]

        catalog = summarize_capabilities(caps, audience="admin")
        keys = [
            (c["intent"], c["backend"], c.get("model", ""), c.get("workflow", ""))
            for c in catalog["capabilities"]
        ]
        self.assertEqual(
            keys,
            [
                ("chat.generate", "llama.cpp", "phi", ""),
                ("chat.generate", "llama.cpp", "qwen", ""),
                ("image.edit", "a1111.local", "", ""),
                ("image.edit", "comfyui.local", "", "w.a"),
                ("image.edit", "comfyui.local", "", "w.b"),
            ],
        )

    def test_catalog_envelope_fields(self) -> None:
        catalog = summarize_capabilities([], audience="client")
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(catalog["audience"], "client")
        self.assertEqual(catalog["capabilities"], [])

    def test_ordering_is_stable_across_input_permutations(self) -> None:
        a = Capability(intent="x", backend="b1", model="m1")
        b = Capability(intent="x", backend="b1", model="m2")
        c = Capability(intent="x", backend="b2")
        first = summarize_capabilities([a, b, c], audience="admin")
        second = summarize_capabilities([c, b, a], audience="admin")
        self.assertEqual(first, second)


class OutputModalityTests(unittest.TestCase):
    def test_existing_media_types_keep_their_modalities(self) -> None:
        from gateway.capabilities import _media_type_modality

        cases = (
            ("image/png", "image"),
            ("audio/wav", "audio"),
            ("video/mp4", "video"),
            ("text/plain", "text"),
            ("application/json", "text"),
            ("model/gltf-binary", None),
        )
        for media_type, expected in cases:
            with self.subTest(media_type=media_type):
                self.assertEqual(_media_type_modality(media_type), expected)

    def test_extra_modality_is_read_from_a_vocabulary_fixture(self) -> None:
        from gateway import capabilities
        from gateway.capability_ids import CAPABILITY_WORDS_PATH, load_modality_words

        document = yaml.safe_load(CAPABILITY_WORDS_PATH.read_text(encoding="utf-8"))
        document["modality_words"].extend(["font", "application"])
        self.assertIsNone(capabilities._media_type_modality("font/woff2"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capability_words.yml"
            path.write_text(yaml.safe_dump(document), encoding="utf-8")
            with patch.object(capabilities, "MODALITY_WORDS", load_modality_words(path)):
                self.assertEqual(capabilities._media_type_modality("font/woff2"), "font")
                self.assertEqual(capabilities._media_type_modality("application/pdf"), "application")
                self.assertEqual(capabilities._media_type_modality("application/json"), "text")
                self.assertEqual(
                    capabilities._derive_output_modalities({
                        "first": {"media_type": "font/woff2"},
                        "duplicate": {"media_type": "font/ttf"},
                        "caption": {"media_type": "text/plain"},
                    }),
                    ("font", "text"),
                )

    def test_bare_words_and_unregistered_majors_are_not_modalities(self) -> None:
        from gateway.capabilities import _media_type_modality

        for media_type in ("", "image", "audio", "video", "application/octet-stream"):
            with self.subTest(media_type=media_type):
                self.assertIsNone(_media_type_modality(media_type))

    def test_shipped_output_media_types_match_the_frozen_base(self) -> None:
        from gateway.capabilities import _media_type_modality

        folder = ROOT / "manifests" / "workflows"
        paths = sorted(path for path in folder.rglob("*") if path.suffix in {".yml", ".yaml"})
        self.assertTrue(paths, "No shipped manifests found; parity must not pass vacuously")
        checked = 0
        for path in paths:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            outputs = document.get("outputs") if isinstance(document, dict) else None
            if not isinstance(outputs, dict):
                continue
            for name, spec in outputs.items():
                if not isinstance(spec, dict):
                    continue
                media_type = str(spec.get("media_type") or "")
                with self.subTest(manifest=str(path.relative_to(ROOT)), output=name):
                    self.assertEqual(
                        _media_type_modality(media_type), _base_media_type_modality(media_type)
                    )
                checked += 1
        self.assertGreater(checked, 0, "No shipped outputs found; parity must not pass vacuously")


class InputModalityTests(unittest.TestCase):
    def test_declared_accepts_do_not_depend_on_intent_name(self) -> None:
        inputs = {"source": {"type": "asset", "accepts": ["audio/wav"]}}
        for intent in ("smell.infer", "image.transcribe", "audio.transcribe"):
            with self.subTest(intent=intent):
                self.assertEqual(
                    _derive_input_modalities(inputs, intent=intent), ("audio",)
                )

    def test_audio_asset_under_image_intent_reaches_both_capability_fields(self) -> None:
        manifest = yaml.safe_load(CHAT_WORKFLOW_YAML)
        manifest["inputs"]["source_image"].update(
            accepts=["audio/wav"], recordable=True
        )
        cap = capability_from_workflow_record(_record(yaml.safe_dump(manifest)))
        self.assertEqual(cap.input_modalities, ("text", "audio"))
        self.assertEqual(cap.recordable_inputs, ("audio",))

    def test_nested_assets_preserve_first_seen_order_and_deduplicate(self) -> None:
        for list_type in ("list", "array"):
            with self.subTest(list_type=list_type):
                inputs = {
                    "prompt": {"type": "string"},
                    "payload": {
                        "type": "object",
                        "properties": {
                            "clip": {"type": "asset", "accepts": ["video/mp4"]},
                            "tracks": {
                                "type": list_type,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "source": {
                                            "type": "asset",
                                            "accepts": [
                                                "audio/wav", "image/png", "audio/mpeg"
                                            ],
                                        },
                                        "caption": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "duplicate": {"type": "asset", "accepts": ["video/webm"]},
                    "count": {"type": "integer"},
                }
                self.assertEqual(
                    _derive_input_modalities(inputs, intent="smell.infer"),
                    ("text", "video", "audio", "image"),
                )

    def test_media_types_use_existing_mapping_without_intent_fallback(self) -> None:
        inputs = {
            "source": {
                "type": "asset",
                "accepts": ["text/plain", "application/json", "application/octet-stream"],
            }
        }
        self.assertEqual(
            _derive_input_modalities(inputs, intent="image.transcribe"),
            ("text", "asset"),
        )

    def test_asset_without_accepts_never_guesses_from_intent(self) -> None:
        for intent in ("image.edit", "audio.transcribe", "speech.recognize",
                       "sound.analyze", "music.analyze", "video.edit", "smell.infer"):
            for spec in ({"type": "asset"}, {"type": "asset", "accepts": []}):
                with self.subTest(intent=intent, spec=spec):
                    self.assertEqual(
                        _derive_input_modalities({"source": spec}, intent=intent),
                        ("asset",),
                    )

    def test_asset_leaf_families_are_shared_and_ordered(self) -> None:
        # Import inside the test: the base lacks this helper, rather than failing collection.
        from gateway.manifests import asset_input_modalities

        cases = (
            ({"accepts": "image/png"}, ("image",)),
            ({"accepts": ["audio/wav", "image/png", "audio/mpeg", "video/mp4"]},
             ("audio", "image", "video")),
            ({"accepts": ["*/*"]}, ("asset",)),
            ({"accepts": ["image/png", "*/*", "application/pdf"]}, ("image", "asset")),
            ({"accepts": ["text/plain", "application/json"]}, ("text",)),
            ({"accepts": "application/json"}, ("text",)),
            ({"media_type": "video/mp4"}, ("video",)),
            ({"accepts": [], "media_type": "audio/wav"}, ("audio",)),
            ({"accepts": ["image/png"], "media_type": "audio/wav"}, ("image",)),
            ({}, ("asset",)),
        )
        for declaration, expected in cases:
            spec = {"type": "asset", **declaration}
            with self.subTest(spec=spec):
                self.assertEqual(asset_input_modalities(spec), expected)
                self.assertEqual(
                    _derive_input_modalities({"source": spec}, intent="smell.infer"),
                    expected,
                )

    def test_schema_one_media_type_is_not_named_by_intent(self) -> None:
        self.assertEqual(
            _derive_input_modalities(
                {"source": {"type": "asset", "media_type": "audio/wav"}},
                intent="image.edit",
            ),
            ("audio",),
        )

    def test_nested_scalar_and_generic_assets_reach_required_modalities(self) -> None:
        from gateway.home_api import _required_input_modalities

        manifest = yaml.safe_load(CHAT_WORKFLOW_YAML)
        manifest["inputs"] = {
            "payload": {"type": "object", "required": True, "properties": {
                "frames": {"type": "array", "items": {"type": "asset", "accepts": "image/png"}},
                "opaque": {"type": "asset"},
            }},
            "optional_audio": {"type": "asset", "accepts": ["audio/wav"]},
        }
        cap = Capability(intent="video.edit", backend="fixture", workflow="fixture",
                         input_modalities=_derive_input_modalities(manifest["inputs"], intent="video.edit"))
        self.assertEqual(cap.input_modalities, ("image", "asset", "audio"))
        record = SimpleNamespace(id="fixture", contract=manifest)
        self.assertEqual(_required_input_modalities(cap, [record]), ["image", "asset"])


class ShippedManifestInputModalityTests(unittest.TestCase):
    """Shipped manifests: an asset's declared accepts names its modality."""

    EXPECTED = {
        "mesh.generate.trellis-2.yml": ("image", "text"),
        "video-ltx-2.5-i2v.yml": ("text", "image"),
        "video-ltx-2.5-keyframes-mid.yml": ("text", "image"),
        "video-ltx-2.5-keyframes.yml": ("text", "image"),
        "video-minimax-h3-i2v.yml": ("text", "image"),
        "video-minimax-h3-keyframes-mid.yml": ("text", "image"),
        "video-minimax-h3-keyframes.yml": ("text", "image"),
        "video-scail-2-replace.yml": ("image", "video", "text"),
    }

    def test_image_inputs_are_not_named_by_the_intent_family(self) -> None:
        folder = ROOT / "manifests" / "workflows"
        for name, expected in self.EXPECTED.items():
            with self.subTest(manifest=name):
                doc = yaml.safe_load((folder / name).read_text())
                self.assertEqual(
                    _derive_input_modalities(doc["inputs"], intent=doc["intent"]),
                    expected,
                )

    def test_every_shipped_asset_with_accepts_maps_to_its_media_family(self) -> None:
        folder = ROOT / "manifests" / "workflows"
        checked = 0
        for path in sorted(folder.glob("*.yml")):
            doc = yaml.safe_load(path.read_text())
            inputs = doc.get("inputs") if isinstance(doc, dict) else None
            if not isinstance(inputs, dict):
                continue
            for key, spec in inputs.items():
                accepts = spec.get("accepts") if isinstance(spec, dict) else None
                if not (isinstance(spec, dict) and spec.get("type") == "asset" and accepts):
                    continue
                families = {str(m).split("/", 1)[0] for m in (accepts if isinstance(accepts, list) else [accepts])}
                got = _derive_input_modalities({key: spec}, intent=str(doc.get("intent") or ""))
                with self.subTest(manifest=path.name, input=key):
                    self.assertEqual(set(got), families & {"image", "audio", "video", "text"} or {"asset"})
                checked += 1
        self.assertGreater(checked, 0)


# Frozen derivation from 1d749a9c9a2a4452e6771a166c6d8c6b4594039b.
# Keep this oracle independent of the shared helper; a new derivation must not bless itself.
def _base_input_modalities(
    inputs: dict[str, Any], *, intent: str
) -> tuple[str, ...]:
    modalities: list[str] = []

    def add(modality: str) -> None:
        if modality not in modalities:
            modalities.append(modality)

    def visit(spec: Any) -> None:
        if not isinstance(spec, dict):
            return
        type_name = str(spec.get("type") or "string")
        if type_name == "string":
            add("text")
        elif type_name == "asset":
            accepts = spec.get("accepts")
            if not accepts:
                add(_base_intent_asset_modality(intent) or "asset")
            else:
                media_types = accepts if isinstance(accepts, list) else [accepts]
                for media_type in media_types:
                    add(_base_media_type_modality(str(media_type)) or "asset")
        elif type_name == "object":
            properties = spec.get("properties")
            if isinstance(properties, dict):
                for child in properties.values():
                    visit(child)
        elif type_name in {"list", "array"}:
            visit(spec.get("items"))

    for spec in inputs.values():
        visit(spec)
    return tuple(modalities)


def _base_media_type_modality(media_type: str) -> str | None:
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("video/"):
        return "video"
    if media_type.startswith("text/") or media_type in {"application/json"}:
        return "text"
    return None


def _base_intent_asset_modality(intent: str) -> str | None:
    if intent.startswith("image."):
        return "image"
    if intent.startswith("audio.") or intent.startswith("speech.") or intent.startswith("sound.") or intent.startswith("music."):
        return "audio"
    if intent.startswith("video."):
        return "video"
    return None


class ShippedManifestBaseParityTests(unittest.TestCase):
    def test_all_shipped_capability_and_required_inputs_match_base(self) -> None:
        from gateway import capabilities, home_api

        folder = ROOT / "manifests" / "workflows"
        paths = sorted(path for path in folder.rglob("*") if path.suffix in {".yml", ".yaml"})
        self.assertTrue(paths, "No shipped manifests found; parity must not pass vacuously")
        for path in paths:
            with self.subTest(manifest=str(path.relative_to(ROOT))):
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertIsInstance(doc, dict)
                record = SimpleNamespace(
                    id=doc["id"], intent=doc["intent"], contract=doc,
                    supported_backends=tuple(sorted(doc["bindings"])),
                    identity=Provenance(True, "local"), facts=STANDARD_FACTS,
                )
                current = capability_from_workflow_record(record)
                current_required = home_api._required_input_modalities(current, [record])
                with patch.object(capabilities, "_derive_input_modalities", _base_input_modalities), \
                     patch.object(home_api, "_derive_input_modalities", _base_input_modalities):
                    base = capability_from_workflow_record(record)
                    base_required = home_api._required_input_modalities(base, [record])
                self.assertEqual(current.input_modalities, base.input_modalities)
                self.assertEqual(current_required, base_required)
                self.assertEqual(current.recordable_inputs, base.recordable_inputs)
                # All remaining capability fields are unchanged too.
                self.assertEqual(current, base)


class WorkflowDerivedCapabilityTests(unittest.TestCase):
    def test_image_edit_workflow_derives_text_plus_image_inputs(self) -> None:
        record = _record(CHAT_WORKFLOW_YAML)
        cap = capability_from_workflow_record(record)

        self.assertEqual(cap.intent, "image.edit")
        self.assertEqual(cap.workflow, "image.edit.controlnet.example")
        self.assertEqual(cap.backend, "comfyui")
        self.assertEqual(cap.input_modalities, ("text", "image"))
        self.assertEqual(cap.output_modalities, ("image",))
        self.assertEqual(cap.execution, "job")
        self.assertEqual(cap.resource_tier, "batch")
        self.assertFalse(cap.streaming)
        self.assertTrue(cap.resources.gpu_required)
        # Trusted manifest contributes no caveat.
        self.assertEqual(cap.caveats, ())

    def test_untrusted_manifest_adds_caveat(self) -> None:
        record = _record(UNTRUSTED_WORKFLOW_YAML, trusted=False, source="community")
        cap = capability_from_workflow_record(record, backend="a1111")

        self.assertEqual(cap.backend, "a1111")
        self.assertIn(UNREVIEWED_WORKFLOW_CAVEAT, cap.caveats)
        # The flag is the record's own claim, so the caveat names no reviewer
        # and no implementation noun, and gives one action.
        self.assertEqual(UNREVIEWED_WORKFLOW_CAVEAT,
                         "This workflow isn't marked as reviewed. Test it before you rely on it.")

    def test_backend_must_be_in_bindings(self) -> None:
        record = _record(CHAT_WORKFLOW_YAML)
        with self.assertRaises(ValueError):
            capability_from_workflow_record(record, backend="not-a-backend")

    def test_default_backend_is_first_sorted(self) -> None:
        record = _record(UNTRUSTED_WORKFLOW_YAML, trusted=False, source="community")
        cap = capability_from_workflow_record(record)
        self.assertEqual(cap.backend, "a1111")  # sorts before comfyui

    def test_rejects_invalid_execution(self) -> None:
        record = _record(CHAT_WORKFLOW_YAML)
        with self.assertRaises(ValueError):
            capability_from_workflow_record(record, execution="someday")

    def test_extra_caveats_are_carried(self) -> None:
        record = _record(CHAT_WORKFLOW_YAML)
        cap = capability_from_workflow_record(
            record, extra_caveats=("long warm-up",)
        )
        self.assertIn("long warm-up", cap.caveats)


class PolicyIntegrationTests(unittest.TestCase):
    def test_with_policy_decision_marks_unavailable_when_denied(self) -> None:
        cap = Capability(
            intent="image.edit",
            backend="cloud.openai",
            lifecycle="warm",
            health="healthy",
        )
        decision = PolicyDecision(
            decision=DENIED,
            sources=("profile:image-agent", "policy:local-only"),
            reasons=("cloud fallback disabled by profile",),
        )

        viewed = cap.with_policy_decision(decision)
        self.assertFalse(viewed.policy_available)
        self.assertFalse(viewed.available)
        self.assertEqual(viewed.policy_sources[0], "profile:image-agent")
        self.assertEqual(
            viewed.policy_reasons, ("cloud fallback disabled by profile",)
        )

    def test_with_policy_decision_marks_available_when_allowed(self) -> None:
        cap = Capability(intent="chat.generate", backend="llama.cpp.gpu1")
        decision = PolicyDecision(
            decision=ALLOWED,
            sources=("profile:default-local",),
            reasons=("no restrictions configured",),
        )
        viewed = cap.with_policy_decision(decision)
        self.assertTrue(viewed.policy_available)
        self.assertTrue(viewed.available)

    def test_capability_with_policy_view_uses_profile_evaluator(self) -> None:
        profile = access(plan_for("image-agent", caps=("image",)))
        permitted = Capability(intent="image.edit", backend="comfyui.local")
        denied = Capability(intent="chat.completions", backend="llama.cpp")

        permitted_view = capability_with_policy_view(permitted, profile)
        denied_view = capability_with_policy_view(denied, profile)

        self.assertTrue(permitted_view.policy_available)
        self.assertFalse(denied_view.policy_available)
        self.assertFalse(denied_view.available)
        self.assertIn("access_plan:image-agent", denied_view.policy_sources)

    def test_capability_with_policy_view_passes_metadata_selectors(self) -> None:
        profile = access(plan_for("family-chat", caps=("chat",)))
        permitted = Capability(
            intent="chat.completions",
            backend="llama.cpp",
            resource_tier="interactive",
        )
        denied = Capability(
            intent="image.generate",
            backend="comfyui",
            execution="job",
            resource_tier="batch",
        )

        self.assertTrue(
            capability_with_policy_view(permitted, profile).policy_available
        )
        self.assertFalse(
            capability_with_policy_view(denied, profile).policy_available
        )

    def test_capability_with_policy_view_honors_explicit_request(self) -> None:
        # Caller wants to layer cloud-fallback policy on top of capability
        # identity. The profile permits the backend but blocks cloud fallback.
        profile = access(plan_for("local-only"))
        cap = Capability(intent="chat.generate", backend="cloud.openai")
        request = PolicyRequest(
            backend="cloud.openai", requires_cloud_fallback=True
        )
        view = capability_with_policy_view(cap, profile, request=request)
        self.assertFalse(view.policy_available)
        self.assertTrue(
            any("when the engine is unavailable" in r for r in view.policy_reasons),
            view.policy_reasons,
        )

    def test_pure_module_does_not_run_evaluator_at_construction(self) -> None:
        # Constructing a capability must not depend on a profile or trigger
        # policy evaluation. We verify this by building a capability without
        # any policy import side-effect path and checking defaults.
        cap = Capability(intent="x", backend="b")
        self.assertTrue(cap.policy_available)
        self.assertEqual(cap.policy_sources, ())
        self.assertEqual(cap.policy_reasons, ())

    def test_default_profile_denies_cloud_capability_view(self) -> None:
        cap = Capability(intent="chat.generate", backend="cloud.openai")
        request = PolicyRequest(
            backend="cloud.openai", requires_cloud_fallback=True
        )
        view = capability_with_policy_view(cap, access(plan_for("default-local")), request=request)
        # Verify evaluator integration matches the policy module's behavior.
        direct = evaluate(access(plan_for("default-local")), request)
        self.assertEqual(direct.decision, DENIED)
        self.assertFalse(view.policy_available)


class UnavailableCapabilityTests(unittest.TestCase):
    def test_unavailable_records_carry_reason_and_lifecycle(self) -> None:
        cap = unavailable_capability(
            intent="video.generate",
            backend="comfyui.video",
            reason="backend not configured",
            lifecycle="stopped",
        )

        self.assertFalse(cap.available)
        self.assertEqual(cap.lifecycle, "stopped")
        self.assertEqual(cap.caveats, ("backend not configured",))

    def test_unavailable_summary_keeps_reason_shown_to_clients(self) -> None:
        cap = unavailable_capability(
            intent="image.upscale",
            backend="a1111.local",
            reason="adapter not registered",
            lifecycle="stopped",
        )
        client = summarize_capability(cap, audience="client")
        self.assertFalse(client["available"])
        self.assertEqual(client["caveats"], ["adapter not registered"])

    def test_unavailable_rejects_unknown_lifecycle(self) -> None:
        with self.assertRaises(ValueError):
            unavailable_capability(
                intent="x", backend="b", reason="why", lifecycle="napping"
            )

    def test_caveated_available_capability(self) -> None:
        # A capability can be available but still carry caveats — e.g.
        # tool-use is partial, structured output isn't supported.
        cap = Capability(
            intent="chat.generate",
            backend="llama.cpp.gpu1",
            caveats=("tool-use partial", "structured output not supported"),
            lifecycle="warm",
            health="healthy",
        )
        self.assertTrue(cap.available)
        self.assertEqual(len(cap.caveats), 2)


class ResourceEstimateTests(unittest.TestCase):
    def test_summary_drops_none_fields(self) -> None:
        est = ResourceEstimate(gpu_required=True)
        self.assertEqual(est.to_summary(), {"gpu_required": True})

    def test_summary_includes_populated_fields(self) -> None:
        est = ResourceEstimate(
            gpu_required=True,
            vram_gb_estimate=18.0,
            duration_class="long",
            max_parallel=1,
        )
        self.assertEqual(
            est.to_summary(),
            {
                "gpu_required": True,
                "vram_gb_estimate": 18.0,
                "duration_class": "long",
                "max_parallel": 1,
            },
        )


def test_missing_installed_files_are_hidden_from_client_discovery_but_explained_to_owner():
    cap = Capability(intent="chat.completions", backend="local", model="missing", availability={
        "available": False, "status": "unavailable", "reason": "Model file is not installed.",
        "evidence": {"kind": "missing_model_file"},
    })
    assert summarize_capabilities([cap], "client")["capabilities"] == []
    owner = summarize_capabilities([cap], "admin")["capabilities"][0]
    assert not owner["available"]
    assert owner["availability"]["reason"] == "Model file is not installed."

if __name__ == "__main__":
    unittest.main(verbosity=2)
