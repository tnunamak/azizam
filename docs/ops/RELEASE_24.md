# Release 24

## What changed

- Product docs: verified fixes to the API, concepts, start and use pages, and
  the docs-drift corrections to README, CONFIG and the retention pages. The
  product docs now live at https://azizam.dev/docs/.
- Depersonalization: the status renderer, `proxy.py`, `start.sh` and a script
  default no longer name the owner's hosts. The renderer shows the request host,
  then an optional `public_url`, then a neutral label.
- Engines as data: `gateway/engine_quarantine.py` and Tabby support are gone.
  Five behaviours that were engine-named code tables are now fields on the
  service record (see ENGINE_KNOWLEDGE_BOUNDARY.md).

## Deploy steps for the engine migration (required)

The gateway no longer reads the old tables. Without the migration, the local
chat service loses absolute-path activation, loaded-model identity, vision
through the aux probe and the context-window probe.

1. Stop the gateway, or pass `--allow-running` and restart afterwards.
2. Back up `data/admin.db` (for example `sqlite3 data/admin.db ".backup 'admin.db.pre-r24'"`).
3. Run `./venv/bin/python scripts/migrations/engine_service_fields.py --dry-run`,
   then `--apply`. The script makes its own backup too and is idempotent.
4. Restart with `scripts/safe-restart`.

The migration deletes the disabled `tabby` service row and writes five fields
on the `kobold` service. A rehearsal on a copy of the live registry went from
21 services to 20; a second run reported 0 changes.
