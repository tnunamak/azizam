# Frozen handler source from fd30cbfca; executed only by differential tests.

@_acts_for("identity", _AUDIO_GENERATE_OPERATION)
async def proxy_audio_generate(
    request: Request, auth_context, *, intents: frontend_mount.ExecutionIntents, store_full: bool = False,
    receipt: RouteReceipt | None = None,
    pinned: pinned_definitions.PinnedDefinitions | None = None, pinned_workflow: str | None = None,
):
    """POST /v1/audio/generate — submit audio generation via ComfyUI.

    ``store_full`` is for the media-job executor, which stores the bytes: they
    keep full metadata and the delivery mode is applied when the asset is served.
    ``pinned`` is what the job pinned at accept. With it the manifest and the graph come from the pin, never from the
    registry or the workflow file; without it (the synchronous route) they are read live. ``pinned_workflow`` is the
    workflow the job row recorded at submit (``audio.generate.<name>``): the model must still resolve to it.
    """
    admission = _graph_media_admission(intents.require_admission(), auth_context=auth_context)
    if admission is not None:
        return admission

    if not _graph_service(_AUDIO_GRAPH_CAPABILITY).enabled:
        return Response(
            content=json.dumps({"error": {"message": "Audio generation not enabled", "type": "service_unavailable"}}),
            status_code=503,
            media_type="application/json"
        )
    adapter = _graph_adapter(_AUDIO_GRAPH_CAPABILITY)
    service_response = (await asyncio.to_thread(_service_availability_response, _graph_service_id(_AUDIO_GRAPH_CAPABILITY),
        service_label=adapter.label,))
    if service_response is not None:
        return service_response
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )

    unknown_top_level = detect_unknown_audio_fields(body)
    if unknown_top_level:
        name, reason = next(iter(unknown_top_level.items()))
        return _gateway_input_refusal(_audio_extension_error_response(reason, param=name), reason)
    _begin_media_delivery(auth_context, receipt)
    metadata_error = _note_media_metadata_body(body)
    if metadata_error is not None:
        return metadata_error
    try:
        audio_graph.check_request(body)
    except audio_graph.InvalidAudioRequest as error:
        return _gateway_input_refusal(openai_error_response(str(error), param=error.param), str(error))

    model = body.get("model", _audio_default_workflow())
    audio_type = body.get("type", "music")

    workflow_name, workflow_cfg = _resolve_audio_workflow(model)
    if not workflow_cfg:
        return model_compatibility_error(
            ModelCompatibilityError(
                f"Model '{model}' is not available for audio generation.",
                code="model_not_found",
            )
        )
    if pinned is not None:
        try:
            manifest = pinned.manifest(DERIVED._GRAPH_BINDING)
            pinned_graph = _pinned_graph(pinned)
            workflow_id = str(manifest.get("id") or "")
            # A graph file can back several workflows. If the caller named this pinned identity,
            # an alias must not redirect it to the artifact's workflow. Filename-based owner keys still work.
            identity_names = (workflow_id, workflow_id.rsplit(".", 1)[-1])
            identity_redirected = str(model) in identity_names and workflow_name not in identity_names
            if (pinned_workflow != workflow_id or identity_redirected
                    or not _manifest_names_audio_workflow(manifest, workflow_name)):
                raise definition_store.DefinitionUnavailable(
                    f"job {pinned.envelope.source_id}: the model now resolves to workflow {workflow_name}, not the one it pinned")
        except definition_store.DefinitionUnavailable as exc:
            _MEDIA_JOB_LOG.warning("pinned audio definitions unusable: %s", log_scrub.describe_error(exc))
            return Response(
                content=json.dumps({"error": {"message": "Workflow definition unavailable", "type": "api_error",
                                              "code": "definition_unavailable"}}),
                status_code=500,
                media_type="application/json",
            )
    else:
        manifest = _load_audio_workflow_manifest(workflow_name)
        workflow_id = str(manifest.get("id") or "") if manifest else ""
        if not workflow_id:
            return model_compatibility_error(
                ModelCompatibilityError(
                    f"Model '{model}' is not available for audio generation.",
                    code="model_not_found",
                )
            )
    if manifest is None:
        return Response(
            content=json.dumps({"error": {"message": f"Workflow manifest missing for {workflow_name}", "type": "api_error"}}),
            status_code=500,
            media_type="application/json"
        )
    # The one effective input spec: the manifest, narrowed by this deployment's audio.workflows limits. The edge reads
    # the public body against it, and the adapter's compiler writes the graph from it; the template's _meta is not read.
    try:
        effective = audio_graph.effective_manifest(manifest, audio_graph.AudioLimits(
            default_duration=workflow_cfg.get("default_duration"),
            min_seconds=workflow_cfg.get("min_seconds"),
            max_seconds=workflow_cfg.get("max_seconds"),
        ))
    except ManifestError as error:
        print(f"[proxy] audio workflow {workflow_name} limits do not fit its manifest: {error}", flush=True)
        return client_errors.client_error(500, "audio_failed", error_type="api_error")
    try:
        inputs = audio_graph.manifest_inputs(body, manifest=effective, stored=_JOB_GENERATED.get())
    except audio_graph.InvalidAudioRequest as error:
        return _gateway_input_refusal(openai_error_response(str(error), param=error.param), str(error))
    except ManifestError as error:
        print(f"[proxy] audio workflow {workflow_name} manifest cannot serve this request: {error}", flush=True)
        return openai_error_response(f"Workflow manifest for {workflow_name} cannot serve this request",
                                     status_code=500, error_type="api_error")
    duration_value = inputs.get("duration")
    seed_val = generated_defaults.first_value(effective["inputs"], inputs)
    # The template is the one the manifest's binding names, so the map and the graph it writes come from one place.
    graph_binding = (manifest.get("bindings") or {}).get(DERIVED._GRAPH_BINDING) or {}
    workflow = pinned_graph if pinned is not None else _load_audio_workflow_file({"file": graph_binding.get("workflow")})
    if not workflow:
        return Response(
            content=json.dumps({"error": {"message": f"Workflow file missing for {workflow_name}", "type": "api_error"}}),
            status_code=500,
            media_type="application/json"
        )

    policy_decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="openai.audio",
            intent=INTENT_AUDIO_GENERATE,
            model=str(model) if model is not None else None,
            workflow=workflow_id,
            backend=_graph_service_id(_AUDIO_GRAPH_CAPABILITY),
            capability_exposure="default",
            capability_trust_tier="stable",
            capability_resource_tier="batch",
        ),
        receipt,
    )
    if policy_decision.denied:
        return _policy_denied_response(policy_decision)
    target_decision = _workflow_target_policy_decision(
        auth_context,
        capability=_AUDIO_GRAPH_CAPABILITY,
        requested=str(model) if model is not None else None,
        resolved=str(workflow_name),
        manifest=manifest,
        extra_ids=(workflow_id,),
    )
    if target_decision is not None and target_decision.denied:
        return _policy_denied_response(target_decision)

    try:
        workflow = node_graph.compile_workflow(effective, DERIVED._GRAPH_BINDING, workflow, inputs)
    except node_graph.InputRefused as error:
        return _gateway_input_refusal(openai_error_response(str(error), param=error.param), str(error))
    except ManifestError as error:
        # The manifest's map does not fit its template: a deployment fault, not the caller's.
        print(f"[proxy] audio workflow {workflow_name} binding does not fit its template: {error}", flush=True)
        return openai_error_response(f"Workflow binding for {workflow_name} does not fit its graph",
                                     status_code=500, error_type="api_error")

    if receipt is not None:
        receipt.backend = _graph_service_id(_AUDIO_GRAPH_CAPABILITY)
        _capture_retention_facts(receipt)
    headers = configured_downstream_headers(auth_context, _graph_service_id(_AUDIO_GRAPH_CAPABILITY))
    try:
        async with engine_egress.async_client(timeout=httpx.Timeout(30, connect=10)) as client:
            graph = _graph(client, _AUDIO_GRAPH_CAPABILITY)
            admission = _graph_media_admission(intents.require_admission(), auth_context=auth_context)
            if admission is not None:
                return admission
            resp = await graph.submit(
                _build_graph_submit_payload(workflow, _AUDIO_GRAPH_CAPABILITY, client_id=GRAPH_CLIENT_ID),
                headers=headers,
            )
            if resp.status != 200:
                return client_errors.client_error(502, "upstream_refused", error_type="api_error", subject=adapter.label)
            prompt_id = resp.json().get("prompt_id")
            if not prompt_id:
                return Response(
                    content=json.dumps({"error": {"message": "No prompt_id returned", "type": "api_error"}}),
                    status_code=502,
                    media_type="application/json"
                )

            audio_id = f"audio_{prompt_id}"
            print(f"[proxy] Audio job submitted: workflow={workflow_name} id={audio_id}", flush=True)

            # Wait for completion (simple polling)
            for _ in range(600):  # 10 min max
                await asyncio.sleep(2)
                history_resp = await graph.history(prompt_id, headers=headers)
                if history_resp.status != 200:
                    continue
                history = history_resp.json()
                if prompt_id not in history:
                    continue
                job = history[prompt_id]
                status_obj = job.get("status", {}) or {}
                status = status_obj.get("status_str") or status_obj.get("status") or ""
                if status == "success":
                    # The result is the file the workflow's selector names (CODE_TO_DATA_S2, D5), within the D6 limits.
                    result_limits = result_bundle.limits_from_config(runtime().config)
                    try:
                        picked = adapter.collect(
                            job, graph_binding, manifest.get("outputs") or {}, limits=result_limits,
                            history_bytes=len(history_resp.raw or b""))
                    except node_graph.SelectionRefused as refusal:
                        if refusal.code == "result_missing":
                            picked = {}
                        else:
                            return _result_refusal_response(refusal.code, 502)
                    selected_name, selected_files = next(iter(picked.items()), (None, ()))
                    file_info = selected_files[0] if selected_files else None
                    if file_info and file_info.get("filename"):
                        # Fetch the audio file
                        params = {"filename": file_info["filename"], "type": file_info.get("type", "output")}
                        if file_info.get("subfolder"):
                            params["subfolder"] = file_info["subfolder"]
                        try:
                            async with output_selectors.FETCH_GATE.slot(result_limits):
                                audio_resp = await graph.view(params, headers=headers)
                        except output_selectors.FetchRefused as refusal:
                            return _result_refusal_response(refusal.code, 504)
                        if audio_resp.status == 200:
                            content_type = audio_resp.headers.get("content-type", "audio/wav")
                            audio_bytes = audio_resp.raw
                            peak_db = workflow_cfg.get("normalize_peak_db")
                            if peak_db is not None:
                                audio_bytes = await private_tmp.run_worker(
                                    _peak_normalize_audio, audio_bytes, content_type, float(peak_db)
                                )
                            audio_suffix = Path(file_info["filename"]).suffix or ".wav"
                            if receipt is not None and not store_full:
                                from gateway import media_ingestion
                                receipt.requested_model = str(model)
                                receipt.resolved_model = workflow_name
                                receipt.resolved_workflow = workflow_id
                                await asyncio.to_thread(media_ingestion.store_audio_response, audio_bytes, content_type, receipt, store_asset=store_asset_bytes)
                            try:
                                if not store_full and media_delivery.current_mode() == provenance.METADATA_NONE:
                                    audio_bytes = await asyncio.to_thread(
                                        media_delivery.deliver_bytes, audio_bytes, audio_suffix, provenance.METADATA_NONE
                                    )
                                else:
                                    audio_bytes = await asyncio.to_thread(
                                        media_delivery.attach_provenance,
                                        audio_bytes,
                                        audio_suffix,
                                        provenance.build_provenance_record(
                                            receipt=media_delivery.current().receipt,
                                            kind=domain_rows.AUDIO,
                                            requested_model=str(model),
                                            resolved_model=workflow_name,
                                            workflow_id=workflow_id,
                                            backend=_graph_service_id(_AUDIO_GRAPH_CAPABILITY),
                                            seed=seed_val,
                                            parameters={
                                                **media_delivery.current().parameters,
                                                "type": audio_type,
                                                "duration": duration_value,
                                            },
                                        ),
                                    )
                            except media_metadata.MediaMetadataError as exc:
                                print(f"[proxy] audio metadata rewrite failed: {log_scrub.describe_error(exc)}", flush=True)
                                return Response(
                                    content=json.dumps({"error": {"message": "Could not prepare audio for delivery", "type": "api_error"}}),
                                    status_code=502,
                                    media_type="application/json",
                                )
                            delivered = Response(
                                content=audio_bytes,
                                status_code=200,
                                media_type=content_type,
                                headers={"X-Audio-Model": workflow_name, "X-Audio-Type": audio_type}
                            )
                            # The job names its bundle result by the output the selector read.
                            delivered.selected_result = selected_name
                            return delivered
                    return Response(
                        content=json.dumps({"error": {"message": "No audio output found", "type": "api_error"}}),
                        status_code=502,
                        media_type="application/json"
                    )
                elif status == "error":
                    err_msg = f"{adapter.label} generation failed"
                    for tag, payload in (status_obj.get("messages") or []):
                        if tag == "execution_error" and isinstance(payload, dict):
                            err_msg = _graph_error_message({"status": {"messages": [["execution_error", payload]]}}, workflow, capability=_AUDIO_GRAPH_CAPABILITY) or err_msg
                            break
                    return Response(
                        content=json.dumps({"error": {"message": err_msg, "type": "api_error"}}),
                        status_code=502,
                        media_type="application/json"
                    )

            return Response(
                content=json.dumps({"error": {"message": "Audio generation timeout", "type": "api_error"}}),
                status_code=504,
                media_type="application/json"
            )
    except TransportFailure as e:
        if e.kind == "timeout":
            return Response(
                content=json.dumps({"error": {"message": f"{adapter.label} request timeout", "type": "api_error"}}),
                status_code=504,
                media_type="application/json"
            )
        if e.kind == "unreachable":
            return Response(
                content=json.dumps({"error": {"message": adapter.unavailable_message(_graph_connection(capability=_AUDIO_GRAPH_CAPABILITY, probe=True)), "type": "api_error"}}),
                status_code=502,
                media_type="application/json"
            )
        return client_errors.client_error(502, "audio_failed", error_type="api_error")
    except Exception as e:
        return client_errors.client_error(502, "audio_failed", error_type="api_error")



@_settle_native_job
async def _run_audio_media_job(job_id: str) -> None:
    async with _audio_media_job_semaphore:
        job = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
        if not job or job.get("status") in media_jobs.TERMINAL_STATUSES:
            return

        payload = _load_audio_media_job_payload(job_id)
        request_shim = _request_shim_from_audio_job_payload(payload or {})
        if payload is None or request_shim is None:
            _mark_audio_media_job_failed(job_id, "Audio media job payload is missing or invalid")
            return

        auth_context = _auth_context_from_image_job_payload(payload)
        if auth_context is None:
            _mark_audio_media_job_failed(job_id, _MEDIA_JOB_CALLER_GONE, error_info=_MEDIA_JOB_CALLER_GONE_INFO)
            return
        request_shim.scope["auth_context"] = auth_context
        admission = _media_job_admission_response(
            str(job.get("intent") or domain_rows.AUDIO_GENERATE), count_queue=False, auth_context=auth_context,
        )
        if admission is not None:
            message, error_info = _media_job_failure(admission)
            _mark_audio_media_job_failed(job_id, message, error_info=error_info)
            return

        # An audio job with a pin runs from what it pinned at accept, or it fails: never from the live file. A job for a
        # workflow whose binding has no pin (not a graph) recorded no envelope and runs as it was accepted.
        pinned_args: dict[str, Any] = {}
        if await asyncio.to_thread(_audio_job_runs_from_pin, job_id, job):
            try:
                pinned_args["pinned"] = await asyncio.to_thread(_load_audio_pin, job_id)
                pinned_args["pinned_workflow"] = job.get("workflow")
            except definition_store.DefinitionUnavailable as exc:
                _MEDIA_JOB_LOG.warning("job %s: %s", job_id, log_scrub.describe_error(exc))
                _mark_audio_media_job_failed(job_id, _DEFINITION_UNAVAILABLE, error_info=_DEFINITION_UNAVAILABLE_INFO)
                return

        try:
            media_jobs.update_status(
                job_id,
                media_jobs.STATUS_RUNNING,
                progress=5,
                metadata_patch={"started_at": int(time.time())},
                path=media_jobs.DB_PATH,
            )
            media_jobs.append_event(
                job_id,
                "started",
                {
                    "phase": "running",
                    "status": media_jobs.STATUS_RUNNING,
                    "progress": 5,
                },
                path=media_jobs.DB_PATH,
            )
            stored_generated = await asyncio.to_thread(_stored_generated, job_id)  # raises when it cannot be read
            worker_token = _ADMISSION_WORKER.set(True)
            try:
                from gateway import media_ingestion
                receipt = await media_ingestion.start_receipt(
                    request_shim, auth_context, intent=str(job.get("intent") or domain_rows.AUDIO_GENERATE), protocol="openai.audio",
                    request_id=str(job["request_id"]), job_id=job_id, route_path="/gateway/v1/media/jobs",
                )
                generated_token = _JOB_GENERATED.set(stored_generated)
                try:
                    response = await proxy_audio_generate(
                        request_shim, auth_context, store_full=True, receipt=receipt, **pinned_args)
                except BaseException as error:
                    await run_receipt_write(media_ingestion.finish_receipt, receipt, status_code=499 if isinstance(error, asyncio.CancelledError) else 500)
                    raise
                finally:
                    _JOB_GENERATED.reset(generated_token)
                await run_receipt_write(media_ingestion.finish_response, receipt, response)
            finally:
                _ADMISSION_WORKER.reset(worker_token)
        except asyncio.CancelledError:
            raise
        except generated_defaults.GeneratedValueUnavailable as exc:
            # The job's stored value is erased or unreadable: it never runs, and nothing draws another.
            _mark_audio_media_job_failed(job_id, _GENERATED_UNAVAILABLE_MESSAGE[exc.erased], http_status=500,
                                         error_info=_GENERATED_UNAVAILABLE_INFO)
            return
        except Exception as exc:  # noqa: BLE001
            _mark_audio_media_job_failed(
                job_id,
                f"Audio media job executor error: {type(exc).__name__}",
                http_status=502,
            )
            return

        status_code = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status_code < 300:
            error = _image_response_json(response).get("error")
            if client_errors.error_fields(error)["code"] == _DEFINITION_UNAVAILABLE_INFO["error_code"]:
                # The route found the job's pin unusable or no longer the workflow its model names.
                _mark_audio_media_job_failed(job_id, _DEFINITION_UNAVAILABLE, http_status=status_code,
                                             error_info=_DEFINITION_UNAVAILABLE_INFO)
                return
            message, error_info = _media_job_failure(response)
            _mark_audio_media_job_failed(job_id, message, http_status=status_code, error_info=error_info)
            return

        content = bytes(getattr(response, "body", b"") or b"")
        media_type = str(getattr(response, "media_type", None) or "audio/wav")
        body = payload.get("body") if isinstance(payload, Mapping) else {}
        response_format = body.get("response_format") if isinstance(body, Mapping) else None
        artifacts = audio_media_jobs.audio_artifacts_from_result(
            content,
            response_format=str(response_format) if response_format else None,
            media_type=media_type,
        )
        # The result limits are checked before the files are saved: a refused result leaves nothing behind.
        result_limits = result_bundle.limits_from_config(runtime().config)
        try:
            result_bundle.check_assets(result_limits, [len(artifact.content) for artifact in artifacts])
        except result_bundle.ResultRefused as exc:
            _fail_refused_result(job_id, exc, status_code)
            return
        assets = await asyncio.to_thread(audio_media_jobs.store_audio_artifacts,
            artifacts,
            store_asset=store_asset_bytes,
            owner_key_hash=_asset_owner_key(auth_context),
        )
        if not assets:
            _mark_audio_media_job_failed(
                job_id,
                "Audio generation succeeded but returned no reusable assets",
                http_status=status_code,
            )
            return

        try:
            # The job lists its assets as before; its result bundle mirrors that list (CODE_TO_DATA_S2, D4).
            result_bundle.mirror_assets(
                job_id, assets, limits=result_limits, name=getattr(response, "selected_result", None) or result_bundle.DEFAULT_NAME,
                progress=100,
                metadata_patch={"completed_at": int(time.time()), **_job_metadata_mode_patch()},
                path=media_jobs.DB_PATH,
            )
        except result_bundle.ResultRefused as exc:
            _fail_refused_result(job_id, exc, status_code)
            return
        media_jobs.append_event(
            job_id,
            "completed",
            {
                **audio_media_jobs.complete_audio_job_payload(assets),
                "status": media_jobs.STATUS_SUCCEEDED,
                "progress": 100,
            },
            path=media_jobs.DB_PATH,
        )
        _delete_audio_media_job_payload(job_id)


