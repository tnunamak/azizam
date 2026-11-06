def _prepare_resolved_openai_request(
    body: Mapping[str, Any],
    *,
    request_path: str,
    target: ResolvedRequestTarget | None,
    execution_service_id: str = "",
    runtime_config: Mapping[str, Any] | None = None,
) -> tuple[Response | None, Mapping[str, Any], bytes | None]:
    """Apply the one request-policy pipeline, then generic reasoning handling."""
    prepared = copy.deepcopy(dict(body))
    if target is None:
        return None, prepared, None
    specs = _per_backend_transform_specs(execution_service_id, runtime_config=runtime_config)
    specs.extend(target.base_transform_specs)
    specs.extend(target.alias_overlay_specs)
    if specs:
        prepared = _run_request_pipeline(
            specs,
            prepared,
            _TransformContext(),
            _TRANSFORM_REGISTRY,
        )
    if request_path == "/v1/chat/completions":
        source = runtime_config if runtime_config is not None else config
        reasoning_error = _ReasoningEffortTransform(source.get("reasoning") or {}).apply(prepared)
        if reasoning_error:
            return Response(
                content=json.dumps({"error": {
                    "message": reasoning_error,
                    "type": "invalid_request_error",
                    "param": "reasoning_effort",
                    "code": "invalid_value",
                }}),
                status_code=400,
                media_type="application/json",
            ), prepared, None
    return None, prepared, None
