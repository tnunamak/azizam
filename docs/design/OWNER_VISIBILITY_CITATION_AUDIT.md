# v3 citation audit against current main

Design: `origin/design/owner-visibility` (`782d763f`); cited code baseline `8a3147f0`.
Current code: `origin/main` at `52408b268215585dab40299f2ebc2bd920ab6bff`.
This table compares every explicit resolvable file:line citation and same-file continuation found in the two v3 documents. References in the adopted test-design appendix to v2 are treated as historical r18 citations and flagged when they do not locate there. A matching span proves text continuity only; use the semantic changes in OWNER_VISIBILITY_STEP1.md before changing behavior. Function ranges are relocation anchors, not claims that the function is unchanged. No live data was read.

| File | v3 line(s) | Comparison | Historical symbol | Current main location |
|---|---:|---|---|---|
| `firehose_metrics.py` | 69 | same cited text | `—` | 71-71 |
| `gateway/accounts.py` | 42 | changed cited text | `—` | — |
| `gateway/accounts.py` | 245-273 | changed cited text | `create_link` | 473-508 (matching lines 474-508) |
| `gateway/accounts.py` | 246 | same cited text | `create_link` | 473-508 (matching lines 474-474) |
| `gateway/accounts.py` | 268-270 | changed cited text | `create_link` | 473-508 (matching lines 503-505) |
| `gateway/accounts.py` | 340-350 | changed cited text | `accept_invite` | 545-593 (matching lines 582-589) |
| `gateway/accounts.py` | 344 | changed cited text | `accept_invite` | 545-593 |
| `gateway/accounts.py` | 346-347 | changed cited text | `accept_invite` | 545-593 |
| `gateway/accounts.py` | 357-373 | same cited text | `reset_password` | 596-612 (matching lines 596-612) |
| `gateway/accounts.py` | 430-436 | same cited text | `change_person` | 642-695 (matching lines 674-680) |
| `gateway/accounts_api.py` | 112 | same cited text | `owner` | 128-134 (matching lines 132-132) |
| `gateway/accounts_api.py` | 239-245 | changed cited text | `create_account_router` | 114-346 (matching lines 302-306) |
| `gateway/activity_api.py` | 19-127 | changed cited text | `create_activity_router` | 16-94 (matching lines 20-107) |
| `gateway/activity_api.py` | 25 | same cited text | `authorize` | 22-29 (matching lines 26-26) |
| `gateway/activity_api.py` | 82-88 | changed cited text | `requests` | 32-92 |
| `gateway/activity_api.py` | 112-127 | changed cited text | `_receipt_source` | removed or moved to another file |
| `gateway/admin_api.py` | 2251 | same cited text | `create_admin_router` | 1741-3421 (matching lines 2246-2246) |
| `gateway/admin_api.py` | 2280 | same cited text | `create_admin_router` | 1741-3421 (matching lines 2307-2307) |
| `gateway/admin_runtime.py` | 333-367 | changed cited text | `create_app` | 372-419 (matching lines 372-395) |
| `gateway/admin_runtime.py` | 344-355 | changed cited text | `create_app` | 372-419 (matching lines 395-395) |
| `gateway/admin_runtime.py` | 356-359 | changed cited text | `create_app` | 372-419 |
| `gateway/admin_runtime.py` | 567-601 | changed cited text | `create_credential` | 619-651 (matching lines 619-651) |
| `gateway/admin_runtime.py` | 585 | same cited text | `create_credential` | 619-651 (matching lines 634-634) |
| `gateway/admin_runtime.py` | 621 | changed cited text | `create_admin_automation_key` | 653-704 |
| `gateway/admin_runtime.py` | 1595-1720 | changed cited text | `_credential_active` | 2303-2309 (matching lines 2303-2475) |
| `gateway/admin_runtime.py` | 1604-1614 | changed cited text | `person_for_app_credential` | 2312-2322 (matching lines 2312-2322) |
| `gateway/admin_runtime.py` | 1640-1676 | changed cited text | `runtime_identity_for_app_credential_hash` | 2399-2433 (matching lines 2410-2425) |
| `gateway/admin_runtime.py` | 1648-1649 | changed cited text | `runtime_identity_for_app_credential_hash` | 2399-2433 (matching lines 2416-2416) |
| `gateway/admin_runtime.py` | 1675 | changed cited text | `runtime_identity_for_app_credential_hash` | 2399-2433 |
| `gateway/admin_runtime.py` | 1690 | same cited text | `runtime_identity_for_session_app` | 2436-2454 (matching lines 2436-2436) |
| `gateway/admin_runtime.py` | 1773 | same cited text | `create_admin_service_account_key` | 2508-2552 (matching lines 2532-2532) |
| `gateway/admin_store.py` | 57 | same cited text | `—` | 83-83 |
| `gateway/admin_store.py` | 117-188 | changed cited text | `—` | 132-201 |
| `gateway/admin_store.py` | 162 | same cited text | `—` | 176-176 |
| `gateway/admin_store.py` | 176 | same cited text | `—` | 189-189 |
| `gateway/admin_store.py` | 176-188 | same cited text | `—` | 189-201 |
| `gateway/admin_store.py` | 182-185 | same cited text | `—` | 195-198 |
| `gateway/admin_store.py` | 343 | same cited text | `_exec_schema` | 409-557 (matching lines 550-550) |
| `gateway/admin_store.py` | 343-387 | same cited text | `_exec_schema` | 409-557 (matching lines 550-653) |
| `gateway/admin_store.py` | 346-387 | same cited text | `_backfill_credential_creators` | 612-656 (matching lines 612-653) |
| `gateway/admin_store.py` | 370-372 | same cited text | `_backfill_credential_creators` | 612-656 (matching lines 636-638) |
| `gateway/admin_store.py` | 584-592 | same cited text | `runtime_auth_connection` | 952-960 (matching lines 952-970) |
| `gateway/admin_store.py` | 1225-1260 | changed cited text | `—` | 1734-1760 |
| `gateway/admin_store.py` | 1236 | same cited text | `create_app` | 1735-1769 (matching lines 1750-1750) |
| `gateway/admin_store.py` | 1238-1243 | changed cited text | `create_app` | 1735-1769 |
| `gateway/admin_store.py` | 1249 | changed cited text | `create_app` | 1735-1769 |
| `gateway/admin_store.py` | 1305 | same cited text | `ensure_personal_app_for_user` | 1772-1803 (matching lines 1798-1798) |
| `gateway/admin_store.py` | 1338 | changed cited text | `ensure_session_app_for_user` | 1806-1846 |
| `gateway/admin_store.py` | 1469-1477 | same cited text | `list_apps_for_user` | 2144-2152 (matching lines 2144-2152) |
| `gateway/admin_store.py` | 1663-1672 | same cited text | `find_runtime_credential_by_hash` | 2424-2440 (matching lines 2424-2433) |
| `gateway/admin_store.py` | 1670 | same cited text | `find_runtime_credential_by_hash` | 2424-2440 (matching lines 2431-2431) |
| `gateway/custom_voices.py` | 1-136 | changed cited text | `—` | 1-136 |
| `gateway/custom_voices.py` | 47-52 | same cited text | `VoiceLimits` | 46-51 (matching lines 46-51) |
| `gateway/custom_voices.py` | 50 | same cited text | `VoiceLimits` | 46-51 (matching lines 49-49) |
| `gateway/custom_voices.py` | 59-62 | same cited text | `owner_dir_name` | 58-61 (matching lines 58-61) |
| `gateway/custom_voices.py` | 103 | same cited text | `_dir_bytes` | 103-104 (matching lines 103-103) |
| `gateway/custom_voices.py` | 135-136 | same cited text | `_owner_paths` | 135-136 (matching lines 135-136) |
| `gateway/home_api.py` | 351 | changed cited text | `home` | 338-414 |
| `gateway/litellm_substrate.py` | 75-89 | same cited text | `compile_litellm_substrate_config` | 73-117 (matching lines 104-118) |
| `gateway/mcp_server.py` | 71 | same cited text | `—` | 91-91 |
| `gateway/mcp_server.py` | 89-99 | same cited text | `—` | 109-119 |
| `gateway/mcp_server.py` | 89-103 | changed cited text | `—` | 109-125 |
| `gateway/mcp_server.py` | 214-243 | same cited text | `_assets_from_response` | 312-341 (matching lines 312-341) |
| `gateway/mcp_server.py` | 227 | same cited text | `_assets_from_response` | 312-341 (matching lines 325-325) |
| `gateway/mcp_server.py` | 229-231 | same cited text | `_assets_from_response` | 312-341 (matching lines 327-329) |
| `gateway/mcp_server.py` | 242 | same cited text | `_assets_from_response` | 312-341 (matching lines 340-340) |
| `gateway/mcp_server.py` | 312-330 | same cited text | `capabilities` | 416-452 (matching lines 416-434) |
| `gateway/mcp_server.py` | 324 | same cited text | `capabilities` | 416-452 (matching lines 428-428) |
| `gateway/mcp_server.py` | 373-411 | same cited text | `run` | 477-515 (matching lines 477-515) |
| `gateway/mcp_server.py` | 454-531 | changed cited text | `_poll` | 558-564 (matching lines 558-688) |
| `gateway/mcp_server.py` | 494-500 | same cited text | `job` | 598-609 (matching lines 600-606) |
| `gateway/mcp_server.py` | 529-530 | same cited text | `call_tool` | 652-690 (matching lines 687-688) |
| `gateway/me_make.py` | 178-224 | same cited text | `add_make_routes` | 127-254 (matching lines 181-227) |
| `gateway/me_make.py` | 449 | same cited text | `_owned_source` | 428-466 (matching lines 452-452) |
| `gateway/me_media.py` | 77 | same cited text | `owner` | 80-81 (matching lines 81-81) |
| `gateway/me_media.py` | 122-160 | changed cited text | `create_me_media_router` | 84-445 (matching lines 127-165) |
| `gateway/me_media.py` | 162-219 | same cited text | `create_me_media_router` | 84-445 (matching lines 167-231) |
| `gateway/me_media.py` | 221-266 | same cited text | `create_me_media_router` | 84-445 (matching lines 233-278) |
| `gateway/me_media.py` | 221-269 | same cited text | `create_me_media_router` | 84-445 (matching lines 233-281) |
| `gateway/me_media.py` | 251-266 | same cited text | `delete_media` | 234-281 (matching lines 263-278) |
| `gateway/me_media.py` | 271-307 | same cited text | `create_me_media_router` | 84-445 (matching lines 386-422) |
| `gateway/me_media.py` | 309-328 | same cited text | `create_me_media_router` | 84-445 (matching lines 424-443) |
| `gateway/me_media.py` | 397-426 | changed cited text | `conservative_asset_deleter` | 676-707 (matching lines 676-707) |
| `gateway/me_media.py` | 405-409 | same cited text | `delete` | 683-705 (matching lines 684-688) |
| `gateway/me_media.py` | 416 | same cited text | `delete` | 683-705 (matching lines 695-695) |
| `gateway/me_media.py` | 440-505 | changed cited text | `_live_role_for_user` | 721-726 (matching lines 721-779) |
| `gateway/me_media.py` | 447 | same cited text | `_live_role_for_user` | 721-726 (matching lines 723-723) |
| `gateway/me_media.py` | 453-458 | same cited text | `_owned_app_ids` | 729-734 (matching lines 729-734) |
| `gateway/me_media.py` | 461-480 | changed cited text | `_media_scope` | 737-754 (matching lines 737-754) |
| `gateway/me_media.py` | 461-481 | changed cited text | `_media_scope` | 737-754 (matching lines 737-755) |
| `gateway/me_media.py` | 483-485 | same cited text | `_scope_requested` | 757-758 (matching lines 757-759) |
| `gateway/me_media.py` | 483-500 | same cited text | `_scope_requested` | 757-758 (matching lines 757-774) |
| `gateway/me_media.py` | 655-665 | changed cited text | `_media_items` | 975-976 (matching lines 1025-1031) |
| `gateway/me_media.py` | 656 | same cited text | `_media_items` | 975-976 (matching lines 1026-1026) |
| `gateway/me_media.py` | 660-663 | changed cited text | `_media_items` | 975-976 |
| `gateway/me_media.py` | 676-677 | changed cited text | `_media_items` | 975-976 |
| `gateway/me_media.py` | 691-694 | changed cited text | `_media_items` | 975-976 |
| `gateway/me_media.py` | 695-723 | changed cited text | `_media_items` | 975-976 |
| `gateway/me_media.py` | 716 | changed cited text | `_media_items` | 975-976 |
| `gateway/me_media.py` | 735-742 | changed cited text | `_media_items` | 975-976 (matching lines 1068-1075) |
| `gateway/me_media.py` | 739-740 | same cited text | `_media_items` | 975-976 (matching lines 1072-1073) |
| `gateway/me_media.py` | 748-788 | changed cited text | `_media_items` | 975-976 (matching lines 1216-1217) |
| `gateway/me_media.py` | 819-837 | same cited text | `_credential_hashes_for_scope` | 1257-1275 (matching lines 1257-1275) |
| `gateway/me_media.py` | 826 | same cited text | `_credential_hashes_for_scope` | 1257-1275 (matching lines 1264-1264) |
| `gateway/me_media.py` | 861-877 | changed cited text | `_deleted_asset_ids` | 1299-1305 (matching lines 1299-1322) |
| `gateway/me_media.py` | 925-942 | same cited text | `_receipt_in_scope` | 1370-1378 (matching lines 1370-1387) |
| `gateway/me_media.py` | 1156-1206 | same cited text | `_provenance` | 1618-1648 (matching lines 1618-1668) |
| `gateway/me_media.py` | 1196-1206 | same cited text | `_media_title` | 1658-1667 (matching lines 1658-1668) |
| `gateway/me_media.py` | 1230-1430 | changed cited text | `_usage_summary` | 1700-1915 (matching lines 1700-1926) |
| `gateway/me_media.py` | 1389-1409 | same cited text | `_usage_summary` | 1700-1915 (matching lines 1889-1909) |
| `gateway/me_media.py` | 1540-1547 | same cited text | `_usage_model` | 2058-2065 (matching lines 2058-2065) |
| `gateway/media_delivery.py` | 174-191 | same cited text | `deliver_bytes` | 174-191 (matching lines 174-191) |
| `gateway/media_delivery.py` | 194-234 | same cited text | `ranged_delivery_response` | 194-234 (matching lines 194-234) |
| `gateway/media_delivery.py` | 208-209 | same cited text | `ranged_delivery_response` | 194-234 (matching lines 208-209) |
| `gateway/media_ingestion.py` | 329 | same cited text | `_normalize_asset` | 320-333 (matching lines 329-329) |
| `gateway/media_jobs.py` | 187-190 | same cited text | `MediaJob` | 178-271 (matching lines 203-206) |
| `gateway/media_jobs.py` | 276-287 | changed cited text | `_filter_for_audience` | 300-311 (matching lines 300-311) |
| `gateway/media_jobs.py` | 1023-1026 | same cited text | `append_event` | 1248-1285 (matching lines 1263-1266) |
| `gateway/media_jobs.py` | 1142-1157 | same cited text | `_filter_event_for_audience` | 1382-1408 (matching lines 1382-1397) |
| `gateway/media_jobs.py` | 1164 | same cited text | `_filter_event_for_audience` | 1382-1408 (matching lines 1404-1404) |
| `gateway/media_metadata.py` | 62 | same cited text | `—` | 62-62 |
| `gateway/media_metadata.py` | 467-500 | same cited text | `strip_metadata` | 498-529 (matching lines 498-531) |
| `gateway/ownership.py` | 1-102 | same cited text | `—` | 1-102 |
| `gateway/ownership.py` | 3-4 | same cited text | `—` | 3-4 |
| `gateway/ownership.py` | 46-55 | same cited text | `stored_owner_key` | 46-55 (matching lines 46-55) |
| `gateway/ownership.py` | 75-77 | same cited text | `owner_key` | 58-82 (matching lines 75-77) |
| `gateway/ownership.py` | 85-94 | same cited text | `credential_owner_keys` | 85-94 (matching lines 85-94) |
| `gateway/prompt_memory.py` | 31-41 | same cited text | `—` | 38-48 |
| `gateway/receipt_store.py` | 349-369 | same cited text | `_with_source` | 386-405 (matching lines 386-406) |
| `gateway/receipts.py` | 185-187 | same cited text | `sanitize_admin_metadata` | 274-322 (matching lines 294-296) |
| `gateway/record_ownership.py` | 11-22 | changed cited text | `owns_record` | 11-19 (matching lines 11-19) |
| `gateway/resource_decisions.py` | 79-96 | same cited text | `project_decision` | 79-119 (matching lines 79-96) |
| `gateway/resource_decisions.py` | 79-109 | same cited text | `project_decision` | 79-119 (matching lines 79-109) |
| `gateway/stt_chunking.py` | 545-595 | changed cited text | `SttResourceGate` | 762-865 (matching lines 762-864) |
| `gateway/stt_chunking.py` | 560-595 | changed cited text | `acquire` | 784-797 (matching lines 784-864) |
| `gateway/user_runtime.py` | 140 | same cited text | `_app_belongs_to_user` | 127-143 (matching lines 127-127) |
| `gateway/user_runtime.py` | 281 | changed cited text | `build_user_portal_snapshot` | 188-254 |
| `gateway/user_runtime.py` | 319 | same cited text | `_owned_app_ids` | 264-276 (matching lines 274-274) |
| `gateway/user_runtime.py` | 350-361 | changed cited text | `build_user_activity_snapshot` | 279-321 (matching lines 306-312) |
| `gateway/user_runtime.py` | 356 | same cited text | `build_user_activity_snapshot` | 279-321 (matching lines 306-306) |
| `gateway/user_runtime.py` | 373-428 | changed cited text | `get_user_receipt` | 324-387 (matching lines 324-387) |
| `gateway/user_runtime.py` | 453-459 | changed cited text | `mint_user_app_key` | 390-438 |
| `gateway/user_runtime.py` | 459 | changed cited text | `mint_user_app_key` | 390-438 |
| `gateway/user_runtime.py` | 502 | same cited text | `ensure_try_session_key` | 441-496 (matching lines 465-465) |
| `gateway/user_runtime.py` | 560-566 | changed cited text | `create_user_app` | 499-536 (matching lines 520-526) |
| `gateway/workflow_call.py` | 131-148 | same cited text | `asset_bytes` | 131-152 (matching lines 131-148) |
| `proxy.py` | 191-204 | same cited text | `—` | 233-250 |
| `proxy.py` | 808 | same cited text | `—` | 947-947 |
| `proxy.py` | 2018 | same cited text | `_provision_existing_user_personal_apps` | 1990-2020 (matching lines 1999-1999) |
| `proxy.py` | 2716 | same cited text | `handle_cli_command` | 2658-2709 (matching lines 2668-2668) |
| `proxy.py` | 6843-6865 | same cited text | `_sign_asset_token` | 6867-6872 (matching lines 6867-6889) |
| `proxy.py` | 6924-6927 | same cited text | `_record_asset_owner` | 6953-6966 (matching lines 6963-6966) |
| `proxy.py` | 6930 | same cited text | `_asset_access_allowed` | 6969-6976 (matching lines 6969-6969) |
| `proxy.py` | 6930-6937 | same cited text | `_asset_access_allowed` | 6969-6976 (matching lines 6969-6976) |
| `proxy.py` | 6934-6936 | same cited text | `_asset_access_allowed` | 6969-6976 (matching lines 6973-6975) |
| `proxy.py` | 7027 | same cited text | `_store_asset_bytes` | 7066-7136 (matching lines 7087-7087) |
| `proxy.py` | 7028-7040 | same cited text | `_store_asset_bytes` | 7066-7136 (matching lines 7088-7102) |
| `proxy.py` | 7037-7052 | same cited text | `_store_asset_bytes` | 7066-7136 (matching lines 7099-7114) |
| `proxy.py` | 7048-7051 | same cited text | `_store_asset_bytes` | 7066-7136 (matching lines 7110-7113) |
| `proxy.py` | 7172 | same cited text | `build_signed_asset_url` | 7257-7274 (matching lines 7257-7257) |
| `proxy.py` | 7186 | same cited text | `build_signed_image_url` | 7277-7288 (matching lines 7277-7277) |
| `proxy.py` | 9352 | same cited text | `_video_job_access_allowed` | 9374-9386 (matching lines 9374-9374) |
| `proxy.py` | 14162 | changed cited text | `_gateway_record_authorizer` | 14134-14141 |
| `proxy.py` | 14612-14668 | changed cited text | `—` | 14582-14672 |
| `proxy.py` | 14620-14626 | changed cited text | `_mcp_internal_call` | 14599-14604 (matching lines 14599-14604) |
| `proxy.py` | 14629-14633 | changed cited text | `_mcp_catalog` | 14646-14650 (matching lines 14646-14649) |
| `proxy.py` | 14636 | same cited text | `_mcp_store_asset` | 14653-14661 (matching lines 14653-14653) |
| `proxy.py` | 14636-14644 | same cited text | `_mcp_store_asset` | 14653-14661 (matching lines 14653-14661) |
| `proxy.py` | 14636-14652 | same cited text | `_mcp_store_asset` | 14653-14661 (matching lines 14653-14669) |
| `proxy.py` | 14647-14652 | same cited text | `_mcp_read_asset` | 14664-14670 (matching lines 14664-14669) |
| `proxy.py` | 14658-14668 | changed cited text | `—` | — |
| `proxy.py` | 14659 | changed cited text | `—` | — |
| `proxy.py` | 16017-16038 | changed cited text | `user_portal_receipt_detail` | 16519-16541 (matching lines 16519-16541) |
| `proxy.py` | 16097 | same cited text | `—` | 16619-16619 |
| `proxy.py` | 16129 | same cited text | `—` | 16649-16649 |
| `proxy.py` | 17098 | same cited text | `_setup_personal_app_for_session` | 17014-17028 (matching lines 17022-17022) |
| `proxy.py` | 17783 | same cited text | `—` | 17610-17610 |
| `proxy.py` | 17849-17856 | changed cited text | `setup_create_first_app` | 17611-17718 (matching lines 17673-17680) |
| `proxy.py` | 23468-23560 | changed cited text | `—` | 23557-23678 |
| `proxy.py` | 23563 | same cited text | `_signed_asset_content_response` | 23681-23783 (matching lines 23681-23681) |
| `proxy.py` | 23617 | same cited text | `_signed_asset_content_response` | 23681-23783 (matching lines 23735-23735) |
| `proxy.py` | 23625-23660 | same cited text | `_signed_asset_content_response` | 23681-23783 (matching lines 23743-23778) |
| `proxy.py` | 23629 | same cited text | `_signed_asset_content_response` | 23681-23783 (matching lines 23747-23747) |
| `proxy.py` | 23630-23647 | same cited text | `_signed_asset_content_response` | 23681-23783 (matching lines 23748-23765) |
| `proxy.py` | 23633-23647 | same cited text | `_signed_asset_content_response` | 23681-23783 (matching lines 23751-23765) |
| `proxy.py` | 25808-25817 | changed cited text | `_responses_row_visible` | 25886-25888 (matching lines 25886-25886) |
| `proxy.py` | 25808-25828 | changed cited text | `_responses_row_visible` | 25886-25888 (matching lines 25886-25899) |
| `proxy.py` | 30203-30208 | same cited text | `—` | 31208-31213 |
| `status_endpoint.py` | 2469-2540 | same cited text | `_recent_renders_videos` | 2492-2530 (matching lines 2492-2563) |
| `status_endpoint.py` | 2981-3031 | same cited text | `_live_video_jobs` | 2999-3059 (matching lines 2999-3049) |
| `status_endpoint.py` | 3960-3963 | same cited text | `build_status_payload` | 3523-3997 (matching lines 3978-3981) |
| `tests/e2e/contracts.py` | 144-145 | same cited text | `run` | 62-204 (matching lines 144-145) |
| `tests/test_activity_api.py` | 44-107 | same cited text | `test_owner_reads_sanitized_receipts_and_stable_pages` | 41-52 (matching lines 45-108) |
| `tests/test_home_api.py` | 121-163 | changed cited text | `test_home_owner_recent_defaults_to_instance_activity` | 130-148 (matching lines 130-172) |
| `tests/test_me_media.py` | 459-482 | same cited text | `test_owner_everyone_scope_is_role_gated_and_audited` | 480-503 (matching lines 480-503) |
| `tests/test_me_media.py` | 536-551 | changed cited text | `test_media_sources_filter_jobs_and_cache_assets` | 521-568 (matching lines 557-567) |
| `tests/test_me_media.py` | 617 | same cited text | `test_usage_cache_respects_embedded_foreign_receipt_scope` | 614-635 (matching lines 633-633) |
| `tests/test_me_media.py` | 704-711 | same cited text | `test_owner_missing_receipt_jobs_recover_source_from_job_identity` | 685-729 (matching lines 720-727) |
| `tests/test_me_media.py` | 877-892 | changed cited text | `test_usage_summary_scopes_by_user_and_owner_person_all` | 875-905 (matching lines 892-905) |
| `tests/test_me_media.py` | 1043-1054 | same cited text | `test_owner_detail_is_explicit_and_audited_but_cannot_delete_foreign` | 1126-1141 (matching lines 1130-1141) |
| `tests/test_prompt_memory_identity_r2.py` | 152 | same cited text | `test_service_output_stays_in_library_without_prompt_or_full_metadata` | 147-171 (matching lines 158-158) |
| `tests/test_workflows_catalog.py` | 753-790 | changed cited text | `test_proxy_route_owner_instance_scope_includes_service_and_local_jobs` | 753-800 (matching lines 753-789) |
| `web/e2e/activity.spec.ts` | 51-92 | changed cited text | `—` | 119-128 |
| `web/e2e/global-setup.ts` | 60-68 | changed cited text | `—` | 95-103 |
| `web/e2e/library.spec.ts` | 293 | same cited text | `—` | 314-314 |
| `web/e2e/library.spec.ts` | 392 | same cited text | `—` | 411-411 |
| `web/e2e/library.spec.ts` | 408 | same cited text | `—` | 427-427 |
| `web/e2e/library.spec.ts` | 418 | same cited text | `—` | 437-437 |
| `web/e2e/live-data-audio.spec.ts` | 11 | same cited text | `—` | 12-12 |
| `web/e2e/live-data-scoping.spec.ts` | 37-65 | changed cited text | `—` | 40-67 |
| `web/e2e/prompt-memory.household.ts` | 216-240 | same cited text | `—` | 218-242 |
| `web/e2e/prompt-memory.household.ts` | 368-375 | same cited text | `—` | 370-377 |
| `web/src/app/library/page.spec.tsx` | 685-700 | same cited text | `—` | 860-875 |
| `web/src/app/library/page.tsx` | 244-245 | same cited text | `—` | 278-279 |
| `web/src/app/library/page.tsx` | 468 | same cited text | `—` | 537-537 |
| `web/src/app/library/page.tsx` | 483 | same cited text | `—` | 552-552 |
| `web/src/app/library/page.tsx` | 944-948 | changed cited text | `—` | 1044-1049 |
| `web/src/app/library/page.tsx` | 948 | same cited text | `—` | 1049-1049 |
| `web/src/app/library/page.tsx` | 997 | same cited text | `—` | 1103-1103 |
| `web/src/app/library/page.tsx` | 1624 | same cited text | `—` | 1814-1814 |
| `web/src/app/library/page.tsx` | 1647 | same cited text | `—` | 1837-1837 |
| `web/src/app/library/page.tsx` | 2274 | same cited text | `—` | 2481-2481 |
| `web/src/app/library/prompt-visibility.spec.tsx` | 228-237 | same cited text | `—` | 228-236 |
| `web/src/lib/audit-description.ts` | 4-5 | same cited text | `—` | 4-5 |
| `web/tests/components/home-library-polish.spec.tsx` | 340 | same cited text | `—` | 340-340 |

225 cited ranges across 45 files compared. All implementation references were independently located by current symbol before use.
