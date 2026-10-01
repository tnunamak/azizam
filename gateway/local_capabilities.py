"""Pure local capability inventory.

Given a ``proxy_config.yml``-shaped dict (and optional enabled flags), produce
:class:`gateway.capabilities.Capability` records describing what *this*
gateway is locally configured to serve. This replaces the small hard-coded
set the gateway has historically advertised.

This module is pure:

* No filesystem reads. Workflow file existence and adapter binaries are not
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
from typing import Any

from gateway.capabilities import Capability, ResourceEstimate

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
    capabilities.extend(_build_video_capabilities(config, backends, flags, known_files))
    capabilities.extend(_build_audio_capabilities(config, backends, flags, known_files))

    return capabilities


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
        out.append(
            Capability(
                intent=INTENT_CHAT,
                backend=backend,
                model=f"role:{role_name}",
                input_modalities=modalities,
                output_modalities=("text",),
                execution="sync",
                streaming=True,
                resources=ResourceEstimate(gpu_required=True),
                caveats=tuple(caveats),
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
                )
            )

    return out


def _build_openai_endpoint_capabilities(
    backends: Mapping[str, Any],
    flags: Mapping[str, bool],
) -> list[Capability]:
    """Generic chat/completions/embeddings exposed by backends that declare
    them under ``backends.<name>.capabilities``.

    These are *not* keyed to a role/model — they represent that a backend
    can satisfy the OpenAI-style endpoint shape. Routers use these to know
    a passthrough completion is locally supported.
    """
    out: list[Capability] = []
    for backend_name, raw in backends.items():
        spec = _as_mapping(raw)
        caps = spec.get("capabilities")
        if not isinstance(caps, list):
            continue
        backend_caps = {str(c).lower() for c in caps if isinstance(c, str)}
        if flags.get("llm", True):
            if "chat" in backend_caps:
                out.append(
                    Capability(
                        intent=INTENT_CHAT,
                        backend=backend_name,
                        input_modalities=_modalities_for_backend(backend_caps),
                        output_modalities=("text",),
                        execution="sync",
                        streaming=True,
                        resources=ResourceEstimate(gpu_required=True),
                    )
                )
            if "completions" in backend_caps:
                out.append(
                    Capability(
                        intent=INTENT_COMPLETIONS,
                        backend=backend_name,
                        input_modalities=("text",),
                        output_modalities=("text",),
                        execution="sync",
                        streaming=True,
                        resources=ResourceEstimate(gpu_required=True),
                    )
                )
        if flags.get("embeddings", True) and "embeddings" in backend_caps:
            out.append(
                Capability(
                    intent=INTENT_EMBEDDINGS,
                    backend=backend_name,
                    input_modalities=("text",),
                    output_modalities=("text",),
                    execution="sync",
                    streaming=False,
                    resources=_resource_estimate(spec, default_gpu_required=False),
                )
            )
    return out


def _build_tts_capabilities(
    backends: Mapping[str, Any], flags: Mapping[str, bool]
) -> list[Capability]:
    if not flags.get("tts", True):
        return []
    out: list[Capability] = []
    for backend_name, raw in backends.items():
        spec = _as_mapping(raw)
        caps = spec.get("capabilities")
        if not isinstance(caps, list):
            continue
        backend_caps = {str(c).lower() for c in caps if isinstance(c, str)}
        if "speech" in backend_caps or "tts" in backend_caps:
            out.append(
                Capability(
                    intent=INTENT_SPEECH,
                    backend=backend_name,
                    input_modalities=("text",),
                    output_modalities=("audio",),
                    execution="sync",
                    streaming=True,
                    tags=(
                        "response_format:wav",
                        "response_format:mp3",
                        "response_format:opus",
                        "response_format:aac",
                        "response_format:flac",
                        "response_format:pcm",
                    ),
                    resources=ResourceEstimate(gpu_required=True),
                )
            )
    return out


def _build_stt_capabilities(
    backends: Mapping[str, Any], flags: Mapping[str, bool]
) -> list[Capability]:
    if not flags.get("stt", True):
        return []
    out: list[Capability] = []
    for backend_name, raw in backends.items():
        spec = _as_mapping(raw)
        caps = spec.get("capabilities")
        if not isinstance(caps, list):
            continue
        backend_caps = {str(c).lower() for c in caps if isinstance(c, str)}
        if "transcriptions" in backend_caps or "stt" in backend_caps:
            out.append(
                Capability(
                    intent=INTENT_TRANSCRIPTIONS,
                    backend=backend_name,
                    input_modalities=("audio",),
                    output_modalities=("text",),
                    execution="sync",
                    streaming=False,
                    resources=ResourceEstimate(gpu_required=True),
                )
            )
        if "translations" in backend_caps or "translation" in backend_caps:
            out.append(
                Capability(
                    intent=INTENT_TRANSLATIONS,
                    backend=backend_name,
                    input_modalities=("audio",),
                    output_modalities=("text",),
                    execution="sync",
                    streaming=False,
                    resources=ResourceEstimate(gpu_required=True),
                    caveats=(
                        "Only advertised when the local STT backend declares translation support.",
                    ),
                )
            )
    return out


def _build_image_capabilities(
    config: Mapping[str, Any],
    backends: Mapping[str, Any],
    flags: Mapping[str, bool],
) -> list[Capability]:
    if not flags.get("images", True):
        return []
    comfy = _as_mapping(backends.get("comfyui"))
    if not comfy:
        return []
    default_model = comfy.get("default_model")
    default_edit = None
    compat = _as_mapping(config.get("model_compatibility"))
    defaults = _as_mapping(compat.get("defaults"))
    if defaults:
        default_edit = defaults.get("image_edits")
        if not default_model:
            default_model = defaults.get("images")

    out: list[Capability] = []
    if isinstance(default_model, str) and default_model:
        out.append(
            Capability(
                intent=INTENT_IMAGE_GENERATE,
                backend="comfyui",
                model=default_model,
                input_modalities=("text",),
                output_modalities=("image",),
                execution="job",
                streaming=False,
                resource_tier="batch",
                resources=ResourceEstimate(gpu_required=True),
                caveats=_workflow_dir_caveats(comfy.get("workflows_dir")),
            )
        )
    if isinstance(default_edit, str) and default_edit:
        out.append(
            Capability(
                intent=INTENT_IMAGE_EDIT,
                backend="comfyui",
                model=default_edit,
                input_modalities=("text", "image"),
                output_modalities=("image",),
                execution="job",
                streaming=False,
                resource_tier="batch",
                resources=ResourceEstimate(gpu_required=True),
                caveats=_workflow_dir_caveats(comfy.get("workflows_dir")),
            )
        )
    return out


def _build_video_capabilities(
    config: Mapping[str, Any],
    backends: Mapping[str, Any],
    flags: Mapping[str, bool],
    known_files: set[str],
) -> list[Capability]:
    if not flags.get("videos", True):
        return []
    videos = _as_mapping(config.get("videos"))
    if not videos:
        return []
    backend = "comfyui" if "comfyui" in backends else "comfyui"  # canonical
    workflows = _as_mapping(videos.get("workflows"))
    aliases = _as_mapping(videos.get("aliases"))
    return _workflow_family_capabilities(
        intent=INTENT_VIDEO_GENERATE,
        backend=backend,
        workflows=workflows,
        aliases=aliases,
        input_modalities=("text",),
        output_modalities=("video",),
        known_files=known_files,
        resource_tier="expensive",
    )


def _build_audio_capabilities(
    config: Mapping[str, Any],
    backends: Mapping[str, Any],
    flags: Mapping[str, bool],
    known_files: set[str],
) -> list[Capability]:
    if not flags.get("audio", True):
        return []
    audio = _as_mapping(config.get("audio"))
    if not audio:
        return []
    workflows = _as_mapping(audio.get("workflows"))
    aliases = _as_mapping(audio.get("aliases"))
    backend = "comfyui" if "comfyui" in backends else "comfyui"
    return _workflow_family_capabilities(
        intent=INTENT_AUDIO_GENERATE,
        backend=backend,
        workflows=workflows,
        aliases=aliases,
        input_modalities=("text",),
        output_modalities=("audio",),
        known_files=known_files,
        resource_tier="batch",
    )


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
                execution="job",
                streaming=False,
                resource_tier=resource_tier,
                resources=ResourceEstimate(gpu_required=True),
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


def _modalities_for_backend(backend_caps: set[str]) -> tuple[str, ...]:
    if "vision" in backend_caps:
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
