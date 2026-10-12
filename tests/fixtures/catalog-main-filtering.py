# Frozen main baseline (f18ef653ae3b7817e60b71f6148b2cdb5b78ce3d), minus the removed Tabby loaded-model branch.
# Executed with test-owned proxy globals; no gateway import or state initialization.
# Compare visible IDs and real probe calls, not the intentionally fixed ID leaks.
from __future__ import annotations

async def _current_loaded_llm_model_ids(active_backend: str | None = None) -> set[str]:
    """Return public ids that resolve to the currently loaded LLM backend."""
    try:
        active = active_backend or await get_active_backend()
    except Exception:
        active = None

    ids: set[str] = set()
    loaded_path = currently_loaded_kobold_model()
    if loaded_path:
        ids.update(USE_LOADED_MODEL_ALIASES)
        ids.add(loaded_path)
        basename = os.path.basename(loaded_path)
        if basename:
            ids.add(basename)
        ids.update(
            _role_catalog_ids_for_loaded_path(
                backend="kobold",
                loaded_path=loaded_path,
                loaded_model_id=basename,
            )
        )
    return {item for item in ids if item}

async def _filter_model_list_for_profile(
    data: list[Any],
    auth_context: AuthContext,
) -> tuple[list[Any], dict[str, Any]]:
    metadata: dict[str, Any] = {}
    filtered = [item for item in data if isinstance(item, dict)]
    registry = ADMIN_STORE.get_service_registry()
    remote_models = provider_execution.litellm_provider_model_lookup(registry)
    profile = _profile_for_auth_context(auth_context)
    provider_refs = providers.provider_refs(registry)
    visible = []
    for item in filtered:
        facts = remote_models.get(str(item.get("id") or ""))
        if facts and facts.get("remote"):
            provider_id = facts["provider_id"]
            if provider_id not in profile.remote.providers:
                continue
            provider = provider_refs.get(provider_id)
            ready = bool(provider and provider.configured)
            item = {**item, "remote": True, "cost_class": "paid-remote", "available": ready}
            if not ready:
                item["reason"] = "provider_key_missing"
        visible.append(item)
    filtered = visible

    allowlist = _profile_model_allowlist(auth_context)
    if allowlist is not None:
        allowed_ids = {normalize_model_id(item) for item in allowlist}
        filtered = [
            item
            for item in filtered
            if normalize_model_id(str(item.get("id") or "")) in allowed_ids
        ]
        metadata["azizam_profile_model_allowlist"] = sorted(allowlist)

    if _profile_target_visibility(auth_context) == "available":
        loaded_ids = await _current_loaded_llm_model_ids()
        loaded_norm = {normalize_model_id(item) for item in loaded_ids}
        comfy_available = _comfy_target_available()
        filtered = [
            item
            for item in filtered
            if (
                normalize_model_id(str(item.get("id") or "")) in loaded_norm
                or (
                    bool(item.get("azizam_current_loaded"))
                    and bool(loaded_norm)
                )
                or (
                    bool(item.get("azizam_workflow"))
                    and str(item.get("azizam_backend") or "") in {"comfyui", "gateway"}
                    and comfy_available
                )
                or (
                    str(item.get("azizam_backend") or "") == "embeddings"
                    and EMBEDDINGS_ENABLED
                )
            )
        ]
        metadata["azizam_target_visibility"] = "available"
        metadata["azizam_target_activation"] = _profile_target_activation(auth_context)
        metadata["azizam_model_access"] = "current_loaded"
        metadata["azizam_current_model_ids"] = sorted(
            item for item in loaded_ids if item and not item.startswith("/")
        )

    return filtered, metadata

async def _context_window_for_model_id(
    model_id: str,
    entry: Mapping[str, Any] | None = None,
) -> int | None:
    found = _context_window_from_mapping(entry)
    if found is not None:
        return found

    backend = None
    if isinstance(entry, Mapping):
        backend = str(entry.get("azizam_backend") or "").strip() or None
    configured_backend, record = _configured_model_record_for_id(model_id)
    backend = backend or configured_backend

    found = _context_window_from_mapping(record)
    if found is not None:
        return found

    found = _configured_context_window_for_backend(backend)
    if found is not None:
        return found
    if engine_quarantine.context_window_from_live_probe(backend):
        return await _kobold_context_window()
    return None

async def _annotate_model_context_windows(data: list[Any]) -> None:
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        found = await _context_window_for_model_id(model_id, item)
        if found is not None:
            item["context_window"] = found
