# Headless owner access

Use an owner automation key with `/admin/api` and `/openapi.json`. App keys
cannot administer the gateway. Browser session mutations still require the
existing CSRF token or action header. This change describes existing access;
it does not change permissions or make session routes accept bearer keys.

## Create an automation key

On the gateway host, use the existing helper with the deployment's database
configuration. It prints a one-time secret; save its last line in a private file
and keep its full output out of shared logs. Its default role is `owner`, which is required for the owner-only operations in the table below.

```sh
set +x
umask 077
./venv/bin/python proxy.py generate-admin-key headless-automation owner > owner-key-output.txt
tail -n 1 owner-key-output.txt > owner-key.txt
rm owner-key-output.txt
chmod 600 owner-key.txt
./venv/bin/python - <<'PY'
from pathlib import Path
key = Path("owner-key.txt").read_text().strip()
header = Path("owner-header.txt")
header.touch(mode=0o600)
header.chmod(0o600)
header.write_text("Authorization: Bearer " + key + "\n")
PY
AZIZAM_URL=https://gateway.example
export AZIZAM_URL
```

An `admin` role key cannot use owner-only operations. Neither role is a
read-only scope. The key's role is enforced on each request. The helper does
not configure a browser session or start a server.

An administrator, not only the owner, may mint an `admin` key with
`POST /admin/api/admin-keys`; only an owner may mint an `owner` key. Every
dashboard key gets a service account of its own, and the name must be new: a
name that is already in use is refused with `409 admin_key_name_taken`, so no
one can take over another key by naming theirs the same. A key made on the host
with the helper above belongs to the operator and has no person behind it; the
helper refuses a name that the dashboard already used. Every other key records
who minted it (a person, or another key), and it works only while that minter
still holds a role that permits it. When an administrator loses the role, their
keys stop working at once. When an owner becomes an administrator, their `owner`
keys work at administrator reach. If the minter cannot be found, the key has no
authority.

A role that the identity provider gave (an admin group or role claim) counts
only while a sign-in has confirmed it within the session length (8 hours by
default). An administrator who is removed from the group and never signs in
again loses the role, and with it every key they minted, when that time ends.
Roles the owner gave by hand do not expire. A sign-in that is refused ends the
provider's roles and all sessions of that person at once; it also ends their
MCP connections and pauses their personal App keys (`person_signin_refused`)
until a later sign-in succeeds. Nothing is deleted, so the keys work again then.

Access plans decide what everyone may do, so only the owner may create, change
or delete one (`403 owner_required` for an administrator). Administrators can
still read plans.

Only the owner may create an App that someone else answers for, change that
App's access plan or accountable person, or mint a key on it. An administrator
may do these only for an App that answers to themselves, so its spend stays
inside the administrator's own access plan. An administrator without an access
plan gets a key that authorizes nothing. A key has no person of its own, so an
`admin` key cannot create Apps or App keys; the refusal is
`403 owner_required`.

Use a private working directory. Keep tracing off (`set +x`) while handling
credentials. The key and header files must have mode 600. The examples read
the secret from files; they do not put it in process arguments or environment
variables. Keep these files out of logs and version control.

## Call the API with curl

```sh
curl --fail-with-body -sS "$AZIZAM_URL/admin/api/apps" \
  -H @owner-header.txt

# Choose an existing app ID from the list. This mints an App key, not an owner key.
APP_ID=app_id_from_list
curl --fail-with-body -sS -X POST "$AZIZAM_URL/admin/api/apps/$APP_ID/keys" \
  -H @owner-header.txt \
  -H 'Content-Type: application/json' --data '{"label":"automation-client"}' \
  > app-key-response.json

curl --fail-with-body -sS "$AZIZAM_URL/admin/api/activity/requests?limit=10" \
  -H @owner-header.txt
curl --fail-with-body -sS "$AZIZAM_URL/admin/api/status" \
  -H @owner-header.txt
```

For denied `/status*` calls, request telemetry no longer touches credential usage
or writes metrics; status pages now share the existing admin telemetry exclusion.

Key creation writes the one-time secret in `credential.api_key` to the private
`app-key-response.json` file; keep that file out of shared logs and version control.
Request activity returns safe receipt records with `next_cursor`. It differs from
the administrative change audit at `/admin/api/audit`. Continue receipt pages
with `before=<next_cursor>` until `next_cursor` is null. Registry writes need the
current `ETag` in `If-Match`; settings that require a revision do too. Do not
silently retry a rejected mutation with a newer revision.

## Use Restish 2.3.0

Install Restish in a private tool directory to build commands from OpenAPI
without an Azizam CLI. Its v1 `api configure` command does not work in v2.
Use a separate config file to avoid ambient profiles. Register the trusted
schema explicitly and store the token in a mode-600 Restish config file:

```sh
set +x
RSH_CONFIG="$PWD/restish-owner.json"
export RSH_CONFIG
umask 077
test -e "$RSH_CONFIG" || printf '{}\n' > "$RSH_CONFIG"
chmod 600 "$RSH_CONFIG"
# Fetch the schema using curl's private header file.
curl --fail-with-body -sS "$AZIZAM_URL/openapi.json" \
  -H @owner-header.txt > owner-openapi.json
# This placeholder is public text. Python replaces it from the private key file.
restish api connect azizam "$AZIZAM_URL" \
  'prompt.credentials.OwnerBearer.token: configure-from-private-file' \
  --spec "$PWD/owner-openapi.json" --replace --yes
./venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
config = Path(os.environ["RSH_CONFIG"])
data = json.loads(config.read_text())
profile = data["apis"]["azizam"]["profiles"]["default"]
key = Path("owner-key.txt").read_text().strip()
profile["credentials"]["OwnerBearer"]["auth"]["params"]["token"] = key
profile["auth"]["params"]["token"] = key
config.write_text(json.dumps(data, indent=2) + "\n")
PY
restish azizam owner-get-admin-api-apps -o json
restish azizam owner-post-admin-api-apps-app-id-keys "$APP_ID" \
  'label: automation-client' -o json \
  > app-key-response.json
restish azizam owner-get-admin-api-activity-requests --limit 10 -o json
restish azizam owner-get-admin-api-status -o json
```

Use `restish azizam <command> --help` for inferred path/query/body arguments.
Legacy handlers that read JSON manually have an open object body schema; their
field validation remains in the handler. Responses without typed models keep
an unspecified schema. The owner metadata does not invent stricter contracts.
Session-only routes stay absent from OpenAPI. HTML owner routes remain outside
the generated command surface. `/admin/api/drain` is loopback-only; its POST
also requires a bearer key even for an owner browser session.

Reconnect after an API upgrade to refresh the cached spec. Repeat the schema fetch, connect, and private-token
update above when reconnecting; `--replace` replaces the current profile.
Operation IDs depend on the HTTP method and path, so renaming a Python handler does not rename a CLI
command. Only explicitly inventoried owner operations receive owner metadata.

### Check results before using them

Capture stdout, stderr, and the exit code separately. Do not use
`--rsh-ignore-status-code`. Do not treat exit 0 as proof that all records arrived
or that a JSON body describes success.

```sh
restish azizam owner-get-admin-api-activity-requests --limit 10 \
  -o json \
  > receipts.json 2> receipts.stderr
rc=$?
test "$rc" -eq 0 || { cat receipts.stderr >&2; exit "$rc"; }
jq -e 'type == "object" and (has("error") | not) and (.records | type == "array")' receipts.json
jq -r '.next_cursor // empty' receipts.json
```

Restish's page cap can return partial results and exit 0. Read stderr for cap
warnings, inspect pagination fields, and follow `next_cursor` explicitly for
Azizam's wrapped receipt collection. Do not assume Restish merges wrapped
objects. `--rsh-max-pages 0` removes the client's cap, but cannot remove a server
cap or guarantee wrapped pagination. Error JSON can accompany exit 0 when a
server returns an error envelope with HTTP 200, or when status ignoring is
configured. Check both the exit code and `error`/expected body shape. Avoid
verbose output with credentials; it can include request headers.

## Registered route inventory

Captured by walking `proxy.app.routes` when the web app moved to `/`. Counts
are per registered method/path, including trailing-slash aliases: **106
owner-bearer** (104 JSON/YAML owner API operations, 2 browser/HTML routes),
**33 session-only**, **2 session-view**, **12 browser/public**; **153 total**.

FastAPI dependency lists are empty on these routes. Auth is an imperative
handler call or injected callback, shown below. `owner-bearer` rejects app keys
and member sessions; browser entry points can deny members by redirecting to `/`. `session-only` requires a cookie and rejects a standalone
owner/app bearer key. `session-view` permits an unauthenticated snapshot.
`browser/public` covers sign-in, first-run, redirect and health contracts;
headers alone do not turn them into owner API operations. Routes outside these
namespaces are explicitly listed in `tests/fixtures/headless_non_control_routes.json`;
that classification does not assert their authorization contract. There are no
mounts. The one route that is not a `Route` is `UiRoute` (`gateway/ui_routes.py`),
just before the catch-all: it serves the web export's registry pages, payloads and
assets, and guards admin pages itself (`tests/test_ui_routes.py`).

The tests walk every route type and recurse through mounts, preserving their
path prefixes. An unclassified route or opaque mount fails the inventory test.
App/member denials use HTTP and check SQLite contents, config and drain state.
Member mutations include valid CSRF/action headers and must return a role denial.
Owner bearer calls reach handler-level validation or a read response after all
authorization checks. Reload and global-cap deletion stop at their first operational
effect. These probes check bearer reachability without performing every mutation.
Selected successful workflows are exercised separately.

| Method and path | Auth class | App key | Handler / auth call | Schema |
| --- | --- | --- | --- | --- |
| `GET /admin/api/activity/requests` | owner-bearer | denied | `gateway.activity_api.requests`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/admin-keys` | owner-bearer | denied | `gateway.admin_api.create_admin_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required to mint owner key; a key works only while the person who minted it still holds a role that permits it | owner |
| `GET /admin/api/apps` | owner-bearer | denied | `gateway.admin_api.list_apps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/apps` | owner-bearer | denied | `gateway.admin_api.create_app`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/apps/{app_id}` | owner-bearer | denied | `gateway.admin_api.disable_app`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PATCH /admin/api/apps/{app_id}` | owner-bearer | denied | `gateway.admin_api.update_app`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/apps/{app_id}/enable` | owner-bearer | denied | `gateway.admin_api.enable_app`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/apps/{app_id}/keys` | owner-bearer | denied | `gateway.admin_api.create_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/apps/{app_id}/trusted-addresses` | owner-bearer | denied | `gateway.admin_api.list_trusted_addresses`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/apps/{app_id}/trusted-addresses` | owner-bearer | denied | `gateway.admin_api.add_trusted_address`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required to trust an address | owner |
| `DELETE /admin/api/apps/{app_id}/trusted-addresses/{entry_id}` | owner-bearer | denied | `gateway.admin_api.delete_trusted_address`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/audit` | owner-bearer | denied | `gateway.admin_api.audit`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/audit-events` | owner-bearer | denied | `gateway.admin_api.audit_events`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/audit/{event_id}` | owner-bearer | denied | `gateway.admin_api.audit_event`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/categories/active` | owner-bearer | denied | `gateway.category_api.categories_active`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/categories/candidates` | owner-bearer | denied | `gateway.category_api.categories_create`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/categories/candidates/{revision}/activate` | owner-bearer | denied | `gateway.category_api.categories_activate`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/categories/history` | owner-bearer | denied | `gateway.category_api.categories_history`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/categories/preview-as` | owner-bearer | denied | `gateway.category_api.categories_preview`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/categories/revisions/{revision}` | owner-bearer | denied | `gateway.category_api.categories_revision`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/categories/revisions/{revision}/restore` | owner-bearer | denied | `gateway.category_api.categories_restore`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/categories/services` | owner-bearer | denied | `gateway.category_api.categories_services`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/config/export` | owner-bearer | denied | `proxy.admin_config_export`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/config/reload` | owner-bearer | denied | `proxy.admin_config_reload`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/credentials` | owner-bearer | denied | `gateway.admin_api.list_credentials`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/credentials/{credential_id}` | owner-bearer | denied | `gateway.admin_api.delete_credential`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/credentials/{credential_id}/revoke` | owner-bearer | denied | `gateway.admin_api.revoke_credential`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/drain` | owner-bearer | denied | `proxy.admin_drain_status`; AdminAuthenticator / _require_admin; owner/admin/superadmin; loopback peer only | owner |
| `POST /admin/api/drain` | owner-bearer | denied | `proxy.admin_drain_set`; AdminAuthenticator / _require_admin; owner/admin/superadmin; loopback peer only; bearer only | owner |
| `GET /admin/api/extensions` | owner-bearer | denied | `gateway.admin_api.list_extension_packages`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/extensions/enable` | owner-bearer | denied | `gateway.admin_api.enable_extension_package`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/extensions/validate` | owner-bearer | denied | `gateway.admin_api.validate_extension_package`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/extensions/{package_id}` | owner-bearer | denied | `gateway.admin_api.remove_extension_package`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/invites` | owner-bearer | denied | `gateway.accounts_api.invites`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/invites` | owner-bearer | denied | `gateway.accounts_api.create_invite`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `DELETE /admin/api/invites/{link_id}` | owner-bearer | denied | `gateway.accounts_api.revoke_invite`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/invites/{link_id}/link` | owner-bearer | denied | `gateway.accounts_api.copy_invite`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/operator/litellm-config` | owner-bearer | denied | `gateway.admin_api.get_litellm_config`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/operator/litellm/keys` | owner-bearer | denied | `gateway.admin_api.list_litellm_keys`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/litellm/keys` | owner-bearer | denied | `gateway.admin_api.create_litellm_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/litellm/keys/delete` | owner-bearer | denied | `gateway.admin_api.delete_litellm_keys`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/operator/host-commands` | owner-bearer | denied | `gateway.admin_api.get_host_commands`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/operator/host-commands` | owner-bearer | denied | `gateway.admin_api.update_host_commands`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/operator/media-job-admission` | owner-bearer | denied | `gateway.admin_api.update_media_job_admission_overrides`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/operator/overrides` | owner-bearer | denied | `gateway.admin_api.list_operator_overrides`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/operator/service-availability` | owner-bearer | denied | `gateway.admin_api.update_service_availability_overrides`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/engine-definitions` | owner-bearer | denied | `gateway.engine_admin.get_engine_definitions`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/engines/check` | owner-bearer | denied | `gateway.engine_admin.check_engine`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/engines/test` | owner-bearer | denied | `gateway.engine_admin.test_engine`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/engines/access` | owner-bearer | denied | `gateway.engine_admin.engine_access`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/engines/{service_id}` | owner-bearer | denied | `gateway.engine_admin.get_engine`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/operator/service-registry` | owner-bearer | denied | `gateway.admin_api.get_service_registry`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/operator/service-registry` | owner-bearer | denied | `gateway.admin_api.update_service_registry`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/services` | owner-bearer | denied | `gateway.admin_api.add_service`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/services/detect-models` | owner-bearer | denied | `gateway.admin_api.detect_service_models`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/services/test` | owner-bearer | denied | `gateway.admin_api.test_service_connection`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/operator/services/{service_id}` | owner-bearer | denied | `gateway.admin_api.remove_service`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PATCH /admin/api/operator/services/{service_id}` | owner-bearer | denied | `gateway.admin_api.update_service`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/services/{service_id}/enabled` | owner-bearer | denied | `gateway.admin_api.set_service_enabled`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/services/{service_id}/secret-entries` | owner-bearer | denied | `gateway.admin_api.change_secret_entries`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required to change a secret | owner |
| `PUT /admin/api/operator/service-registry/excluded/{section}/{record_id}` | owner-bearer | denied | `gateway.admin_api.fix_excluded_record`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/operator/service-registry/excluded/{section}/{record_id}` | owner-bearer | denied | `gateway.admin_api.remove_excluded_record`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/overview` | owner-bearer | denied | `gateway.admin_api.overview`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/people` | owner-bearer | denied | `gateway.accounts_api.people`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `DELETE /admin/api/people/{user_id}` | owner-bearer | denied | `gateway.accounts_api.remove_person`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PATCH /admin/api/people/{user_id}` | owner-bearer | denied | `gateway.accounts_api.update_person`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/people/{user_id}/reset` | owner-bearer | denied | `gateway.accounts_api.reset_link`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/providers` | owner-bearer | denied | `gateway.provider_admin.list_providers`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/providers` | owner-bearer | denied | `gateway.provider_admin.add_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/providers/-/catalog` | owner-bearer | denied | `gateway.provider_admin.list_catalog`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/providers/-/global-caps` | owner-bearer | denied | `gateway.provider_admin.clear_global_caps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/providers/-/global-caps` | owner-bearer | denied | `gateway.provider_admin.set_global_caps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/providers/-/test` | owner-bearer | denied | `gateway.provider_admin.test_unsaved_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/providers/{provider_id}` | owner-bearer | denied | `gateway.provider_admin.remove_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/providers/{provider_id}` | owner-bearer | denied | `gateway.provider_admin.get_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/providers/{provider_id}/caps` | owner-bearer | denied | `gateway.provider_admin.clear_provider_caps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/providers/{provider_id}/caps` | owner-bearer | denied | `gateway.provider_admin.set_provider_caps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/providers/{provider_id}/key` | owner-bearer | denied | `gateway.provider_admin.delete_provider_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/providers/{provider_id}/key` | owner-bearer | denied | `gateway.provider_admin.set_provider_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/providers/{provider_id}/models` | owner-bearer | denied | `gateway.provider_admin.update_provider_models`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/providers/{provider_id}/reconciliation` | owner-bearer | denied | `gateway.provider_admin.list_provider_reconciliation`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/providers/{provider_id}/reconciliation/{reservation_id}` | owner-bearer | denied | `gateway.provider_admin.reconcile_provider_reservation`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/providers/{provider_id}/test` | owner-bearer | denied | `gateway.provider_admin.test_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/session` | owner-bearer | denied | `gateway.admin_api.session`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/settings/defaults` | owner-bearer | denied | `gateway.settings_api.get_defaults`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/settings/defaults` | owner-bearer | denied | `gateway.settings_api.put_defaults`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/effective` | owner-bearer | denied | `gateway.settings_api.get_effective`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/effective/export` | owner-bearer | denied | `gateway.settings_api.export_effective`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/instance` | owner-bearer | denied | `gateway.settings_api.get_instance`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/settings/instance` | owner-bearer | denied | `gateway.settings_api.put_instance`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/prompt-memory` | owner-bearer | denied | `gateway.settings_api.get_prompt_memory`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/settings/prompt-memory` | owner-bearer | denied | `gateway.settings_api.put_prompt_memory`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/receipts` | owner-bearer | denied | `gateway.settings_api.get_receipts`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/settings/receipts` | owner-bearer | denied | `gateway.settings_api.put_receipts`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/receipts/preview` | owner-bearer | denied | `gateway.settings_api.preview_receipts`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/setup-dismiss` | session-only | denied | `gateway.home_api.dismiss_setup`; Browser session; mutations require CSRF/action header | hidden |
| `POST /admin/api/setup-evidence` | session-only | denied | `gateway.home_api.setup_evidence`; Browser session; mutations require CSRF/action header | hidden |
| `GET /admin/api/setup-progress` | owner-bearer | denied | `gateway.home_api.setup_progress`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/signin` | owner-bearer | denied | `gateway.signin_api.get_snapshot`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/signin/preview` | owner-bearer | denied | `gateway.signin_api.preview`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/signin/discover` | owner-bearer | denied | `gateway.signin_api.discover_route`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/signin/test` | owner-bearer | denied | `gateway.signin_api.start_test`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/signin/test/{test_id}/start` | owner-bearer | denied | `gateway.signin_api.begin_test`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/signin/test/{test_id}` | owner-bearer | denied | `gateway.signin_api.get_test`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `DELETE /admin/api/signin/test/{test_id}` | owner-bearer | denied | `gateway.signin_api.cancel_test`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/signin/link-owner` | owner-bearer | denied | `gateway.signin_api.link_owner`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/signin` | owner-bearer | denied | `gateway.signin_api.save`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/signin/notice/dismiss` | owner-bearer | denied | `gateway.signin_api.dismiss_notice`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/signin/owner-password` | owner-bearer | denied | `gateway.signin_api.owner_password`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/status` | owner-bearer | denied | `gateway.admin_api.status_payload`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/users` | owner-bearer | denied | `gateway.admin_api.list_users`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/access-plans/{plan_id}` | owner-bearer | denied | `gateway.admin_api.delete_access_plan`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/access-plans` | owner-bearer | denied | `gateway.admin_api.list_access_plans`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/access-plans/editor` | owner-bearer | denied | `gateway.admin_api.access_plan_editor`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/access-plans/{plan_id}` | owner-bearer | denied | `gateway.admin_api.get_access_plan`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/access-plans` | owner-bearer | denied | `gateway.admin_api.create_access_plan`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/access/check` | owner-bearer | denied | `gateway.admin_api.check_access`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/access/preview` | owner-bearer | denied | `gateway.admin_api.preview_access`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/access-plans/{plan_id}` | owner-bearer | denied | `gateway.admin_api.update_access_plan`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/oidc/callback` | browser/public | no owner authority | `proxy.admin_oidc_callback`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /auth/api/invite` | browser/public | no owner authority | `gateway.accounts_api.inspect`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `POST /auth/api/invite/accept` | browser/public | no owner authority | `gateway.accounts_api.accept`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `POST /auth/api/login` | browser/public | no owner authority | `gateway.accounts_api.login`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `GET /auth/api/options` | browser/public | no owner authority | `gateway.accounts_api.get_options`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `POST /auth/api/password-reset` | browser/public | no owner authority | `gateway.accounts_api.reset`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `GET /auth/logout-confirm` | session-only | denied | `proxy.logout_confirmation`; Browser session; mutations require CSRF/action header | hidden |
| `GET /login` | browser/public | no owner authority | `proxy.browser_login`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /logout` | session-only | denied | `proxy.admin_logout`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/access-request` | session-only | denied | `proxy.user_portal_request_access`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/activity` | session-view | no owner authority | `proxy.user_portal_activity`; Optional browser session; returns empty unauthenticated activity without cookie | hidden |
| `POST /me/api/apps` | session-only | denied | `proxy.user_portal_create_app`; Browser session; mutations require CSRF/action header | hidden |
| `DELETE /me/api/apps/{app_id}` | session-only | denied | `proxy.user_portal_delete_app`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/apps/{app_id}/keys` | session-only | denied | `proxy.user_portal_mint_key`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/connection-test` | session-only | denied | `gateway.home_api.connection_test`; Browser session; mutations require CSRF/action header | hidden |
| `DELETE /me/api/credentials/{credential_id}` | session-only | denied | `proxy.user_portal_revoke_key`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/credentials/{credential_id}/access` | session-only | denied | `proxy.user_portal_credential_access`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/credentials/{credential_id}/revoke` | session-only | denied | `proxy.user_portal_revoke_key`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/home` | session-only | denied | `gateway.home_api.home`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/jobs` | session-only | denied | `gateway.me_media.list_jobs`; Browser session; own jobs only | hidden |
| `GET /me/api/jobs/{job_id}` | session-only | denied | `gateway.me_media.job_detail`; Browser session; own jobs only | hidden |
| `GET /me/api/make/jobs` | session-only | denied | `gateway.me_make.list_jobs`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/make/jobs` | session-only | denied | `gateway.me_make.submit`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/make/jobs/{job_id}` | session-only | denied | `gateway.me_make.get_job`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/make/jobs/{job_id}/cancel` | session-only | denied | `gateway.me_make.cancel_job`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/mcp/connections` | session-only | denied | `gateway.mcp_oauth.connections`; Browser session; own connections only | hidden |
| `POST /me/api/mcp/connections/{grant_id}/revoke` | session-only | denied | `gateway.mcp_oauth.revoke_connection`; Browser session; own connections only; mutations require CSRF/action header | hidden |
| `GET /me/api/mcp/requests/{request_id}` | session-only | denied | `gateway.mcp_oauth.consent_details`; Browser session; same-browser binding cookie | hidden |
| `POST /me/api/mcp/requests/{request_id}` | session-only | denied | `gateway.mcp_oauth.consent_decision`; Browser session; same-browser binding cookie; mutations require CSRF/action header | hidden |
| `GET /me/api/media` | session-only | denied | `gateway.me_media.list_media`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/media/remove-missing` | session-only | denied | `gateway.me_media.remove_missing_media`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/media/confirm-folder` | session-only | denied | `gateway.me_media.confirm_media_folder`; Browser session; mutations require CSRF/action header | hidden |
| `DELETE /me/api/media/{asset_id}` | session-only | denied | `gateway.me_media.delete_media`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/media/{asset_id}` | session-only | denied | `gateway.me_media.media_detail`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/media/{asset_id}/download` | session-only | denied | `gateway.me_media.media_download`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/models/catalog` | session-only | denied | `gateway.models_catalog.models_catalog`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/prompt-memory` | session-only | denied | `gateway.me_prompt_memory.get_prompt_memory`; Browser session; mutations require CSRF/action header | hidden |
| `PUT /me/api/prompt-memory` | session-only | denied | `gateway.me_prompt_memory.put_prompt_memory`; Browser session; mutations require CSRF/action header | hidden |
| `DELETE /me/api/prompt-memory/prompts` | session-only | denied | `gateway.me_prompt_memory.delete_prompt_memory`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/receipts/{request_id}` | session-only | denied | `proxy.user_portal_receipt_detail`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/session` | session-view | no owner authority | `proxy.user_portal_session`; Optional browser session; returns unauthenticated view without cookie | hidden |
| `GET /me/api/status` | session-only | denied | `gateway.home_api.member_status`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/try-key` | session-only | denied | `proxy.user_portal_try_key`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/usage/summary` | session-only | denied | `gateway.me_media.usage_summary`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/workflows` | session-only | denied | `gateway.workflows_catalog.workflows_catalog`; Browser session; mutations require CSRF/action header | hidden |
| `GET /setup/api/state` | browser/public | no owner authority | `gateway.setup_api.setup_state`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `GET /setup/console` | browser/public | no owner authority | `proxy.setup_console`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /setup/claim` | browser/public | no owner authority | `proxy.setup_claim_admin_key`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /setup/first-app` | session-only | denied | `proxy.setup_create_first_app`; Browser session; mutations require CSRF/action header | hidden |
| `POST /setup/local-login` | browser/public | no owner authority | `proxy.setup_local_login`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /setup/local-owner` | browser/public | no owner authority | `proxy.setup_create_local_owner`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `GET /status/raw` | owner-bearer | denied | `proxy.status_dashboard_raw`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /status/raw/` | owner-bearer | denied | `proxy.status_dashboard_raw`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |

For Library reads, owners and superadmins can use `scope=everyone` or `person=all`.
They can also use `person=self`, `person=local-network`, or a specific person ID.
For Usage, the broader selector is `person`.
`scope=everyone` alone does not expand Usage.
Ordinary members cannot use `person` selectors.
Both Jobs endpoints remain own-jobs-only for owners too.
These routes require a browser session. An owner key does not replace that session.

## Recorded Restish fixture run

Restish **2.3.0** was installed under `~/.tmp/restish`, then exercised in a
Bubblewrap filesystem/network namespace against the registered `proxy.app`
routes on **127.0.0.1:44061**. The fixture used fresh SQLite stores, disabled
server lifespan, mocked health observations, and called no inference engines.
Setup discovered **245 operations**, including **104/104 configured owner
operations**. This is fixture evidence, not a live deployment check.

The earlier commands used explicit fixture bearer headers and succeeded.
Restish 2.3.0's private config format and 104/104 configured owner bindings were
checked offline with the committed schema. The revised file/config examples
have not been rerun over HTTP in this recorded run.
These output filters kept the transcript short
and prevented the one-time App key from entering logs:

```sh
# Append to the corresponding generated command from the guide.
# List apps:
-f 'body.apps[].name'
# Mint key:
-f 'body.credential.display_name'
# Read receipts:
-f 'body.records[].receipt.request_id'
# Check status:
-f 'body.status.current_health.state'
```

Real stdout and exit codes (status reflects the fixture's mocked observations):

```text
List apps (exit 0)
[
  "headless-fixture"
]
Mint app key (label only) (exit 0)
"automation-client"
Read receipts (exit 0)
[
  "headless-fixture-receipt"
]
Check status (health only) (exit 0)
"bad"
Invalid owner credential (exit 4)
{
  "error": {
    "code": "admin_unauthenticated",
    "message": "invalid admin credential"
  }
}
```

The invalid-credential case set the profile token to an invalid fixture value.
The run did not test page-cap exhaustion, interrupted pagination, provider
mutations, or real engine health. Generic URL commands can reuse credentials
from a matching registered API; use a separate config for another identity.
