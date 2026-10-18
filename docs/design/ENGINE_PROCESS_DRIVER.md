# Owned process lifecycle (engine contract v2.1)

`process` starts owner-authored argv without a shell. The owner must enable
**Allow Azizam to run commands on this computer** first. The permission is off
by default. Engine records and the permission are owner-only writes with audit
events. New `process` and `command` recipes use argv arrays; legacy command
strings remain readable for existing records.

On Linux, Azizam starts a small supervisor in a new session. It launches the
owner argv without a shell and remains alive if the engine exits. A private
runtime file under
`data/processes/` stores the session leader's PID, kernel start tick, declared
port, and argv digest. On restart, matching live state is adopted. A stale PID
or changed recipe is never treated as owned. Normal shutdown sends SIGTERM to
the group, then SIGKILL after the declared stop deadline. A crash leaves the
state file for adoption at the next start. A fixed port is required; Azizam
refuses a port already accepting connections before it spawns. Readiness comes
only from a declared `process_ready` observation, never from the process merely
existing or a model inventory response.

The restart budget counts launch attempts in a sliding window and adds bounded
backoff. An intentional stop clears the budget. `idle_owner: gateway` permits
idle-stop after the declared interval and after active requests finish;
`idle_owner: engine` never lets the gateway stop for idleness. Logs have a byte
cap and replace declared environment values with `[redacted]` before storage.

Windows needs a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, a durable
creation-time identity, and an adoption policy that can prove the process
belongs to the recorded job before terminating it. A PID alone is unsafe there.
The current implementation fails closed on Windows. macOS also fails closed
until a kernel-backed start-time identity and restart recovery are implemented.
The platform-gated tests document these limits rather than claiming support.
