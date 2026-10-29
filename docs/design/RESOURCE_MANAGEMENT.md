# Resource admission for lifecycle changes

Opt-in GPU admission serializes lifecycle changes that overlap on a device. A
service can still share a card with other services. Changes on independent cards
can run concurrently. The serving lease keeps an active request's selection in
place until its response finishes.

## Operator policy

Use the existing service registry import/edit interface. This example is data,
not a preset attached to any engine:

```yaml
resources:
  management:
    mode: capacity
    devices:
      GPU-example-uuid:
        observe_mb: target_additional_peak
    headroom_mb: 512
    queue_wait_s: 60
    observe_quiescent: allocation_quiescent
observations:
  target_additional_peak:
    http: {path: /capacity, select: /target/additional_peak_mb}
  allocation_quiescent:
    http: {path: /capacity, select: /allocation_quiescent}
```

`observe_mb` names an ordinary declared observation. Its value must be a
non-negative integer in MiB. It must bound the **additional peak allocation** of
the complete change, including preparation and selection. This is a contract
with the configured backend, not a value the gateway can infer from its name.
An observation can report prospective allocation from model metadata without
manual per-model sizes. If no such fact exists, the owner can instead declare
`additional_peak_mb: 4096`. No field means unknown; explicit zero means zero.
Booleans, negative values and floats are refused. `headroom_mb` is added per card.

Each offering can override demand with `resources.devices`, using the same UUID
keys and claim shapes. Placement remains service policy. A card index is not a
stable identity and is not accepted in this policy. Configure every card the
operation can allocate on. Demand observations must describe the target offering;
use separate observation names for offerings when the backend needs that.

Absent `resources.management` preserves existing behavior and does not sample
GPUs. No policy is seeded automatically. Invalid opt-in policy fails closed.
Unmanaged processes are not started or stopped by this mechanism. Explicit
owner-declared selection/residency operations remain available independently of
process ownership.

## Runtime behavior

The coordinator acquires all target device locks in UUID order with one bounded
queue deadline. It reads demand and samples live device totals **after** acquiring
the locks. It uses `ResourceClaim` and the existing admission evaluator on each
card. Device usage includes external compute, graphics and driver allocations.
Unknown, stale, duplicate or replaced device evidence cannot establish capacity.
The owner-status cache and passive service footprints are never allocation proofs.

Locks reserve the devices through mutation, readiness verification and any
configured restoration. Waiting changes sample again after their predecessor
settles; they cannot both spend the same free-memory sample. Nested declarative
load preparation uses the selection's reservation. A disconnect waits for the
control transaction to settle before releasing it and still returns cancellation
to the caller. Cancellation while merely waiting releases partial lock sets.

Integration covers `SwitchController` (including protocol-adapter selection),
`ResidencyController.load`, runtime wake, and opted-in readiness/start and swap
calls in the proxy. Already verified selection and healthy process requests do
not require another allocation claim. Resource-managed legacy swaps always drain
serving leases, even if the legacy drain setting is disabled. Runtime unload,
sleep and wake take the service lock and drain serving leases before mutation.
Low-level driver calls outside these runtime paths are not scheduled here.

`resource_management` in owner status reports current reservations and unresolved
devices. It is read fresh when status is projected, independently of the GPU
status cache. A failed mutation may still allocate remotely. Its device is
therefore blocked for subsequent managed loads, even if a new sample looks free.
`ResourceCoordinator.reconcile` / `EngineControlRuntime.reconcile_resources`
clears only that service's uncertainty when its declared `observe_quiescent`
returns the known boolean `true`. This must prove that **no outstanding control
work can allocate later**. Health, zero usage and no active inference requests do
not establish that fact. There is no new admin HTTP reconciliation endpoint.

## Limits and integration gates

- Reservations and unresolved state are local to one gateway process. Run one
  lifecycle-controlling worker per device. Separate workers/gateways need a shared
  reservation ledger before enabling this policy. Restart loses unresolved state;
  the operator must establish quiescence before resuming after a failed operation.
- External consumers can allocate after a sample; this is admission coordination,
  not a hardware memory guarantee. Readiness verification is the backend's declared
  contract. A successful response that lies about readiness cannot be made safe
  by a GPU free-memory sample.
- No eviction or automatic unloading of another engine occurs. No memory credit
  is granted for an expected unload. Large replacements that fit only after
  releasing their old model can defer. This conservative behavior needs a separate
  declarative release/verify/recovery policy before it can be expanded safely.
- Passive resident high-water marks remain diagnostic, not prospective demand.
  Runtime token/context growth after admission is outside this load coordinator.
  CPU, RAM, cross-host placement and model file-size inference are not added.
- This slice has deterministic fake-device and fake-operation tests. It has not
  been deployed or tested against live engines. Independent review is an
  integration gate; the implementation worker was explicitly forbidden to delegate.

The regression suite is `tests/test_resource_management.py`. Run it through
`scripts/run-hermetic-pytest.sh`, which executes committed code in isolated
filesystem, network, process and IPC namespaces. The same runner checks existing
contract runtime, lifecycle/drain and engine-knowledge boundary tests.
