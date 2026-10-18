# Owned process lifecycle (engine contract v2.1)

`process` starts owner-authored argv without a shell. The owner must enable
**Allow Azizam to run commands on this computer** first. The permission is off
by default. Engine records and the permission are owner-only writes with audit
events. New `process` and `command` recipes use argv arrays; legacy command
strings remain readable for existing records.

On Linux, Azizam starts a small supervisor in a new session. It launches the
owner argv without a shell and remains alive if the engine exits. The supervisor
owns four things, so that none of them depends on the gateway staying alive:
- **State.** Before it starts the engine, the supervisor writes
  `data/processes/<id>.json` itself, atomically, with its PID, kernel start
  tick, declared port and argv digest. The gateway only confirms it. A gateway
  crash right after the spawn therefore leaves a state file, not an untracked
  orphan.
- **Output.** The engine writes to a pipe that the supervisor holds. The gateway
  holds no pipe, so a gateway restart cannot give an adopted engine a broken
  stdout.
- **Log.** `<id>.log` is redacted, capped at `log_max_bytes` and rotated to
  `<id>.log.1` when full. A failing log write is dropped and the supervisor keeps
  draining, so an engine never blocks on its own output. If the log cannot be
  opened at all, the engine still starts. Redaction covers the declared `env`
  values, `opk_`/`oak_` keys, and values in the argv that follow a secret-named
  flag (`--api-key X`, `--token=X`, `NAME_PASSWORD=X`).
- **Group.** The supervisor is the group leader and outlives the engine.

The state directory is `0700` and its files are `0600`. A state file is only a
claim. Azizam believes it when the PID is above 1 and is not the gateway's own
group, the start tick is a string that matches `/proc`, the process leads its
own process group, and its command line is this supervisor with the recorded
argv. Otherwise no signal is sent. On restart, matching live state is adopted.
Normal shutdown sends SIGTERM to the group, then SIGKILL after the declared stop
deadline. A fixed port is required; Azizam refuses a port already accepting
connections before it spawns. Readiness comes only from a declared
`process_ready` observation, never from the process merely existing or a model
inventory response.

Readiness failure counts only at start time, against the process Azizam just
started. A live engine whose probe fails is marked unhealthy. It keeps its
process while requests are in flight, and one waiter that disconnects during a
cold start does not stop the start. After the requests drain, the restart policy
(budget and backoff) replaces it.

**Command permission (decision D13).** The permission is the operator setting
`allow_host_commands`, read from the live admin database. Turning it off blocks
new starts. It does not kill running owned processes: they keep serving until
they are stopped or the gateway restarts, and the API result says so. At boot,
with the permission off, stale recipes are terminated. A one-time migration turns
the permission on for installs that already declare command, process or systemd
lifecycles and writes an audit entry that names them. New installs stay off.

**systemd `KillMode`.** When the gateway itself runs as a systemd unit with the
default `KillMode=control-group`, stopping or restarting that unit kills every
process in its cgroup, including the engines this driver started in their own
sessions. Adoption after a restart then has nothing to adopt. To let owned
engines survive a gateway restart, set `KillMode=process` on the gateway unit
(or run the engines in their own unit or scope). `scripts/safe-restart` already
restarts the gateway deliberately; use it, not a bare `systemctl restart`.

The restart budget counts launch attempts in a sliding window and adds bounded
backoff. An intentional stop clears the budget. `idle_owner: gateway` permits
idle-stop after the declared interval and after active requests finish;
`idle_owner: engine` never lets the gateway stop for idleness. `idle_stop_s` must be a non-negative
number; a bad value fails preflight.

Windows needs a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, a durable
creation-time identity, and an adoption policy that can prove the process
belongs to the recorded job before terminating it. A PID alone is unsafe there.
The current implementation fails closed on Windows. macOS also fails closed
until a kernel-backed start-time identity and restart recovery are implemented.
The platform-gated tests document these limits rather than claiming support.

**Control file paths.** A control call may carry a path only under an absolute,
owner-allowlisted root. A protocol operation declares which of its arguments are
paths with `"arguments": {"model_path": {"type": "path"}}`. Every string under a
declared argument is checked whatever its form, and a relative one is refused.
Elsewhere a string is a path only when it is absolute, starts with `~`, `./`,
`../` or `file:`, or is `.` or `..`. Any other string is an identifier, so a
model id such as `org/model` passes unchanged, whether or not a file of that name
exists in the gateway's working directory. Paths are sent as their real path,
and checked again just before the request: a symlink or file swapped in after the
first check is refused. The engine still opens the file by name, so this narrows
the race but cannot close it. An argument that the engine reads as a file must be
declared, because the gateway cannot tell from the value alone.
