"""Generated defaults (CODE_TO_DATA_S3 D6, step S3.6): the reviewed generators, one draw per request, the stored value.

Four things are held here:

- the module: a draw fills only an input the caller left empty, stays inside the rule's range intersected with the input's
  bounds, reads a stored value back, and takes an injected source;
- the routes: for every bundled workflow, a route draws exactly where it drew before this change, with the same range
  (the old rule is written out below as the oracle), and no route names the input it generates;
- the job: a value is drawn once when the job is accepted, kept in its envelope, read by every retry and restart, and
  emptied by erasure;
- the draws that stay: every other integer draw in the gateway is listed with its reason.
"""

from __future__ import annotations

import ast
import asyncio
import json
import math
import re
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch

import pytest
import yaml

import proxy
from gateway import generated_defaults, media_jobs
from gateway.definition_store import DefinitionStore
from gateway.engine_protocol import audio_graph
from gateway.job_envelope import build_job_envelope
from gateway.manifests import ManifestError
from gateway.workflow_contract import GENERATORS
from tests.support.access import access_context, authenticated

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_LOW, PUBLIC_HIGH = 0, 2**32 - 1


def rule(low: Any = PUBLIC_LOW, high: Any = PUBLIC_HIGH) -> dict[str, Any]:
    return {"generator": "random_integer", "minimum": low, "maximum": high}


def inputs_with(**spec: Any) -> dict[str, Any]:
    return {"n": {"type": "integer", "generate": rule(), **spec}}


class Recorder:
    """An injected source that records the range it was asked for and gives the lowest value in it."""

    def __init__(self, value: int | None = None) -> None:
        self.asked: list[tuple[int, int]] = []
        self.value = value

    def __call__(self, low: int, high: int) -> int:
        self.asked.append((low, high))
        return low if self.value is None else self.value


def refuse_a_draw(low: int, high: int) -> int:
    raise AssertionError(f"a value was drawn from {low}..{high}; it should have been read or given")


# ---------------------------------------------------------------------------------------------------------- module


@pytest.mark.parametrize(("spec", "expected"), [
    ({}, (PUBLIC_LOW, PUBLIC_HIGH)),
    ({"minimum": 3, "maximum": 9}, (3, 9)),
    ({"minimum": 3.5, "maximum": 9.5}, (4, 9)),
    ({"minimum": -10, "maximum": 2**40}, (PUBLIC_LOW, PUBLIC_HIGH)),
], ids=["no-input-bounds", "narrower-input", "fractional-bounds", "wider-input"])
def test_a_draw_comes_from_the_rule_bounds_intersected_with_the_input_bounds(spec, expected):
    source = Recorder()
    generated_defaults.resolve_generated(inputs_with(**spec), {}, source=source)
    assert source.asked == [expected]


def test_a_narrower_rule_narrows_the_range():
    source = Recorder()
    generated_defaults.resolve_generated({"n": {"type": "integer", "generate": rule(10, 20)}}, {}, source=source)
    assert source.asked == [(10, 20)]


def test_a_draw_is_stored_with_its_generator_id():
    block = generated_defaults.resolve_generated(inputs_with(), {}, source=Recorder(7))
    assert block == {"n": {"generator": "random_integer", "value": 7}}


def test_nothing_is_drawn_for_a_value_the_caller_gave():
    assert generated_defaults.resolve_generated(inputs_with(), {"n": 5}, source=refuse_a_draw) == {}
    assert generated_defaults.resolved_values(inputs_with(), {"n": 5}, source=refuse_a_draw) == {"n": 5}


def test_a_caller_cannot_set_an_input_only_the_gateway_fills():
    inputs = inputs_with(settable=False)
    source = Recorder(9)
    assert generated_defaults.resolved_values(inputs, {"n": 5}, source=source) == {"n": 9}
    assert len(source.asked) == 1


def test_a_stored_value_is_read_back_and_never_drawn_again():
    stored = {"n": {"generator": "random_integer", "value": 41}}
    block = generated_defaults.resolve_generated(inputs_with(), {}, stored=stored, source=refuse_a_draw)
    assert block == stored


def test_a_stored_value_from_another_generator_is_refused_not_redrawn():
    stored = {"n": {"generator": "something_else", "value": 41}}
    with pytest.raises(generated_defaults.GeneratedValueUnavailable) as raised:
        generated_defaults.resolve_generated(inputs_with(), {}, stored=stored, source=refuse_a_draw)
    assert raised.value.erased is False


def test_an_erased_stored_value_is_refused_and_never_drawn_again():
    stored = {"n": {"generator": "random_integer", "erased": True}}
    with pytest.raises(generated_defaults.GeneratedValueUnavailable) as raised:
        generated_defaults.resolve_generated(inputs_with(), {}, stored=stored, source=refuse_a_draw)
    assert raised.value.erased is True


@pytest.mark.parametrize("entry", [{"generator": "random_integer"}, {}, "text", None],
                         ids=["no-value", "empty", "not-an-object", "null"])
def test_a_stored_entry_that_cannot_be_read_is_refused_and_never_redrawn(entry):
    with pytest.raises(generated_defaults.GeneratedValueUnavailable) as raised:
        generated_defaults.resolve_generated(inputs_with(), {}, stored={"n": entry}, source=refuse_a_draw)
    assert raised.value.erased is False


@pytest.mark.parametrize("value", [3, 99, "7", True, 4.0], ids=["off-the-step-grid", "out-of-range", "string", "bool", "float"])
def test_a_stored_value_that_does_not_fit_the_input_is_refused_and_never_redrawn(value):
    inputs = inputs_with(type="integer", step=2, offset=0, minimum=0, maximum=10)
    stored = {"n": {"generator": "random_integer", "value": value}}
    with pytest.raises(generated_defaults.GeneratedValueUnavailable) as raised:
        generated_defaults.resolve_generated(inputs, {}, stored=stored, source=refuse_a_draw)
    assert raised.value.erased is False


def test_a_stored_value_outside_the_rules_own_bounds_is_refused():
    inputs = inputs_with(generate=rule(0, 10))
    stored = {"n": {"generator": "random_integer", "value": 99}}
    with pytest.raises(generated_defaults.GeneratedValueUnavailable):
        generated_defaults.resolve_generated(inputs, {}, stored=stored, source=refuse_a_draw)


def test_a_stored_value_that_fits_the_input_is_read_back():
    stored = {"n": {"generator": "random_integer", "value": 4}}
    inputs = inputs_with(type="integer", step=2, offset=0, minimum=0, maximum=10)
    assert generated_defaults.resolve_generated(inputs, {}, stored=stored, source=refuse_a_draw) == stored


def test_an_input_with_no_stored_entry_still_draws_for_a_job_made_before_values_were_kept():
    source = Recorder(17)
    assert generated_defaults.resolve_generated(inputs_with(), {}, stored={}, source=source)["n"]["value"] == 17
    assert len(source.asked) == 1


@pytest.mark.parametrize(("low", "high", "base", "offset", "expected"), [
    (0, 9, 9, 1, 0),            # the upper bound wraps to the lower bound
    (0, 9, 9, 0, 9),            # no offset: the base itself
    (0, 9, 8, 5, 3),
    (0, 9, 3, 25, 8),           # more offsets than the range holds
    (4, 4, 4, 0, 4),            # a range of one has one value
    (4, 4, 4, 7, 4),
    (3, 4, 4, 1, 3),            # a range of two alternates
    (3, 4, 3, 1, 4),
    (3, 4, 3, 2, 3),
    (PUBLIC_LOW, PUBLIC_HIGH, PUBLIC_HIGH, 1, PUBLIC_LOW),  # the public maximum no longer reaches 2**32
    (PUBLIC_LOW, PUBLIC_HIGH, PUBLIC_HIGH - 1, 3, 1),
], ids=str)
def test_a_batch_round_wraps_its_value_inside_the_inputs_declared_range(low, high, base, offset, expected):
    inputs = {"n": {"type": "integer", "minimum": low, "maximum": high, "generate": rule()}}
    assert generated_defaults.offset_values(inputs, {"n": base}, offset) == {"n": expected}


def test_a_batch_round_stays_inside_a_range_the_input_narrows_below_the_rule():
    inputs = {"n": {"type": "integer", "minimum": 10, "maximum": 12, "generate": rule()}}
    values = [generated_defaults.offset_values(inputs, {"n": 12}, offset)["n"] for offset in range(7)]
    assert values == [12, 10, 11, 12, 10, 11, 12]


def test_a_batch_round_uses_the_rule_bounds_when_the_input_names_none():
    inputs = {"n": {"type": "integer", "generate": rule(5, 6)}}
    assert [generated_defaults.offset_values(inputs, {"n": 5}, offset)["n"] for offset in range(3)] == [5, 6, 5]


def test_a_range_with_no_integer_is_a_fault_of_the_document():
    with pytest.raises(ManifestError):
        generated_defaults.resolve_generated(inputs_with(minimum=2**32, maximum=2**33), {}, source=Recorder())
    with pytest.raises(ManifestError):
        generated_defaults.resolve_generated(inputs_with(minimum=3.2, maximum=3.8), {}, source=Recorder())


def test_a_generator_no_reviewed_code_backs_is_refused_at_run():
    inputs = {"n": {"type": "integer", "generate": {"generator": "unreviewed"}}}
    with pytest.raises(ManifestError):
        generated_defaults.resolve_generated(inputs, {}, source=Recorder())


def test_a_drawn_value_the_input_does_not_accept_is_refused():
    with pytest.raises(ManifestError):
        generated_defaults.resolve_generated(inputs_with(step=2, offset=0), {}, source=lambda low, high: 3)


def test_the_default_source_is_operating_system_randomness(monkeypatch):
    asked: list[int] = []
    monkeypatch.setattr(secrets, "randbelow", lambda bound: asked.append(bound) or 4)
    assert generated_defaults.secure_source(10, 19) == 14
    assert asked == [10]
    value = generated_defaults.resolve_generated(inputs_with(), {})["n"]["value"]
    assert PUBLIC_LOW <= value <= PUBLIC_HIGH


def test_the_reviewed_generators_are_the_ones_the_contract_checks_at_save():
    assert set(generated_defaults.REVIEWED) == set(GENERATORS)
    assert generated_defaults.persistence() == {"random_integer": "value"}


def test_a_route_that_records_one_value_reads_the_first_generated_input():
    inputs = {"a": {"type": "string"}, "b": {"type": "integer", "generate": rule()}, "c": {"type": "integer", "generate": rule()}}
    assert generated_defaults.first_value(inputs, {"c": 3, "b": 2, "a": "x"}) == 2
    assert generated_defaults.first_value(inputs, {"a": "x"}) is None


# ---------------------------------------------------------------------------------------- the routes, differential


def bundled(*intents: str) -> list[pytest.param]:
    found = []
    for path in sorted((ROOT / "manifests" / "workflows").glob("*.yml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        binding = manifest["bindings"].get("comfyui") or {}
        if manifest["intent"] in intents and binding.get("workflow") and "compose" not in binding and "role" not in binding:
            found.append(pytest.param(manifest, id=path.stem))
    return found


def drew_before_by_audio_route(manifest: Mapping[str, Any]) -> tuple[int, int] | None:
    """The old audio edge: it drew when the manifest declared `seed`, from the public range and the declared bounds."""
    spec = manifest["inputs"].get("seed")
    if spec is None:
        return None
    return max(0, math.ceil(spec.get("minimum", 0))), min(2**32 - 1, math.floor(spec.get("maximum", 2**32 - 1)))


def drew_before_by_image_route(manifest: Mapping[str, Any]) -> tuple[int, int] | None:
    """The old image rounds: they drew from 0 to 2**32-1 when the graph binding mapped `seed`."""
    mapped = (manifest["bindings"]["comfyui"].get("map") or {}).get("seed")
    return (0, 2**32 - 1) if mapped else None


def draw_now_by_audio_route(manifest: Mapping[str, Any], body: Mapping[str, Any]) -> list[tuple[int, int]]:
    source = Recorder()
    effective = audio_graph.effective_manifest(manifest, audio_graph.AudioLimits())
    audio_graph.manifest_inputs({"prompt": "x", **body}, manifest=effective, source=source)
    return source.asked


def draw_now_by_image_route(manifest: Mapping[str, Any], body: Mapping[str, Any]) -> list[tuple[int, int]]:
    source = Recorder()
    with patch.object(generated_defaults, "secure_source", source):
        proxy._image_generated_values(manifest, body)
    return source.asked


@pytest.mark.parametrize("manifest", bundled("audio.generate"))
def test_the_audio_route_draws_exactly_where_it_drew_before_with_the_same_range(manifest):
    before = drew_before_by_audio_route(manifest)
    assert draw_now_by_audio_route(manifest, {}) == ([before] if before else [])
    assert draw_now_by_audio_route(manifest, {"seed": 5}) == [], "a caller's own value is never drawn over"


@pytest.mark.parametrize("manifest", bundled("image.generate"))
def test_the_image_generation_route_draws_exactly_where_it_drew_before_with_the_same_range(manifest):
    before = drew_before_by_image_route(manifest)
    assert draw_now_by_image_route(manifest, {}) == ([before] if before else [])
    given = {"extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"seed": 5}}}
    expected_after_giving = [before] if before and manifest["inputs"]["seed"].get("settable") is False else []
    assert draw_now_by_image_route(manifest, given) == expected_after_giving


@pytest.mark.parametrize("manifest", bundled("image.edit"))
def test_the_image_edit_route_draws_exactly_where_it_drew_before_with_the_same_range(manifest):
    before = drew_before_by_image_route(manifest)
    assert draw_now_by_image_route(manifest, {}) == ([before] if before else [])
    given = {"extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"seed": 5}}}
    expected_after_giving = [before] if before and manifest["inputs"]["seed"].get("settable") is False else []
    assert draw_now_by_image_route(manifest, given) == expected_after_giving


def test_the_mesh_route_still_sends_no_value_when_the_caller_gives_none():
    manifest = yaml.safe_load((ROOT / "manifests" / "workflows" / "mesh.generate.trellis-2.yml").read_text(encoding="utf-8"))
    assert generated_defaults.generated_inputs(manifest["inputs"]) == {}


# ------------------------------------------------------------------------------------------------- the token test


def word_hits(text: str, word: str) -> list[str]:
    # A `seed=` keyword argument is the receipt's own field name (the provenance record), not a selection by name.
    text = re.sub(r"\b" + re.escape(word) + r"=", "", text)
    return re.findall(r"(?<![a-z0-9_-])" + re.escape(word) + r"(?![a-z0-9_-])", text.lower())


def function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"{path.name} has no function {name}")


# Code that reads or fills a generated input for a route. None may name the input; the document names it.
GENERIC_PATHS = {
    ROOT / "proxy.py": [
        "_image_generated_values", "_bind_generated_image_inputs", "_accepted_generated", "_stored_generated",
        "_generate_images_with_adaptive_batching", "proxy_audio_generate", "_submit_audio_media_job",
        "gateway_image_generations_media_job", "_run_audio_media_job", "_run_image_media_job",
    ],
    ROOT / "gateway" / "engine_protocol" / "audio_graph.py": ["manifest_inputs", "generated_block"],
    ROOT / "gateway" / "definition_store.py": ["clear_generated_in"],
    ROOT / "gateway" / "media_jobs.py": ["_clear_generated_in"],
}


def test_the_token_test_sees_the_word():
    assert word_hits('if name == "seed":', "seed") == ["seed"]
    assert word_hits("legacy_seed_field and Re-seed and seeded", "seed") == []


def test_no_core_code_names_seed():
    assert word_hits((ROOT / "gateway" / "generated_defaults.py").read_text(encoding="utf-8"), "seed") == []
    for path, functions in GENERIC_PATHS.items():
        for name in functions:
            assert word_hits(function_source(path, name), "seed") == [], f"{path.name}:{name} names the input it generates"


# ----------------------------------------------------------------------------------------------- the draws that stay

# Every integer draw in proxy.py and gateway/: (file, function) -> (how many, why it stays).
LISTED_DRAWS = {
    ("proxy.py", "_proxy_graph_video_create"): (2, "the video route draws its own value; video moves with the video workflow slice"),
    ("proxy.py", "_submit_video_chain"): (1, "the video chain route draws its own value; video moves with the video workflow slice"),
    ("proxy.py", "_reseed_image_workflow"): (2, "a graph with no contract (graph metadata path) has no generate rule; the offset tile composition draws per tile; both go with their roles"),
    ("proxy.py", "_proxy_offset_inpaint_tile_request"): (1, "the offset tile composition's seam pass draws its own value; the composition is a reviewed role"),
    ("gateway/a1111_api.py", "map_payload"): (1, "an A1111 client's own frontend field, mapped for the route; a frontend profile, not a document"),
    ("gateway/avatar_generation.py", "new_seed"): (1, "a person's avatar number, not a workflow input"),
    ("gateway/generated_defaults.py", "secure_source"): (1, "the reviewed generators' own source, the one place a generated value is drawn"),
    ("gateway/stt_measure.py", "paired_excess"): (1, "bootstrap resampling in a measurement, not an input"),
    ("gateway/stt_measure.py", "rate_interval"): (1, "bootstrap resampling in a measurement, not an input"),
    ("gateway/stt_measure.py", "difference"): (2, "bootstrap resampling in a measurement, not an input"),
    ("gateway/workflow_equality.py", "generated_value"): (1, "the equality tool's reproducible stand-in for a generator, seeded by workflow and input"),
}
DRAW_CALLS = {"randint", "randrange", "getrandbits", "randbelow", "randbits"}
DRAW_MODULES = {"random", "secrets"}


def draw_sites(text: str, path: str) -> dict[tuple[str, str], int]:
    """Where a source text draws a whole number, by (file, function). A site is any of:

    - an attribute named like a draw (``random.randint``, ``r.randint`` after ``import random as r``, ``rng.randrange``),
      whether it is called or only referred to (``pick = random.randint``);
    - a name bound by ``from random import randint`` or ``from secrets import randbelow as pick``, under whatever alias,
      each time it is used;
    - ``from random import *`` or a dynamic import of ``random``/``secrets``, which hide the names, so they count as
      sites of their own."""
    tree = ast.parse(text)
    drawn_names: set[str] = set()
    random_modules: set[str] = set()
    importers = {"__import__", "import_module"}  # names that load a module by its text, under any alias
    found: dict[tuple[str, str], int] = {}

    def add(owner: str, count: int = 1) -> None:
        found[(path, owner)] = found.get((path, owner), 0) + count

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            random_modules.update(alias.asname or alias.name for alias in node.names if alias.name in DRAW_MODULES)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in DRAW_MODULES:
            for alias in node.names:
                if alias.name in DRAW_CALLS:
                    drawn_names.add(alias.asname or alias.name)
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            importers.update(alias.asname for alias in node.names if alias.name == "import_module" and alias.asname)

    def walk(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            scope = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
            if isinstance(child, ast.Attribute) and child.attr in DRAW_CALLS:
                add(owner)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id in drawn_names:
                add(owner)
            elif isinstance(child, ast.ImportFrom) and child.module and child.module.split(".")[0] in DRAW_MODULES \
                    and any(alias.name == "*" for alias in child.names):
                add(f"<star import from {child.module}>")
            elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "getattr" \
                    and len(child.args) >= 2 and isinstance(child.args[0], ast.Name) \
                    and child.args[0].id in random_modules and isinstance(child.args[1], ast.Constant) \
                    and child.args[1].value in DRAW_CALLS:
                add(owner)
            elif isinstance(child, ast.Call) and isinstance(child.func, (ast.Name, ast.Attribute)) \
                    and getattr(child.func, "id", getattr(child.func, "attr", "")) in importers:
                module = child.args[0] if child.args else next(
                    (keyword.value for keyword in child.keywords if keyword.arg == "name"), None)
                if isinstance(module, ast.Constant) and module.value in DRAW_MODULES:
                    add(f"<dynamic import of {module.value}>")
            walk(child, scope)

    walk(tree, "<module>")
    return found


def integer_draws() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py"))]:
        found.update(draw_sites(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT))))
    return found


# Every way a draw can be written that the ratchet must see (positive controls), and look-alikes it must not (negative).
EVASIONS = {
    "attribute call": "import random\ndef f():\n    return random.randint(1, 2)\n",
    "module alias": "import random as r\ndef f():\n    return r.randint(1, 2)\n",
    "secrets module alias": "import secrets as s\ndef f():\n    return s.randbelow(5)\n",
    "from-import": "from random import randint\ndef f():\n    return randint(1, 2)\n",
    "from-import alias": "from random import randint as draw\ndef f():\n    return draw(1, 2)\n",
    "secrets from-import alias": "from secrets import randbelow as pick\ndef f():\n    return pick(5)\n",
    "reference without a call": "import random\ndef f():\n    draw = random.randint\n    return draw(1, 2)\n",
    "alias used as a value": "from random import randrange as draw\ndef f():\n    return [draw]\n",
    "an instance": "import random\ndef f():\n    return random.SystemRandom().getrandbits(8)\n",
    "star import": "from random import *\n",
    "secrets randbits attribute": "import secrets\ndef f():\n    return secrets.randbits(8)\n",
    "secrets randbits from-import": "from secrets import randbits\ndef f():\n    return randbits(8)\n",
    "secrets randbits aliased from-import": "from secrets import randbits as bits\ndef f():\n    return bits(8)\n",
    "aliased import_module": "from importlib import import_module as load\ndef f():\n    return load('secrets')\n",
    "keyword aliased import_module": "from importlib import import_module as load\ndef f():\n    return load(name='secrets')\n",
    "literal getattr draw": "import secrets\ndef f():\n    return getattr(secrets, 'randbits')(8)\n",
    "literal getattr module alias": "import random as r\ndef f():\n    return getattr(r, 'randint')(1, 2)\n",
    "importlib attribute": "import importlib\ndef f():\n    return importlib.import_module('random')\n",
    "dynamic import": "def f():\n    return __import__('random').randint(1, 2)\n",
}
LOOK_ALIKES = {
    "unused import": "import random\nfrom random import randint\n",
    "other function of the module": "import random\nfrom random import seed as reseed\ndef f():\n    random.seed(1)\n    reseed(2)\n",
    "a local function with the name": "def randint(a, b):\n    return a\ndef f():\n    return randint(1, 2)\n",
    "another module's name": "from other import randint as draw\ndef f():\n    return draw(1, 2)\n",
    "aliased import_module of another module": "from importlib import import_module as load\ndef f():\n    return load('json')\n",
    "getattr unrelated attribute": "import secrets\ndef f():\n    return getattr(secrets, 'token_hex')(8)\n",
    "getattr unrelated module": "import other\ndef f():\n    return getattr(other, 'randbits')(8)\n",
    "keyword unrelated function": "def load(name):\n    return name\ndef f():\n    return load(name='secrets')\n",
    "a name that is only like an alias": "from random import randint as draw\ndef f(drawn):\n    return drawn\n",
}


@pytest.mark.parametrize("source", EVASIONS.values(), ids=EVASIONS.keys())
def test_the_draw_ratchet_sees_every_way_to_draw(source):
    assert sum(draw_sites(source, "x.py").values()) >= 1


@pytest.mark.parametrize("source", LOOK_ALIKES.values(), ids=LOOK_ALIKES.keys())
def test_the_draw_ratchet_does_not_flag_look_alikes(source):
    assert draw_sites(source, "x.py") == {}


def test_the_draw_ratchet_counts_each_site_once_and_names_its_function():
    source = "from random import randint as draw\nimport random\ndef f():\n    return draw(1, 2) + random.randint(1, 2)\n"
    assert draw_sites(source, "x.py") == {("x.py", "f"): 2}


def test_every_integer_draw_in_the_gateway_is_listed_with_its_reason():
    assert integer_draws() == {key: count for key, (count, _why) in LISTED_DRAWS.items()}
    assert all(why for _count, why in LISTED_DRAWS.values())


# ------------------------------------------------------------------------------------------------------------ the job


class StubRequest:
    method = "POST"
    scope: dict[str, Any] = {"auth_context": access_context()}
    query_params: dict[str, str] = {}

    def __init__(self, body: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> None:
        self._body = dict(body)
        self.headers = dict(headers or {"host": "gateway.test"})
        self.url = SimpleNamespace(scheme="http", netloc="gateway.test")

    async def json(self) -> dict[str, Any]:
        return dict(self._body)


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    with authenticated():
        yield db


def submit_image(body: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> str:
    scheduled: list[str] = []
    with patch.object(proxy, "_schedule_image_media_job", lambda job_id: scheduled.append(job_id) or True):
        response = asyncio.run(proxy.gateway_image_generations_media_job(
            StubRequest(body, {"host": "gateway.test", **(headers or {})})))
    assert response.status_code == 202, response.body
    return json.loads(response.body)["job_id"]


def submit_audio(body: Mapping[str, Any]) -> str:
    with patch.object(proxy, "_schedule_audio_media_job", lambda job_id: True):
        response = asyncio.run(proxy._dispatch_gateway_media_job_submission(
            request=StubRequest({}), payload={"intent": "audio.generate", "parameters": dict(body)}, audience="client"))
    assert response is not None and response.status_code == 202, response and response.body
    return json.loads(response.body)["job_id"]


IMAGE_BODY = {"model": "flux2-klein-4b", "prompt": "a lighthouse"}
TILE_BODY = {"model": "flux2-klein-4b-seamless-tile", "prompt": "moss on stone"}
AUDIO_BODY = {"model": "tangoflux", "prompt": "rain on a tin roof", "duration": 8}


def kept(db: Path, job_id: str) -> Mapping[str, Mapping[str, Any]]:
    envelope = DefinitionStore(db).envelope(job_id)
    assert envelope is not None
    return envelope.generated


def test_an_accepted_image_job_draws_once_and_keeps_the_value_in_its_envelope(jobs_db):
    source = Recorder(12345)
    with patch.object(generated_defaults, "secure_source", source):
        job_id = submit_image(IMAGE_BODY)
    assert source.asked == [(PUBLIC_LOW, PUBLIC_HIGH)]
    assert kept(jobs_db, job_id) == {"seed": {"generator": "random_integer", "value": 12345}}


def test_an_accepted_audio_job_draws_once_and_keeps_the_value_in_its_envelope(jobs_db):
    source = Recorder(777)
    with patch.object(generated_defaults, "secure_source", source):
        job_id = submit_audio(AUDIO_BODY)
    assert source.asked == [(PUBLIC_LOW, PUBLIC_HIGH)]
    assert kept(jobs_db, job_id) == {"seed": {"generator": "random_integer", "value": 777}}


def test_a_job_whose_caller_gave_the_value_keeps_none(jobs_db):
    with patch.object(generated_defaults, "secure_source", refuse_a_draw):
        image = submit_image({**IMAGE_BODY, "extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"seed": 9}}})
        audio = submit_audio({**AUDIO_BODY, "seed": 9})
    assert kept(jobs_db, image) == {} and kept(jobs_db, audio) == {}


def test_a_replay_of_the_same_idempotency_key_draws_nothing_and_answers_the_same_job(jobs_db):
    source = Recorder(31)
    headers = {"idempotency-key": "same-key"}
    with patch.object(generated_defaults, "secure_source", source):
        first = submit_image(IMAGE_BODY, headers)
        second = submit_image(IMAGE_BODY, headers)
    assert first == second
    assert len(source.asked) == 1
    assert kept(jobs_db, first)["seed"]["value"] == 31


def test_a_restart_reads_the_stored_value_and_never_draws_again(jobs_db):
    with patch.object(generated_defaults, "secure_source", Recorder(2468)):
        job_id = submit_image(IMAGE_BODY)
    # A restart: nothing of the first process remains but the database.
    stored = proxy._stored_generated(job_id)
    token = proxy._JOB_GENERATED.set(stored)
    try:
        with patch.object(generated_defaults, "secure_source", refuse_a_draw):
            first = proxy._image_generated_values(proxy._find_image_manifest("image.generate", "flux2-klein-4b"), IMAGE_BODY)
            retry = proxy._image_generated_values(proxy._find_image_manifest("image.generate", "flux2-klein-4b"), IMAGE_BODY)
    finally:
        proxy._JOB_GENERATED.reset(token)
    assert first == retry == {"seed": 2468}


def test_the_audio_route_reads_the_stored_value_on_a_retry(jobs_db):
    with patch.object(generated_defaults, "secure_source", Recorder(1357)):
        job_id = submit_audio(AUDIO_BODY)
    manifest = proxy._load_audio_workflow_manifest("tangoflux")
    effective = audio_graph.effective_manifest(manifest, audio_graph.AudioLimits())
    for _attempt in range(2):
        inputs = audio_graph.manifest_inputs(
            {"prompt": "x"}, manifest=effective, stored=proxy._stored_generated(job_id), source=refuse_a_draw)
        assert inputs["seed"] == 1357


def test_the_job_runners_hand_the_stored_value_to_their_route(jobs_db, monkeypatch):
    with patch.object(generated_defaults, "secure_source", Recorder(8642)):
        job_id = submit_image(IMAGE_BODY)
    seen: list[Any] = []

    async def route(request, auth_context, receipt=None):
        seen.append(proxy._JOB_GENERATED.get())
        return proxy.Response(status_code=500, content=b"{}", media_type="application/json")

    monkeypatch.setattr(proxy, "proxy_image_request", route)
    monkeypatch.setattr(proxy, "_image_media_job_max_attempts", lambda: 1)
    asyncio.run(proxy._run_image_media_job(job_id))
    assert seen and seen[0]["seed"]["value"] == 8642
    assert proxy._JOB_GENERATED.get() is None, "the value is not left set after the run"


def test_the_stored_value_survives_a_second_envelope_read_through_the_result_identity(jobs_db):
    from gateway import result_bundle

    with patch.object(generated_defaults, "secure_source", Recorder(5)):
        job_id = submit_image(IMAGE_BODY)
    with media_jobs.connection(jobs_db) as conn:
        assert result_bundle._envelope_identity(conn, job_id) is not None


# --------------------------------------------------------------------------------------------------------- erasure


def envelope_with(value: int):
    return build_job_envelope(
        source_kind="workflow", source_id="wf-erase", contract={"inputs": {"n": {"type": "integer"}}},
        binding={"map": {"n": [{"node": "1", "input": "n"}]}}, template=b"graph", protocol_id="p", adapter_id="a",
        interface_version=1, service_id="s", origin="http://engine.test:1",
        generated={"n": {"generator": "random_integer", "value": value}})


def make_job(db: Path, app_id: str, value: int) -> str:
    job = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", workflow="wf-erase", app_id=app_id, request_id=f"req-{app_id}"),
        envelope=envelope_with(value), path=db, maker_from_request=False)
    return job.job_id


def test_erasure_empties_a_non_empty_generated_block_and_keeps_the_pins_and_other_people(tmp_path):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    mine, theirs = make_job(db, "app_mine", 611223344), make_job(db, "app_theirs", 655443322)
    assert kept(db, mine)["n"]["value"] == 611223344
    owner = media_jobs.stored_owner_key(None, app_id="app_mine")

    media_jobs.erase_prompt_derivatives({owner}, before=int(__import__("time").time()) + 5, path=db)

    assert kept(db, mine) == {"n": {"generator": "random_integer", "erased": True}}
    assert DefinitionStore(db).envelope(mine).contract == envelope_with(1).envelope.contract, "its pins are kept"
    assert kept(db, theirs)["n"]["value"] == 655443322, "another person is untouched"
    text = "\n".join(str(row) for row in sqlite_rows(db, "job_envelope"))
    assert "611223344" not in text and "655443322" in text


def test_the_kill_switch_wildcard_empties_every_block(tmp_path):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    ids = [make_job(db, "app_a", 1111), make_job(db, "app_b", 2222)]
    media_jobs.erase_prompt_derivatives({"*"}, before=int(__import__("time").time()) + 5, path=db)
    assert [kept(db, job_id) for job_id in ids] == [{"n": {"generator": "random_integer", "erased": True}}] * 2


def make_job_at(db: Path, app_id: str, value: int, created_at: float) -> str:
    job = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", workflow="wf-erase", app_id=app_id, request_id=f"req-{app_id}-{created_at}",
                            created_at=created_at),
        envelope=envelope_with(value), path=db, maker_from_request=False)
    return job.job_id


ERASED_BLOCK = {"n": {"generator": "random_integer", "erased": True}}


@pytest.mark.parametrize("cutoff_owner", ["owner", "wildcard"])
@pytest.mark.parametrize("offset, erased", [(-5, True), (0, True), (5, False)], ids=["before", "equal", "after"])
def test_a_job_created_after_erasure_is_stored_with_its_values_erased_when_the_cutoff_covers_it(
        tmp_path, cutoff_owner, offset, erased):
    """Late create: its other derivatives are cleared and the job is kept, so its generated values are erased markers too."""
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    cutoff = 1_900_000_000
    owner = media_jobs.stored_owner_key(None, app_id="app_mine")
    media_jobs.erase_prompt_derivatives({"*"} if cutoff_owner == "wildcard" else {owner}, before=cutoff, path=db)

    mine = make_job_at(db, "app_mine", 611223344, cutoff + offset)
    other = make_job_at(db, "app_other", 655443322, cutoff + offset)

    assert kept(db, mine) == (ERASED_BLOCK if erased else {"n": {"generator": "random_integer", "value": 611223344}})
    text = "\n".join(str(row) for row in sqlite_rows(db, "job_envelope"))
    assert ("611223344" in text) is (not erased)
    # The control: another owner is covered only by the wildcard.
    other_erased = erased and cutoff_owner == "wildcard"
    assert kept(db, other) == (ERASED_BLOCK if other_erased else {"n": {"generator": "random_integer", "value": 655443322}})


def sqlite_rows(db: Path, table: str) -> list[tuple]:
    import sqlite3

    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - test table names


# ------------------------------------------------------------------------ faults, durability and the three read states


def job_rows(db: Path) -> list[tuple]:
    return sqlite_rows(db, "media_jobs")


def spool_files(tmp_path: Path) -> list[Path]:
    return [path for path in (tmp_path / "payloads").rglob("*") if path.is_file()] if (tmp_path / "payloads").exists() else []


def submit_without_checking(route: str, body: Mapping[str, Any]) -> tuple[Any, list[str]]:
    """Accept one job on the image or audio route. Returns the response and the job ids that were scheduled."""
    scheduled: list[str] = []
    schedule = lambda job_id: scheduled.append(job_id) or True  # noqa: E731
    with patch.object(proxy, "_schedule_image_media_job", schedule), patch.object(proxy, "_schedule_audio_media_job", schedule):
        if route == "image":
            response = asyncio.run(proxy.gateway_image_generations_media_job(StubRequest(body)))
        else:
            response = asyncio.run(proxy._dispatch_gateway_media_job_submission(
                request=StubRequest({}), payload={"intent": "audio.generate", "parameters": dict(body)}, audience="client"))
    return response, scheduled


ROUTE_BODIES = {"image": IMAGE_BODY, "audio": AUDIO_BODY}


def empty_range_generator(rule, spec, source):
    raise ManifestError("the generator's range and the input's bounds share no integer")


@pytest.mark.parametrize("route", ["image", "audio"])
@pytest.mark.parametrize("fault", ["value-outside-the-input", "generator-fault"])
def test_a_generator_fault_at_accept_is_a_500_that_creates_and_submits_nothing(jobs_db, tmp_path, route, fault):
    broken = generated_defaults.ReviewedGenerator("value", empty_range_generator)
    with patch.object(generated_defaults, "secure_source", lambda low, high: -5), \
            patch.dict(generated_defaults.REVIEWED, {"random_integer": broken} if fault == "generator-fault" else {}):
        response, scheduled = submit_without_checking(route, ROUTE_BODIES[route])
    assert response is not None and response.status_code == 500, getattr(response, "body", response)
    assert json.loads(response.body)["error"]["code"] == "generated_value_unavailable"
    assert scheduled == [], "nothing was submitted"
    assert job_rows(jobs_db) == [], "no job row"
    assert spool_files(tmp_path) == [], "no payload left behind"
    # A later request draws for itself; the failed one cannot be revived by it.
    with patch.object(generated_defaults, "secure_source", Recorder(9)):
        job_id = submit_image(IMAGE_BODY) if route == "image" else submit_audio(AUDIO_BODY)
    assert len(job_rows(jobs_db)) == 1 and job_id


@pytest.mark.parametrize("fault", ["value-outside-the-input", "generator-fault"])
def test_a_generator_fault_on_the_synchronous_image_routes_is_a_500(fault):
    broken = generated_defaults.ReviewedGenerator("value", empty_range_generator)
    manifest = proxy._find_image_manifest("image.generate", "flux2-klein-4b")
    with patch.object(generated_defaults, "secure_source", lambda low, high: -5), \
            patch.dict(generated_defaults.REVIEWED, {"random_integer": broken} if fault == "generator-fault" else {}):
        with pytest.raises(ManifestError) as raised:
            proxy._image_generated_values(manifest, {})
    assert proxy._generated_value_fault_response(raised.value).status_code == 500
    # Both synchronous call sites answer that response, not the 503 of an invalid workflow configuration.
    source = (ROOT / "proxy.py").read_text(encoding="utf-8")
    calls = re.findall(r"_image_generated_values\(manifest, (?:body|form_dict)\)\n\s+except ManifestError as exc:\n\s+return (\w+)\(", source)
    assert calls == ["_generated_value_fault_response"] * 2


@pytest.mark.parametrize("compile_from_pin", [False, True])
@pytest.mark.parametrize("route", ["image", "audio"])
@pytest.mark.parametrize("how", ["no-envelope", "write-fails"])
def test_a_job_that_cannot_store_its_generated_value_is_not_created_whatever_compile_from_pin_says(
        jobs_db, tmp_path, route, how, compile_from_pin):
    def refuse_write(*args, **kwargs):
        raise OSError("disk full")

    patches = [patch.object(proxy, "_compile_from_pin", lambda capability: compile_from_pin)]
    if how == "no-envelope":
        patches.append(patch.object(proxy, "_graph_job_envelope", lambda *args, **kwargs: None))
    else:
        patches.append(patch("gateway.definition_store.record_envelope_in", refuse_write))
    with patch.object(generated_defaults, "secure_source", Recorder(4)):
        for entered in patches:
            entered.start()
        try:
            response, scheduled = submit_without_checking(route, ROUTE_BODIES[route])
        finally:
            for entered in patches:
                entered.stop()
    assert response is not None and response.status_code == 503, getattr(response, "body", response)
    assert scheduled == [] and job_rows(jobs_db) == [] and spool_files(tmp_path) == []


def test_a_job_whose_caller_gave_every_value_needs_no_envelope(jobs_db):
    with patch.object(proxy, "_graph_job_envelope", lambda *args, **kwargs: None), \
            patch.object(proxy, "_compile_from_pin", lambda capability: False), \
            patch.object(generated_defaults, "secure_source", refuse_a_draw):
        response, scheduled = submit_without_checking(
            "image", {**IMAGE_BODY, "extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"seed": 9}}})
    assert response.status_code == 202 and len(scheduled) == 1


def read_with_context(job_id: str, manifest_inputs_source=refuse_a_draw):
    """What the job runner does: read the stored block, set it, and ask the image route for the job's values."""
    manifest = proxy._find_image_manifest("image.generate", "flux2-klein-4b")
    token = proxy._JOB_GENERATED.set(proxy._stored_generated(job_id))
    try:
        with patch.object(generated_defaults, "secure_source", manifest_inputs_source):
            return proxy._image_generated_values(manifest, IMAGE_BODY)
    finally:
        proxy._JOB_GENERATED.reset(token)


def test_a_job_made_before_values_were_kept_still_draws(jobs_db):
    """(a) legacy: its envelope has an empty block, or it has no envelope at all."""
    manifest = proxy._find_image_manifest("image.generate", "flux2-klein-4b")
    with_empty_block = submit_image({**IMAGE_BODY, "extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"seed": 9}}})
    assert kept(jobs_db, with_empty_block) == {}
    without_envelope = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", workflow="image.generate.flux2-klein-4b", request_id="legacy-none"),
        path=jobs_db, maker_from_request=False).job_id
    assert proxy._stored_generated(without_envelope) == {}
    for job_id in (with_empty_block, without_envelope):
        source = Recorder(61)
        assert read_with_context(job_id, source) == {"seed": 61}
        assert len(source.asked) == 1, "the legacy draw is kept so an in-flight job survives the deploy"
    assert manifest is not None


def test_a_job_whose_values_were_erased_never_draws_and_never_runs(jobs_db, monkeypatch):
    """(b) erased."""
    with patch.object(generated_defaults, "secure_source", Recorder(52)):
        job_id = submit_image(IMAGE_BODY)
    DefinitionStore(jobs_db).clear_generated(job_id)
    with pytest.raises(generated_defaults.GeneratedValueUnavailable) as raised:
        read_with_context(job_id)
    assert raised.value.erased is True

    calls: list[str] = []

    async def route(request, auth_context, receipt=None):
        calls.append("route")
        return proxy.Response(status_code=500, content=b"{}", media_type="application/json")

    monkeypatch.setattr(proxy, "proxy_image_request", route)
    monkeypatch.setattr(proxy, "_image_media_job_max_attempts", lambda: 3)
    monkeypatch.setattr(proxy, "_image_media_job_retry_base_seconds", lambda: 0)
    with patch.object(generated_defaults, "secure_source", refuse_a_draw):
        asyncio.run(proxy._run_image_media_job(job_id))
    job = media_jobs.get(job_id, audience="internal", path=jobs_db)
    assert job["status"] == media_jobs.STATUS_FAILED and "erased" in job["error"]
    assert calls == [], "the route never ran, so nothing was submitted"


@pytest.mark.parametrize("damage", ["not json", "no-value"])
def test_a_job_whose_stored_values_cannot_be_read_fails_closed_and_never_redraws(jobs_db, monkeypatch, damage):
    """(c) unreadable or malformed."""
    import sqlite3

    with patch.object(generated_defaults, "secure_source", Recorder(53)):
        job_id = submit_image(IMAGE_BODY)
    with sqlite3.connect(jobs_db) as conn:
        if damage == "not json":
            conn.execute("UPDATE job_envelope SET envelope_json = 'not json' WHERE job_id = ?", (job_id,))
        else:
            body = json.loads(conn.execute("SELECT envelope_json FROM job_envelope WHERE job_id = ?", (job_id,)).fetchone()[0])
            body["generated"]["seed"] = {"generator": "random_integer"}
            conn.execute("UPDATE job_envelope SET envelope_json = ? WHERE job_id = ?", (json.dumps(body), job_id))
    with pytest.raises(generated_defaults.GeneratedValueUnavailable) as raised:
        read_with_context(job_id)
    assert raised.value.erased is False

    calls: list[str] = []

    async def route(request, auth_context, receipt=None):
        calls.append("route")
        return proxy.Response(status_code=500, content=b"{}", media_type="application/json")

    monkeypatch.setattr(proxy, "proxy_image_request", route)
    monkeypatch.setattr(proxy, "_image_media_job_max_attempts", lambda: 3)
    monkeypatch.setattr(proxy, "_image_media_job_retry_base_seconds", lambda: 0)
    with patch.object(generated_defaults, "secure_source", refuse_a_draw):
        asyncio.run(proxy._run_image_media_job(job_id))
    job = media_jobs.get(job_id, audience="internal", path=jobs_db)
    assert job["status"] == media_jobs.STATUS_FAILED and "cannot be read" in job["error"]
    assert calls == []


@pytest.mark.parametrize("state", ["erased", "unreadable"])
def test_the_audio_runner_fails_a_job_whose_stored_values_are_gone_without_calling_its_route(jobs_db, monkeypatch, state):
    import sqlite3

    with patch.object(generated_defaults, "secure_source", Recorder(54)):
        job_id = submit_audio(AUDIO_BODY)
    if state == "erased":
        DefinitionStore(jobs_db).clear_generated(job_id)
    else:
        with sqlite3.connect(jobs_db) as conn:
            conn.execute("UPDATE job_envelope SET envelope_json = 'not json' WHERE job_id = ?", (job_id,))
    calls: list[str] = []

    async def route(*args, **kwargs):
        calls.append("route")
        return proxy.Response(status_code=500, content=b"{}", media_type="application/json")

    monkeypatch.setattr(proxy, "proxy_audio_generate", route)
    with patch.object(generated_defaults, "secure_source", refuse_a_draw):
        asyncio.run(proxy._run_audio_media_job(job_id))
    job = media_jobs.get(job_id, audience="internal", path=jobs_db)
    assert job["status"] == media_jobs.STATUS_FAILED
    # An unreadable envelope is first met where the audio runner reads the job's pin; it fails closed there.
    assert ("erased" if state == "erased" else "no longer available") in job["error"]
    assert calls == []


# ----------------------------------------------------------------------------------------------- the same-key race


def test_two_concurrent_requests_with_one_key_get_one_job_and_one_stored_value(jobs_db, tmp_path):
    """Both requests read "no job yet" before either creates one, so both draw. Only the request whose job row wins keeps
    its envelope; the other answers that job. The value it drew reaches nothing: not the stored envelope, a response,
    a submission or the job database."""
    import threading

    barrier = threading.Barrier(2, timeout=30)
    real_get = media_jobs.get_by_request_id
    lock = threading.Lock()
    drawn: list[int] = []

    def both_miss_first(*args, **kwargs):
        found = real_get(*args, **kwargs)
        barrier.wait()  # neither request goes on until both have looked
        return found

    def distinct_values(low: int, high: int) -> int:
        with lock:
            drawn.append(918273000 + len(drawn) * 111111)
            return drawn[-1]

    results: list[tuple[Any, None]] = []
    scheduled: list[str] = []  # every scheduling call, from both requests

    def record_schedule(job_id: str) -> bool:
        with lock:
            scheduled.append(job_id)
        return True

    def one_request() -> None:
        response = asyncio.run(proxy.gateway_image_generations_media_job(
            StubRequest(IMAGE_BODY, {"host": "gateway.test", "idempotency-key": "race-key"})))
        with lock:
            results.append((response, None))

    original_schedule = proxy._schedule_image_media_job
    # One patch of the scheduler for both threads, installed and restored here, outside them: a patch made inside each
    # thread lets one restore the other's mock and leak it to later tests.
    with patch.object(proxy, "_schedule_image_media_job", record_schedule), \
            patch.object(media_jobs, "get_by_request_id", both_miss_first), \
            patch.object(generated_defaults, "secure_source", distinct_values):
        threads = [threading.Thread(target=one_request) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
    assert proxy._schedule_image_media_job is original_schedule, "the scheduler patch is gone after both threads joined"

    assert len(results) == 2 and len(drawn) == 2, "both requests drew"
    ids = {json.loads(response.body)["job_id"] for response, _ in results}
    assert len(ids) == 1 and all(response.status_code in (200, 202) for response, _ in results)
    assert len(scheduled) == 1, "one submission"
    (job_id,) = ids
    assert scheduled == [job_id]
    stored = kept(jobs_db, job_id)["seed"]["value"]
    assert stored in drawn
    loser = next(value for value in drawn if value != stored)
    surfaces = [response.body.decode() for response, _ in results]
    surfaces += [str(row) for table in ("media_jobs", "job_envelope", "media_job_events") for row in sqlite_rows(jobs_db, table)]
    surfaces += [path.read_text(encoding="utf-8", errors="replace") for path in spool_files(tmp_path)]
    assert not any(str(loser) in text for text in surfaces), "the losing request's value reaches nothing"
    assert len(job_rows(jobs_db)) == 1
    payloads = spool_files(tmp_path)
    assert len(payloads) == 1, f"the loser's payload file is absent; the spool holds {payloads}"
    assert str(stored) in payloads[0].read_text(encoding="utf-8", errors="replace") or job_id in payloads[0].name


def test_a_job_whose_binding_has_no_pin_is_still_accepted_and_draws_at_run(jobs_db, monkeypatch):
    """A bundled workflow with a generated input and a binding that is not the graph binding (a gateway composition)
    builds no envelope. Requiring one would refuse every such job; it keeps the behaviour it had before S3.6: the value
    is drawn when the job runs, not stored."""
    manifest = proxy._find_image_manifest("image.generate", TILE_BODY["model"])
    assert manifest is not None and generated_defaults.generated_inputs(manifest.get("inputs") or {})
    assert not proxy._manifest_has_pin(manifest)
    assert proxy._manifest_has_pin(proxy._find_image_manifest("image.generate", IMAGE_BODY["model"]))
    job_id = submit_image(TILE_BODY)
    assert DefinitionStore(jobs_db).envelope(job_id) is None

    drawn_at_run: list[Any] = []

    async def route(request, auth_context, receipt=None):
        drawn_at_run.append(proxy._image_generated_values(manifest, TILE_BODY))
        return proxy.Response(status_code=500, content=b"{}", media_type="application/json")

    monkeypatch.setattr(proxy, "proxy_image_request", route)
    monkeypatch.setattr(proxy, "_image_media_job_max_attempts", lambda: 1)
    source = Recorder(4321)
    with patch.object(generated_defaults, "secure_source", source):
        asyncio.run(proxy._run_image_media_job(job_id))
    assert drawn_at_run == [{"seed": 4321}], "the run drew the value, since nothing was stored"
    assert len(source.asked) == 1
    assert proxy._JOB_GENERATED.get() is None


def test_an_audio_job_whose_binding_has_no_pin_is_accepted_without_an_envelope_and_draws_at_run(jobs_db, monkeypatch):
    base = proxy._load_audio_workflow_manifest("tangoflux")
    assert proxy._manifest_has_pin(base)
    pinless = {**base, "bindings": {name: binding for name, binding in base["bindings"].items()
                                    if name != proxy.DERIVED._GRAPH_BINDING}}
    assert not proxy._manifest_has_pin(pinless)
    monkeypatch.setattr(proxy, "_load_audio_workflow_manifest", lambda name: pinless)
    monkeypatch.setattr(proxy, "_compile_from_pin", lambda capability: False)
    with patch.object(generated_defaults, "secure_source", Recorder(2468)):
        job_id = submit_audio(AUDIO_BODY)  # asserts a 202: not refused for lacking an envelope
    assert DefinitionStore(jobs_db).envelope(job_id) is None

    seen: list[Any] = []
    drawn_at_run: list[Any] = []
    effective = audio_graph.effective_manifest(pinless, audio_graph.AudioLimits())

    async def route(*args, **kwargs):
        stored = proxy._JOB_GENERATED.get()
        seen.append(stored)
        inputs = audio_graph.manifest_inputs(AUDIO_BODY, manifest=effective, stored=stored)
        drawn_at_run.append(inputs["seed"])
        return proxy.Response(status_code=500, content=b"{}", media_type="application/json")

    async def no_receipt(*args, **kwargs):
        return SimpleNamespace(receipt_id="rcpt_test")

    from gateway import media_ingestion

    monkeypatch.setattr(proxy, "proxy_audio_generate", route)
    monkeypatch.setattr(media_ingestion, "start_receipt", no_receipt)
    monkeypatch.setattr(media_ingestion, "finish_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(media_ingestion, "finish_receipt", lambda *args, **kwargs: None)
    source = Recorder(4321)
    with patch.object(generated_defaults, "secure_source", source):
        asyncio.run(proxy._run_audio_media_job(job_id))
    assert seen == [{}], "nothing is stored for the run to read, so the route draws"
    assert source.asked == [(PUBLIC_LOW, PUBLIC_HIGH)], "the audio run draws exactly once"
    assert drawn_at_run == [4321], "the route uses the value drawn at run, not the accept-time value"
    assert media_jobs.get(job_id, audience="internal", path=jobs_db)["status"] != media_jobs.STATUS_QUEUED


def test_the_context_is_clean_in_the_same_task_after_a_read_failure_and_the_next_job_sees_its_own_value(jobs_db, monkeypatch):
    """Both jobs run in ONE task. The first cannot be read; the second must neither see its leftovers nor a stale value."""
    with patch.object(generated_defaults, "secure_source", Recorder(111)):
        gone = submit_image(IMAGE_BODY)
    with patch.object(generated_defaults, "secure_source", Recorder(222)):
        fine = submit_image({**IMAGE_BODY, "prompt": "a second lighthouse"})
    DefinitionStore(jobs_db).clear_generated(gone)
    seen: list[Any] = []

    async def route(request, auth_context, receipt=None):
        seen.append(proxy._JOB_GENERATED.get())
        return proxy.Response(status_code=500, content=b"{}", media_type="application/json")

    monkeypatch.setattr(proxy, "proxy_image_request", route)
    monkeypatch.setattr(proxy, "_image_media_job_max_attempts", lambda: 1)
    after: list[Any] = []

    async def both() -> None:
        await proxy._run_image_media_job(gone)
        after.append(proxy._JOB_GENERATED.get())
        await proxy._run_image_media_job(fine)
        after.append(proxy._JOB_GENERATED.get())

    asyncio.run(both())
    assert after == [None, None], "the context is empty after the failed read and after the next run"
    assert [entry["seed"]["value"] for entry in seen] == [222], "only the second job reached its route, with its own value"
    assert media_jobs.get(gone, audience="internal", path=jobs_db)["status"] == media_jobs.STATUS_FAILED


@pytest.mark.parametrize("require", [True, False])
def test_a_fault_after_the_envelope_is_written_rolls_the_envelope_back(jobs_db, require):
    """The write succeeds and the record then fails: no envelope and no references are left, and with ``require`` no job row."""
    from gateway import definition_store

    real = definition_store.record_envelope_in

    def after_write(conn, job_id, envelope, **kwargs):
        real(conn, job_id, envelope, **kwargs)
        assert conn.execute("SELECT COUNT(*) FROM job_envelope").fetchone()[0] == 1, "the envelope was written"
        raise OSError("disk full after the write")

    job = media_jobs.MediaJob(intent="image.generate", workflow="wf-erase", app_id="app_x", request_id="req-after-write")
    with patch("gateway.definition_store.record_envelope_in", after_write):
        if require:
            with pytest.raises(media_jobs.EnvelopeNotRecorded):
                media_jobs.create(job, envelope=envelope_with(5), path=jobs_db, maker_from_request=False, require_envelope=True)
            assert job_rows(jobs_db) == []
        else:
            media_jobs.create(job, envelope=envelope_with(5), path=jobs_db, maker_from_request=False)
            assert len(job_rows(jobs_db)) == 1, "best effort: the job stays"
    assert sqlite_rows(jobs_db, "job_envelope") == [], "the written envelope was rolled back"
    assert sqlite_rows(jobs_db, "definition_ref") == []


@pytest.mark.parametrize("route", ["image", "audio"])
def test_a_graph_workflow_whose_envelope_fails_to_build_is_still_refused(jobs_db, tmp_path, route):
    with patch.object(proxy, "_graph_job_envelope", lambda *args, **kwargs: None), \
            patch.object(proxy, "_compile_from_pin", lambda capability: False), \
            patch.object(generated_defaults, "secure_source", Recorder(7)):
        response, scheduled = submit_without_checking(route, ROUTE_BODIES[route])
    assert response.status_code == 503 and scheduled == [] and job_rows(jobs_db) == [] and spool_files(tmp_path) == []
