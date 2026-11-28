# Code-to-data moves M11–M20

The release data preserves the previous values. Tests change the release document before loading an isolated consumer and verify its effective policy. This is release-file evidence; it does not certify hot reload of these startup defaults.

| Slice | Data home and evidence | Confidence |
|---|---|---|
| M11 | `engine_record_defaults.yml`, service defaults. The original timeout omission sentinel and falsey-value comparison stay unchanged; only the comparison defaults table moves. Authorization regression tests cover all three review probes. | 93% |
| M12 | `defaults/workflow_execution.yml`. Save and run read the same validated release bounds and preserve lowering by owner config. Tests change the common ceiling and compare both consumers. | 96% |
| M13 | `adapter_upload_policy.yml`. Admission, advertised size and multipart/header limits read release values; the existing owner upload setting still lowers or raises caps within release ceilings. | 94% |
| M14 | The same upload policy supplies cache, scratch, sweep and hold values; chooser and materializer share the run hold. A controlled clock proves protection before expiry and eviction after expiry. | 95% |
| M15 | `adapter_discovery.yml`. TTL, stale retry, failure backoff and parallelism preserve existing values. A clock test changes list expiry without changing the compatibility table. | 94% |
| M16 | Workflow contract annotations declare normalized status promises. Graph class counts remain diagnostic. Status reads declarations even on a cached inventory; an undeclared graph promises no capabilities. | 88% |
| M17 | `local_capability_rows.yml` binds compatibility Python export names to declared operation ids. The existing operation relations still project new operations in data order. | 93% |
| M18 | `workflow_retention.yml`. Deleted-job lifetime is separate from member request-history retention because tombstones implement deletion and rollback safety. Immediate hiding stays code. | 95% |
| M19 | `models_catalog_operations.yml` holds six exact templates; status passes discovered model/intent pairs to their renderer. Gateway messages hold the stream-failure text, preserving the previous published diagnostic text. | 89% |
| M20 | The existing video-grid fact rule supplies direct, configured and manifest size fallbacks. Tests change its multiple and check all three consumers. | 97% |

## Why these homes

Workflow execution uses the release-and-override mechanism already implemented and documented in `CONFIG_HOMES.md`. That design names Helm defaults and sparse values, Renovate configuration layering and package-manager release configuration as precedent. Adapter policy and tombstone lifetime use release-only schema pairs directly under `registry/`, following the existing engine-record default reader. They are deliberately absent from owner-section discovery. `adapter_uploads` remains the sole owner setting for upload enablement and size caps. Schema validation happens before consumption; they do not import those systems' deployment or plugin mechanisms. Particular ceilings are release policy; positive/type checks remain code.

Workflow status promises belong to the versioned contract because they describe what a workflow offers, independent of node implementation. This follows the manifest/version/interface boundary compared with Lambda, Replicate and Windmill in `CODE_TO_DATA_S4_S8.md`, sections 4–5. The annotation is a local display projection, not a claim that those systems define these three fields. Class inventory remains with graph diagnostics.

Compatibility export bindings remain distinct from typed operation relations. The existing catalog is the authority for operation identity; the Python names only support current callers. Status templates reuse the existing catalog example home, and message text reuses the existing message home. No new executable adapter or plugin mechanism is introduced.

## Verification limits

Targeted hermetic runs cover policy changes, existing upload/cache behavior, registry save bounds, workflow run behavior, admin authorization, local capability projection, status rendering and both engine guards. No live service, remote backend, browser, or full-suite claim is made. The mechanical repository census is a candidate worklist, not proof that every policy family has received semantic review. A global 100% completion claim remains unsupported until that review finishes.
