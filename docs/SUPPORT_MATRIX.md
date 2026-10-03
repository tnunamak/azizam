# Public API support matrix

Generated from the registered FastAPI app, including routes hidden from OpenAPI.
No servers are started. Account/admin, setup, showcase, internal routes and static mounts are excluded.
Public discovery and diagnostic pages are included alongside JSON and WebSocket APIs.

**Supported** means the handler exists, not complete vendor-spec conformance or backend availability.
**Partial** identifies protocol subsets and configuration-dependent vendor features.
**Unsupported** identifies explicit rejection routes; the actual rejection status is shown.
Authentication or policy rejection can occur before the listed unsupported response.
All execution routes still need a configured, available backend and access-plan permission.

See [vendor facades](VENDOR_FACADES.md) for protocol limits and [configuration ownership](CONFIG_SOURCE_OF_TRUTH.md).

Regenerate from a clean committed tree: `python3 scripts/gen_support_matrix.py --write`.
Check: `scripts/run-hermetic-pytest.sh -q tests/test_support_matrix.py`.

| Method | Route | Support | Limits | Code evidence |
|---|---|---|---|---|
| `GET` | `/` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `root_browser_entrypoint` |
| `HEAD` | `/` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `head_root` |
| `GET` | `/.well-known/ai-gateway` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `well_known_ai_gateway` |
| `GET` | `/.well-known/oauth-protected-resource` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/mcp_server.py](../gateway/mcp_server.py) `protected_resource` |
| `GET` | `/.well-known/oauth-protected-resource/mcp` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/mcp_server.py](../gateway/mcp_server.py) `protected_resource` |
| `GET` | `/.well-known/openai-proxy` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `well_known_openai_proxy` |
| `GET` | `/.well-known/skills` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `well_known_skills` |
| `GET` | `/.well-known/skills/{skill_id}/SKILL.md` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `well_known_skill_file` |
| `POST` | `/anthropic/v1/messages` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `anthropic_messages` |
| `POST` | `/anthropic/v1/messages/count_tokens` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `anthropic_count_tokens` |
| `GET` | `/anthropic/v1/models` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `anthropic_models` |
| `GET` | `/anthropic/v1/models/{model_id:path}` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `anthropic_model_retrieve` |
| `HEAD` | `/api/blobs/{digest}` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_blobs` |
| `POST` | `/api/blobs/{digest}` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_blobs` |
| `POST` | `/api/chat` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_chat` |
| `POST` | `/api/copy` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_copy` |
| `POST` | `/api/create` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_create` |
| `DELETE` | `/api/delete` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_delete` |
| `POST` | `/api/embed` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_embed` |
| `POST` | `/api/embeddings` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_embeddings` |
| `POST` | `/api/generate` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_generate` |
| `GET` | `/api/ps` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_ps` |
| `POST` | `/api/pull` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_pull` |
| `POST` | `/api/push` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_push` |
| `POST` | `/api/show` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_show` |
| `GET` | `/api/tags` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_tags` |
| `GET` | `/api/version` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_version` |
| `GET` | `/docs` | supported | Implemented route; requires applicable policy and backend configuration | FastAPI built-in |
| `HEAD` | `/docs` | supported | Implemented route; requires applicable policy and backend configuration | FastAPI built-in |
| `GET` | `/docs/oauth2-redirect` | supported | Implemented route; requires applicable policy and backend configuration | FastAPI built-in |
| `HEAD` | `/docs/oauth2-redirect` | supported | Implemented route; requires applicable policy and backend configuration | FastAPI built-in |
| `GET` | `/favicon.ico` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `status_favicon` |
| `GET` | `/firehose` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `status_dashboard_firehose` |
| `GET` | `/gateway/v1/aliases` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `list_aliases` |
| `GET` | `/gateway/v1/aliases/resolve` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `resolve_alias_dry_run` |
| `GET` | `/gateway/v1/assets/{asset_id}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `gateway_asset_metadata` |
| `GET` | `/gateway/v1/assets/{asset_id}/content` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `gateway_asset_content` |
| `GET` | `/gateway/v1/capabilities` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `list_capabilities` |
| `GET` | `/gateway/v1/extensions` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `list_extensions` |
| `POST` | `/gateway/v1/faces/likeness` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/identity_api.py](../gateway/identity_api.py) `likeness_route` |
| `POST` | `/gateway/v1/images/identity` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/identity_api.py](../gateway/identity_api.py) `identity_route` |
| `GET` | `/gateway/v1/media/jobs` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `list_media_jobs` |
| `POST` | `/gateway/v1/media/jobs` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `create_media_job` |
| `POST` | `/gateway/v1/media/jobs/image-generations` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `gateway_image_generations_media_job` |
| `GET` | `/gateway/v1/media/jobs/{job_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `get_media_job` |
| `POST` | `/gateway/v1/media/jobs/{job_id}/cancel` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `cancel_media_job` |
| `GET` | `/gateway/v1/media/jobs/{job_id}/content` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `gateway_media_job_content` |
| `GET` | `/gateway/v1/media/jobs/{job_id}/events` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `list_media_job_events` |
| `POST` | `/gateway/v1/media/jobs/{job_id}/events` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `append_media_job_event` |
| `GET` | `/gateway/v1/models/{model}/capabilities` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `get_model_capabilities` |
| `GET` | `/gateway/v1/providers` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `provider_native_list` |
| `GET` | `/gateway/v1/providers/{provider}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `provider_native_detail` |
| `DELETE` | `/gateway/v1/providers/{provider}/{native_path:path}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `provider_native_request` |
| `GET` | `/gateway/v1/providers/{provider}/{native_path:path}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `provider_native_request` |
| `OPTIONS` | `/gateway/v1/providers/{provider}/{native_path:path}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `provider_native_request` |
| `PATCH` | `/gateway/v1/providers/{provider}/{native_path:path}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `provider_native_request` |
| `POST` | `/gateway/v1/providers/{provider}/{native_path:path}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `provider_native_request` |
| `PUT` | `/gateway/v1/providers/{provider}/{native_path:path}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `provider_native_request` |
| `GET` | `/gateway/v1/receipts` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `list_receipts` |
| `GET` | `/gateway/v1/receipts/{request_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `get_receipt` |
| `GET` | `/gateway/v1/resources` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `list_resources` |
| `GET` | `/gateway/v1/workflows` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `list_workflows` |
| `GET` | `/gateway/v1/workflows/{workflow_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/api.py](../gateway/api.py) `get_workflow` |
| `POST` | `/gateway/v1/workflows/{workflow_id}/run` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `run_video_manifest_workflow` |
| `GET` | `/health` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `health` |
| `GET` | `/llms.txt` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `llms_txt` |
| `GET` | `/logout` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `browser_logout_alias` |
| `DELETE` | `/mcp` | unsupported | 405; stateless POST transport only | [gateway/mcp_server.py](../gateway/mcp_server.py) `mcp_no_stream` |
| `GET` | `/mcp` | unsupported | 405; stateless POST transport only | [gateway/mcp_server.py](../gateway/mcp_server.py) `mcp_no_stream` |
| `POST` | `/mcp` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/mcp_server.py](../gateway/mcp_server.py) `mcp_post` |
| `HEAD` | `/ollama/api/blobs/{digest}` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_blobs` |
| `POST` | `/ollama/api/blobs/{digest}` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_blobs` |
| `POST` | `/ollama/api/chat` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_chat` |
| `POST` | `/ollama/api/copy` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_copy` |
| `POST` | `/ollama/api/create` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_create` |
| `DELETE` | `/ollama/api/delete` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_delete` |
| `POST` | `/ollama/api/embed` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_embed` |
| `POST` | `/ollama/api/embeddings` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_embeddings` |
| `POST` | `/ollama/api/generate` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_generate` |
| `GET` | `/ollama/api/ps` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_ps` |
| `POST` | `/ollama/api/pull` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_pull` |
| `POST` | `/ollama/api/push` | unsupported | 501 after authentication and policy checks | [proxy.py](../proxy.py) `ollama_push` |
| `POST` | `/ollama/api/show` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_show` |
| `GET` | `/ollama/api/tags` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_tags` |
| `GET` | `/ollama/api/version` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `ollama_version` |
| `GET` | `/openai/v1/assets/{asset_id}/content` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `assets_content` |
| `DELETE` | `/openai/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `GET` | `/openai/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `PATCH` | `/openai/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `POST` | `/openai/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `PUT` | `/openai/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `POST` | `/openai/v1/audio/generate` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_generate` |
| `GET` | `/openai/v1/audio/models` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_models` |
| `POST` | `/openai/v1/audio/speech` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_speech` |
| `POST` | `/openai/v1/audio/transcriptions` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_transcriptions` |
| `POST` | `/openai/v1/audio/translations` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_translations` |
| `GET` | `/openai/v1/audio/voice_consents` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `list_consents` |
| `POST` | `/openai/v1/audio/voice_consents` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `create_consent` |
| `DELETE` | `/openai/v1/audio/voice_consents/{consent_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `delete_consent` |
| `GET` | `/openai/v1/audio/voice_consents/{consent_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `get_consent` |
| `POST` | `/openai/v1/audio/voice_consents/{consent_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `rename_consent` |
| `GET` | `/openai/v1/audio/voices` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `list_voices` |
| `POST` | `/openai/v1/audio/voices` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `create_voice` |
| `DELETE` | `/openai/v1/audio/voices/{voice_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `delete_voice` |
| `GET` | `/openai/v1/audio/voices/{voice_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `get_voice` |
| `POST` | `/openai/v1/audio/voices/{voice_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `update_voice` |
| `GET` | `/openai/v1/audio/voices/{voice_id}/sample` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `voice_sample` |
| `DELETE` | `/openai/v1/batches{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_batches_unsupported` |
| `GET` | `/openai/v1/batches{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_batches_unsupported` |
| `POST` | `/openai/v1/batches{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_batches_unsupported` |
| `POST` | `/openai/v1/chat/completions` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `chat_completions` |
| `POST` | `/openai/v1/completions` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `completions` |
| `POST` | `/openai/v1/embeddings` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `embeddings` |
| `DELETE` | `/openai/v1/files{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_files_unsupported` |
| `GET` | `/openai/v1/files{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_files_unsupported` |
| `POST` | `/openai/v1/files{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_files_unsupported` |
| `DELETE` | `/openai/v1/fine_tuning{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_fine_tuning_unsupported` |
| `GET` | `/openai/v1/fine_tuning{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_fine_tuning_unsupported` |
| `POST` | `/openai/v1/fine_tuning{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_fine_tuning_unsupported` |
| `POST` | `/openai/v1/images/edits` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `images_edits` |
| `POST` | `/openai/v1/images/generations` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `images_generations` |
| `POST` | `/openai/v1/images/variations` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `images_variations` |
| `GET` | `/openai/v1/images/{image_id}/content` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `images_content` |
| `GET` | `/openai/v1/models` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `list_models` |
| `WS` | `/openai/v1/realtime` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `realtime_v1` |
| `DELETE` | `/openai/v1/realtime{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_realtime_unsupported` |
| `GET` | `/openai/v1/realtime{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_realtime_unsupported` |
| `POST` | `/openai/v1/realtime{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_realtime_unsupported` |
| `POST` | `/openai/v1/rerank` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `rerank_v1` |
| `POST` | `/openai/v1/responses` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `create_response` |
| `POST` | `/openai/v1/responses/input_tokens` | unsupported | 400 unsupported_operation; no local tokenizer or cancellation | [proxy.py](../proxy.py) `count_response_input_tokens` |
| `DELETE` | `/openai/v1/responses/{response_id}` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `delete_response` |
| `GET` | `/openai/v1/responses/{response_id}` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `retrieve_response` |
| `POST` | `/openai/v1/responses/{response_id}/cancel` | unsupported | 400 unsupported_operation; no local tokenizer or cancellation | [proxy.py](../proxy.py) `cancel_response` |
| `GET` | `/openai/v1/responses/{response_id}/input_items` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `list_response_input_items` |
| `GET` | `/openai/v1/videos` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_list` |
| `POST` | `/openai/v1/videos` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_create` |
| `POST` | `/openai/v1/videos/edits` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_edit` |
| `POST` | `/openai/v1/videos/extensions` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_extend` |
| `DELETE` | `/openai/v1/videos/{video_id}` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_delete` |
| `GET` | `/openai/v1/videos/{video_id}` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_status` |
| `GET` | `/openai/v1/videos/{video_id}/content` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_content` |
| `POST` | `/openai/v1/videos/{video_id}/remix` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_remix` |
| `GET` | `/openai/v1/voices` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `list_voices` |
| `GET` | `/openapi.json` | supported | Implemented route; requires applicable policy and backend configuration | FastAPI built-in |
| `HEAD` | `/openapi.json` | supported | Implemented route; requires applicable policy and backend configuration | FastAPI built-in |
| `GET` | `/r/{request_id}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `receipt_deep_link` |
| `GET` | `/redoc` | supported | Implemented route; requires applicable policy and backend configuration | FastAPI built-in |
| `HEAD` | `/redoc` | supported | Implemented route; requires applicable policy and backend configuration | FastAPI built-in |
| `POST` | `/rerank` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `rerank_root` |
| `POST` | `/sdapi/v1/extra-batch-images` | unsupported | 501 after authentication and policy checks | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `POST` | `/sdapi/v1/extra-single-image` | unsupported | 501 after authentication and policy checks | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `POST` | `/sdapi/v1/img2img` | unsupported | 501 after authentication and policy checks | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `POST` | `/sdapi/v1/interrogate` | unsupported | 501 after authentication and policy checks | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `POST` | `/sdapi/v1/interrupt` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `GET` | `/sdapi/v1/latent-upscale-modes` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `GET` | `/sdapi/v1/options` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `POST` | `/sdapi/v1/options` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `POST` | `/sdapi/v1/png-info` | unsupported | 501 after authentication and policy checks | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `GET` | `/sdapi/v1/progress` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `GET` | `/sdapi/v1/samplers` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `GET` | `/sdapi/v1/schedulers` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `GET` | `/sdapi/v1/sd-models` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `GET` | `/sdapi/v1/sd-vae` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `POST` | `/sdapi/v1/txt2img` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `GET` | `/sdapi/v1/upscalers` | partial | Protocol subset or configured-backend feature; see vendor facades | [gateway/a1111_api.py](../gateway/a1111_api.py) `route` |
| `GET` | `/status` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `status_dashboard_index` |
| `GET` | `/v1/assets/{asset_id}/content` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `assets_content` |
| `DELETE` | `/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `GET` | `/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `PATCH` | `/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `POST` | `/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `PUT` | `/v1/assistants{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_assistants_unsupported` |
| `POST` | `/v1/audio/generate` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_generate` |
| `GET` | `/v1/audio/models` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_models` |
| `POST` | `/v1/audio/speech` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_speech` |
| `POST` | `/v1/audio/transcriptions` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_transcriptions` |
| `POST` | `/v1/audio/translations` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `audio_translations` |
| `GET` | `/v1/audio/voice_consents` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `list_consents` |
| `POST` | `/v1/audio/voice_consents` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `create_consent` |
| `DELETE` | `/v1/audio/voice_consents/{consent_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `delete_consent` |
| `GET` | `/v1/audio/voice_consents/{consent_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `get_consent` |
| `POST` | `/v1/audio/voice_consents/{consent_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `rename_consent` |
| `GET` | `/v1/audio/voices` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `list_voices` |
| `POST` | `/v1/audio/voices` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `create_voice` |
| `DELETE` | `/v1/audio/voices/{voice_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `delete_voice` |
| `GET` | `/v1/audio/voices/{voice_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `get_voice` |
| `POST` | `/v1/audio/voices/{voice_id}` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `update_voice` |
| `GET` | `/v1/audio/voices/{voice_id}/sample` | supported | Implemented route; requires applicable policy and backend configuration | [gateway/voices_api.py](../gateway/voices_api.py) `voice_sample` |
| `DELETE` | `/v1/batches{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_batches_unsupported` |
| `GET` | `/v1/batches{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_batches_unsupported` |
| `POST` | `/v1/batches{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_batches_unsupported` |
| `POST` | `/v1/chat/completions` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `chat_completions` |
| `POST` | `/v1/completions` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `completions` |
| `POST` | `/v1/embeddings` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `embeddings` |
| `DELETE` | `/v1/files{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_files_unsupported` |
| `GET` | `/v1/files{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_files_unsupported` |
| `POST` | `/v1/files{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_files_unsupported` |
| `DELETE` | `/v1/fine_tuning{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_fine_tuning_unsupported` |
| `GET` | `/v1/fine_tuning{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_fine_tuning_unsupported` |
| `POST` | `/v1/fine_tuning{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_fine_tuning_unsupported` |
| `POST` | `/v1/images/edits` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `images_edits` |
| `POST` | `/v1/images/generations` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `images_generations` |
| `POST` | `/v1/images/variations` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `images_variations` |
| `GET` | `/v1/images/{image_id}/content` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `images_content` |
| `POST` | `/v1/messages` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `anthropic_messages` |
| `POST` | `/v1/messages/count_tokens` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `anthropic_count_tokens` |
| `GET` | `/v1/models` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `list_models` |
| `GET` | `/v1/models/{model_id:path}` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `anthropic_model_retrieve` |
| `WS` | `/v1/realtime` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `realtime_v1` |
| `DELETE` | `/v1/realtime{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_realtime_unsupported` |
| `GET` | `/v1/realtime{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_realtime_unsupported` |
| `POST` | `/v1/realtime{rest:path}` | unsupported | 400 unsupported_operation (not 501) | [proxy.py](../proxy.py) `openai_realtime_unsupported` |
| `POST` | `/v1/rerank` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `rerank_v1` |
| `POST` | `/v1/responses` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `create_response` |
| `POST` | `/v1/responses/input_tokens` | unsupported | 400 unsupported_operation; no local tokenizer or cancellation | [proxy.py](../proxy.py) `count_response_input_tokens` |
| `DELETE` | `/v1/responses/{response_id}` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `delete_response` |
| `GET` | `/v1/responses/{response_id}` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `retrieve_response` |
| `POST` | `/v1/responses/{response_id}/cancel` | unsupported | 400 unsupported_operation; no local tokenizer or cancellation | [proxy.py](../proxy.py) `cancel_response` |
| `GET` | `/v1/responses/{response_id}/input_items` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `list_response_input_items` |
| `GET` | `/v1/status` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `v1_status` |
| `GET` | `/v1/videos` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_list` |
| `POST` | `/v1/videos` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_create` |
| `POST` | `/v1/videos/edits` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_edit` |
| `POST` | `/v1/videos/extensions` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_extend` |
| `DELETE` | `/v1/videos/{video_id}` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_delete` |
| `GET` | `/v1/videos/{video_id}` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_status` |
| `GET` | `/v1/videos/{video_id}/content` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_content` |
| `POST` | `/v1/videos/{video_id}/remix` | partial | Protocol subset or configured-backend feature; see vendor facades | [proxy.py](../proxy.py) `videos_remix` |
| `GET` | `/v1/voices` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `list_voices` |
| `POST` | `/v2/rerank` | supported | Implemented route; requires applicable policy and backend configuration | [proxy.py](../proxy.py) `rerank_v2` |
| `DELETE` | `/{path:path}` | unsupported | 404 for unmatched public paths | [proxy.py](../proxy.py) `proxy_request` |
| `GET` | `/{path:path}` | unsupported | 404 for unmatched public paths | [proxy.py](../proxy.py) `proxy_request` |
| `OPTIONS` | `/{path:path}` | unsupported | 404 for unmatched public paths | [proxy.py](../proxy.py) `proxy_request` |
| `PATCH` | `/{path:path}` | unsupported | 404 for unmatched public paths | [proxy.py](../proxy.py) `proxy_request` |
| `POST` | `/{path:path}` | unsupported | 404 for unmatched public paths | [proxy.py](../proxy.py) `proxy_request` |
| `PUT` | `/{path:path}` | unsupported | 404 for unmatched public paths | [proxy.py](../proxy.py) `proxy_request` |
