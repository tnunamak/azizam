# W4 train 67 gate fixes

Branch: `c2d/w4-ports-fix1`, based on `origin/int/t67` at `833fe4fd8b9b97baf9fb700bf92bc554402ecf93`.

The audio prepared run now opts out of generic lifecycle metadata writes. The native scheduler owns those lifecycle
facts; adding `protocol_job`, `protocol_state` and `protocol_submitting` changed stored metadata and job answers.
Queued runs retain their result bundles. Synchronous runs retain the original asset-only Library projection, without
new result-stage rows or a public `results` field. Asset handles and success still commit together before delivery.
The completion transaction removes the private pending-publication flag and restores the original null queue/start
phase fields. The store accepts explicit trusted phase overrides after its stale-copy preservation logic.

The 502 diagnosis exposed a direct call without a receipt trying to save `kind="audio"`, where storage accepts `aud`.
The original direct call did not store a result. It now follows the declared operation and returns its bytes through
the existing unindexed-result delivery path. Routed calls with receipts retain storage, result-limit checks, and
cancellation cleanup. The metadata choice is captured before the delivery context closes, so queued jobs keep their
submit-time `metadata_mode`.

The forget regression used a mock handler that returned bytes without running the new storage owner. Its assertions
now wrap the real route and use a shared fake HTTP engine. It still checks the request's forget decision, the stored
samples, absence of retained private metadata, and request-payload release. Its request uses the supported `native`
response format rather than relying on the old mock to bypass format validation.

Inventory decisions:

- `tests/test_job_envelope_sites.py`: classify prepared `run` as unpinned. It publishes a synchronous response using
  definitions already resolved for that request; queued workflow submissions still pin at acceptance.
- `tests/test_job_envelope_release.py`: account for `_fail_unpublished_job` and startup `_recover_media_jobs` calling
  `discard_failed`. The common deletion primitive releases references transactionally; the W4 cancellation/recovery
  checks cover the new callers.
- `tests/test_content_access_inventory.py`: pin the exact new `_run_audio_media_job` body and the extraction of generic
  completion from `run` to `_complete`. Event types and existing content access classifications are unchanged.
  The fixture itself is unchanged; reviewed revisions live explicitly in the test file.
- `gateway/client_errors.py`: recognize the existing emitted `result_refused` code in the closed gateway code list.
- Ad hoc fake count: use shared `HandlerTransport` in the declared-delivery test and migrate both audio manifest test
  files to `tests/support/fake_upstream/audio.py`. The fake ceiling stays 46.

No owner oracle, frozen route graph, manifest or golden trace was edited. No new test cases were added. Exact budget
proposal: no edits; `tests/budgets.toml:678` stays `pytest = 30239`, and line 926 stays
`ad_hoc_upstream_fake_files = 46`.

Every file named in the supplied failure list ran individually after committing, one suite at a time, with this command
(the output XML and scratch logs were removed after recording the evidence):

```sh
TMPDIR="$HOME/.tmp/gate-tmp" nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out <unique-output.xml> -n 4 tests/<file>.py
```

| File | Quoted pass line |
| --- | --- |
| `test_adapter_differential.py` | "30 passed, 2 xfailed in 9.83s" |
| `test_adapter_differential_s7.py` | "71 passed, 4 xfailed in 12.79s" |
| `test_audio_yue2_sa3_workflows.py` | "14 passed in 10.62s" |
| `test_content_access_inventory.py` | "9 passed in 14.50s" |
| `test_fake_ratchet.py` | "3 passed in 15.10s" |
| `test_job_envelope_release.py` | "8 passed in 16.98s" |
| `test_job_envelope_sites.py` | "25 passed in 13.45s" |
| `test_media_contract_validation.py` | "14 passed in 10.09s" |
| `test_prompt_memory_confirmation_r2.py` | "63 passed in 24.25s" |
| `test_s7_parity_audio.py` | "94 passed, 4 xfailed in 18.04s" |
| `test_secret_review_r9_client_egress.py` | "323 passed in 17.67s" |
| `test_tangoflux_audio_generate_manifest.py` | "33 passed in 10.69s" |

The additional committed-tree regression run used the same environment and runner:

```sh
TMPDIR="$HOME/.tmp/gate-tmp" nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out <unique-output.xml> -n 4 tests/test_w4_audio_declared.py tests/test_w4_mesh_declared.py tests/test_w4_audio_failure_parity.py tests/test_audio_media_job_integration.py tests/test_declared_delivery.py tests/test_declared_operation_job.py tests/test_result_bundle_audio.py tests/test_media_jobs_contract.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py
```

Output: "207 passed in 23.26s". The xfails in the required files were pre-existing and were not edited.

Expect it to work because the original differential goldens now pass over both the wire and stored jobs, while real
worker storage preserves forget and submit-time metadata choices. Confidence: 97% for the tested local paths. No live
service was touched, no service was restarted, and no reviewer was started.
