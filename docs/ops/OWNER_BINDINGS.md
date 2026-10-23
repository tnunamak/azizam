# App bindings: who may add a key or an address, and how to restore them

Owner visibility, step 2 (`docs/design/OWNER_VISIBILITY.md` §3.5, §8.1, §8.3).

A database made before this release has not had its cut-over applied. The gateway
starts it anyway, in **pre-cut-over** mode: the route checks below already hold, the
startup back-fill still records the creator that the audit log proves for each key,
and the start logs one line that the cut-over is pending. The cut-over (preview,
accept, apply) makes the guards hold for good. A fresh database starts after the
cut-over.

## Who adds a key or a trusted address

An App key or a trusted address is added by the person the App answers for.
The owner role does not change this. The owner still runs plans and turns Apps
on and off.

- A person adds a key to their own App from their browser session, or with
  `POST /me/api/apps/{app_id}/keys`.
- A person adds a trusted address to their own App from their browser session,
  with `POST /me/api/apps/{app_id}/trusted-addresses`.
- The admin routes `POST /admin/api/apps/{app_id}/keys` and
  `POST /admin/api/apps/{app_id}/trusted-addresses` accept the same person.
- Any other caller gets `403 not_your_app`. A control-plane key adds no
  binding for any App, including the owner's own.

A database installed fresh by this release also refuses, in the database
itself, a key or address whose creator is not the App's person. The guards
are listed in `gateway/app_binding_guard.py`.

## Keep a binding made before the cut-over

A key or trusted address made before provenance was recorded may have no creator,
or name someone else. The apply revokes such a key and removes such an address. To
keep one that you still use, the person the App answers for makes it theirs:

- `POST /me/api/credentials/{credential_id}/adopt` keeps an App key.
- `POST /me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt` keeps a trusted address.

Both need a browser sign-in and a recent sign-in (the same step-up as adding a key).
Only the App's person can keep one of its bindings: the owner gets `403 not_your_app`
for another person's App. A key that already names the person is left as it is.
Each keep writes an audit event (`credential.adopt`, `app.trusted_address.adopt`).

Keys and addresses are kept one at a time. From the browser console on the signed-in
Azizam page:

```js
const csrf = (await (await fetch("/auth/api/options")).json()).signed_in_oidc.csrf_token;
await fetch("/me/api/credentials/<key id>/adopt", {
  method: "POST", headers: { "x-azizam-csrf": csrf, "x-azizam-user-action": "1" },
});
```

A `401` with `reauth_required` means confirm your password (or sign in again), then
repeat the call. The Apps page does not show a Keep control yet.

## The first App

Do not use a control-plane key for the first App. `POST /setup/first-app` answers
`403` for such a key and writes nothing.

Use one of these:

1. **Owner sign-in.** Sign in as the owner (local owner setup at `/setup/local-owner`,
   or the identity provider). Then open the setup console at `/setup/console`, and create the first App key.
   The App is that person's personal App.
2. **Owner CLI bootstrap.** This is documented here as the intended path for a host
   operator who has no browser. **It is not built.** No command in this release
   issues a binding without a signed-in person. Until it is built, use option 1.

## Apply the cut-over

Do this once per database, in this order.

1. **Start the gateway once** with this release, so its migrations run.
2. **Keep** each binding you still use (above).
3. **Stop the gateway.** Check first with `scripts/safe-restart --dry-run`, then stop the
   service. Apply refuses while the gateway runs: a running gateway holds its
   private directory lock (`private-tmp/.owner.lock` beside `admin.db`, or
   `AZIZAM_PRIVATE_TMPDIR`), and apply checks that lock.
4. **Preview a copy** of `admin.db`. Read the `BLOCKS APPLY` lines; each means an App
   would stop while in use. Keep a binding on it, or disable the App, then preview again.

   ```sh
   python -m gateway.app_binding_preview --admin-db /path/to/copy/admin.db
   ```

5. **Accept the digest** the preview printed. The digest covers what the cut-over
   decides. It ignores the clock and the times of last use.

   ```sh
   python -m gateway.app_binding_cutover accept --admin-db /path/to/admin.db --digest sha256:... --by "Your name"
   ```

   Accept refuses a digest that is not the digest of the database now. It also
   refuses a preview that blocks apply. Preview again after any change.
6. **Apply.** It backs the database up, then changes it in one transaction.

   ```sh
   python -m gateway.app_binding_cutover apply --admin-db /path/to/admin.db
   ```

   Apply refuses unless an accepted digest matches the database now and no App blocks.
   It also refuses while another process has the database open (apply holds the
   database's `.lock` file while it runs, so a gateway that starts meanwhile waits).
   Apply does this, in order:
   - copies the database to `admin.db.pre-ownership-cutover-<epoch>` (mode 0600);
   - revokes every App key without a proven creator;
   - removes every trusted address without a proven creator;
   - installs the guards and records the ownership version;
   - prints each key and address it changed, the backup path and the guard count.

   A failure at any step rolls the whole change back. The backup stays where it is.
7. **Start the gateway** with `scripts/safe-restart`. A restart after apply never runs the
   creator back-fill again, and it puts back any guard that was dropped.

To undo an apply, stop the gateway, move `admin.db` aside, copy the backup back as
`admin.db`, and start the gateway. It starts in pre-cut-over mode, and the cut-over is
pending again. Keys revoked by the apply are live again in the backup.

## Restore

A restore copies the database files back. A copy brings back what it held.

- App keys revoked after the copy are live again.
- Trusted addresses removed after the copy are trusted again.

Before you restore:

1. Record the live keys and addresses that are revoked or removed. You need the list
   after the restore.

After you restore, and before you start the gateway:

2. Revoke again each key from the list in step 1:

   ```sql
   UPDATE credentials SET revoked_at = strftime('%s', 'now')
   WHERE subject_kind = 'app' AND id IN ('<key id>', ...) AND revoked_at IS NULL;
   ```

3. Remove again each address from the list in step 1:

   ```sql
   DELETE FROM app_trusted_addresses WHERE id IN ('<address id>', ...);
   ```

4. Start the gateway with `scripts/safe-restart`.

Members do not regain access to a key or address that stays revoked. Reissue a key
instead of un-revoking it. A revoked App key never becomes live again (the guard
refuses it).

## Preview the cut-over

The preview is read-only. It opens the database read-only, and it writes nothing.
Run it on a copy of `admin.db`:

```sh
python -m gateway.app_binding_preview --admin-db /path/to/copy/admin.db
python -m gateway.app_binding_preview --admin-db /path/to/copy/admin.db --json
```

The preview lists, for each App:

- its accountable person and access plan;
- each live key and trusted address, with its recorded creator and whether that
  creator is proven;
- each revocation and removal the cut-over would make;
- each App that the apply would refuse to stop: an App used in the last 30 days
  that would keep no binding, or an App whose only binding is an address whose use
  history is not available on `admin.db`.

It also says what it does not evaluate. The classification of creations that have
no recorded person is not done here: those records are in media sidecars.

The `--json` output carries a `digest`. Cite that digest when you accept a preview.

The apply, its acceptance and the guards are described in "Apply the cut-over" above.
Creations with no recorded person or App (design §10.2 Q2) are not handled in this release:
they live in media sidecars, and classifying them, or creating the "No person recorded" App,
waits for the media-sidecar part of the design.
