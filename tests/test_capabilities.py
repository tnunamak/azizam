"""Tests for the pure capability model."""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from gateway.capabilities import (  # noqa: E402
    Capability,
    ResourceEstimate,
    capability_from_workflow_record,
    capability_with_policy_view,
    summarize_capabilities,
    summarize_capability,
    unavailable_capability,
)


from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.policy import (  # noqa: E402
    ALLOWED,
    DENIED,
    AppProfile,
    CapabilitySelector,
    PolicyDecision,
    PolicyRequest,
    evaluate,
)


CHAT_WORKFLOW_YAML = textwrap.dedent(
    """
    schema_version: 1
    version: "0.1.0"
    id: image.edit.controlnet.example
    intent: image.edit
    source: local
    trusted: true
    inputs:
      prompt:
        type: string
        required: true
      source_image:
        type: asset
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
    schema_version: 1
    version: "0.1.0"
    id: image.generate.untrusted.example
    intent: image.generate
    source: community
    trusted: false
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


def _record(yaml_text: str):
    manifest = yaml.safe_load(yaml_text)
    registry = WorkflowRegistry()
    return registry.add_manifest(manifest, source_path="fixture.yml")


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
            caveats=("workflow manifest is untrusted",),
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
        self.assertEqual(client["caveats"], ["workflow manifest is untrusted"])

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
        record = _record(UNTRUSTED_WORKFLOW_YAML)
        cap = capability_from_workflow_record(record, backend="a1111")

        self.assertEqual(cap.backend, "a1111")
        self.assertIn("workflow manifest is untrusted", cap.caveats)

    def test_backend_must_be_in_bindings(self) -> None:
        record = _record(CHAT_WORKFLOW_YAML)
        with self.assertRaises(ValueError):
            capability_from_workflow_record(record, backend="not-a-backend")

    def test_default_backend_is_first_sorted(self) -> None:
        record = _record(UNTRUSTED_WORKFLOW_YAML)
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
        profile = AppProfile(
            id="image-agent",
            backends=frozenset({"comfyui.local"}),
        )
        permitted = Capability(intent="image.edit", backend="comfyui.local")
        denied = Capability(intent="image.edit", backend="cloud.openai")

        permitted_view = capability_with_policy_view(permitted, profile)
        denied_view = capability_with_policy_view(denied, profile)

        self.assertTrue(permitted_view.policy_available)
        self.assertFalse(denied_view.policy_available)
        self.assertFalse(denied_view.available)
        self.assertIn("profile:image-agent", denied_view.policy_sources)

    def test_capability_with_policy_view_passes_metadata_selectors(self) -> None:
        profile = AppProfile(
            id="family-chat",
            allow=CapabilitySelector(
                intents=frozenset({"chat.completions"}),
                exposure=frozenset({"default"}),
                trust_tiers=frozenset({"stable"}),
                resource_tiers=frozenset({"interactive"}),
            ),
        )
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
        profile = AppProfile(
            id="local-only",
            backends=frozenset({"cloud.openai"}),
        )
        cap = Capability(intent="chat.generate", backend="cloud.openai")
        request = PolicyRequest(
            backend="cloud.openai", requires_cloud_fallback=True
        )
        view = capability_with_policy_view(cap, profile, request=request)
        self.assertFalse(view.policy_available)
        self.assertTrue(
            any("cloud fallback" in r for r in view.policy_reasons),
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
        from gateway.policy import DEFAULT_PROFILE

        cap = Capability(intent="chat.generate", backend="cloud.openai")
        request = PolicyRequest(
            backend="cloud.openai", requires_cloud_fallback=True
        )
        view = capability_with_policy_view(cap, DEFAULT_PROFILE, request=request)
        # Verify evaluator integration matches the policy module's behavior.
        direct = evaluate(DEFAULT_PROFILE, request)
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

    def test_unavailable_summary_keeps_reason_visible_to_clients(self) -> None:
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
