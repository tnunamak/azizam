"""The segment-chain composer: one engine graph from a template of named segment variants (CODE_TO_DATA S7, M9).

A chain makes one engine graph that renders a first segment (or loads a source clip) and adds segments one after
another, each pinned to the end of the clip so far. Which nodes a segment needs depends on its position, not on the
engine: this module knows only the *shape* of a template document.

A template document holds two parts, both data:

- ``binding``: the frame grid (``frame_step``, ``frame_offset``), the context window rule, the shot minimum, the negative
  default, the request options, the node-id prefix of a segment and ``rule``, the ordered list that picks a variant for
  a position. A rule entry names a ``variant`` and the facts it needs (``index``, ``source``); the first entry whose
  facts all hold wins, an entry without facts is the default.
- ``template``: ``shared`` nodes (always added), ``source`` nodes (added when the request extends a clip), ``variants``
  (each a set of nodes, the ``unit`` that reports progress and the ``exports`` that link it to the next segment) and
  ``finish`` (the sound join and the nodes that close the graph).

Inside a node, a string ``"$name"`` is a parameter, ``["@x", n]`` is output ``n`` of node ``x`` of the same segment, and
``{"$if": fact, "then": A, "else": B}`` picks by a fact (``"!fact"`` negates; with no ``else`` a false fact drops the
input). A node may carry ``"if"`` or ``"unless"`` with a fact name. The module names no engine, model, node class or
workflow; a name the template uses that this module does not provide is a template problem, found at load.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# What a template may read. A name outside these sets is refused at load.
PARAMETERS = frozenset({
    "fps", "fps_whole", "width", "height", "prompt", "negative", "seed", "frames", "context", "context_text",
    "context_start", "sampled_frames", "context_seconds", "segment_seconds", "source_seconds", "source_file",
    "context_strength", "first_frame_file", "first_frame_strength", "filename_prefix", "frames_in", "latent_in",
    "source_sound", "frames_out", "sound_out",
})
FACTS = frozenset({"index", "source", "sound", "scaled", "first_frame", "sound_out"})
EXPORTS = frozenset({"frames", "sound", "latent"})
_MISSING = object()


class ChainTemplateError(ValueError):
    """The template document is malformed. Raised at load, never at request time."""


@dataclass(frozen=True)
class SourceCheck:
    fps: float
    tolerance: float
    message: str


@dataclass(frozen=True)
class ChainBinding:
    model_family: str
    stride_reason: str
    frame_step: int
    frame_offset: int
    default_context_frames: int
    context_windows: tuple[int, ...]  # empty: the window is a whole number of grid steps
    min_shot_seconds: float
    options: tuple[str, ...]
    negative_default: str
    node_prefix: str
    rule: tuple[Mapping[str, Any], ...]
    source_check: SourceCheck | None = None
    filename_prefix: str = ""  # the output prefix when the request names none


@dataclass(frozen=True)
class ChainTemplate:
    id: str
    binding: ChainBinding
    shared: Mapping[str, Any]
    source: Mapping[str, Any]
    variants: Mapping[str, Mapping[str, Any]]
    finish: Mapping[str, Any]


@dataclass
class Composed:
    graph: dict[str, Any]
    save_node: str
    units: list[dict[str, str]] = field(default_factory=list)
    total_frames: int = 0


# ----------------------------------------------------------------------------------------------------------- load


def _bad(where: str, message: str) -> ChainTemplateError:
    return ChainTemplateError(f"{where}: {message}")


def _number(binding: Mapping[str, Any], key: str) -> float:
    value = binding.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"binding.{key}", "must be a number")
    return value


def _positive_int(binding: Mapping[str, Any], key: str) -> int:
    value = binding.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _bad(f"binding.{key}", "must be a whole number of at least 1")
    return value


def _scan(value: Any, where: str, local: frozenset[str]) -> None:
    """Refuse a parameter, fact or local link the composer does not provide."""
    if isinstance(value, str):
        if value.startswith("$") and value[1:] not in PARAMETERS:
            raise _bad(where, f"unknown parameter {value}")
    elif isinstance(value, list):
        if len(value) == 2 and isinstance(value[0], str) and value[0].startswith("@") and isinstance(value[1], int):
            if value[0][1:] not in local:
                raise _bad(where, f"link to {value[0]}, which is not a node of this variant")
            return
        for item in value:
            _scan(item, where, local)
    elif isinstance(value, Mapping):
        if "$if" in value:
            fact = str(value["$if"]).removeprefix("!")
            if fact not in FACTS:
                raise _bad(where, f"unknown fact {value['$if']}")
            unknown = sorted(set(value) - {"$if", "then", "else"})
            if unknown or "then" not in value:
                raise _bad(where, "an $if holds then and optionally else")
        for key, item in value.items():
            if key != "$if":
                _scan(item, where, local)


def _scan_nodes(nodes: Any, where: str, *, local: frozenset[str]) -> None:
    if not isinstance(nodes, Mapping):
        raise _bad(where, "must be a mapping of nodes")
    for node_id, node in nodes.items():
        spot = f"{where}.{node_id}"
        if not isinstance(node, Mapping) or not isinstance(node.get("class_type"), str) \
                or not isinstance(node.get("inputs"), Mapping):
            raise _bad(spot, "a node needs a class_type and inputs")
        for gate in ("if", "unless"):
            if gate in node and node[gate] not in FACTS:
                raise _bad(spot, f"unknown fact {node[gate]}")
        _scan(node["inputs"], spot, local)


def _scan_exports(exports: Any, where: str, local: frozenset[str]) -> None:
    if not isinstance(exports, Mapping) or not set(exports) <= EXPORTS:
        raise _bad(where, f"exports are some of {sorted(EXPORTS)}")
    _scan(dict(exports), where, local)


def load(document: Mapping[str, Any]) -> ChainTemplate:
    """Check a template document and compile it. Raises ``ChainTemplateError``."""
    if not isinstance(document, Mapping):
        raise _bad("template", "must be a mapping")
    chain_id = document.get("id")
    raw, template = document.get("binding"), document.get("template")
    if not isinstance(chain_id, str) or not chain_id or not isinstance(raw, Mapping) or not isinstance(template, Mapping):
        raise _bad("template", "needs an id, a binding and a template")
    windows = raw.get("context_windows", [])
    if not isinstance(windows, list) or not all(isinstance(w, int) and not isinstance(w, bool) for w in windows):
        raise _bad("binding.context_windows", "must be a list of whole numbers")
    options = raw.get("options")
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise _bad("binding.options", "must be a list of names")
    rule = raw.get("rule")
    if not isinstance(rule, list) or not rule or not all(isinstance(r, Mapping) and isinstance(r.get("variant"), str)
                                                          for r in rule):
        raise _bad("binding.rule", "must be a list of entries that each name a variant")
    check = raw.get("source_check")
    source_check = None
    if check is not None:
        if not isinstance(check, Mapping) or not isinstance(check.get("message"), str):
            raise _bad("binding.source_check", "needs fps, tolerance and message")
        source_check = SourceCheck(float(_number(check, "fps")), float(_number(check, "tolerance")), check["message"])
    prefix = raw.get("node_prefix")
    if not isinstance(prefix, str) or "{index}" not in prefix:
        raise _bad("binding.node_prefix", "must hold {index}")
    binding = ChainBinding(
        model_family=str(raw.get("model_family", "")), stride_reason=str(raw.get("stride_reason", "")),
        frame_step=_positive_int(raw, "frame_step"), frame_offset=int(_number(raw, "frame_offset")),
        default_context_frames=_positive_int(raw, "default_context_frames"), context_windows=tuple(windows),
        min_shot_seconds=float(_number(raw, "min_shot_seconds")), options=tuple(options),
        negative_default=str(raw.get("negative_default", "")), node_prefix=prefix,
        rule=tuple(copy.deepcopy(list(rule))), source_check=source_check,
        filename_prefix=str(raw.get("filename_prefix", "")))

    variants = template.get("variants")
    if not isinstance(variants, Mapping) or not variants:
        raise _bad("template.variants", "must be a non-empty mapping")
    for entry in rule:
        if entry["variant"] not in variants:
            raise _bad("binding.rule", f"names variant {entry['variant']!r}, which the template does not declare")
        unknown = sorted(set(entry) - {"variant", "index", "source"})
        if unknown:
            raise _bad("binding.rule", f"unknown facts {unknown}")
    if set(rule[-1]) != {"variant"}:
        raise _bad("binding.rule", "the last entry is the default: it names a variant and no facts")
    for name, variant in variants.items():
        where = f"template.variants.{name}"
        nodes = variant.get("nodes") if isinstance(variant, Mapping) else None
        local = frozenset(nodes) if isinstance(nodes, Mapping) else frozenset()
        _scan_nodes(nodes, f"{where}.nodes", local=local)
        unit = variant.get("unit")
        if not isinstance(unit, Mapping) or set(unit) != {"sample", "decode"} or not set(unit.values()) <= local:
            raise _bad(f"{where}.unit", "must name the sample and decode nodes of the variant")
        _scan_exports(variant.get("exports"), f"{where}.exports", local)
    shared, source, finish = template.get("shared", {}), template.get("source", {}), template.get("finish")
    _scan_nodes(shared, "template.shared", local=frozenset())
    if source:
        _scan_nodes(source.get("nodes"), "template.source.nodes", local=frozenset())
        _scan_exports(source.get("exports"), "template.source.exports", frozenset())
    if not isinstance(finish, Mapping):
        raise _bad("template.finish", "is required")
    join = finish.get("sound_join")
    if join is not None and (not isinstance(join, Mapping) or "{k}" not in str(join.get("id"))
                             or not {"class_type", "first", "next"} <= set(join)):
        raise _bad("template.finish.sound_join", "needs an id with {k}, a class_type, first and next")
    _scan_nodes(finish.get("nodes"), "template.finish.nodes", local=frozenset())
    if finish.get("save_node") not in (finish.get("nodes") or {}):
        raise _bad("template.finish.save_node", "must name a finish node")
    return ChainTemplate(chain_id, binding, copy.deepcopy(dict(shared)), copy.deepcopy(dict(source)),
                         copy.deepcopy(dict(variants)), copy.deepcopy(dict(finish)))


# --------------------------------------------------------------------------------------------------------- compose


def _fact(name: str, facts: Mapping[str, Any]) -> bool:
    return not facts[name[1:]] if name.startswith("!") else bool(facts[name])


def _resolve(value: Any, params: Mapping[str, Any], facts: Mapping[str, Any], prefix: str) -> Any:
    if isinstance(value, str):
        return copy.deepcopy(params[value[1:]]) if value.startswith("$") else value
    if isinstance(value, list):
        if len(value) == 2 and isinstance(value[0], str) and value[0].startswith("@") and isinstance(value[1], int):
            return [prefix + value[0][1:], value[1]]
        return [_resolve(item, params, facts, prefix) for item in value]
    if isinstance(value, Mapping):
        if "$if" in value:
            if _fact(value["$if"], facts):
                return _resolve(value["then"], params, facts, prefix)
            return _resolve(value["else"], params, facts, prefix) if "else" in value else _MISSING
        out = {}
        for key, item in value.items():
            resolved = _resolve(item, params, facts, prefix)
            if resolved is not _MISSING:
                out[key] = resolved
        return out
    return value


def _add_nodes(graph: dict[str, Any], nodes: Mapping[str, Any], params: Mapping[str, Any],
               facts: Mapping[str, Any], prefix: str) -> None:
    for node_id, node in nodes.items():
        if ("if" in node and not facts[node["if"]]) or ("unless" in node and facts[node["unless"]]):
            continue
        graph[prefix + node_id] = {"class_type": node["class_type"],
                                   "inputs": _resolve(node["inputs"], params, facts, prefix)}


def _exports(exports: Mapping[str, Any], params: Mapping[str, Any], facts: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    found = {}
    for key, value in exports.items():
        resolved = _resolve(value, params, facts, prefix)
        if resolved is not _MISSING and resolved is not None:
            found[key] = resolved
    return found


def pick_variant(binding: ChainBinding, *, index: int, source: bool) -> str:
    facts = {"index": index, "source": source}
    for entry in binding.rule:
        if all(facts[key] == value for key, value in entry.items() if key != "variant"):
            return entry["variant"]
    raise ChainTemplateError("binding.rule: no entry matches")  # the last entry has no facts, so this cannot happen


def compose(template: ChainTemplate, spec: Any, *, context: int, negative: str) -> Composed:
    """Build the graph for ``spec`` (segments, size, rate, seed, source, first frame, output prefix).

    ``negative`` is the text of the negative prompt. ``context`` is the number of frames of the clip so far that pin each added segment.
    """
    binding = template.binding
    fps = float(spec.fps)
    source = spec.source
    graph: dict[str, Any] = {}
    sound_parts: list[Any] = []
    state: dict[str, Any] = {"frames": None, "latent": None, "source_sound": None}
    scaled = source is not None and (source.width, source.height) != (spec.width, spec.height)
    base_facts = {
        "source": source is not None, "scaled": scaled, "sound": True if source is None else bool(source.has_audio),
        "first_frame": bool(spec.first_frame_image), "sound_out": False, "index": 0,
    }
    base = {
        "fps": fps, "fps_whole": int(round(fps)), "width": spec.width, "height": spec.height,
        "negative": negative, "context": context, "context_text": str(context),
        "context_seconds": context / fps, "context_strength": spec.context_strength,
        "first_frame_file": spec.first_frame_image, "first_frame_strength": spec.first_frame_strength,
        "filename_prefix": spec.filename_prefix if spec.filename_prefix is not None else template.binding.filename_prefix,
        "frames_in": None, "latent_in": None, "source_sound": None,
        "frames_out": None, "sound_out": None, "prompt": "", "seed": spec.seed, "frames": 0, "context_start": 0,
        "sampled_frames": 0, "segment_seconds": 0.0, "source_seconds": 0.0, "source_file": None,
    }
    _add_nodes(graph, template.shared, base, base_facts, "")
    have = 0
    if source is not None:
        have = source.frames
        params = {**base, "source_file": source.filename, "source_seconds": source.frames / fps}
        _add_nodes(graph, template.source["nodes"], params, base_facts, "")
        found = _exports(template.source["exports"], params, base_facts, "")
        state["frames"] = found.get("frames")
        if "sound" in found:
            sound_parts.append(found["sound"])
            state["source_sound"] = found["sound"]

    units: list[dict[str, str]] = []
    for index, segment in enumerate(spec.segments):
        variant = template.variants[pick_variant(binding, index=index, source=source is not None)]
        prefix = binding.node_prefix.replace("{index}", str(index))
        facts = {**base_facts, "index": index}
        params = {
            **base, "prompt": segment.prompt, "seed": spec.seed + index, "frames": segment.frames,
            "context_start": have - context, "sampled_frames": context + segment.frames,
            "segment_seconds": segment.frames / fps, "frames_in": state["frames"], "latent_in": state["latent"],
            "source_sound": state["source_sound"],
        }
        _add_nodes(graph, variant["nodes"], params, facts, prefix)
        found = _exports(variant["exports"], params, facts, prefix)
        state["frames"] = found.get("frames", state["frames"])
        state["latent"] = found.get("latent", state["latent"])
        if "sound" in found:
            sound_parts.append(found["sound"])
        units.append({role: prefix + node for role, node in variant["unit"].items()})
        have += segment.frames

    sound = None
    join = template.finish.get("sound_join")
    if join is not None and sound_parts:
        sound = sound_parts[0]
        for k, part in enumerate(sound_parts[1:]):
            node_id = join["id"].replace("{k}", str(k))
            graph[node_id] = {"class_type": join["class_type"],
                              "inputs": {**copy.deepcopy(join.get("inputs", {})), join["first"]: sound, join["next"]: part}}
            sound = [node_id, 0]
    elif sound_parts:
        sound = sound_parts[0]
    closing = {**base, "frames_out": state["frames"], "sound_out": sound}
    _add_nodes(graph, template.finish["nodes"], closing, {**base_facts, "sound_out": sound is not None}, "")
    return Composed(graph=graph, save_node=template.finish["save_node"], units=units, total_frames=have)

