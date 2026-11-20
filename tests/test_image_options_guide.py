"""Keep the caller guide aligned with the workflow-owned image controls."""

import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "guides" / "IMAGE_OPTIONS.md"
SAMPLER_TEST = Path(__file__).with_name("test_image_sampler_controls.py")
KREA_MANIFEST = ROOT / "manifests" / "workflows" / "krea2-turbo.yml"
JSON_FENCE = re.compile(r"^\s*```json\s*\n(.*?)^\s*```\s*$", re.MULTILINE | re.DOTALL)
INLINE_LINK = re.compile(r"\[[^\]\n]+\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)")


def _guide():
    return GUIDE.read_text(encoding="utf-8")


def _left_out():
    spec = importlib.util.spec_from_file_location("_image_sampler_controls", SAMPLER_TEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LEFT_OUT


def test_guide_names_image_controls():
    text = _guide()
    missing = [name for name in ("steps", "sampler", "scheduler") if not re.search(rf"\b{name}\b", text)]
    assert not missing, f"missing image controls: {missing}"


def test_guide_names_workflows_without_sampler_controls():
    text = _guide()
    missing = sorted(workflow for workflow in _left_out() if workflow not in text)
    assert not missing, f"missing workflows without sampler controls: {missing}"


def _krea_inputs():
    return yaml.safe_load(KREA_MANIFEST.read_text(encoding="utf-8"))["inputs"]


def test_guide_workflow_example_uses_only_krea_manifest_inputs():
    blocks = JSON_FENCE.findall(_guide())
    assert blocks, "missing JSON image request example"
    options = []
    for block in blocks:
        body = json.loads(block)
        if not isinstance(body, dict) or not isinstance(body.get("extensions"), dict):
            continue
        workflow = body["extensions"].get("com.azizam.workflow")
        if isinstance(workflow, dict) and "steps" in workflow:
            options.append(workflow)
    assert options, "missing extensions.com.azizam.workflow example containing steps"
    inputs = _krea_inputs()
    for workflow in options:
        unknown = sorted(workflow.keys() - inputs.keys())
        assert not unknown, f"unrecognized krea2-turbo workflow inputs: {unknown}"
        for name, value in workflow.items():
            spec = inputs[name]
            if "minimum" in spec or "maximum" in spec:
                assert type(value) is int, f"{name}={value!r} must be an integer"
                if "minimum" in spec:
                    assert value >= spec["minimum"], f"{name}={value} is below minimum {spec['minimum']}"
                if "maximum" in spec:
                    assert value <= spec["maximum"], f"{name}={value} is above maximum {spec['maximum']}"
            if "enum" in spec:
                assert value in spec["enum"], f"{name}={value!r} is not a manifest enum value"


CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def _copied_choice_names(text):
    """Sampler and scheduler values the guide shows as code. Prose is skipped: values such as normal and simple are also
    plain English words."""
    code = "\n".join(CODE.findall(text))
    inputs = _krea_inputs()
    return sorted({name for control in ("sampler", "scheduler") for name in inputs[control]["enum"]
                   if re.search(rf"\b{re.escape(name)}\b", code)})


def test_guide_does_not_copy_model_specific_sampler_names():
    names = _copied_choice_names(_guide())
    assert not names, f"hard-coded sampler or scheduler names: {names}"


def test_copied_choice_names_reads_code_and_skips_prose():
    assert _copied_choice_names('Set `"sampler": "lms"` here.') == ["lms"]
    assert _copied_choice_names('```json\n{"scheduler": "karras"}\n```') == ["karras"]
    assert _copied_choice_names("Keep the normal, simple default.") == []


def test_guide_relative_links_resolve_to_repository_files():
    missing = []
    for href in INLINE_LINK.findall(_guide()):
        uri = urlsplit(href)
        if uri.scheme or uri.netloc or not uri.path or uri.path.startswith("/"):
            continue
        target = (GUIDE.parent / unquote(uri.path)).resolve()
        if not target.is_relative_to(ROOT) or not target.is_file():
            missing.append(href)
    assert not missing, f"broken image guide links: {missing}"
