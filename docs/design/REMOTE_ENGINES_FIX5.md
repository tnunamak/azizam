# Remote engine gate corrections

Base: train 67 commit 833fe4fd8. Branch: c2d/remote-engines-fix5.

## Causes and treatment

| Failure group | Cause | Treatment |
| --- | --- | --- |
| Admin secret sweep, sweep writes, registry session writes, secret session writes, partial registry, partial boot | The authority writer read excluded non-object records as mappings. These fixtures represent supported partial-registry state; the doubles are valid. | Check mapping shape before reading connection or enabled fields. Preserve excluded records and keep owner authorization when an existing remote record changes. |
| Engine address review | Local connection doubles omit the new optional remote fields. | Production uses getattr defaults; explicit capability markers and known remote destinations still refuse unbound access. |
| Protocol transition egress | The test constructs Runtime without its initializer. | Declare the optional capability attribute on Runtime. |
| Native transition egress | The stopped-engine spy did not accept the hook's refreshed keyword. This double was wrong. | Forward hook keywords to the real check before recording a send. |
| Content access inventory | The completion writer moved from run to _complete; the integrated audio worker also changed. | Update function/hash metadata under Python 3.12, retaining every access classification. |
| Fake ratchet | Remote tests added two ad hoc upstream mechanisms; the integrated delivery test added another. | Use HandlerTransport from shared support and move the TLS peer into tests/support/fake_upstream. Keep the ceiling at 46. |

No owner oracle is edited. No new cases are added and no oracle line change is
proposed: tests/budgets.toml:678 remains pytest = 30239 and its line 926 remains
ad_hoc_upstream_fake_files = 46. Reviewed literal lists and the frozen route graph
are unchanged. No public output or parity golden is changed.

## Verification scope

All 31 original files were recovered from the owner's w3b.xml report and run
together, with seven additional security, performance, TLS, boundary and parity
files. The broad run reported `47 failed, 1884 passed, 10 xfailed in 277.79s`.
Its remaining audio/video parity and inventory failures overlap the owner's
separate W4/video attribution lists; that combined run is not a passing gate.

After fixing the inventory hashes and shared transport imports, the final run
covers every file named in w3b.txt, the delivery file and the remote security,
performance and protocol files. Per-file counts below come from JUnit, using
the final run where available and the broad run for other files. They include
failures rather than presenting an unsuccessful file as passed.

All runs use TMPDIR=$HOME/.tmp/gate-tmp, nice -n 15, the hermetic runner,
and four workers, with one suite running at a time. The closing amendment adds
documentation receipts only; production and test code are unchanged.

Expect it to work because the original failure oracles exercise excluded-record
preservation, session checks, credential exposure, local URL joining and send-time
lease checks. Remote refusal and connector tests check that optional-field defaults
do not grant remote authority. Confidence: 0.92 for this correction; the integrated
train still needs its other audio/video failures resolved.

## Final focused result

`562 passed, 30 warnings in 124.07s (0:02:04)`.

## Per-file JUnit results

Original 31 files; final results supersede broad results for changed groups.

| File | Result | Run |
| --- | --- | --- |
| tests/test_adapter_differential.py | `27 passed, 3 failed, 2 xfailed` | broad |
| tests/test_adapter_differential_s7.py | `64 passed, 7 failed, 4 xfailed` | broad |
| tests/test_admin_secret_sweep.py | `10 passed` | final |
| tests/test_admin_secret_sweep_writes.py | `137 passed` | final |
| tests/test_audio_yue2_sa3_workflows.py | `10 passed, 4 failed` | broad |
| tests/test_ci_local.py | `73 passed` | broad |
| tests/test_content_access_inventory.py | `9 passed` | final |
| tests/test_domain_literal_ratchet.py | `8 passed, 1 failed` | broad |
| tests/test_engine_address_review_fixes.py | `112 passed` | final |
| tests/test_fake_ratchet.py | `3 passed` | final |
| tests/test_job_envelope_release.py | `7 passed, 1 failed` | broad |
| tests/test_job_envelope_sites.py | `24 passed, 1 failed` | broad |
| tests/test_media_contract_validation.py | `13 passed, 1 failed` | broad |
| tests/test_media_inputs_upload_routes.py | `31 passed, 6 failed` | broad |
| tests/test_openai_schema_conformance.py | `9 passed, 2 failed` | broad |
| tests/test_partial_boot_round6.py | `14 passed` | final |
| tests/test_prompt_memory_confirmation_r2.py | `60 passed, 3 failed` | broad |
| tests/test_registry_partial_operator.py | `52 passed` | final |
| tests/test_registry_writes_check_the_session.py | `20 passed` | final |
| tests/test_route_execution_intents.py | `56 passed, 1 failed` | broad |
| tests/test_runtime_route_policy.py | `47 passed, 1 failed` | broad |
| tests/test_s7_parity_audio.py | `92 passed, 2 failed, 4 xfailed` | broad |
| tests/test_secret_review_r9_client_egress.py | `322 passed, 1 failed` | broad |
| tests/test_secret_writes_check_the_session.py | `16 passed` | final |
| tests/test_t58_domain_data.py | `13 passed, 1 failed` | broad |
| tests/test_transition_gate_egress.py | `37 passed` | final |
| tests/test_transition_gate_native.py | `5 passed` | final |
| tests/test_video_extend_h3.py | `48 passed, 1 failed` | broad |
| tests/test_video_remix_replace.py | `19 passed, 2 failed` | broad |
| tests/test_video_subject.py | `29 passed, 4 failed` | broad |

## Supplemental files

| File | Result | Run |
| --- | --- | --- |
| tests/test_c2d_model_metadata_parity.py | `150 passed` | broad |
| tests/test_c2d_openapi_parity.py | `159 passed` | broad |
| tests/test_declared_delivery.py | `12 passed` | final |
| tests/test_engine_knowledge_boundary.py | `29 passed` | broad |
| tests/test_engine_protocol_round3.py | `80 passed` | final |
| tests/test_implicit_marker_ratchet.py | `15 passed` | broad |
| tests/test_remote_performance.py | `8 passed` | final |
| tests/test_remote_security_review.py | `47 passed` | final |

## Commands

```sh
TMPDIR="$HOME/.tmp/gate-tmp" nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../fix5-gate.xml -n 4 tests/test_adapter_differential.py tests/test_adapter_differential_s7.py tests/test_admin_secret_sweep.py tests/test_admin_secret_sweep_writes.py tests/test_audio_yue2_sa3_workflows.py tests/test_ci_local.py tests/test_content_access_inventory.py tests/test_domain_literal_ratchet.py tests/test_engine_address_review_fixes.py tests/test_fake_ratchet.py tests/test_job_envelope_release.py tests/test_job_envelope_sites.py tests/test_media_contract_validation.py tests/test_media_inputs_upload_routes.py tests/test_openai_schema_conformance.py tests/test_partial_boot_round6.py tests/test_prompt_memory_confirmation_r2.py tests/test_registry_partial_operator.py tests/test_registry_writes_check_the_session.py tests/test_route_execution_intents.py tests/test_runtime_route_policy.py tests/test_s7_parity_audio.py tests/test_secret_review_r9_client_egress.py tests/test_secret_writes_check_the_session.py tests/test_t58_domain_data.py tests/test_transition_gate_egress.py tests/test_transition_gate_native.py tests/test_video_extend_h3.py tests/test_video_remix_replace.py tests/test_video_subject.py tests/test_remote_security_review.py tests/test_remote_performance.py tests/test_engine_protocol_round3.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py tests/test_c2d_openapi_parity.py tests/test_c2d_model_metadata_parity.py --tb=short

TMPDIR="$HOME/.tmp/gate-tmp" nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../fix5-final.xml -n 4 tests/test_admin_secret_sweep.py tests/test_admin_secret_sweep_writes.py tests/test_content_access_inventory.py tests/test_declared_delivery.py tests/test_engine_address_review_fixes.py tests/test_engine_protocol_round3.py tests/test_fake_ratchet.py tests/test_partial_boot_round6.py tests/test_registry_partial_operator.py tests/test_registry_writes_check_the_session.py tests/test_remote_performance.py tests/test_remote_security_review.py tests/test_secret_writes_check_the_session.py tests/test_transition_gate_egress.py tests/test_transition_gate_native.py --tb=short
```
