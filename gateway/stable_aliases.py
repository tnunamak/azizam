"""Pure two-layer model alias resolution.

Layer 1 (``openai``): OpenAI model names (``gpt-4o``, ``tts-1``,
``gpt-image-1`` ...) matched by ordered glob patterns. Each pattern points at a
layer-2 name, so re-pointing a stable alias also moves every OpenAI name that
points at it.

Layer 2 (``stable``): the gateway's own semantic names (``fast``, ``image``,
``tts-fast`` ...). They never change meaning. Each one resolves to a
concrete role, workflow, or model id, or (speech and transcription) to an
ordered list of *engines* where the first deployed engine wins.

Everything lives in the ``model_aliases`` config section (release defaults in
``registry/defaults/model_aliases.yml``, the owner's changes in ``admin.db``), one
sub-section per operation::

    model_aliases:
      chat:
        stable:   {fast: default-fast, default: default}
        openai:   [{match: "gpt-4o-mini*", to: fast}, ...]
      speech:
        engines:  {omnivoice: {service: tts, model: k2-fsa/OmniVoice, ...}}
        stable:   {tts: [omnivoice], tts-fast: [supertonic, omnivoice]}
        openai:   [{match: "tts-1", to: tts-fast}]

A stable name whose target is ``null`` exists and is not configured: asking for it answers
``alias_not_configured``. The release ships its names that way, so a fresh install names no model.

This module is pure: no FastAPI, no network, no config loading. Callers pass in
the section mapping and an ``engine_available`` predicate.
"""
from __future__ import annotations

import copy
import fnmatch
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from gateway.config_layers import DANGLING, INVALID, Problem
from gateway.public_failure import describe

OPERATIONS = (
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
# Request operations that share another operation's alias table.
_SECTION_FOR_OPERATION = {
    "completions": "chat",
    "translations": "transcriptions",
}
ENGINE_OPERATIONS = ("speech", "transcriptions", "rerank", "decisions")
_MAX_CHAIN = 6

EngineAvailable = Callable[[str, Mapping[str, Any]], bool]


class AliasUnavailableError(Exception):
    """A known alias or engine name resolves to nothing that is deployed."""

    def __init__(self, message: str, *, code: str = "model_not_deployed", fallback_declared: bool = False,
                 details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.fallback_declared = fallback_declared
        # Alias names, engine ids and candidates: for builders, not the sentence.
        self.details = dict(details or {})

    @classmethod
    def public(cls, code: str, model: str, *, fallback_declared: bool = False,
               **details: Any) -> "AliasUnavailableError":
        layer, message = describe(code, audience="api", model=model)
        return cls(message, code=code, fallback_declared=fallback_declared,
                   details={"layer": layer, **details})


@dataclass(frozen=True)
class AliasResolution:
    requested: str
    operation: str
    layer: str  # "openai" | "stable" | "engine"
    chain: tuple[str, ...]
    target: str  # concrete role/workflow/model id, or the engine id
    engine: str | None = None
    service: str | None = None
    upstream_model: str | None = None
    skipped_engines: tuple[str, ...] = ()
    engine_spec: Mapping[str, Any] | None = None
    # Deployed engines after the picked one in the same tier, as (engine id, service id), in order.
    # A caller that cannot start the picked engine may try these; empty for a name that is an engine id.
    fallbacks: tuple[tuple[str, str | None], ...] = ()
    fallback_declared: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "requested": self.requested,
            "operation": self.operation,
            "layer": self.layer,
            "chain": list(self.chain),
            "resolves_to": self.target,
        }
        if self.engine:
            out["engine"] = self.engine
            out["service"] = self.service
            out["upstream_model"] = self.upstream_model
        if self.skipped_engines:
            out["skipped_engines"] = list(self.skipped_engines)
        if self.fallbacks:
            out["fallback_engines"] = [engine for engine, _service in self.fallbacks]
        return out


def section_for_operation(operation: str) -> str:
    return _SECTION_FOR_OPERATION.get(operation, operation)


def _norm(name: Any) -> str:
    return str(name or "").strip().lower()


def _section(cfg: Mapping[str, Any] | None, operation: str) -> Mapping[str, Any]:
    if not isinstance(cfg, Mapping):
        return {}
    section = cfg.get(section_for_operation(operation))
    return section if isinstance(section, Mapping) else {}


def _stable_map(section: Mapping[str, Any]) -> dict[str, Any]:
    raw = section.get("stable")
    if not isinstance(raw, Mapping):
        return {}
    return {_norm(k): v for k, v in raw.items() if _norm(k)}


def _engine_map(section: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = section.get("engines")
    if not isinstance(raw, Mapping):
        return {}
    return {
        _norm(k): v for k, v in raw.items() if _norm(k) and isinstance(v, Mapping)
    }


def _patterns(section: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = section.get("openai")
    return [p for p in raw if isinstance(p, Mapping)] if isinstance(raw, list) else []


def _engine_resolution(
    *,
    requested: str,
    operation: str,
    layer: str,
    chain: tuple[str, ...],
    engine_id: str,
    spec: Mapping[str, Any],
    skipped: tuple[str, ...] = (),
    fallbacks: tuple[tuple[str, str | None], ...] = (),
    fallback_declared: bool = False,
) -> AliasResolution:
    return AliasResolution(
        requested=requested,
        operation=operation,
        layer=layer,
        chain=chain,
        target=engine_id,
        engine=engine_id,
        service=str(spec.get("service") or "") or None,
        upstream_model=str(spec.get("model") or "") or None,
        skipped_engines=skipped,
        engine_spec=spec,
        fallbacks=fallbacks,
        fallback_declared=fallback_declared,
    )


def _fallbacks_after(
    candidates: list[Any],
    picked: str,
    engines: Mapping[str, Mapping[str, Any]],
    available: EngineAvailable,
) -> tuple[tuple[str, str | None], ...]:
    names = [_norm(raw) for raw in candidates]
    rest = names[names.index(picked) + 1 :] if picked in names else []
    return tuple(
        (engine_id, str(engines[engine_id].get("service") or "") or None)
        for engine_id in rest
        if engine_id in engines and available(engine_id, engines[engine_id])
    )


def _pick_engine(
    candidates: list[Any],
    engines: Mapping[str, Mapping[str, Any]],
    available: EngineAvailable,
) -> tuple[str, Mapping[str, Any], tuple[str, ...]] | None:
    skipped: list[str] = []
    for raw in candidates:
        engine_id = _norm(raw)
        spec = engines.get(engine_id)
        if spec is None:
            skipped.append(engine_id)
            continue
        if available(engine_id, spec):
            return engine_id, spec, tuple(skipped)
        skipped.append(engine_id)
    return None


def resolve_alias(
    cfg: Mapping[str, Any] | None,
    operation: str,
    name: Any,
    *,
    engine_available: EngineAvailable = lambda _id, _spec: True,
) -> AliasResolution | None:
    """Resolve ``name`` for ``operation``.

    Returns ``None`` when ``name`` is not an alias (the caller keeps treating it
    as a concrete model id). Raises :class:`AliasUnavailableError` when the name
    is a known alias or engine but nothing behind it is deployed, or (code
    ``alias_not_configured``) when the owner has not set its target yet.
    """
    requested = str(name or "").strip()
    key = _norm(requested)
    if not key:
        return None
    section = _section(cfg, operation)
    if not section:
        return None
    stable = _stable_map(section)
    engines = _engine_map(section)
    section_name = section_for_operation(operation)

    chain: list[str] = []
    layer = ""
    cur = key

    if cur not in stable and cur not in engines:
        for pattern in _patterns(section):
            match = _norm(pattern.get("match"))
            if not match or not fnmatch.fnmatchcase(cur, match):
                continue
            if pattern.get("passthrough"):
                return None
            target = _norm(pattern.get("to"))
            if not target:
                continue
            layer = "openai"
            chain.append(f"{requested} (matches {pattern.get('match')})")
            cur = target
            break
        else:
            return None

    for _ in range(_MAX_CHAIN):
        if cur in stable:
            layer = layer or "stable"
            spec = stable[cur]
            chain.append(cur)
            if isinstance(spec, list):
                picked = _pick_engine(spec, engines, engine_available)
                if picked is None:
                    raise AliasUnavailableError.public(
                        ("model_not_deployed" if any(_norm(c) in engines for c in spec) else "alias_misconfigured"),
                        requested, fallback_declared=len(spec) > 1,
                        alias=cur, candidates=[str(c) for c in spec],
                    )
                engine_id, engine_spec, skipped = picked
                chain.append(engine_id)
                return _engine_resolution(
                    requested=requested,
                    operation=operation,
                    layer=layer,
                    chain=tuple(chain),
                    engine_id=engine_id,
                    spec=engine_spec,
                    skipped=skipped,
                    fallbacks=_fallbacks_after(spec, engine_id, engines, engine_available),
                    fallback_declared=len(spec) > 1,
                )
            target = str(spec or "").strip()
            if not target:
                raise AliasUnavailableError.public(
                    "alias_not_configured", requested, alias=cur, section=section_name,
                )
            target_key = _norm(target)
            if target_key != cur and (target_key in stable or target_key in engines):
                cur = target_key
                continue
            if target_key != cur:
                chain.append(target)
            return AliasResolution(
                requested=requested,
                operation=operation,
                layer=layer,
                chain=tuple(chain),
                target=target,
            )
        if cur in engines:
            spec = engines[cur]
            if not engine_available(cur, spec):
                raise AliasUnavailableError.public("model_not_deployed", requested, engine=cur)
            chain.append(cur)
            return _engine_resolution(
                requested=requested,
                operation=operation,
                layer=layer or "engine",
                chain=tuple(chain),
                engine_id=cur,
                spec=spec,
            )
        break
    raise AliasUnavailableError.public(
        "alias_misconfigured", requested, section=section_name, reason="unknown target or alias loop",
    )


def _pointer(*parts: Any) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _alias_issues(cfg: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """Structural problems as ``(JSON Pointer, text)``; the pointer is the entry the problem sits on ('' = the section)."""
    issues: list[tuple[str, str]] = []
    if not isinstance(cfg, Mapping):
        return [("", "model_aliases must be a mapping")]
    for operation in cfg:
        if operation not in OPERATIONS:
            issues.append((_pointer(operation), f"unknown operation section: {operation}"))
    for operation in OPERATIONS:
        section = cfg.get(operation)
        if section is None:
            continue
        if not isinstance(section, Mapping):
            issues.append((_pointer(operation), f"{operation}: must be a mapping"))
            continue
        stable = _stable_map(section)
        engines = _engine_map(section)
        if engines and operation not in ENGINE_OPERATIONS:
            issues.append((_pointer(operation, "engines"),
                           f"{operation}: engines are only valid for speech/transcriptions/rerank/decisions"))
        for engine_id, spec in engines.items():
            where = _pointer(operation, "engines", engine_id)
            if not spec.get("service"):
                issues.append((where, f"{operation}.engines.{engine_id}: missing service"))
            if not spec.get("model"):
                issues.append((where, f"{operation}.engines.{engine_id}: missing model"))
        for name, spec in stable.items():
            where = _pointer(operation, "stable", name)
            if spec is None:
                continue  # exists, not configured: a request answers alias_not_configured
            if isinstance(spec, list):
                if not spec:
                    issues.append((where, f"{operation}.stable.{name}: engine list must not be empty"))
                if operation not in ENGINE_OPERATIONS:
                    issues.append((where, f"{operation}.stable.{name}: engine list not allowed here"))
                for cand in spec:
                    if _norm(cand) not in engines:
                        issues.append((where, f"{operation}.stable.{name}: unknown engine {cand!r}"))
            elif not isinstance(spec, str) or not spec.strip():
                issues.append((where, f"{operation}.stable.{name}: target must be a non-empty string"))
            elif operation in ENGINE_OPERATIONS:
                issues.append((where, f"{operation}.stable.{name}: must be an engine list"))
        for pattern in _patterns(section):
            where = _pointer(operation, "openai")
            if not pattern.get("match"):
                issues.append((where, f"{operation}.openai: entry without match"))
            elif not pattern.get("passthrough"):
                for published in pattern_names(pattern):
                    if not fnmatch.fnmatchcase(_norm(published), _norm(pattern["match"])):
                        issues.append((where, f"{operation}.openai[{pattern['match']}]: "
                                              f"name {published!r} does not match the pattern"))
                target = _norm(pattern.get("to"))
                if not target:
                    issues.append((where, f"{operation}.openai[{pattern['match']}]: missing to"))
                elif target not in stable and target not in engines:
                    issues.append((where, f"{operation}.openai[{pattern['match']}]: "
                                          f"to={target!r} is not a stable alias or engine"))
        # Loop check: follow every string chain.
        for name in stable:
            seen = {name}
            cur = name
            while isinstance(stable.get(cur), str):
                nxt = _norm(stable[cur])
                if nxt == cur or nxt not in stable:
                    break
                if nxt in seen:
                    issues.append((_pointer(operation, "stable", name), f"{operation}.stable.{name}: alias loop"))
                    break
                seen.add(nxt)
                cur = nxt
    return issues


def validate_aliases(cfg: Mapping[str, Any] | None) -> list[str]:
    """Return structural problems in a ``model_aliases`` mapping (empty = ok)."""
    return [text for _pointer_, text in _alias_issues(cfg)]


def alias_problems(candidate: Mapping[str, Any] | None, context: Mapping[str, Any] | None = None) -> list[Problem]:
    """The ``model_aliases`` section's rule for the config layer: a structure fault is invalid, a gone service dangles.

    A loop, an unknown engine or an empty target would break resolution, so the layer refuses it on write and skips it
    on load. An engine whose ``service`` the registry no longer holds is the owner's data that is true, but its
    target is gone: it is applied and shown dangling, never refused (design 3.5). With no registry in the context
    (the release self-check at start) only the structure is judged. A stable name's string target (a role, a workflow,
    a model id) cannot be checked here: what it may name depends on the operation and on what runs.
    """
    problems = [Problem(pointer or None, "alias_invalid", INVALID, text) for pointer, text in _alias_issues(candidate)]
    registry = (context or {}).get("registry")
    services = registry.get("services") if isinstance(registry, Mapping) else None
    if isinstance(candidate, Mapping) and isinstance(services, Mapping):
        for operation in OPERATIONS:
            for engine_id, spec in _engine_map(_section(candidate, operation)).items():
                service = str(spec.get("service") or "")
                if service and service not in services:
                    problems.append(Problem(
                        _pointer(operation, "engines", engine_id), "service_gone", DANGLING,
                        f"{operation}.engines.{engine_id}: service {service!r} is not in the service registry",
                    ))
    return problems


def concrete_targets(cfg: Mapping[str, Any] | None, operation: str) -> dict[str, str]:
    """Map every string-valued stable alias to its final concrete target."""
    section = _section(cfg, operation)
    out: dict[str, str] = {}
    for name in _stable_map(section):
        try:
            res = resolve_alias(cfg, operation, name)
        except AliasUnavailableError:
            continue
        if res is not None and res.engine is None:
            out[name] = res.target
    return out


# (operation in model_aliases, config section that holds its legacy ``aliases`` table), in the order of the two proxy
# constants VIDEO_ALIASES and AUDIO_ALIASES.
LEGACY_ALIAS_TABLES = (("videos", "videos"), ("audio_generate", "audio"))


def fold_workflow_aliases(config: dict[str, Any]) -> None:
    """Mirror video/audio-generate aliases into the legacy alias tables.

    The video and audio-generate paths, media jobs and capability builders read
    ``videos.aliases`` / ``audio.aliases``. Folding keeps those readers working
    while ``model_aliases`` stays the single place an operator edits. Exact
    names only; glob patterns are applied by the request resolver.
    """
    cfg = config.get("model_aliases")
    for operation, key in LEGACY_ALIAS_TABLES:
        section = _section(cfg, operation)
        if not section:
            continue
        target_section = config.get(key)
        if not isinstance(target_section, dict):
            continue
        aliases = dict(target_section.get("aliases") or {})
        for name, target in concrete_targets(cfg, operation).items():
            aliases[name] = target
        for pattern in _patterns(section):
            match = str(pattern.get("match") or "")
            if pattern.get("passthrough") or any(c in match for c in "*?["):
                continue
            res = resolve_alias(cfg, operation, match)
            if res is not None and res.engine is None:
                aliases[match.lower()] = res.target
        target_section["aliases"] = aliases


def pattern_names(pattern: Mapping[str, Any]) -> list[str]:
    """Literal names a wildcard pattern publishes in model lists (``names:``)."""
    raw = pattern.get("names")
    if not isinstance(raw, list):
        return []
    return [str(name).strip() for name in raw if isinstance(name, str) and name.strip()]


def _published_name_rows(
    cfg: Mapping[str, Any] | None,
    operation: str,
    pattern: Mapping[str, Any],
    engine_available: EngineAvailable,
) -> list[dict[str, Any]]:
    """One discovery row per literal name a pattern publishes, shaped like a stable row.

    A pattern such as ``jev-*`` accepts any name, so it cannot be listed. Its
    ``names`` are the ones the owner wants shown. Each is a real alias: it
    resolves through the pattern to the same place any matching request does.
    """
    rows: list[dict[str, Any]] = []
    for name in pattern_names(pattern):
        row: dict[str, Any] = {"alias": _norm(name), "via": str(pattern.get("match") or "")}
        try:
            res = resolve_alias(cfg, operation, name, engine_available=engine_available)
        except AliasUnavailableError as exc:
            row["resolves_to"] = None
            row["error"] = str(exc)
        else:
            if res is not None:
                row["resolves_to"] = res.target
                if res.engine:
                    row["engine"] = res.engine
                    row["service"] = res.service
                    row["upstream_model"] = res.upstream_model
                if res.skipped_engines:
                    row["skipped_engines"] = list(res.skipped_engines)
        rows.append(row)
    return rows


def describe_aliases(
    cfg: Mapping[str, Any] | None,
    *,
    engine_available: EngineAvailable = lambda _id, _spec: True,
    operation: str | None = None,
) -> dict[str, Any]:
    """Discovery view: what every alias currently resolves to."""
    out: dict[str, Any] = {}
    operations = (operation,) if operation else OPERATIONS
    for op in operations:
        section = _section(cfg, op)
        if not section:
            continue
        stable_rows: list[dict[str, Any]] = []
        for name, spec in _stable_map(section).items():
            row: dict[str, Any] = {"alias": name}
            try:
                res = resolve_alias(cfg, op, name, engine_available=engine_available)
            except AliasUnavailableError as exc:
                row["resolves_to"] = None
                row["error"] = str(exc)
                if exc.code == "alias_not_configured":
                    row["configured"] = False  # the name exists; no target is set (a client list leaves it out)
            else:
                if res is not None:
                    row["resolves_to"] = res.target
                    if res.engine:
                        row["engine"] = res.engine
                        row["service"] = res.service
                        row["upstream_model"] = res.upstream_model
                    if res.skipped_engines:
                        row["skipped_engines"] = list(res.skipped_engines)
            if isinstance(spec, list):
                row["candidates"] = [_norm(c) for c in spec]
            stable_rows.append(row)
        openai_rows: list[dict[str, Any]] = []
        for pattern in _patterns(section):
            match = str(pattern.get("match") or "")
            if pattern.get("passthrough"):
                openai_rows.append({"match": match, "passthrough": True})
                continue
            stable_rows.extend(_published_name_rows(cfg, op, pattern, engine_available))
            row = {"match": match, "to": str(pattern.get("to") or "")}
            try:
                probe = match.replace("*", "x").replace("?", "x")
                res = resolve_alias(cfg, op, probe, engine_available=engine_available)
                row["resolves_to"] = res.target if res else None
            except AliasUnavailableError as exc:
                row["resolves_to"] = None
                row["error"] = str(exc)
            openai_rows.append(row)
        engine_rows = [
            {
                "engine": engine_id,
                "service": spec.get("service"),
                "model": spec.get("model"),
                "deployed": bool(engine_available(engine_id, spec)),
                **{
                    k: spec[k]
                    for k in ("description", "placement", "planned")
                    if k in spec
                },
            }
            for engine_id, spec in _engine_map(section).items()
        ]
        entry: dict[str, Any] = {"stable": stable_rows, "openai": openai_rows}
        if engine_rows:
            entry["engines"] = engine_rows
        out[op] = entry
    return copy.deepcopy(out)


__all__ = [
    "AliasResolution",
    "alias_problems",
    "AliasUnavailableError",
    "ENGINE_OPERATIONS",
    "OPERATIONS",
    "concrete_targets",
    "describe_aliases",
    "fold_workflow_aliases",
    "pattern_names",
    "resolve_alias",
    "section_for_operation",
    "validate_aliases",
]
