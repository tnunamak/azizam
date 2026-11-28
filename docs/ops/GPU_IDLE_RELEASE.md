# GPU idle release

When a GPU pool has its lease free but cannot fit a waiting claim,
the gateway asks idle engines on overlapping GPUs to unload.
If any engine confirms release, the gateway reads capacity again and
uses that second decision. A release does not guarantee admission.

## Configure the pool and engines

Set `resource_capacity.devices` on the pool. Use a non-empty YAML list
of distinct, non-negative integers. Strings, booleans, fractions and
duplicates are invalid. The gateway sorts the list. Omit the field
to leave the pool with no devices for idle release.

Example pool record fields only. Replace the IDs, numbers and
observer path and pointers with your pool's values.

```yaml
resource_capacity:
  enabled: true
  mode: exclusive-route-pool
  pool_id: example-pool
  devices: [0]
  max_claim_bytes: 4294967296
  reserve_bytes: 1073741824
  retry_after_seconds: 5
  timeout_seconds: 60
  observer:
    service: example-observer
    path: /capacity
    free_pointer: /free_bytes
    total_pointer: /total_bytes
```

On each participating engine, declare `residency.unload.op` and
`resources.gpu.devices`. The unload operation goes to the engine's
control address. Use an operation the engine already declares there.

Example engine record fields only. The operation name is illustrative.
Use `verify: none` only when the operation's success response is enough
evidence; otherwise omit it and let the controller check that the
resident observation is false. Other verification values are refused.

```yaml
enabled: true
residency:
  unload:
    op: example_unload
    verify: none
resources:
  gpu:
    devices: [0]
```

Put `residency` where the record already keeps it. If the record has a top-level
`residency` mapping, even an empty one, the gateway reads that one and ignores
`config.residency`.

Declare idle release only on engines whose requests the gateway sends itself
(speech, transcription and workflow engines). Requests the gateway hands to its
LiteLLM library are not counted, so the gate cannot see a chat in flight on such
an engine and could unload it mid-reply.

An engine's device list must contain at least one non-negative integer
and no other kind of value. The gateway treats a missing, empty or
malformed list as no devices. It collapses duplicate engine indices into
a set. A top-level resources mapping takes precedence over
`config.resources`; the gateway checks the latter only when the former
is not a mapping.

## When release runs

- The pool takes the per-pool lock before reading memory. If a caller sets
  `wait=false` and the lock is held, it refuses immediately without
  calling release. A waiting caller first waits for the lock.
- Inside the lock the first capacity decision controls release. Release runs only
  when that decision refuses the claim. An accepted claim does not invoke release.
  If the memory reading fails before it returns a decision, release is not reached.
- The gateway calls release at most once per lease attempt, not repeatedly
  while waiting. It reads capacity again only when `release == "freed"`.
- An explicitly disabled engine, or one without a non-empty unload
  operation, is not selected. A single shared device index is enough to
  select an enabled engine. No shared indices means no unload request.

## Safety

The unload gate checks in-flight requests to the engine's configured
addresses and claims those addresses under the same lock. Requests stay
counted through response-body close. Health and metadata probes do not
count. The gate also refuses when another unload already holds an address.
An engine the gate refuses answers busy. Its unload is not sent.

If the gate succeeds, new counted requests to those addresses wait for
release to finish. The unload callback must await the send in the same
task that acquired the gate. Sending from another task waits on its
own gate. A finally block releases the gate and wakes waiting requests.

The gateway asks matched engines concurrently. An ordinary exception
from one member does not stop the others. Any confirmed release wins;
otherwise any busy result wins; otherwise nothing was freed. Cancellation
is not treated as an ordinary failure.

## What you can see

The warning text is exactly `idle release member failed` for a member
exception and `idle release failed` for an ordinary release-callback
exception at the pool.

If the pool still refuses, its exception carries `status` and
`release`. The release values are `not_asked`, `freed`,
`engine_busy` and `nothing_freed`. The non-waiting refusal while the
lease is held sets `lease_held` to true. These files do not show how
an API response or job state serializes these fields.

## Troubleshoot

- **No pool devices:** Add the correct indices to `resource_capacity.devices`.
- **No engine unload:** Check that the engine is enabled and
  `residency.unload.op` names an operation it can run.
- **No overlap:** Compare pool and engine indices. At least one must match.
- **Engine always busy:** Check for long-running requests or open response
  streams. Let them finish; idle release does not interrupt them.
