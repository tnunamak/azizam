# App bindings: who may add a key or an address, and how to restore them

Owner visibility, step 2 (`docs/design/OWNER_VISIBILITY.md` §3.5, §8.1, §8.3).

A database made before this release has not had its cut-over applied. The gateway
starts it anyway, in **pre-cut-over** mode: the route checks below already hold, the
startup back-fill still records the creator that the audit log proves for each key,
and the start logs one line that the cut-over is pending. The back-fill runs only
until the cut-over is applied, so a deploy never waits on the owner. The cut-over
(preview, accept, apply) makes the guards hold for good.

A fresh database installs its guards on its first start. If that start dies before
the guards are in place, the next start installs them. It never runs in pre-cut-over
mode.

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
- Any other caller gets `403 not_your_app` on an admin route. On a `/me` route, an
  App that is not the person's gets `404 app_not_found`, the same answer as for
  minting a key. A key or address on such an App gets the same `404` as one that does
  not exist: `credential_not_found` for a key, `app_not_found` for an address. A disabled
  App gets `403 app_disabled`. A control-plane key adds no binding for any App,
  including the owner's own.

A database installed fresh by this release also refuses, in the database itself, a
key or address whose creator is not the App's person. It also refuses a change to an
App key's secret or expiry. The guards are listed in `gateway/app_binding_guard.py`.

## Keep or replace a binding made before the cut-over

A key or trusted address made before provenance was recorded may have no creator, or
may name someone else. The apply revokes such a key and removes such an address. Before
the apply, a person who still uses one either keeps it or replaces it.

**Keep a key or an address** keeps the binding and makes the person its recorded creator:

- `POST /me/api/credentials/{credential_id}/adopt` keeps an App key.
- `POST /me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt` keeps a trusted address.

Keep is refused with `409 keep_not_provable` unless nobody else could have made the
binding. Nobody else could have made it only when all of these are true:

- the signed-in person is the App's accountable person, and the sole person who holds
  the owner role now. A revoked owner grant does not count. Neither does a service
  account that holds the owner role, which is the owner's own control-plane key;
- the App has always answered to that person. No audit event names another accountable
  person for it, and the schema-7 migration did not change it;
- the record names no creator other than the person. It may name no creator at all;
- the audit log names no other creator for this binding.

So only the owner can keep. Members replace. Another person's key or App gets the same
`404` as a missing one: `404 credential_not_found` for a key, `404 app_not_found` for an
address. A key that already names the person is left as it is, with no confirmation.

**Administrators on record.** Before the cut-over, an administrator could issue a key or
an address on any App, and so could a past one. So when a person who held the admin,
superadmin or owner role at any time could have made the binding, keeping it is refused
with `409 keep_needs_confirmation` until the owner confirms:

```json
{"error": {"code": "keep_needs_confirmation", "message": "...",
           "administrators": {"people": ["Alex Example"], "unnamed_people": 1, "admin_keys": 23}}}
```

`administrators` counts them in three parts:

- `people` names each person by display name, sorted.
- `unnamed_people` counts the people with no display name. An email address is never shown
  as a name, so a person whose only name is an address is counted here.
- `admin_keys` counts the control-plane keys (service accounts) that held the admin or
  superadmin role, one per service account, and names none of them. A service account that held only the
  owner role is the owner's own control-plane key and is not counted.

The `message` says the same thing in one sentence, and leaves out an empty part: "Administrators at
the time could also have made this key: Alex Example, 1 person with no name, and 23 administrator
keys." The counts take the singular when they are one: "1 person with no name", "1 administrator key".
The owner confirms by keeping again with this body, where `true` is a JSON boolean (the string
`"true"` does not count):

```json
{"confirm_admins_could_have_made_it": true}
```

The audit event records the administrators confirmed, in `confirmed_administrators`, in the same three parts.

**Replace a key** issues a new key and revokes the old one:

- `POST /me/api/credentials/{credential_id}/replace`, from the browser session. It does not ask for a
  recent sign-in: it is a person's own key (`docs/ops/SIGN_IN_SESSIONS.md`).
- The new key is on the same App, has the old key's name, and has no expiry, as a key
  made from the Apps page has. The App's access plan applies to it unchanged.
- The insert, the revocation and the audit event share one transaction. A failure
  leaves the old key live and no new key behind.
- The response is `201`: `credential` holds the new key, with its secret in `api_key`
  (shown once), and `replaced_credential_id` holds the old key's id.
- It answers a key of another person's App as a missing key, with `404 credential_not_found`, and an
  address of another person's App with `404 app_not_found`. A disabled App is refused with `403 app_disabled`.
  A key that is missing, revoked, not an App key, or on a session App is `404 credential_not_found`.

**Add an address again.** An address that cannot be kept is refused with
`409 keep_not_provable`. The person adds it again with
`POST /me/api/apps/{app_id}/trusted-addresses/{entry_id}/readd`. The gateway removes the old
entry and adds the same address, with its note, as the person's own entry. Both happen in one
transaction with the audit event, so a failed step leaves the old entry in place. The answer
is `201` with the new `trusted_address` and the `replaced_entry_id`. It asks for a recent
sign-in, as adding an address does. Another person's App, and a session App, get
`404 app_not_found`. An entry that the App does not hold gets `404 trusted_address_not_found`.

Each keep writes an audit event (`credential.adopt`, `app.trusted_address.adopt`). Each
replacement writes `credential.replace`, naming both keys. Each removal writes
`app.trusted_address.remove`. Each add-again writes `app.trusted_address.readd`, naming the
entry it replaced.

The gateway names the one remedy each binding the apply would remove needs. The
snapshot at `/me/api/session` carries it, and so does the owner's trusted-address list:

- A key the apply would remove has `cutover`: `keep` or `replace`. A trusted address has
  `cutover`: `keep` or `readd`. A binding whose recorded creator is another person also
  has `made_by_name`. A binding the apply keeps has no `cutover`.
- The gateway decides `keep` with the rule the keep routes apply, so the page and the
  routes never disagree. Anyone else gets `replace` (a key) or `readd` (an address).
- `addresses_to_check` on each App, for the owner only, counts its addresses with a
  `cutover`.

The Apps page shows only that remedy for each binding, and the owner's count line covers
keys and addresses alike:

- A key with `keep` shows the notice and a **Keep** button in the **Rename keys** list of
  its App.
- A key with `replace` shows a notice and a **Replace key** button. The new key has the
  old key's name and is on the same App, and it is shown once. The old key's row goes
  away. A personal App's row offers the same replacement when one key needs it.
- A trusted address with `keep` shows the keep notice and button in the **Trusted
  addresses** list of its App. With `readd` it shows **Add again**, which adds the address
  again in one step. Only the owner can open that list.
- Above the list of Apps, one line counts the bindings with a `cutover`, and links to
  the first App that has one.

A key or address that names another person is removed by the apply too. The Apps page
flags it with its remedy, so it no longer waits for a check in the preview.

When administrators on record could also have made a binding, Keep opens a dialog that names the people and counts the
rest first, with the title "Keep this key?" or "Keep this address?". Confirming sends the confirmation body below.
Cancelling sends nothing. A keep that the gateway asks to confirm, on a page that was not up to date, opens the same
dialog.

A keep can also be sent by hand. From the browser console on the signed-in Azizam page, the
owner keeps a key like this. If the answer is `409 keep_needs_confirmation`, read its
`administrators`, then send the same request with the confirmation body:

```js
const csrf = (await (await fetch("/auth/api/options")).json()).signed_in_oidc.csrf_token;
await fetch("/me/api/credentials/<key id>/adopt", {
  method: "POST",
  headers: { "x-azizam-csrf": csrf, "x-azizam-user-action": "1", "content-type": "application/json" },
  body: JSON.stringify({ confirm_admins_could_have_made_it: true }),  // only after reading the administrators
});
```

A keep does not ask for a recent sign-in: it is a person's own key or address
(`docs/ops/SIGN_IN_SESSIONS.md`).

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
2. **Keep or replace** each binding you still use (above).
3. **Stop the gateway.** Check first with `scripts/safe-restart --dry-run`, then stop the
   service. Apply refuses while a gateway runs. A gateway takes `admin.db.running`, beside
   `admin.db`, shared when its app starts, and keeps it until the app shuts down. The app
   takes it whichever way it is launched: `python proxy.py`, `uvicorn proxy:app`, or
   `scripts/ui/serve.py`. Apply takes that file exclusively. Apply also checks the
   gateway's private directory lock (`private-tmp/.owner.lock`, or `AZIZAM_PRIVATE_TMPDIR`)
   where it can see it.
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
   - revokes every App key without a proven creator, including a key whose App row is gone;
   - removes every trusted address without a proven creator, including an address whose App row is gone;
   - checks that no unproven key or address is still live, and refuses if one is;
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
instead of un-revoking it: the person makes a new key with
`POST /me/api/apps/{app_id}/keys`. A revoked App key never becomes live again (the guard
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
- each App that the apply would refuse to stop: an App used in the last 30 days that
  would keep no binding, or an App whose only binding is an address whose use
  history is not available on `admin.db`.

A key or address whose App row is gone is listed as an App with no App row. The
cut-over revokes or removes it, and it blocks nothing: such a binding cannot
authenticate, because the runtime joins the App.

It also says what it does not evaluate. The classification of creations that have
no recorded person is not done here: those records are in media sidecars.

The `--json` output carries a `digest`. Cite that digest when you accept a preview.

The apply, its acceptance and the guards are described in "Apply the cut-over" above.
Creations with no recorded person or App (design §10.2 Q2) are not handled in this release:
they live in media sidecars, and classifying them, or creating the "No person recorded" App,
waits for the media-sidecar part of the design.
