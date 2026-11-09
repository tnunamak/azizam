"""Pure local capability inventory.

Given a ``proxy_config.yml``-shaped dict (and optional enabled flags), produce
:class:`gateway.capabilities.Capability` records describing what *this*
gateway is locally configured to serve. This replaces the small hard-coded
set the gateway has historically advertised.

Capability builders are pure after package rows load once at import:

* No per-call filesystem reads. Workflow file existence and adapter binaries are not
  probed here. Callers may pass optional ``known_workflow_files`` metadata
  (a set of relative paths the caller has already inventoried) to suppress
  "workflow file may not exist on disk" caveats.
* No imports from ``proxy.py``. The dict shape is the contract.
* No network. Health and lifecycle are reported ``unknown`` unless the caller
  threads them in separately.
* No reads of environment variables.

Policy filtering by :class:`gateway.policy.AppProfile` is intentionally *not*
done here — that belongs to discovery, where the capabilities are projected
for a specific audience and profile. We only emit raw capability records.

Vision modality inference is conservative:

* A role with a ``mmproj`` path or whose name is ``vision`` is treated as
  supporting ``image`` input.
* A catalog entry with ``mmproj`` not ``None`` is treated likewise.
* The ``vision`` capability listed on a backend in ``backends.*.capabilities``
  also flags vision on roles/models bound to that backend.

Paths and secrets (e.g. ``model``, ``mmproj``, ``path``) are never copied
onto the :class:`Capability` records, so audience-scoped summaries cannot
leak local paths even when the ``internal`` audience is requested.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from gateway.capabilities import Capability, ResourceEstimate
from gateway.model_files import missing_model_file_caveat, model_file_availability

# Intent vocabulary used by the gateway. Mirrors what proxy.py advertises
# today, kept here so this module does not depend on the proxy.
INTENT_CHAT = "chat.completions"
INTENT_COMPLETIONS = "text.completions"
INTENT_EMBEDDINGS = "text.embeddings"
INTENT_SPEECH = "speech.synthesis"
INTENT_TRANSCRIPTIONS = "speech.transcription"
INTENT_TRANSLATIONS = "speech.translation"
INTENT_IMAGE_GENERATE = "image.generate"
INTENT_IMAGE_EDIT = "image.edit"
INTENT_VIDEO_GENERATE = "video.generate"
INTENT_AUDIO_GENERATE = "audio.generate"


def build_local_capabilities(
    config: Mapping[str, Any],
    *,
    enabled_flags: Mapping[str, bool] | None = None,
    known_workflow_files: Iterable[str] | None = None,
    backends: Mapping[str, Any] | None = None,
) -> list[Capability]:
    """Build raw :class:`Capability` records from a proxy config dict.

    ``enabled_flags`` toggles whole capability families. Recognised keys:
    ``llm``, ``embeddings``, ``tts``, ``stt``, ``images``, ``videos``,
    ``audio``. Missing keys default to ``True``.

    ``known_workflow_files`` is the set of workflow file paths the caller
    has already verified exist (relative to the proxy working directory).
    When supplied, workflow entries whose ``file`` is *not* in the set get
    an extra caveat. When ``None`` (the default), no existence claim is
    made — callers that don't want to do that check just leave it unset.

    ``backends`` overrides the backends map read from ``config["backends"]``.
    Pass a live projection (e.g. from
    ``service_registry.runtime_backends(registry)``) so that capability
    discovery reflects the current DB service registry rather than the
    frozen-at-boot config.  When ``None`` (the default) the function falls
    back to ``config.get("backends")``.
    """
    flags = dict(enabled_flags or {})
    known_files = set(known_workflow_files or ())

    capabilities: list[Capability] = []

    if backends is None:
        backends = _as_mapping(config.get("backends"))
    else:
        backends = _as_mapping(backends)
    capabilities.extend(_build_llm_capabilities(config, backends, flags))
    capabilities.extend(_build_openai_endpoint_capabilities(backends, flags))
    capabilities.extend(_build_tts_capabilities(backends, flags))
    capabilities.extend(_build_stt_capabilities(backends, flags))
    capabilities.extend(_build_image_capabilities(config, backends, flags))
    capabilities.extend(_build_workflow_capabilities(config, backends, flags, known_files))

    return capabilities


_LOCAL_CAPABILITY_ROWS_PATH = (
    Path(__file__).resolve().parents[1] / "registry" / "local_capability_rows.yml"
)


def _load_local_capability_rows(
    path: str | Path = _LOCAL_CAPABILITY_ROWS_PATH,
) -> tuple[
    Mapping[str, tuple[str, tuple[str, ...], tuple[str, ...], str]],
    tuple[tuple[str, str, str, tuple[str, ...], tuple[str, ...], str], ...],
    tuple[Mapping[str, Any], ...],
    Mapping[str, Any],
    tuple[Mapping[str, Any], ...],
    Mapping[str, Any],
]:
    """Load ordered package facts; errors identify the file and logical row."""
    source = Path(path)

    def fail(where: str, message: str) -> None:
        raise ValueError(f"{source}: {where}: {message}")

    def fields(
        row: Any, required: set[str], where: str, optional: frozenset[str] = frozenset(),
    ) -> None:
        if not isinstance(row, Mapping):
            fail(where, "must be a mapping")
        missing = required - row.keys()
        unknown = row.keys() - required - optional
        if missing:
            fail(where, f"missing fields: {', '.join(sorted(missing))}")
        if unknown:
            fail(where, f"unknown fields: {', '.join(sorted(map(str, unknown)))}")

    def text(row: Mapping[str, Any], key: str, where: str) -> str:
        value = row[key]
        if not isinstance(value, str) or not value.strip():
            fail(where, f"{key} must be a non-empty string")
        return value

    def modalities(row: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
        value = row[key]
        if not isinstance(value, list) or not value or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            fail(where, f"{key} must be a non-empty list of non-empty strings")
        return tuple(value)

    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        fail("document row 0", f"could not read rows: {error}")
    fields(
        document, {
            "schema_version", "alias_operations", "workflow_families",
            "image_defaults", "image_rows", "workflow_defaults",
        },
        "document row 0", frozenset({"backend_rows"}),
    )
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        fail("document row 0", "schema_version must be 1")

    aliases = {}
    families = []
    common = {"intent", "input_modalities", "output_modalities"}
    for table, name_key, required in (
        ("alias_operations", "name", common | {"name", "execution"}),
        ("workflow_families", "section", common | {"section", "enabled_flag", "resource_tier"}),
    ):
        rows = document[table]
        if not isinstance(rows, list):
            fail("document row 0", f"{table} must be a list")
        names = set()
        for number, row in enumerate(rows, start=1):
            where = f"{table} row {number}"
            fields(row, required, where)
            name = text(row, name_key, where)
            if name in names:
                fail(where, f"duplicate name: {name!r}")
            names.add(name)
            intent = text(row, "intent", where)
            inputs = modalities(row, "input_modalities", where)
            outputs = modalities(row, "output_modalities", where)
            if table == "alias_operations":
                execution = text(row, "execution", where)
                if execution not in {"sync", "job"}:
                    fail(where, "execution must be sync or job")
                aliases[name] = (intent, inputs, outputs, execution)
            else:
                flag = text(row, "enabled_flag", where)
                tier = text(row, "resource_tier", where)
                if tier not in {"batch", "expensive"}:
                    fail(where, "resource_tier must be batch or expensive")
                families.append((name, flag, intent, inputs, outputs, tier))
    default_fields = {
        "section_required", "backend_presence", "default_backend",
        "execution", "streaming", "gpu_required",
    }

    def family_defaults(table: str, required: set[str]) -> Mapping[str, Any]:
        where = f"{table} row 1"
        row = document[table]
        fields(row, required, where)
        frozen = dict(row)
        for key in ("section", "enabled_flag", "default_backend", "backend_presence", "execution"):
            if key in required:
                text(row, key, where)
        for key in ("section_required", "streaming", "gpu_required"):
            if type(row[key]) is not bool:
                fail(where, f"{key} must be a boolean")
        if row["backend_presence"] not in {"declared", "nonempty_mapping"}:
            fail(where, "backend_presence must be declared or nonempty_mapping")
        if row["execution"] not in {"sync", "job"}:
            fail(where, "execution must be sync or job")
        if "resource_tier" in required:
            tier = text(row, "resource_tier", where)
            if tier not in {"batch", "expensive"}:
                fail(where, "resource_tier must be batch or expensive")
        if "output_modalities" in required:
            frozen["output_modalities"] = modalities(row, "output_modalities", where)
        return MappingProxyType(frozen)

    image_defaults = family_defaults("image_defaults", default_fields | {
        "section", "enabled_flag", "output_modalities", "resource_tier",
    })
    workflow_defaults = family_defaults("workflow_defaults", default_fields)
    if not isinstance(document["image_rows"], list):
        fail("document row 0", "image_rows must be a list")
    images = []
    intents = set()
    for number, row in enumerate(document["image_rows"], start=1):
        where = f"image_rows row {number}"
        fields(row, {"intent", "input_modalities", "model_sources"}, where)
        intent = text(row, "intent", where)
        if intent in intents:
            fail(where, f"duplicate intent: {intent!r}")
        intents.add(intent)
        sources = modalities(row, "model_sources", where)
        for source_path in sources:
            parts = source_path.split(".")
            if len(parts) < 2 or any(not part or part != part.strip() for part in parts):
                fail(where, f"model_sources must contain dotted paths: {source_path!r}")
            if parts[0] not in {"backend", "config"}:
                fail(where, f"unknown model_sources root: {parts[0]!r}")
        images.append(MappingProxyType({
            "intent": intent,
            "input_modalities": modalities(row, "input_modalities", where),
            "model_sources": sources,
        }))
    backend_rows = []
    triggers_seen: dict[str, int] = {}
    rows = document.get("backend_rows", [])
    if not isinstance(rows, list):
        fail("document row 0", "backend_rows must be a list")
    required = {
        "group", "triggers", "flag", "intent", "inputs", "outputs",
        "streaming", "execution", "gpu_required",
    }
    optional = frozenset({
        "vision", "resources_from", "tags", "action_label", "recordable_inputs", "caveats",
    })
    for number, row in enumerate(rows, start=1):
        where = f"backend_rows row {number}"
        fields(row, required, where, optional)
        group = text(row, "group", where)
        if group not in {"endpoints", "speech", "recognition"}:
            fail(where, "unknown group")
        flag = text(row, "flag", where)
        if flag not in {"llm", "embeddings", "tts", "stt", "images", "videos", "audio"}:
            fail(where, "unknown flag")
        triggers = tuple(value.lower() for value in modalities(row, "triggers", where))
        for trigger in triggers:
            if trigger in triggers_seen:
                fail(where, f"duplicate trigger: {trigger!r} (first in backend_rows row {triggers_seen[trigger]})")
            triggers_seen[trigger] = number
        execution = text(row, "execution", where)
        if execution not in {"sync", "job"}:
            fail(where, "execution must be sync or job")
        for key in ("streaming", "gpu_required"):
            if type(row[key]) is not bool:
                fail(where, f"{key} must be a boolean")
        frozen = {
            "group": group, "triggers": triggers, "flag": flag,
            "intent": text(row, "intent", where),
            "inputs": modalities(row, "inputs", where),
            "outputs": modalities(row, "outputs", where),
            "streaming": row["streaming"], "execution": execution,
            "gpu_required": row["gpu_required"],
        }
        for key in ("tags", "recordable_inputs", "caveats"):
            if key in row:
                frozen[key] = modalities(row, key, where)
        if "action_label" in row:
            frozen["action_label"] = text(row, "action_label", where)
        if "resources_from" in row:
            if text(row, "resources_from", where) != "backend":
                fail(where, "resources_from must be backend")
            frozen["resources_from"] = "backend"
        if "vision" in row:
            vision_where = f"{where} vision"
            fields(row["vision"], {"trigger", "inputs"}, vision_where)
            frozen["vision"] = MappingProxyType({
                "trigger": text(row["vision"], "trigger", vision_where).lower(),
                "inputs": modalities(row["vision"], "inputs", vision_where),
            })
        backend_rows.append(MappingProxyType(frozen))
    return (
        MappingProxyType(aliases), tuple(families), tuple(backend_rows),
        image_defaults, tuple(images), workflow_defaults,
    )


def load_local_capability_rows(
    path: str | Path = _LOCAL_CAPABILITY_ROWS_PATH,
) -> tuple[
    Mapping[str, tuple[str, tuple[str, ...], tuple[str, ...], str]],
    tuple[tuple[str, str, str, tuple[str, ...], tuple[str, ...], str], ...],
]:
    """Keep the original two-table API, validating all supplied tables."""
    aliases, families = _load_local_capability_rows(path)[:2]
    return aliases, families


def load_backend_capability_rows(
    path: str | Path = _LOCAL_CAPABILITY_ROWS_PATH,
) -> tuple[Mapping[str, Any], ...]:
    """Load immutable backend rows; legacy two-table files yield no rows."""
    return _load_local_capability_rows(path)[2]


def load_local_capability_image_rows(
    path: str | Path = _LOCAL_CAPABILITY_ROWS_PATH,
) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...], Mapping[str, Any]]:
    """Return deeply read-only image defaults/rows and workflow defaults."""
    return _load_local_capability_rows(path)[3:]


(
    _ALIAS_INTENTS, _WORKFLOW_FAMILIES, _BACKEND_ROWS,
    _IMAGE_DEFAULTS, _IMAGE_ROWS, _WORKFLOW_DEFAULTS,
) = _load_local_capability_rows()


def build_alias_capabilities(alias_views: Mapping[str, Any]) -> list[Capability]:
    """One capability per stable alias, tagged with what it resolves to.

    ``alias_views`` is the output of ``stable_aliases.describe_aliases``.
    Aliases whose target is currently unresolvable are skipped.
    """
    out: list[Capability] = []
    for operation, view in alias_views.items():
        spec = _ALIAS_INTENTS.get(operation)
        if spec is None:
            continue
        intent, inputs, outputs, execution = spec
        for row in _as_mapping(view).get("stable", ()):
            row = _as_mapping(row)
            alias = row.get("alias")
            target = row.get("resolves_to")
            if not alias or not target:
                continue
            tags = ["alias", f"resolves_to:{target}"]
            if row.get("engine"):
                tags.append(f"engine:{row['engine']}")
            out.append(
                Capability(
                    intent=intent,
                    backend=str(row.get("service") or "gateway"),
                    model=str(alias),
                    input_modalities=inputs,
                    output_modalities=outputs,
                    execution=execution,
                    tags=tuple(tags),
                    title=row.get("title") or row.get("display_name"),
                    description=row.get("description"),
                    capability=row.get("capability"),
                )
            )
    return out


def _build_llm_capabilities(
    config: Mapping[str, Any],
    backends: Mapping[str, Any],
    flags: Mapping[str, bool],
) -> list[Capability]:
    if not flags.get("llm", True):
        return []

    out: list[Capability] = []
    roles = _as_mapping(config.get("roles"))
    for role_name, role_spec in roles.items():
        spec = _as_mapping(role_spec)
        backend = spec.get("backend")
        if not isinstance(backend, str) or not backend:
            continue
        modalities = _llm_input_modalities(
            role_name=str(role_name),
            spec=spec,
            backend=backend,
            backends=backends,
        )
        caveats: list[str] = []
        if backend not in backends:
            caveats.append(f"backend {backend!r} not declared under backends")
        missing_file = missing_model_file_caveat(spec)
        if missing_file:
            caveats.append(missing_file)
        declared_model = spec.get("model")
        model_title = None
        if isinstance(declared_model, str) and "/" in declared_model:
            # Display the declared weights, never their private filesystem path.
            model_title = declared_model.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        out.append(
            Capability(
                intent=INTENT_CHAT,
                backend=backend,
                model=f"role:{role_name}",
                title=spec.get("title") or spec.get("display_name") or model_title,
                description=spec.get("description"),
                input_modalities=modalities,
                output_modalities=("text",),
                execution="sync",
                streaming=True,
                resources=ResourceEstimate(gpu_required=True),
                caveats=tuple(caveats),
                availability=model_file_availability(spec),
            )
        )

    catalog = config.get("models")
    if isinstance(catalog, list):
        for entry in catalog:
            spec = _as_mapping(entry)
            model_id = spec.get("id")
            backend = spec.get("backend")
            if not isinstance(model_id, str) or not isinstance(backend, str):
                continue
            if not model_id or not backend:
                continue
            modalities = _llm_input_modalities(
                role_name=model_id,
                spec=spec,
                backend=backend,
                backends=backends,
                consider_role_name=False,
            )
            caveats: list[str] = []
            if backend not in backends:
                caveats.append(f"backend {backend!r} not declared under backends")
            missing_file = missing_model_file_caveat(spec)
            if missing_file:
                caveats.append(missing_file)
            out.append(
                Capability(
                    intent=INTENT_CHAT,
                    backend=backend,
                    model=model_id,
                    input_modalities=modalities,
                    output_modalities=("text",),
                    execution="sync",
                    streaming=True,
                    resources=ResourceEstimate(gpu_required=True),
                    caveats=tuple(caveats),
                    availability=model_file_availability(spec),
                )
            )

    return out


def _build_backend_capabilities(
    backends: Mapping[str, Any], flags: Mapping[str, bool], group: str,
) -> list[Capability]:
    """Preserve backend-major, then row order within each public group."""
    rows = tuple(row for row in _BACKEND_ROWS if row["group"] == group)
    out: list[Capability] = []
    for backend_name, raw in backends.items():
        spec = _as_mapping(raw)
        caps = spec.get("capabilities")
        if not isinstance(caps, list):
            continue
        backend_caps = {str(c).lower() for c in caps if isinstance(c, str)}
        for row in rows:
            if not flags.get(row["flag"], True) or not backend_caps.intersection(row["triggers"]):
                continue
            inputs = row["inputs"]
            vision = row.get("vision")
            if vision is not None and vision["trigger"] in backend_caps:
                inputs = vision["inputs"]
            resources = (
                _resource_estimate(spec, default_gpu_required=row["gpu_required"])
                if row.get("resources_from") == "backend"
                else ResourceEstimate(gpu_required=row["gpu_required"])
            )
            extras = {
                key: row[key]
                for key in ("tags", "action_label", "recordable_inputs", "caveats")
                if key in row
            }
            out.append(Capability(
                intent=row["intent"], backend=backend_name,
                input_modalities=inputs, output_modalities=row["outputs"],
                execution=row["execution"], streaming=row["streaming"],
                resources=resources, **extras,
            ))
    return out


def _build_openai_endpoint_capabilities(
    backends: Mapping[str, Any], flags: Mapping[str, bool],
) -> list[Capability]:
    return _build_backend_capabilities(backends, flags, "endpoints")


def _build_tts_capabilities(
    backends: Mapping[str, Any], flags: Mapping[str, bool],
) -> list[Capability]:
    return _build_backend_capabilities(backends, flags, "speech")


def _build_stt_capabilities(
    backends: Mapping[str, Any], flags: Mapping[str, bool],
) -> list[Capability]:
    return _build_backend_capabilities(backends, flags, "recognition")


def _family_backend(section: Mapping[str, Any], family: str, default_backend: str) -> str:
    backend = section.get("backend", default_backend)
    if not isinstance(backend, str) or not backend:
        raise ValueError(
            f"{family}.backend must be a non-empty string; got {backend!r}"
        )
    return backend


def _family_backend_present(backends: Mapping[str, Any], backend: str, presence: str) -> bool:
    if presence == "declared":
        return backend in backends
    return bool(_as_mapping(backends.get(backend)))


def _model_source(path: str, roots: Mapping[str, Any]) -> Any:
    value: Any = roots
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _build_image_capabilities(
    config: Mapping[str, Any],
    backends: Mapping[str, Any],
    flags: Mapping[str, bool],
) -> list[Capability]:
    defaults = _IMAGE_DEFAULTS
    if not flags.get(defaults["enabled_flag"], True):
        return []
    section = _as_mapping(config.get(defaults["section"]))
    if defaults["section_required"] and not section:
        return []
    backend = _family_backend(section, defaults["section"], defaults["default_backend"])
    if not _family_backend_present(backends, backend, defaults["backend_presence"]):
        return []
    backend_spec = _as_mapping(backends.get(backend))
    roots = {"backend": backend_spec, "config": config}
    out: list[Capability] = []
    for row in _IMAGE_ROWS:
        model = None
        for source in row["model_sources"]:
            model = _model_source(source, roots)
            if model:
                break
        # Preserve first-truthy precedence before the type check: a truthy
        # non-string value suppresses the row rather than selecting a fallback.
        if not isinstance(model, str) or not model:
            continue
        out.append(
            Capability(
                intent=row["intent"],
                backend=backend,
                model=model,
                input_modalities=row["input_modalities"],
                output_modalities=defaults["output_modalities"],
                execution=defaults["execution"],
                streaming=defaults["streaming"],
                resource_tier=defaults["resource_tier"],
                resources=ResourceEstimate(gpu_required=defaults["gpu_required"]),
                caveats=_workflow_dir_caveats(backend_spec.get("workflows_dir")),
            )
        )
    return out


def _build_workflow_capabilities(
    config: Mapping[str, Any],
    backends: Mapping[str, Any],
    flags: Mapping[str, bool],
    known_files: set[str],
) -> list[Capability]:
    out: list[Capability] = []
    for (
        section_key, flag_key, intent, input_modalities, output_modalities,
        resource_tier,
    ) in _WORKFLOW_FAMILIES:
        if not flags.get(flag_key, True):
            continue
        section = _as_mapping(config.get(section_key))
        if _WORKFLOW_DEFAULTS["section_required"] and not section:
            continue
        backend = _family_backend(section, section_key, _WORKFLOW_DEFAULTS["default_backend"])
        if not _family_backend_present(backends, backend, _WORKFLOW_DEFAULTS["backend_presence"]):
            continue
        out.extend(
            _workflow_family_capabilities(
                intent=intent,
                backend=backend,
                workflows=_as_mapping(section.get("workflows")),
                aliases=_as_mapping(section.get("aliases")),
                input_modalities=input_modalities,
                output_modalities=output_modalities,
                known_files=known_files,
                resource_tier=resource_tier,
            )
        )
    return out


def _workflow_family_capabilities(
    *,
    intent: str,
    backend: str,
    workflows: Mapping[str, Any],
    aliases: Mapping[str, Any],
    input_modalities: tuple[str, ...],
    output_modalities: tuple[str, ...],
    known_files: set[str],
    resource_tier: str,
) -> list[Capability]:
    out: list[Capability] = []
    alias_index: dict[str, list[str]] = {}
    for alias, target in aliases.items():
        if not isinstance(alias, str) or not isinstance(target, str):
            continue
        alias_index.setdefault(target, []).append(alias)

    for workflow_id, raw_spec in workflows.items():
        spec = _as_mapping(raw_spec)
        if not isinstance(workflow_id, str) or not workflow_id:
            continue
        caveats: list[str] = []
        file_path = spec.get("file")
        if isinstance(file_path, str) and known_files:
            if file_path not in known_files:
                caveats.append(
                    "workflow file not in known set; existence not verified"
                )
        elif not isinstance(file_path, str):
            caveats.append("workflow has no declared file")
        bound_aliases = sorted(alias_index.get(workflow_id, []))
        if bound_aliases:
            caveats.append("aliases: " + ", ".join(bound_aliases))
        out.append(
            Capability(
                intent=intent,
                backend=backend,
                workflow=workflow_id,
                input_modalities=input_modalities,
                output_modalities=output_modalities,
                execution=_WORKFLOW_DEFAULTS["execution"],
                streaming=_WORKFLOW_DEFAULTS["streaming"],
                resource_tier=resource_tier,
                resources=ResourceEstimate(gpu_required=_WORKFLOW_DEFAULTS["gpu_required"]),
                caveats=tuple(caveats),
            )
        )
    return out


def _llm_input_modalities(
    *,
    role_name: str,
    spec: Mapping[str, Any],
    backend: str,
    backends: Mapping[str, Any],
    consider_role_name: bool = True,
) -> tuple[str, ...]:
    has_mmproj = bool(spec.get("mmproj"))
    backend_spec = _as_mapping(backends.get(backend))
    backend_caps_raw = backend_spec.get("capabilities")
    backend_caps: set[str] = set()
    if isinstance(backend_caps_raw, list):
        backend_caps = {
            str(c).lower() for c in backend_caps_raw if isinstance(c, str)
        }
    name_implies_vision = consider_role_name and role_name.lower() == "vision"
    if has_mmproj or name_implies_vision or "vision" in backend_caps:
        return ("text", "image")
    return ("text",)


def _resource_estimate(
    spec: Mapping[str, Any],
    *,
    default_gpu_required: bool,
) -> ResourceEstimate:
    resources = _as_mapping(spec.get("resources"))
    gpu_required = resources.get("gpu_required", default_gpu_required)
    if not isinstance(gpu_required, bool):
        gpu_required = str(gpu_required).strip().lower() in {"1", "true", "yes", "on"}

    vram = resources.get("vram_gb_estimate")
    try:
        vram_gb_estimate = float(vram) if vram is not None else None
    except (TypeError, ValueError):
        vram_gb_estimate = None

    max_parallel = resources.get("max_parallel")
    try:
        max_parallel_value = int(max_parallel) if max_parallel is not None else None
    except (TypeError, ValueError):
        max_parallel_value = None

    duration_class = resources.get("duration_class")
    return ResourceEstimate(
        gpu_required=gpu_required,
        vram_gb_estimate=vram_gb_estimate,
        duration_class=str(duration_class) if duration_class else None,
        max_parallel=max_parallel_value,
    )


def _workflow_dir_caveats(workflows_dir: Any) -> tuple[str, ...]:
    if not isinstance(workflows_dir, str) or not workflows_dir:
        return ("no workflows_dir configured",)
    return ()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


__all__ = [
    "build_alias_capabilities",
    "INTENT_CHAT",
    "INTENT_COMPLETIONS",
    "INTENT_EMBEDDINGS",
    "INTENT_SPEECH",
    "INTENT_TRANSCRIPTIONS",
    "INTENT_IMAGE_GENERATE",
    "INTENT_IMAGE_EDIT",
    "INTENT_VIDEO_GENERATE",
    "INTENT_AUDIO_GENERATE",
    "build_local_capabilities",
]
