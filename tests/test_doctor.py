from __future__ import annotations

import json
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import doctor  # noqa: E402


VALID_MANIFEST = textwrap.dedent(
    """
    schema_version: 1
    id: image.generate.doctor
    intent: image.generate
    inputs:
      prompt:
        type: string
        required: true
      steps:
        type: integer
        minimum: 1
        maximum: 9
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/doctor.json
        map:
          prompt:
            - node: "6"
              input: text
          steps:
            - node: "10"
              input: steps
    """
).strip()


class DoctorTests(unittest.TestCase):
    def test_manifest_registry_reports_valid_fixture_directory(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            manifest_dir = Path(raw)
            (manifest_dir / "doctor.yml").write_text(VALID_MANIFEST, encoding="utf-8")

            result = doctor.check_manifest_registry(manifest_dir).to_dict()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["details"]["manifest_count"], 1)
        self.assertEqual(result["details"]["workflow_ids"], ["image.generate.doctor"])

    def test_workflow_binding_shape_uses_committed_artifact_without_network(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_dir = root / "manifests" / "workflows"
            workflow_dir = root / "workflows" / "images"
            manifest_dir.mkdir(parents=True)
            workflow_dir.mkdir(parents=True)
            (manifest_dir / "doctor.yml").write_text(VALID_MANIFEST, encoding="utf-8")
            (workflow_dir / "doctor.json").write_text(
                json.dumps(
                    {
                        "6": {"inputs": {"text": ""}},
                        "10": {"inputs": {"steps": 1}},
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(doctor, "ROOT", root):
                result = doctor.check_workflow_bindings(manifest_dir).to_dict()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["details"]["checked"], 1)

    def test_workflow_binding_shape_reports_missing_target(self) -> None:
        import tempfile

        broken = VALID_MANIFEST.replace('node: "10"', 'node: "404"')
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_dir = root / "manifests" / "workflows"
            workflow_dir = root / "workflows" / "images"
            manifest_dir.mkdir(parents=True)
            workflow_dir.mkdir(parents=True)
            (manifest_dir / "doctor.yml").write_text(broken, encoding="utf-8")
            (workflow_dir / "doctor.json").write_text(
                json.dumps({"6": {"inputs": {"text": ""}}, "10": {"inputs": {"steps": 1}}}),
                encoding="utf-8",
            )

            with mock.patch.object(doctor, "ROOT", root):
                result = doctor.check_workflow_bindings(manifest_dir).to_dict()

        self.assertEqual(result["status"], "fail")
        self.assertIn("ComfyUI binding target missing", result["details"]["errors"][0]["message"])

    def test_http_checks_are_skipped_without_base_url(self) -> None:
        result = doctor.check_http_json("public_health", None, "/health", 1).to_dict()

        self.assertEqual(result["status"], "warn")
        self.assertIn("skipped", result["message"])

    def test_http_check_uses_fetch_json_and_reports_keys_only(self) -> None:
        with mock.patch.object(
            doctor,
            "fetch_json",
            return_value=(200, {"status": "healthy", "token": "secret-value"}),
        ) as fetch:
            result = doctor.check_http_json("public_health", "http://proxy.test/", "/health", 1).to_dict()

        fetch.assert_called_once_with("http://proxy.test/health", 1)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["details"]["keys"], ["status", "token"])
        self.assertNotIn("secret-value", repr(result))

    def test_auth_env_reports_only_redacted_presence(self) -> None:
        result = doctor.check_auth_env({"OPENAI_PROXY_API_KEY": "sk-local-secret"}).to_dict()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["details"]["variables"], {"OPENAI_PROXY_API_KEY": "<redacted>"})
        self.assertNotIn("sk-local-secret", repr(result))

    def test_run_doctor_does_not_read_local_key_store(self) -> None:
        def forbidden_open(*args, **kwargs):  # type: ignore[no-untyped-def]
            path = str(args[0])
            if path.endswith("local-key-store.yml"):
                raise AssertionError("doctor must not read local key stores")
            return original_open(*args, **kwargs)

        original_open = Path.open
        with mock.patch.object(doctor, "check_manifest_registry", return_value=doctor.ok("manifest_registry", "ok")):
            with mock.patch.object(doctor, "check_workflow_bindings", return_value=doctor.ok("workflow_bindings", "ok")):
                with mock.patch.object(doctor, "fetch_json", return_value=(200, {"ok": True})):
                    with mock.patch.object(
                        doctor,
                        "check_auth_env",
                        return_value=doctor.warn("auth_env", "no auth environment variables detected"),
                    ):
                        with mock.patch.object(Path, "open", forbidden_open):
                            report = doctor.run_doctor(
                                base_url="http://proxy.test",
                                comfy_url="http://comfy.test",
                            )

        self.assertEqual(report["status"], "warn")
        self.assertNotIn("proxy_api_keys", repr(report))


if __name__ == "__main__":
    unittest.main()
