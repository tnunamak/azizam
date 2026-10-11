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

It prints only before/after byte counts. SQLite locks protect concurrent writes;
requests that need this database may wait or fail with a busy error during the
rebuild. Back up the database before maintenance. The command compacts retained
rows and enables later incremental reclamation; it does not delete receipts.

Media job coordination records retain their separate 14-day window. Their asset
ownership is stored independently; Library assets and receipt cost history do
not depend on keeping those coordination records forever.
