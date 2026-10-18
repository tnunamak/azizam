# Safe restart

**Restart the gateway only with `scripts/safe-restart`.** A bare
`systemctl --user restart azizam` cuts every streaming (SSE) reply and
synchronous media call that is running.

```
scripts/safe-restart --dry-run   # what is in flight? changes nothing
scripts/safe-restart             # build stale UI, wait for idle + quiet, drain, restart, verify
scripts/safe-restart --quiet-seconds 0   # skip the quiet-window wait
scripts/safe-restart --llama     # drain chat, wait, restart local-llm
scripts/safe-restart --force     # skip the wait; KILLS in-flight work
scripts/safe-restart --skip-web-build  # skip checking and rebuilding web/out
```

Options: `--timeout SEC` (default 3600), `--quiet-seconds SEC` (default 300, so a deploy waits for 5 idle minutes and never lands mid-session; 20 is enough only when nobody is using an agent; 0 = off), `--ignore-async-jobs`,
`--health-timeout SEC`, `--unit`, `--llama-unit`, `--url`, `--public-url URL`, `--public-timeout SEC`.

The public check is off by default. After a gateway restart, `--public-url https://your.host` (or `AZIZAM_PUBLIC_URL`) also waits for `/health` at that address, which confirms that your reverse proxy marks the gateway up again. Without it, the script prints `public health check skipped: set --public-url or AZIZAM_PUBLIC_URL to enable it`. An empty value also skips the check.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Restarted and healthy (`--dry-run`: idle) |
| 1 | Restarted, but local health, public health or the drain flag could not be confirmed; with a new web export, the new process stays running and the previous export is kept |
| 2 | Usage error |
| 3 | Work still in flight at the deadline, NOT restarted (`--dry-run`: busy) |
| 4 | Drain endpoint unreachable or refused, NOT restarted (use `--force`) |
| 5 | `systemctl stop`, `start` or `restart` failed |
| 6 | Another `safe-restart` is running |
| 7 | Web build or publication failed; read the recovery message |
| 130 | Interrupted; drain is switched off only if the interrupt came before the stop-publish-start sequence |

## Admin key

The script needs an admin bearer key in `AZIZAM_ADMIN_KEY`,
or in the file named by
`AZIZAM_ADMIN_KEY_FILE` (default `~/.config/azizam/admin-key`, mode
0600). Create one with `python3 proxy.py generate-admin-key safe-restart admin`.
The key is never printed.

## How it works

- Before a gateway restart, the script checks the static web export served at
  `/`. If `web/out/index.html` is missing or a web input is newer than the
  last completed export, it copies the web sources into a temporary sibling
  directory and runs `npm run build` there, using the installed dependencies.
  The script builds the export before the drain but publishes it only after the
  drain succeeds. A drained gateway still serves reads, so with a new export
  the script does not use `systemctl restart`. It stops the unit, then
  publishes, then starts the unit. The old process never serves the new
  export. If the stop fails, nothing is published (exit 5). Publication
  atomically exchanges the staged `out` directory with `web/out` using Linux
  `renameat2`. The live export stays intact during compilation and the drain,
  including if the build fails, times out, or is interrupted. If the start
  fails or is interrupted, the script stops the unit again. It then swaps the
  previous export back and leaves the unit stopped. If it cannot prove the
  stop, it keeps both exports. In both cases drain stays on, and the script
  prints the exact recovery command. If the start succeeds but a later check
  fails (local health, public health or the drain flag; exit 1), the new process
  keeps running with the new export. The previous export is kept for a
  manual rollback.
- The drain wait takes a new drain snapshot on every poll. If the drain
  endpoint cannot be read, or the drain flag has gone off, the script
  does not count the gateway as idle. It does not restart, and at the
  deadline it exits 3. The page registry `gateway/ui_pages.json`
  (page name to public, member or admin) is a web build input, so a change to it
  triggers a rebuild.
  It ignores `node_modules`, `.next`, `out`, and generated TypeScript files.
  The completion marker records the source paths, so removing an input also
  triggers a rebuild. An older export without this marker is rebuilt once
  to establish the baseline.
  The marker uses the build start time and input paths, so edits made during
  the build remain detectable on the next restart.
  Build time is before the quiet wait and drain, so the gateway keeps serving
  requests. A failed build stops the restart, including with `--force`.
  `--skip-web-build` skips this check; `--dry-run` and `--llama` also skip it.
  The build needs installed web dependencies and Node/npm on `PATH`.
- **Two phases, in this order.** An agent turn is a chain of chat calls with
  tool steps between them. "Nothing in flight" is true in every gap, so a
  drain that starts in a gap would answer the agent's next call with 503.
  1. *Quiet wait (gateway still accepting).* The script polls until no chat
     request is in flight and none started or finished for `--quiet-seconds`
     (default 300). Media work in flight does not hold this phase. New requests are accepted and served normally, so a
     running agent never sees a refusal. Each new chat call restarts the
     window. The gateway computes it (`seconds_since_chat_activity` in the
     drain snapshot), so the script and the gateway share one clock.
  2. *Drain, then restart at once.* The script sets the drain flag, waits for
     all in-flight work (media included; new media is now refused, so it cannot
     starve) for at least 30 s or the rest of the deadline, then restarts.
  The deadline (`--timeout`) covers both phases. If it passes in phase 1,
  nothing was drained, so nothing needs undoing; exit code 3. A tool step that
  runs longer than the quiet window can still be cut between calls; raise
  `--quiet-seconds` for that case. Only chat paths count as activity (see
  `CHAT_PATHS` in `gateway/drain.py`); image/audio/video are covered by the
  in-flight and async-job counts.
- `--force` skips both waits. A gateway that predates the activity fields
  (the first deploy of this feature) gets a warning and the quiet check is
  skipped.
- `GET /admin/api/drain` reports `last_chat_activity_at` (wall-clock epoch
  seconds; now, while a chat runs; null if no chat since the process started)
  and `seconds_since_chat_activity` (0 while a chat runs; null likewise).

- `gateway/drain.py` counts requests from start to the last response byte
  (pure ASGI, so an SSE stream stays counted while tokens flow). Async media
  jobs (image, audio, video) are in-process tasks. They are durable and resume
  after a restart, but they restart from the beginning, so they count as in
  flight.
- `GET/POST /admin/api/drain` shows and sets the flag. It needs an admin bearer
  key and a loopback socket peer (`X-Forwarded-For` is ignored). The flag lives
  in memory, so a restart clears it, and it expires after `ttl_s` (default
  30 min, max 2 h) if the script dies.
- While draining, new POST/PUT/PATCH requests get `503`, `Retry-After`, and
  `{"error": {"code": "gateway_draining", "type": "server_error", ...}}`.
  Reads, job polls, cancels (`.../cancel`), `/health` and `/admin/*` still work.
- `--llama` also waits for llama-server `/slots` to be idle, restarts
  `local-llm`, waits for its `/health`, then switches the gateway drain off.
  llama-server cannot drain by itself, so the gateway is the place to stop new
  chat.

## Plain `systemctl restart`

If someone skips the script, `docs/ops/azizam-reliability.conf` still
gives a bounded graceful stop: `KillMode=mixed` signals `start.sh`, which
forwards SIGTERM to the proxy and stops LiteLLM only after the proxy exits.
uvicorn waits up to `AZIZAM_GRACEFUL_SHUTDOWN_S` (default 120 s);
`TimeoutStopSec=150s` then SIGKILLs the rest. New requests already fail during
that stop, so use `safe-restart`.

## First deploy (the running process has no drain code)

1. Confirm the gateway is idle by hand (the status page, and
   `curl -s localhost:5051/slots | grep -c '"is_processing":true'`).
2. Create the admin key, store it in the key file, then
   `scripts/safe-restart --force`. The old process cannot drain.
3. After that restart, install the drop-in and reload (no restart needed):
   `install -d ~/.config/systemd/user/azizam.service.d && install -m 0644 docs/ops/azizam-reliability.conf ~/.config/systemd/user/azizam.service.d/reliability.conf && systemctl --user daemon-reload`.
4. Check: `scripts/safe-restart --dry-run` should print `dry run: idle`.
