"""Video handler path copied verbatim from proxy.py at fd30cbfca.

The loader supplies that revision's unchanged dependencies and request-local
ports. Do not edit the frozen function bodies; their hashes are pinned beside it.
"""
from __future__ import annotations


def _video_workflow_table() -> Mapping[str, Any]:
    return _videos().get("workflows") or {}


async def proxy_graph_video_create(
    request: Request,
    auth_context: AuthContext,
    *,
    body_override: Mapping[str, Any] | None = None,
    remixed_from_video_id: str | None = None,
    receipt: RouteReceipt | None = None,
) -> Response:
    """POST /v1/videos — submit a job and return {video_id, status: queued}.

    `body_override` and `remixed_from_video_id` let the remix routes reuse this
    pipeline with an already-parsed body. Image inputs of the request hold a media input lease until it ends.
    """
    return await _with_request_media_inputs(lambda holder: _proxy_graph_video_create(
        request, auth_context, body_override=body_override, remixed_from_video_id=remixed_from_video_id,
        receipt=receipt, media_holder=holder,
    ))


async def _proxy_graph_video_create(
    request: Request,
    auth_context: AuthContext,
    *,
    body_override: Mapping[str, Any] | None = None,
    remixed_from_video_id: str | None = None,
    receipt: RouteReceipt | None = None,
    media_holder: media_inputs.Holder,
) -> Response:
    if receipt is None:
        from gateway import media_ingestion
        receipt = await media_ingestion.start_receipt(
            request, auth_context, intent=domain_rows.VIDEO_GENERATE, protocol="openai.video",
            route_path=str(getattr(getattr(request, "url", None), "path", "/v1/videos")),
        )
        try:
            response = await _proxy_graph_video_create(
                request, auth_context, body_override=body_override,
                remixed_from_video_id=remixed_from_video_id, receipt=receipt, media_holder=media_holder,
            )
        except BaseException as error:
            await run_receipt_write(media_ingestion.finish_receipt, receipt, status_code=499 if isinstance(error, asyncio.CancelledError) else 500)
            raise
        return await run_receipt_write(media_ingestion.finish_response, receipt, response)

    # Create has no mounted row to read this from (see registry/README.md, "Execution intents"): it stays spelled here.
    admission_intent = domain_rows.VIDEO_GENERATE
    admission = _graph_media_admission(
        admission_intent, auth_context=auth_context,
    )
    if admission is not None:
        return admission

    if not _graph_service(_VIDEO_GRAPH_CAPABILITY).enabled:
        return Response(
            content=json.dumps({"error": {"message": "Video generation not enabled", "type": "service_unavailable"}}),
            status_code=503,
            media_type="application/json"
        )
    service_response = (await asyncio.to_thread(_service_availability_response, _graph_service_id(_VIDEO_GRAPH_CAPABILITY),
        service_label=_graph_adapter(_VIDEO_GRAPH_CAPABILITY).label,))
    if service_response is not None:
        return service_response

    content_type = request.headers.get("content-type", "")
    if body_override is not None:
        body = dict(body_override)
    elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        # openai-python's videos.create always uploads as multipart/form-data so
        # that input_reference can be a binary upload. Normalize the form into
        # the JSON body shape the rest of this handler expects.
        try:
            body = await _read_video_create_form_body(request)
        except _VideoFormParseError as exc:
            return _json_error(
                400, str(exc), param=exc.param,
                code="unknown_parameter" if exc.param else None,
            )
    else:
        try:
            body = await request.json()
        except Exception:
            return Response(
                content=json.dumps({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}),
                status_code=400,
                media_type="application/json"
            )
    unknown_video_fields = detect_unknown_top_level_fields(
        body,
        STANDARD_VIDEO_CREATE_FIELDS,
    )
    if unknown_video_fields:
        first_name, first_reason = next(iter(unknown_video_fields.items()))
        return Response(
            content=json.dumps({"error": {
                "message": first_reason,
                "type": "invalid_request_error",
                "param": first_name,
                "code": "unknown_parameter",
            }}),
            status_code=400,
            media_type="application/json",
        )
    # Gateway-level controls (extensions.com.azizam.video) are not workflow inputs.
    try:
        video_control_values = video_controls.parse_video_controls(body)
    except video_controls.VideoControlError as exc:
        return _json_error(400, str(exc), param=exc.param, code="invalid_video_control")
    audio_disabled = video_control_values.get(video_controls.AUDIO_CONTROL) is False
    _begin_media_delivery(auth_context, receipt)
    metadata_error = _note_media_metadata_body(body)
    if metadata_error is not None:
        return metadata_error

    raw_prompt = body.get("prompt")
    if raw_prompt is not None and not isinstance(raw_prompt, str):
        return _gateway_input_error("prompt must be a string", param="prompt")
    prompt = (raw_prompt or "").strip()
    requested_model = body.get("model") or _video_default_workflow()
    public_model = str(requested_model)
    size_from_body = body.get("size")
    seconds = body.get("seconds")
    seed = body.get("seed")
    input_reference = body.get("input_reference")  # optional path / URL / data-URI / raw b64 (I2V)
    image_strength = body.get("image_strength")  # optional float 0..1, default 1.0 when input_reference present

    if not prompt and body.get("shots") is None:
        return _gateway_input_error("prompt is required")

    # OpenAI-compat: rewrite sora-* aliases to the local default before workflow lookup.
    try:
        model_resolution = resolve_model_for_operation("videos", requested_model)
    except ModelCompatibilityError as e:
        return model_compatibility_error(e)
    if model_resolution.is_openai_alias:
        print(
            f"[proxy] Video model alias {requested_model} -> {model_resolution.effective_model}",
            flush=True,
        )
        requested_model = model_resolution.effective_model

    workflow_name, workflow_cfg = _resolve_video_workflow(requested_model)
    if not workflow_cfg:
        return model_compatibility_error(
            ModelCompatibilityError(
                f"Model '{requested_model}' is not available for video generation.",
                code="model_not_found",
            )
        )
    # Some models ship separate text-to-video and image-to-video graphs (LTX-2.5,
    # MiniMax H3). `i2v_workflow` on the t2v entry names the image variant; it
    # is selected when the request carries input_reference.
    i2v_workflow_name = workflow_cfg.get("i2v_workflow")
    if input_reference and i2v_workflow_name:
        i2v_cfg = _video_workflow_table().get(i2v_workflow_name)
        if i2v_cfg:
            workflow_name, workflow_cfg = i2v_workflow_name, i2v_cfg
    if workflow_cfg.get("requires_input_reference") and not input_reference:
        return _gateway_input_error(
            f"Model {workflow_name} is image-to-video and requires input_reference", param="input_reference",
            job_message="This model is image-to-video and requires input_reference")
    # Long video: `shots`, or `seconds` past the model's native length when the
    # model names a chain workflow, or a chain workflow itself (video-long).
    chain_name = chain_cfg = None
    if video_extend.engine_for(workflow_cfg.get("builder")):
        chain_name, chain_cfg = workflow_name, workflow_cfg
    else:
        try:
            beyond_native = seconds is not None and float(seconds) > float(workflow_cfg.get("max_seconds", 8))
        except (TypeError, ValueError):
            beyond_native = False
        if body.get("shots") is not None or (beyond_native and workflow_cfg.get("chain_workflow")):
            chain_name, chain_cfg = _chain_workflow_for(requested_model)
            if chain_cfg is None:
                return _gateway_input_error(
                    gateway_messages.GATEWAY_MESSAGES["long_video_not_supported"].format(
                        model=workflow_name, max_seconds=f"{workflow_cfg.get('max_seconds', 8):g}"),
                    param="shots" if body.get("shots") is not None else "seconds",
                    code="long_video_not_supported",
                    job_message=f"This model cannot render shots or clips longer than "
                                f"{workflow_cfg.get('max_seconds', 8):g} s")
    if chain_cfg is not None:
        return await _submit_video_chain(
            request, auth_context, body=body, workflow_name=str(chain_name),
            requested_model=requested_model, public_model=public_model, media_holder=media_holder,
            admission_intent=admission_intent)
    policy_decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="openai.video",
            intent=INTENT_VIDEO_GENERATE,
            model=str(requested_model) if requested_model is not None else None,
            workflow=workflow_id_for_video_key(workflow_name),
            backend=_graph_service_id(_VIDEO_GRAPH_CAPABILITY),
            capability_exposure="default",
            capability_trust_tier="stable",
            capability_resource_tier="expensive",
        ),
        receipt,
    )
    if policy_decision.denied:
        return _policy_denied_response(policy_decision)
    target_decision = _workflow_target_policy_decision(
        auth_context,
        capability=_VIDEO_GRAPH_CAPABILITY,
        requested=str(requested_model) if requested_model is not None else None,
        resolved=str(workflow_name),
        extra_ids=(workflow_id_for_video_key(workflow_name),),
    )
    if target_decision is not None and target_decision.denied:
        return _policy_denied_response(target_decision)

    workflow_template = _load_video_workflow_file(workflow_cfg)
    if workflow_template is None:
        return Response(
            content=json.dumps({"error": {"message": f"Workflow file missing for {workflow_name}", "type": "api_error"}}),
            status_code=500,
            media_type="application/json"
        )

    receipt.backend = _graph_service_id(_VIDEO_GRAPH_CAPABILITY)
    _capture_retention_facts(receipt)

    # Video-conditioned workflows (remix, character replace) take a source video
    # as an extension input. Resolve and probe it now: its frame rate and length
    # decide the frame count below. Upload happens later, after validation.
    staged_assets: dict[str, tuple[bytes, str, str]] = {}
    source_probe = None
    video_manifest = None
    _workflow_block = (
        (body.get("extensions") or {}).get(WORKFLOW_EXTENSION_NAMESPACE)
        if isinstance(body.get("extensions"), Mapping)
        else body.get("extensions")
    )
    if isinstance(_workflow_block, Mapping):
        # Gateway-level controls (metadata) are not workflow inputs.
        _workflow_block = {
            k: v for k, v in _workflow_block.items() if k not in provenance.RESERVED_GATEWAY_CONTROLS
        }
    has_workflow_extensions = (
        bool(workflow_cfg.get("requires_extensions")) or bool(body.get("inputs")) or bool(_workflow_block)
    )
    # An explicit `frames` input, when the manifest declares one, is the
    # authoritative length: it wins over the length derived from `seconds`.
    requested_frames: int | None = None
    if has_workflow_extensions:
        try:
            video_manifest = _find_video_manifest(str(workflow_name), workflow_cfg)
        except ManifestError as e:
            return Response(
                content=json.dumps({"error": {"message": str(e), "type": "api_error"}}),
                status_code=500,
                media_type="application/json",
            )
        if video_manifest is None:
            return Response(
                content=json.dumps({"error": {
                    "message": f"Workflow {workflow_name} does not have a video workflow manifest",
                    "type": "api_error",
                    "code": "workflow_manifest_missing",
                }}),
                status_code=500,
                media_type="application/json",
            )
        try:
            body = merge_workflow_inputs(body)
            ext_block = (body.get("extensions") or {}).get(WORKFLOW_EXTENSION_NAMESPACE)
            if workflow_cfg.get("requires_extensions") and isinstance(ext_block, Mapping):
                body["extensions"] = {
                    **body["extensions"],
                    WORKFLOW_EXTENSION_NAMESPACE: video_inputs.coerce_form_scalars(
                        ext_block, video_manifest.get("inputs") or {}
                    ),
                }
            validation, unknown_fields = validate_video_workflow_extensions(body, video_manifest, backend=DERIVED._GRAPH_BINDING)
            rejected = {**validation.rejected, **unknown_fields}
            # Preserve the required-picture errors before reporting other invalid inputs.
            for name, reason in validation.rejected.items():
                spec = (video_manifest.get("inputs") or {}).get(name) or {}
                if reason == "required input missing" and spec.get("type") == "asset":
                    message = f"{name} is required for this model"
                    if name == workflow_cfg.get("source_video_input"):
                        message += ": pass a video id, URL or data URI"
                    raise _VideoAssetError(
                        message, param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{name}",
                    )
            if rejected:
                name, reason = next(iter(rejected.items()))
                return _json_error(400, reason, param=name, code="invalid_workflow_extension")
            if "frames" in ((video_manifest or {}).get("inputs") or {}):
                supplied_frames = validation.accepted.get("frames")
                if supplied_frames is not None:
                    requested_frames = int(supplied_frames)
            if workflow_cfg.get("requires_extensions") and workflow_cfg.get("source_video_input"):
                staged_assets, source_probe = await _resolve_video_workflow_assets(
                    body, video_manifest, workflow_cfg, auth_context, request=request, holder=media_holder,
                )
        except ExtensionError as e:
            return Response(
                content=json.dumps({"error": {
                    "message": str(e),
                    "type": "invalid_request_error",
                    "code": "invalid_workflow_extension",
                }}),
                status_code=400,
                media_type="application/json",
            )
        except _VideoAssetError as e:
            return _video_asset_error_response(e)

    if workflow_cfg.get("subject_select"):
        # Character replace: the person selection only edits the graph, so a bad
        # one is refused here, before any picture or video reaches the engine's
        # input directory. The rules live in video_subject, and the graph edit
        # below reads the same point helper once the extensions are bound.
        try:
            video_subject.validate_selection(
                ((body.get("extensions") or {}).get(WORKFLOW_EXTENSION_NAMESPACE) or {})
            )
        except video_subject.SubjectError as exc:
            return _gateway_input_error(str(exc), param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{exc.param}",
                                        code="invalid_workflow_extension")

    # Validate / clamp request inputs.
    max_seconds = float(workflow_cfg.get("max_seconds", 8))
    min_seconds = float(workflow_cfg.get("min_seconds", 1))
    source_fps = source_probe.fps if source_probe is not None else None
    source_seconds = (
        source_probe.frames / source_probe.fps if source_probe is not None else None
    )
    try:
        if seconds is not None:
            seconds_val = float(seconds)
        elif source_seconds is not None:
            # No seconds given: keep the source's length, up to the workflow default.
            seconds_val = min(
                float(workflow_cfg.get("default_seconds", max_seconds)),
                max_seconds,
                source_seconds,
            )
        else:
            seconds_val = min(5.0, max_seconds)
    except (TypeError, ValueError):
        return _gateway_input_error("seconds must be a number")
    if seconds_val < min_seconds or seconds_val > max_seconds:
        return _gateway_input_error(f"seconds must be between {min_seconds} and {max_seconds} for model {workflow_name}",
                                    job_message=f"seconds must be between {min_seconds} and {max_seconds}")

    if source_seconds is not None and seconds_val > source_seconds + 0.05:
        return Response(
            content=json.dumps({
                "error": {
                    "message": (
                        f"seconds ({seconds_val:g}) is longer than the source video "
                        f"({source_seconds:.2f} s)"
                    ),
                    "type": "invalid_request_error",
                    "param": "seconds",
                }
            }),
            status_code=400,
            media_type="application/json",
        )

    size = size_from_body or workflow_cfg.get("default_size") or "1280x720"
    width, height = video_grid.resolve_size(size, video_grid.SizeSpec(
        default="", max=workflow_cfg.get("max_size", "1280x720"), multiple=int(workflow_cfg.get("size_multiple", 32))))
    # The output keeps the source clip's frame rate for video-conditioned workflows.
    frame_rate = float(source_fps or workflow_cfg.get("frame_rate", 25))
    # Frame count must be step*k+offset (LTX: 8k+1; MiniMax H3: 17k+5; Wan: 4k+1).
    length_step = int(workflow_cfg.get("length_step", 8))
    length = video_grid.seconds_to_length(
        seconds_val,
        frame_rate,
        step=length_step,
        offset=int(workflow_cfg.get("length_offset", 1)),
    )
    if source_probe is not None or workflow_cfg.get("max_length"):
        # Never ask for more frames than the source has, or than the model was tested at.
        cap = int(workflow_cfg["max_length"]) if workflow_cfg.get("max_length") else None
        length = video_inputs.snap_length_down(
            length,
            step=length_step,
            offset=int(workflow_cfg.get("length_offset", 1)),
            available=min(
                [v for v in (cap, source_probe.frames if source_probe is not None else None) if v]
                or [length]
            ),
        )
    if requested_frames is not None:
        # An explicit frame count is exact; it overrides `seconds` and the
        # seconds-derived snap; the manifest bounds it (minimum, maximum, grid).
        length = requested_frames
    # `actual_seconds` is what the rendered mp4 will actually contain (within
    # codec/container rounding). It starts as the LTX 8N+1-snapped duration for
    # short workflows; long-form overrides this further down.
    actual_seconds = length / frame_rate
    duration_adjustment_reason = None

    # Picture assets do not affect duration or dimensions; validate those before fetching.
    if workflow_cfg.get("requires_extensions") and not workflow_cfg.get("source_video_input"):
        try:
            staged_assets, source_probe = await _resolve_video_workflow_assets(
                body, video_manifest, workflow_cfg, auth_context, request=request, holder=media_holder,
            )
        except _VideoAssetError as exc:
            return _video_asset_error_response(exc)

    # Inject parameters into a deep copy of the workflow.
    workflow = copy.deepcopy(workflow_template)
    # The node roles are the workflow's binding (`node_roles` in its videos entry), checked against the graph when the
    # file loads (`_load_video_workflow_file`); the graph file carries no roles of its own.
    meta = workflow_cfg.get("node_roles") or {}

    prompt_node = meta.get("prompt_node")
    neg_node = meta.get("negative_prompt_node")
    size_node = meta.get("size_node")
    # length_node must be explicit. Falling back to size_node is unsafe because
    # source-of-truth Primitive width/height nodes use the same `value` key as a
    # PrimitiveInt length node, so the size value would be overwritten by length.
    # Workflows that don't drive length per-request (e.g. long-form chunked loops
    # that compute it from chunk_seconds) should leave length_node unset.
    length_node = meta.get("length_node")
    seed_node = meta.get("seed_node")
    fr_node = meta.get("frame_rate_node")

    if prompt_node and prompt_node in workflow:
        # `prompt_field` names the text input when it is not CLIPTextEncode's `text`
        # (MiniMax H3's conditioning node takes `prompt`).
        workflow[prompt_node]["inputs"][meta.get("prompt_field", "text")] = prompt

    # negative prompt left at template default unless body overrides
    negative_prompt = body.get("negative_prompt")
    if negative_prompt and neg_node and neg_node in workflow:
        workflow[neg_node]["inputs"]["text"] = negative_prompt

    # Helpers to accept either a single node id or a list of node ids in the node roles.
    # Some workflows have a single source-of-truth Primitive node feeding many
    # consumers; others spread the field across consumer nodes directly. Accept
    # both so workflow JSONs can stay close to ComfyUI's native format.
    def _as_list(v):
        if v is None:
            return []
        return list(v) if isinstance(v, (list, tuple)) else [v]

    def _set_field(node_id, candidate_keys, value):
        if not node_id or node_id not in workflow:
            return False
        inputs = workflow[node_id].get("inputs", {})
        for k in candidate_keys:
            if k in inputs:
                inputs[k] = value
                return True
        return False

    # size_node forms supported:
    #   "7"                   -> single node with both width/height keys (e.g. EmptyLatentImage)
    #   ["W_NID", "H_NID"]    -> two separate INTConstant/PrimitiveInt source nodes; index 0 is
    #                            width, index 1 is height. Each takes a single `value` key.
    #   width_node/height_node in the node roles override the list/single behavior.
    width_node = meta.get("width_node")
    height_node = meta.get("height_node")
    size_list = _as_list(size_node)
    if width_node is None and isinstance(size_node, (list, tuple)) and len(size_list) >= 2:
        width_node = size_list[0]
        height_node = size_list[1]
    if width_node is not None or height_node is not None:
        if width_node:
            _set_field(width_node, ["width", "value"], width)
        if height_node:
            _set_field(height_node, ["height", "value"], height)
    else:
        for nid in size_list:
            _set_field(nid, ["width"], width)
            _set_field(nid, ["height"], height)

    # length_node: also accept "value" (PrimitiveInt source-of-truth pattern)
    # and "frames_number" (LTXVEmptyLatentAudio variant). length_nodes (plural)
    # in the node roles is honored too for fan-out when multiple consumers need the same
    # length without a Primitive node.
    length_targets = _as_list(meta.get("length_nodes")) or _as_list(length_node)
    for nid in length_targets:
        _set_field(nid, ["length", "value", "frames_number"], length)
    if length_targets and abs(actual_seconds - seconds_val) > 0.05:
        duration_adjustment_reason = video_grid.stride_reason(length_step)

    # Remix workflows slice the source clip; keep half a frame of slack past `length`.
    for nid in _as_list(meta.get("duration_node")):
        _set_field(nid, ["duration"], round((length + 0.5) / frame_rate, 4))

    fr_targets = _as_list(meta.get("frame_rate_nodes")) or _as_list(fr_node)
    for nid in fr_targets:
        _set_field(nid, ["frame_rate", "value"], frame_rate)

    try:
        seed_val = int(seed) if seed is not None else random.randint(0, 2**32 - 1)
    except (TypeError, ValueError):
        seed_val = random.randint(0, 2**32 - 1)
    seed_targets = _as_list(meta.get("seed_nodes")) or _as_list(seed_node)
    for nid in seed_targets:
        # `value` covers INTConstant nodes used as a shared seed source that
        # downstream RandomNoise/Math nodes derive from. Without `value` the seed silently
        # never reaches the graph and ComfyUI's full-graph cache returns
        # byte-identical mp4s for repeat prompts.
        _set_field(nid, ["noise_seed", "seed", "value"], seed_val)

    # I2V: when input_reference is supplied, resolve to bytes, upload to ComfyUI,
    # and wire it into the workflow via the workflow's node roles.
    #
    # Workflow contract (uniform across aliases — Option A: one workflow per alias
    # handles both T2V and I2V):
    #   * `image_node`: LoadImage node id whose `image` field gets set to the
    #     uploaded filename. Fields: ["image"].
    #   * `image_strength_node` (LTX): PrimitiveFloat node id whose `value`
    #     field gates conditioning strength. 0.0 = pure T2V passthrough,
    #     1.0 = first frame pinned to the image. Fields: ["value"].
    #   * `image_target_node` + `image_target_field` (Wan): conditional wiring
    #     mode. The proxy sets `inputs[<field>] = [image_node, 0]` to enable
    #     I2V; absent for T2V (lets the I2V latent node fall through its
    #     start_image=None T2V branch). No strength knob on Wan.
    #
    # T2V path: leave image_strength_node at 0.0 (LTX) / leave start_image
    # unwired (Wan). The placeholder image stays in the LoadImage field —
    # harmless because the LTX inplace node operates at strength=0 (mask=1.0,
    # full noise) and the Wan LoadImage is orphaned (executor skips it).
    if input_reference:
        # image_node alone is enough: first-frame-only workflows (MiniMax H3) wire the
        # LoadImage output in the graph and have no strength knob.
        if not meta.get("image_node"):
            return Response(
                content=json.dumps({
                    "error": {
                        "message": (
                            f"Workflow {workflow_name} does not support I2V."
                        ),
                        "type": "invalid_request_error",
                    }
                }),
                status_code=400,
                media_type="application/json"
            )

        try:
            image_bytes, image_filename = await _resolve_input_reference(
                input_reference,
                auth_context,
                request=request,
                holder=media_holder,
                accepts=_input_accepts(video_manifest, VIDEO_REFERENCE_INPUT),
            )
        except _InputReferenceError as e:
            return Response(
                content=json.dumps({
                    "error": {"message": str(e), "type": "invalid_request_error"}
                }),
                status_code=400,
                media_type="application/json"
            )

        # Upload to the engine's input cache (same upload used by image-edit).
        upload_headers = configured_downstream_headers(auth_context, _graph_service_id(_VIDEO_GRAPH_CAPABILITY))
        try:
            async with engine_egress.async_client(timeout=httpx.Timeout(60, connect=10)) as upload_client:
                files = {node_graph.UPLOAD_FIELD: (image_filename, image_bytes, "image/png")}
                upload_resp = await _graph(upload_client, "videos").upload(files, headers=upload_headers)
        except Exception:
            return client_errors.client_error(502, "upload_failed", error_type="api_error")
        if upload_resp.status != 200:
            return client_errors.client_error(502, "upload_failed", error_type="api_error", subject=_graph_adapter('videos').label)
        try:
            uploaded_filename = upload_resp.json().get("name")
        except Exception:
            uploaded_filename = None
        if not uploaded_filename:
            return Response(
                content=json.dumps({
                    "error": {"message": f"{_graph_adapter('videos').label} did not return uploaded filename", "type": "api_error"}
                }),
                status_code=502,
                media_type="application/json"
            )
        print(f"[proxy] Video I2V uploaded image: {uploaded_filename}", flush=True)

        # Wire the uploaded filename into the LoadImage node.
        image_node = meta["image_node"]
        if not _set_field(image_node, [_NODE_IMAGE_INPUT], uploaded_filename):
            return Response(
                content=json.dumps({
                    "error": {
                        "message": f"Workflow {workflow_name} image_node {image_node} missing or has no `image` input",
                        "type": "api_error",
                    }
                }),
                status_code=500,
                media_type="application/json"
            )

        # Strength: clamp to [0,1]. Default 1.0 when an image is provided.
        try:
            strength_val = float(
                image_strength if image_strength is not None else meta.get("image_strength_default", 1.0)
            )
        except (TypeError, ValueError):
            strength_val = 1.0
        strength_val = max(0.0, min(1.0, strength_val))

        strength_node = meta.get("image_strength_node")
        if strength_node:
            # LTX: set the PrimitiveFloat that feeds LTXVImgToVideoInplaceKJ.strength_*.
            _set_field(strength_node, ["value"], strength_val)

        target_node = meta.get("image_target_node")
        target_field = meta.get("image_target_field")
        if target_node and target_field:
            # Wan: connect start_image link from image_node into the I2V latent node.
            if target_node in workflow:
                workflow[target_node].setdefault("inputs", {})[target_field] = [image_node, 0]

    if staged_assets:
        body, upload_error = await _upload_video_workflow_assets(body, staged_assets, auth_context)
        if upload_error is not None:
            return upload_error
    if has_workflow_extensions:
        try:
            manifest = video_manifest or _find_video_manifest(str(workflow_name), workflow_cfg)
            if manifest is None:
                return Response(
                    content=json.dumps({"error": {
                        "message": f"Workflow {workflow_name} does not have a video workflow manifest",
                        "type": "invalid_request_error",
                        "code": "workflow_manifest_missing",
                    }}),
                    status_code=400,
                    media_type="application/json",
                )
            extension_binding = bind_video_workflow_extensions(
                body,
                manifest,
                workflow,
                backend=DERIVED._GRAPH_BINDING,
            )
        except (ExtensionError, ManifestError) as e:
            return Response(
                content=json.dumps({"error": {
                    "message": str(e),
                    "type": "invalid_request_error",
                    "code": "invalid_workflow_extension",
                }}),
                status_code=400,
                media_type="application/json",
            )
        if not extension_binding.ok:
            rejected = dict(extension_binding.validation.rejected)
            rejected.update(extension_binding.unknown_top_level)
            first_name, first_reason = next(iter(rejected.items()))
            return Response(
                content=json.dumps({"error": {
                    "message": first_reason,
                    "type": "invalid_request_error",
                    "param": first_name,
                    "code": "invalid_workflow_extension",
                }}),
                status_code=400,
                media_type="application/json",
            )
        workflow = extension_binding.request

    subject_record: dict[str, Any] | None = None
    if workflow_cfg.get("subject_select"):
        # Character replace: pick which person is replaced, and remember how to
        # read how many were tracked once the job is done. The selection itself
        # was validated before the uploads; this rewires the bound graph.
        block = ((body.get("extensions") or {}).get(WORKFLOW_EXTENSION_NAMESPACE) or {})
        try:
            video_subject.apply_point(workflow, block, width, height, meta)
            if block.get("reference_fit", True) not in (False, "false", "False", 0):
                video_subject.add_reference_fit(workflow, meta)
        except video_subject.SubjectError as exc:
            return _json_error(400, str(exc), param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{exc.param}",
                               code="invalid_workflow_extension")
        subject_record = {
            "probe_node": meta["probe_node"],
            "explicit": video_subject.explicit_selection(block),
            "subject": str(block.get("source_subject") or "person"),
        }

    record_extra: dict[str, Any] = {}
    if subject_record:
        record_extra["subject"] = subject_record
    if audio_disabled:
        record_extra[domain_rows.AUDIO] = False
    return await _submit_video_graph(
        record_extra=record_extra or None,
        workflow=workflow,
        auth_context=auth_context,
        workflow_name=workflow_name,
        requested_model=requested_model,
        public_model=public_model,
        prompt=prompt,
        width=width,
        height=height,
        length=length,
        seed_val=seed_val,
        seconds_val=seconds_val,
        actual_seconds=actual_seconds,
        remixed_from_video_id=remixed_from_video_id,
        duration_adjustment_reason=duration_adjustment_reason,
        admission_intent=admission_intent,
    )


async def _submit_video_chain(
    request: Request,
    auth_context: AuthContext,
    *,
    body: Mapping[str, Any],
    workflow_name: str,
    requested_model: Any,
    public_model: str,
    source: Any = None,
    source_video_id: str | None = None,
    media_holder: media_inputs.Holder | None = None,
    admission_intent: str,
) -> Response:
    """Plan, build and queue a long-video chain (create with shots or long
    seconds, or an extension of `source`). ``admission_intent`` is the intent the calling route admits under."""
    extending = source is not None
    try:
        chain_controls = video_controls.parse_video_controls(body)
    except video_controls.VideoControlError as exc:
        return _json_error(400, str(exc), param=exc.param, code="invalid_video_control")
    audio_disabled = chain_controls.get(video_controls.AUDIO_CONTROL) is False
    workflow_cfg = _video_workflow_table().get(workflow_name) or {}
    engine = video_extend.engine_for(workflow_cfg.get("builder"))
    try:
        options = _parse_chain_options(body, engine)
        shots = _parse_chain_shots(body, default_seconds=8.0, min_seconds=engine.min_shot_seconds)
        if body.get("inputs"):
            raise video_extend.PlanError(
                "inputs is not supported for long video; use extensions.com.azizam.workflow",
                code="invalid_workflow_extension", param="inputs")
    except video_extend.PlanError as exc:
        return _json_error(400, str(exc), code=exc.code, param=exc.param)
    if extending and body.get("input_reference"):
        return _json_error(400, "input_reference is not supported on extensions; the source video is the context",
                           param="input_reference", code="unsupported_parameter")

    delivery = media_delivery.current()
    receipt = delivery.receipt if delivery is not None else None
    denial = _chain_policy_denial(auth_context, requested_model, workflow_name, receipt=receipt)
    if denial is not None:
        return denial

    if receipt is not None:
        receipt.backend = _graph_service_id(_VIDEO_GRAPH_CAPABILITY)
        _capture_retention_facts(receipt)

    # Source video: resolve, probe, then upload to ComfyUI only after validation.
    probe = None
    source_bytes = None
    source_name = None
    if extending:
        try:
            source_bytes, source_name, _mime = await _resolve_video_workflow_asset(
                source, kind=domain_rows.VIDEO, param=_VIDEO_FIELD, auth_context=auth_context, request=request)
        except _VideoAssetError as exc:
            return _video_asset_error_response(exc)
        if not source_bytes or len(source_bytes) > video_inputs.MAX_SOURCE_VIDEO_BYTES:
            return _json_error(400, "video is empty or larger than the size limit", param=_VIDEO_FIELD)
        probe = await private_tmp.run_worker(
            video_inputs.probe_video, source_bytes, video_inputs.suffix_for_video(source_name, _mime),
            workdir=private_tmp.current() / "video_inputs")
        if probe is None:
            return _json_error(400, "video is not a readable video", param=_VIDEO_FIELD)

    fps = float(probe.fps) if probe is not None else float(workflow_cfg.get("frame_rate", 24))
    if probe is not None:
        try:
            engine.check_source(fps)
        except video_extend.PlanError as exc:
            return _json_error(400, str(exc), code=exc.code, param=exc.param)
    context_seconds = options.get("context_seconds", video_extend.DEFAULT_CONTEXT_SECONDS)
    tail_frames = engine.tail_frames(context_seconds, fps)
    if probe is not None and probe.frames < tail_frames:
        return _json_error(
            400, f"the source video has {probe.frames} frames; extending needs at least {tail_frames} "
            f"for context_seconds={context_seconds:g}", param=_VIDEO_FIELD, code="video_too_short")

    try:
        segments = engine.plan_segments(
            shots, fps=fps, has_source=extending,
            max_segment_seconds=float(workflow_cfg.get("chain_segment_seconds", _video_chain_segment_seconds())),
            state_ledger=options.get("state_ledger"))
    except video_extend.PlanError as exc:
        return _json_error(400, str(exc), code=exc.code, param=exc.param)
    source_frames = probe.frames if probe is not None else 0
    total_seconds = video_extend.total_seconds(segments, fps, source_frames)
    if total_seconds > _video_max_total_seconds() + 0.05:
        return _json_error(
            400,
            f"total video length would be {total_seconds:.1f} s"
            + (f" ({source_frames / fps:.1f} s source + {total_seconds - source_frames / fps:.1f} s new)" if extending else "")
            + f", over the {_video_max_total_seconds():g} s limit (videos.max_total_seconds). "
            "Quality drops with each added segment; finish this video or start a new one.",
            code="video_total_length_exceeded", param="seconds")

    # Size: requested, else the source's, else the workflow default.
    size = body.get("size")
    if not size and probe is not None:
        size = f"{probe.width}x{probe.height}"
    width, height = video_grid.resolve_size(
        str(size or workflow_cfg.get("default_size") or "960x512"),
        video_grid.SizeSpec(default="", max=workflow_cfg.get("max_size", "1280x720"),
                            multiple=int(workflow_cfg.get("size_multiple", 32))))

    try:
        seed_val = int(body["seed"]) if body.get("seed") is not None else random.randint(0, 2**32 - 1)
    except (TypeError, ValueError):
        return _json_error(400, "seed must be an integer", param="seed")

    first_frame_image = None
    first_frame_strength = 1.0
    try:
        if source_bytes is not None:
            uploaded = await _upload_graph_input(
                source_bytes, source_name or "source.mp4", "video/mp4", auth_context)
        if body.get("input_reference"):
            try:
                image_bytes, image_name = await _resolve_input_reference(
                    body["input_reference"], auth_context, request=request, holder=media_holder,
                    accepts=_input_accepts(_find_video_manifest(workflow_name, workflow_cfg), VIDEO_REFERENCE_INPUT))
            except _InputReferenceError as exc:
                return _json_error(400, str(exc), param="input_reference")
            first_frame_image = await _upload_graph_input(image_bytes, image_name, "image/png", auth_context)
            if body.get("image_strength") is not None:
                try:
                    first_frame_strength = max(0.0, min(1.0, float(body["image_strength"])))
                except (TypeError, ValueError):
                    return _json_error(400, "image_strength must be a number", param="image_strength")
    except _VideoAssetError as exc:
        return _video_asset_error_response(exc)

    spec = video_extend.ChainSpec(
        segments=segments, width=width, height=height, fps=fps, seed=seed_val,
        negative=str(body.get("negative_prompt") or engine.default_negative),
        context_frames=tail_frames,
        context_strength=options.get("context_strength", video_extend.DEFAULT_CONTEXT_STRENGTH),
        source=(video_extend.SourceVideo(uploaded, probe.frames, fps, probe.width, probe.height, probe.has_audio)
                if probe is not None else None),
        first_frame_image=first_frame_image, first_frame_strength=first_frame_strength)
    built = engine.build(spec)
    total_seconds = built.total_frames / fps

    prompt_text = "\n".join(p for p, _ in shots)
    chain_info = {
        "segments": len(segments),
        "segment_frames": [s.frames for s in segments],
        "shots": len(shots),
        "units": built.units,
        "node_prefix": engine.binding.node_prefix,
        "total_frames": built.total_frames,
        "fps": fps,
        "context_seconds": context_seconds,
        "source_frames": source_frames,
    }
    seconds_requested = sum(s for _, s in shots) + source_frames / fps
    reason = engine.stride_reason if abs(total_seconds - seconds_requested) > 0.05 else None
    return await _submit_video_graph(
        workflow=built.graph,
        auth_context=auth_context,
        workflow_name=workflow_name,
        requested_model=requested_model,
        public_model=public_model,
        prompt=prompt_text,
        width=width,
        height=height,
        length=built.total_frames,
        seed_val=seed_val,
        seconds_val=seconds_requested,
        actual_seconds=total_seconds,
        remixed_from_video_id=source_video_id,
        duration_adjustment_reason=reason,
        record_extra={"chain": chain_info, "extended_from_video_id": source_video_id,
                      "seconds": _seconds_text(total_seconds), "total_seconds": round(total_seconds, 3),
                      **({domain_rows.AUDIO: False} if audio_disabled else {})},
        payload_extra={"extended_from_video_id": source_video_id, "total_seconds": round(total_seconds, 3),
                       "segments": len(segments), "seconds": _seconds_text(total_seconds)},
        progress_units=built.units,
        admission_intent=admission_intent,
    )


async def _legacy_video_create(request: Request, caller: AuthContext, body: Mapping[str, Any]) -> Response:
    """A create the wire shape hands back (chains, workflow extensions, references it cannot read): the old pipeline on the body
    already read. It enforces the route policy and opens its one receipt itself, as the removed route did, because the row has
    opened none. ``tests/test_legacy_video_ratchet.py`` counts these hand-offs."""
    policy_response = await asyncio.to_thread(
        lambda: _enforce_runtime_route_policy(caller, "/v1/videos", policy=DERIVED._RUNTIME_ROUTE_POLICY.get("/v1/videos") or _row_route_policy("/v1/videos")))
    if policy_response is not None:
        return policy_response
    return await proxy_graph_video_create(request, caller, body_override=body)


def _chain_policy_denial(auth_context: AuthContext, requested_model: Any, workflow_name: str, *, receipt: RouteReceipt | None = None) -> Response | None:
    decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="openai.video",
            intent=INTENT_VIDEO_GENERATE,
            model=str(requested_model) if requested_model is not None else None,
            workflow=workflow_id_for_video_key(workflow_name),
            backend=_graph_service_id(_VIDEO_GRAPH_CAPABILITY),
            capability_exposure="default",
            capability_trust_tier="stable",
            capability_resource_tier="expensive",
        ),
        receipt,
    )
    if decision.denied:
        return _policy_denied_response(decision)
    target = _workflow_target_policy_decision(
        auth_context,
        capability=_VIDEO_GRAPH_CAPABILITY,
        requested=str(requested_model) if requested_model is not None else None,
        resolved=str(workflow_name),
        extra_ids=(workflow_id_for_video_key(workflow_name),),
    )
    if target is not None and target.denied:
        return _policy_denied_response(target)
    return None


def _policy_denied_response(decision: PolicyDecision) -> Response:
    """403 in the OpenAI error shape with the design §3.3 access code."""
    reason = decision.reasons[0] if decision.reasons else "This isn't in your access plan."
    error: dict[str, Any] = {"message": reason, "type": "access_error", "code": decision.code or "policy_denied"}
    if decision.side:
        error["details"] = {"side": decision.side, "plans": [s.split(":", 1)[1] for s in decision.sources
                                                         if s.startswith("access_plan:")]}
    return _gateway_refusal(Response(
        content=json.dumps({"error": error}),
        status_code=403,
        media_type="application/json",
    ), "authorization")


# Frozen from main c52835c90: train 100 added the text-role field check to the live loader.
def _load_video_workflow_file(workflow_cfg: dict) -> Optional[dict]:
    """Load the JSON for a configured video workflow.

    Attaches the UI-format twin under `__ui_workflow__` so the submit site can
    embed it via `extra_data.extra_pnginfo.workflow` for mp4 PNGInfo metadata.
    """
    file_path = workflow_cfg.get("file")
    if not file_path:
        return None
    p = Path(file_path)
    if not p.is_absolute():
        # Resolve relative paths against the proxy's directory.
        p = Path(__file__).parent / p
    if not p.exists():
        return None
    with open(p) as f:
        workflow = json.load(f)
    errors = validate_workflow_graph(workflow)
    if errors:
        print(
            f"[proxy] invalid video {_graph_adapter(_VIDEO_GRAPH_CAPABILITY).label} workflow {p}: {'; '.join(errors)}",
            flush=True,
        )
        return None
    # A role that names no node of the file is a template problem, found here and not when a request fills the graph.
    roles = workflow_cfg.get("node_roles") or {}
    dangling = sorted(
        str(key) for key, value in roles.items()
        if key.endswith(("_node", "_nodes"))
        and any(str(node) not in workflow for node in (value if isinstance(value, list) else [value]))
        and not (workflow_cfg.get("subject_select") and key in ("point_detect_node", "fit_canvas_node"))
    )
    if not roles or dangling:
        print(f"[proxy] invalid video workflow {p}: node roles {dangling or 'missing'}", flush=True)
        return None
    if workflow_cfg.get("subject_select"):
        try:
            video_subject.check_roles(roles, workflow)
        except video_subject.SubjectRoleError as exc:
            print(f"[proxy] invalid video workflow {p}: {exc}", flush=True)
            return None
    ui = _load_ui_workflow(p)
    if ui is not None:
        workflow["__ui_workflow__"] = ui
    return workflow
