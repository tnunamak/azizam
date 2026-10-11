# Receipt retention

Receipts keep request metadata and Usage history, including measured costs.
They do not store prompts or request bodies. New and existing installations
keep receipts forever unless the owner chooses an expiry period.

In **Settings → Receipts → Keep receipts**, choose **Forever**, **1 year**
(365 days), **90 days**, or **30 days**. A finite choice shows how many receipts
Save will permanently delete. Choosing a value or loading its preview deletes
nothing. Save uses that preview's cutoff, including child receipts. Rows exactly
at the cutoff survive. If the count changes or the preview is over 15 minutes old,
review a fresh preview before saving. Settings use the same owner, CSRF and
revision checks as other operator settings and persist in `admin.db`.

The gateway applies saved expiry at startup and hourly. If receipt deletion
fails after the setting commits, the API reports pending cleanup; maintenance
retries the saved choice. Usage reports the actual setting. Keeping receipts
cannot restore previously deleted history.

Usage coverage starts at the later of the month start and a persisted history
floor. That floor advances when receipts are deleted and never moves back when
retention increases. An existing store starts with its earliest surviving
receipt, or the upgrade time if empty, because older pruning cannot be undone.
A fresh store starts at installation. Usage marks the month as partial when
the floor cuts into it; the floor is a conservative boundary, not a reconstruction
of pre-upgrade history.

## Reclaim unused database space

SQLite reuses free pages, so a large file does not mean all its bytes contain
receipts. New receipt databases use incremental auto-vacuum. Hourly maintenance
reclaims up to 256 free pages per pass, independently of expiry. No full database
rebuild runs in the gateway startup, request handlers, or hourly maintenance.

Existing files need an explicit rebuild to enable incremental auto-vacuum.
The owner can run this command during quiet hours, with free disk space for
SQLite's temporary rebuild files:

```sh
./venv/bin/python scripts/compact-receipts --db /path/to/receipts.db
```

It prints before/after logical database bytes, main-file bytes, WAL bytes and
combined physical bytes. The original `before_bytes` and `after_bytes` fields
are logical sizes. If a reader prevents WAL truncation, `checkpoint_pending`
is true and the command exits nonzero with a message to retry when readers are
idle. It never removes the WAL itself. SQLite locks protect concurrent writes;
requests that need this database may wait or fail with a busy error during the
rebuild. Back up the database before maintenance. The command compacts retained
rows and enables later incremental reclamation; it does not delete receipts.

Jobs, job events and video records also remain until explicit deletion. See
[Data retention](DATA_RETENTION.md) for the inventory of stored data and cleanup.
