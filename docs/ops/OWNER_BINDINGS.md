# App bindings: who may add a key or an address, and how to restore them

Owner visibility, step 2 (`docs/design/OWNER_VISIBILITY.md` §3.5, §8.1, §8.3).

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

**Not in this release:** the apply that makes the revocations and removals, its
acceptance record, and the start check that refuses to run until acceptance. Until
they exist, an existing database keeps its current bindings and is not guarded.
Only a fresh database is guarded.
