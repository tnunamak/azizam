"""Move 19: the workflow prompts are reviewed text kept as versioned data (registry/workflow_prompts.yml).

The literals below are the strings the code produced before the move, captured from the running functions. A prompt
that changes by one byte fails here; to change a prompt, change the data and this pin together.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import patch

import pytest

import proxy
from gateway import workflow_prompts
from tests.support.access import auth_identity  # noqa: E402
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401

REPO = Path(__file__).resolve().parent.parent

DECIDER_SYSTEM_PROMPT = "Decide whether an image generation request should have its background removed and returned as a transparent PNG. Use the user's intent, not keyword matching. Return only JSON with keys remove_background boolean, confidence number 0..1, and reason string. Choose true for reusable assets such as logos, stickers, product cutouts, icons, isolated objects for commerce, or explicitly transparent-background intent. Choose false for portraits, landscapes, scenes, paintings, context-rich images, and ordinary photos where the environment is part of the result."
IMAGE_VARIATIONS = 'Create a creative variation of this image: keep the same subject, composition, and overall style, but vary fine detail and texture.'
SEAM_TAIL = '. Repair only the masked center seam cross so the texture wraps seamlessly on all edges. Preserve the source material, style, scale, lighting, and brush texture. Do not add borders, text, logos, objects, frames, or directional features.'


def test_image_variations_prompt_keeps_its_bytes() -> None:
    assert proxy.IMAGE_VARIATIONS_PROMPT == IMAGE_VARIATIONS


def test_tile_seam_prompt_keeps_its_bytes_for_two_example_prompts() -> None:
    assert proxy._tile_seam_prompt("a mossy stone wall", None) == 'a mossy stone wall. Repair only the masked center seam cross so the texture wraps seamlessly on all edges. Preserve the source material, style, scale, lighting, and brush texture. Do not add borders, text, logos, objects, frames, or directional features.'
    assert proxy._tile_seam_prompt("tileable oak bark, soft light", "") == 'tileable oak bark, soft light. Repair only the masked center seam cross so the texture wraps seamlessly on all edges. Preserve the source material, style, scale, lighting, and brush texture. Do not add borders, text, logos, objects, frames, or directional features.'


def test_tile_seam_prompt_inserts_the_prompt_verbatim_even_with_braces() -> None:
    assert proxy._tile_seam_prompt("a {wall} {0}", None) == "a {wall} {0}" + SEAM_TAIL


def test_tile_seam_prompt_keeps_an_explicit_seam_prompt() -> None:
    assert proxy._tile_seam_prompt("ignored", "  my own seam text ") == "my own seam text"


def _decide(fake_upstreams) -> dict:
    fake = fake_upstreams.asgi(load_scenario("openai-compatible/json-decision", model="bg-decider-model"), host="bg.fk.test")

    async def active_backend() -> str:
        return "kobold"

    async def model_for_backend(_backend: str) -> str:
        return "bg-decider-model"

    auth = proxy.AuthContext(identity=auth_identity(name="p", key_hash="sha256:p"))
    with patch.object(proxy, "_image_background_removal_enabled", lambda: True), \
         patch.object(proxy, "get_active_backend", active_backend), \
         patch.dict(proxy.DERIVED.LLM_BACKEND_HOSTS, {"kobold": fake.base_url}), \
         patch.object(proxy, "current_llm_model_for_backend", model_for_backend):
        asyncio.run(proxy.decide_image_background_removal("A logo", auth_context=auth))
    [sent] = fake.calls("chat")
    return sent.json


def test_background_removal_decider_sends_the_pinned_system_prompt(fake_upstreams) -> None:
    system, user = _decide(fake_upstreams)["messages"]
    assert system == {"role": "system", "content": DECIDER_SYSTEM_PROMPT}
    assert user == {"role": "user", "content": "A logo"}


# --- the code follows the data -------------------------------------------------------------------------------------

def _with_prompt(monkeypatch, name: str, text: str) -> None:
    prompts = dict(workflow_prompts._PROMPTS)
    prompts[name] = workflow_prompts.Prompt(prompts[name].version, prompts[name].slots, text)
    monkeypatch.setattr(workflow_prompts, "_PROMPTS", prompts)


def test_the_decider_sends_whatever_the_data_says(monkeypatch, fake_upstreams) -> None:
    _with_prompt(monkeypatch, "image_background_removal_decider", "decide it differently")
    system, _ = _decide(fake_upstreams)["messages"]
    assert system["content"] == "decide it differently"


def test_the_seam_repair_follows_the_data_and_fills_the_slot(monkeypatch) -> None:
    _with_prompt(monkeypatch, "tile_seam_repair", "{prompt} -- mend the seam")
    assert proxy._tile_seam_prompt("bark", None) == "bark -- mend the seam"


def test_the_proxy_constant_is_the_data_text() -> None:
    assert proxy.IMAGE_VARIATIONS_PROMPT == workflow_prompts.render("image_variations")


def test_no_source_file_keeps_a_second_copy_of_a_prompt() -> None:
    """The two former copies are one: the text lives in the data file only."""
    for relative, fragments in (
        ("proxy.py", ("Decide whether an image generation", "Create a creative variation", "Repair only the masked center seam")),
        ("gateway/voices_api.py", ("Hello, and welcome",)),
    ):
        source = (REPO / relative).read_text(encoding="utf-8")
        for fragment in fragments:
            assert fragment not in source, (relative, fragment)


def test_every_prompt_has_a_version() -> None:
    assert {name: workflow_prompts.version(name) for name in workflow_prompts._PROMPTS} == {
        "image_background_removal_decider": 1, "image_variations": 1, "tile_seam_repair": 1, "voice_design_sample_script": 1,
    }


def test_a_text_that_uses_an_undeclared_slot_is_refused(tmp_path) -> None:
    text = (REPO / "registry/workflow_prompts.yml").read_text(encoding="utf-8").replace("{prompt}. Repair", "{promt}. Repair")
    bad = tmp_path / "workflow_prompts.yml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="uses slots"):
        workflow_prompts.load_prompts(bad)


def test_a_slot_that_is_not_given_is_refused() -> None:
    with pytest.raises(ValueError, match="takes slots"):
        workflow_prompts.render("tile_seam_repair")
