# Safe restart

**Restart the gateway only with `scripts/safe-restart`.** A bare
`systemctl --user restart openai-proxy` cuts every streaming (SSE) reply and
synchronous media call that is running.

```
scripts/safe-restart --dry-run   # what is in flight? changes nothing
scripts/safe-restart             # drain, wait (max 15 min), restart, verify
scripts/safe-restart --llama     # drain chat, wait, restart llama-bee
scripts/safe-restart --force     # skip the wait; KILLS in-flight work
```

Options: `--timeout SEC` (default 900), `--ignore-async-jobs`,
`--health-timeout SEC`, `--unit`, `--llama-unit`, `--url`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Restarted and healthy (`--dry-run`: idle) |
| 1 | Restarted, but health or the drain flag could not be confirmed |
| 2 | Usage error |
| 3 | Work still in flight at the deadline, NOT restarted (`--dry-run`: busy) |
| 4 | Drain endpoint unreachable or refused, NOT restarted (use `--force`) |
| 5 | `systemctl restart` failed |
| 6 | Another `safe-restart` is running |
| 130 | Interrupted; drain was switched off |

## Admin key

The script needs an admin bearer key in `AI_GATEWAY_ADMIN_KEY`,
`VIVID_ADMIN_API_KEY` or `OPENAI_PROXY_ADMIN_KEY`, or in the file named by
`AI_GATEWAY_ADMIN_KEY_FILE` (default `~/.config/openai-proxy/admin-key`, mode
0600). Create one with `python3 proxy.py generate-admin-key safe-restart admin`.
The key is never printed.

## How it works

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
  `llama-bee`, waits for its `/health`, then switches the gateway drain off.
  llama-server cannot drain by itself, so the gateway is the place to stop new
  chat.

## Plain `systemctl restart`

If someone skips the script, `docs/ops/openai-proxy-reliability.conf` still
gives a bounded graceful stop: `KillMode=mixed` signals `start.sh`, which
forwards SIGTERM to the proxy and stops LiteLLM only after the proxy exits.
uvicorn waits up to `AI_GATEWAY_GRACEFUL_SHUTDOWN_S` (default 120 s);
`TimeoutStopSec=150s` then SIGKILLs the rest. New requests already fail during
that stop, so use `safe-restart`.

## First deploy (the running process has no drain code)

1. Confirm the gateway is idle by hand (the status page, and
   `curl -s localhost:5051/slots | grep -c '"is_processing":true'`).
2. Create the admin key, store it in the key file, then
   `scripts/safe-restart --force`. The old process cannot drain.
3. After that restart, install the drop-in and reload (no restart needed):
   `install -m 0644 docs/ops/openai-proxy-reliability.conf ~/.config/systemd/user/openai-proxy.service.d/reliability.conf && systemctl --user daemon-reload`.
4. Check: `scripts/safe-restart --dry-run` should print `dry run: idle`.
