"""Check the image-options guide against the repository's model contract."""
import json
import re
from pathlib import Path

import yaml

import test_image_sampler_controls as sampler_controls


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/guides/IMAGE_OPTIONS.md"
MANIFEST = ROOT / "manifests/workflows/krea2-turbo.yml"


def test_guide_names_each_sampler_control():
    guide = GUIDE.read_text(encoding="utf-8")
    for name in ("steps", "sampler", "scheduler"):
        assert f"`{name}`" in guide, f"guide must name the {name} input"


def test_guide_names_each_workflow_without_controls():
    guide = GUIDE.read_text(encoding="utf-8")
    for workflow in sampler_controls.LEFT_OUT:
        assert f"`{workflow}`" in guide, f"guide must name {workflow}"


def test_guide_example_uses_workflow_inputs():
    guide = GUIDE.read_text(encoding="utf-8")
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    examples = [json.loads(body) for body in re.findall(r"```json\s*\n(.*?)\n```", guide, re.S)]
    requests = [
        body for body in examples
        if isinstance(body, dict) and body.get("model") == manifest["id"]
    ]
    assert len(requests) == 1, "guide must contain one Krea 2 Turbo request example"
    request = requests[0]
    options = request["extensions"]["com.azizam.workflow"]
    assert "steps" in options, "example must set steps"
    assert set(options) <= manifest["inputs"].keys(), "example uses unknown workflow inputs"


def test_guide_does_not_hardcode_sampler_names():
    guide = GUIDE.read_text(encoding="utf-8").casefold()
    for sampler in ("euler", "dpmpp_2m", "res_multistep"):
        assert sampler not in guide, f"guide hardcodes sampler {sampler}"


def test_guide_relative_links_point_to_repository_files():
    guide = GUIDE.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)", guide):
        path = target.split("#", 1)[0]
        if not path or re.match(r"^(?:[a-zA-Z][\w+.-]*:|//)", path):
            continue
        resolved = (GUIDE.parent / path).resolve()
        assert resolved.is_relative_to(ROOT) and resolved.is_file(), f"broken link: {target}"
