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
umask 077
./venv/bin/python proxy.py generate-admin-key headless-automation owner > owner-key-output.txt
tail -n 1 owner-key-output.txt > owner-key.txt
rm owner-key-output.txt
AZIZAM_OWNER_KEY=$(cat owner-key.txt)
export AZIZAM_OWNER_KEY
AZIZAM_URL=https://gateway.example
export AZIZAM_URL
```

An `admin` role key cannot use owner-only operations. Neither role is a
read-only scope. The key's role is enforced on each request. The helper does
not configure a browser session or start a server.

## Call the API with curl

```sh
curl --fail-with-body -sS "$AZIZAM_URL/admin/api/apps" \
  -H "Authorization: Bearer $AZIZAM_OWNER_KEY"

# Choose an existing app ID from the list. This mints an App key, not an owner key.
APP_ID=app_id_from_list
curl --fail-with-body -sS -X POST "$AZIZAM_URL/admin/api/apps/$APP_ID/keys" \
  -H "Authorization: Bearer $AZIZAM_OWNER_KEY" \
  -H 'Content-Type: application/json' --data '{"label":"automation-client"}'

curl --fail-with-body -sS "$AZIZAM_URL/admin/api/activity/requests?limit=10" \
  -H "Authorization: Bearer $AZIZAM_OWNER_KEY"
curl --fail-with-body -sS "$AZIZAM_URL/admin/api/status" \
  -H "Authorization: Bearer $AZIZAM_OWNER_KEY"
```

Key creation returns the secret once in `credential.api_key`; store it privately.
Request activity returns safe receipt records with `next_cursor`. It differs from
the administrative change audit at `/admin/api/audit`. Continue receipt pages
with `before=<next_cursor>` until `next_cursor` is null. Registry writes need the
current `ETag` in `If-Match`; settings that require a revision do too. Do not
silently retry a rejected mutation with a newer revision.

## Use Restish 2.3.0

Install Restish in a private tool directory to build commands from OpenAPI
without an Azizam CLI. Its v1 `api configure` command does not work in v2.
Use a separate config file to avoid ambient profiles. Register the trusted
schema explicitly, configure an environment-backed token, and pass the auth header on calls:

```sh
RSH_CONFIG="$PWD/restish-owner.json"
export RSH_CONFIG
umask 077
test -e "$RSH_CONFIG" || printf '{}\n' > "$RSH_CONFIG"
chmod 600 "$RSH_CONFIG"
restish api connect azizam "$AZIZAM_URL" \
  'prompt.credentials.OwnerBearer.token: env:AZIZAM_OWNER_KEY' \
  --spec "$AZIZAM_URL/openapi.json" --yes
restish azizam owner-get-admin-api-apps \
  -H "Authorization: Bearer $AZIZAM_OWNER_KEY" -o json
restish azizam owner-post-admin-api-apps-app-id-keys "$APP_ID" \
  'label: automation-client' -H "Authorization: Bearer $AZIZAM_OWNER_KEY" -o json
restish azizam owner-get-admin-api-activity-requests --limit 10 \
  -H "Authorization: Bearer $AZIZAM_OWNER_KEY" -o json
restish azizam owner-get-admin-api-status \
  -H "Authorization: Bearer $AZIZAM_OWNER_KEY" -o json
```

Use `restish azizam <command> --help` for inferred path/query/body arguments.
Legacy handlers that read JSON manually have an open object body schema; their
field validation remains in the handler. Responses without typed models keep
an unspecified schema. The owner metadata does not invent stricter contracts.
Session-only routes stay absent from OpenAPI. HTML owner routes remain outside
the generated command surface. `/admin/api/drain` is loopback-only; its POST
also requires a bearer key even for an owner browser session.

Reconnect after an API upgrade to refresh the cached spec. Operation IDs depend
on the HTTP method and path, so renaming a Python handler does not rename a CLI
command. Only explicitly inventoried owner operations receive owner metadata.

### Check results before using them

Capture stdout, stderr, and the exit code separately. Do not use
`--rsh-ignore-status-code`. Do not treat exit 0 as proof that all records arrived
or that a JSON body describes success.

```sh
restish azizam owner-get-admin-api-activity-requests --limit 10 \
  -H "Authorization: Bearer $AZIZAM_OWNER_KEY" -o json \
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

Captured by walking `proxy.app.routes` at base `33810bda`. Counts are per
registered method/path, including trailing-slash aliases: **126 owner-bearer**
(104 JSON/YAML owner API operations, 22 browser/HTML routes), **33 session-only**,
**1 session-view**, **19 browser/public**; **179 total**.

FastAPI dependency lists are empty on these routes. Auth is an imperative
handler call or injected callback, shown below. `owner-bearer` rejects app keys
and member sessions. `session-only` requires a cookie and rejects a standalone
owner/app bearer key. `session-view` permits an unauthenticated snapshot.
`browser/public` covers sign-in, first-run, redirect and health contracts;
headers alone do not turn them into owner API operations. Public data-plane
routes and static mounts are outside this control/browser inventory.

The tests compare this table and its fixture with every registered route in
these namespaces. They exercise app/member denials through HTTP and compare
SQLite contents and drain state. Every bearer route reaches a checkpoint after
real owner credential validation; operational effects are stopped there.
That exhaustive test proves auth reachability, not successful execution of
every mutation. Selected successful workflows are exercised separately.

| Method and path | Auth class | App key | Handler / auth call | Schema |
| --- | --- | --- | --- | --- |
| `GET /admin` | owner-bearer | denied | `proxy.admin_dashboard`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `HEAD /admin` | browser/public | no owner authority | `proxy.head_admin`; Public health check | hidden |
| `GET /admin/` | owner-bearer | denied | `proxy.admin_dashboard`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/api/activity/requests` | owner-bearer | denied | `gateway.activity_api.requests`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/admin-keys` | owner-bearer | denied | `gateway.admin_api.create_admin_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required to mint owner key | owner |
| `GET /admin/api/apps` | owner-bearer | denied | `gateway.admin_api.list_apps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/apps` | owner-bearer | denied | `gateway.admin_api.create_app`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/apps/{app_id}` | owner-bearer | denied | `gateway.admin_api.disable_app`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PATCH /admin/api/apps/{app_id}` | owner-bearer | denied | `gateway.admin_api.update_app`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/apps/{app_id}/enable` | owner-bearer | denied | `gateway.admin_api.enable_app`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/apps/{app_id}/keys` | owner-bearer | denied | `gateway.admin_api.create_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
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
| `GET /admin/api/config/export` | owner-bearer | denied | `proxy.admin_config_export`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
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
| `GET /admin/api/oidc-config` | owner-bearer | denied | `gateway.admin_api.get_oidc_config`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/oidc-config` | owner-bearer | denied | `gateway.admin_api.update_oidc_config`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/operator/litellm-config` | owner-bearer | denied | `gateway.admin_api.get_litellm_config`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/operator/litellm/keys` | owner-bearer | denied | `gateway.admin_api.list_litellm_keys`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/litellm/keys` | owner-bearer | denied | `gateway.admin_api.create_litellm_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/litellm/keys/delete` | owner-bearer | denied | `gateway.admin_api.delete_litellm_keys`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/operator/media-job-admission` | owner-bearer | denied | `gateway.admin_api.update_media_job_admission_overrides`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/operator/overrides` | owner-bearer | denied | `gateway.admin_api.list_operator_overrides`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/operator/service-availability` | owner-bearer | denied | `gateway.admin_api.update_service_availability_overrides`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/operator/service-presets` | owner-bearer | denied | `gateway.admin_api.get_service_presets`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/operator/service-registry` | owner-bearer | denied | `gateway.admin_api.get_service_registry`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/operator/service-registry` | owner-bearer | denied | `gateway.admin_api.update_service_registry`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/services` | owner-bearer | denied | `gateway.admin_api.add_service`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/services/detect-models` | owner-bearer | denied | `gateway.admin_api.detect_service_models`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/services/test` | owner-bearer | denied | `gateway.admin_api.test_service_connection`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/operator/services/{service_id}` | owner-bearer | denied | `gateway.admin_api.remove_service`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PATCH /admin/api/operator/services/{service_id}` | owner-bearer | denied | `gateway.admin_api.update_service`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/operator/services/{service_id}/enabled` | owner-bearer | denied | `gateway.admin_api.set_service_enabled`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/operator/sign-in` | owner-bearer | denied | `gateway.accounts_api.get_mode`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/operator/sign-in` | owner-bearer | denied | `gateway.accounts_api.set_mode`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/operator/user-access-defaults` | owner-bearer | denied | `gateway.admin_api.update_user_access_defaults`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/overview` | owner-bearer | denied | `gateway.admin_api.overview`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/people` | owner-bearer | denied | `gateway.accounts_api.people`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `DELETE /admin/api/people/{user_id}` | owner-bearer | denied | `gateway.accounts_api.remove_person`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PATCH /admin/api/people/{user_id}` | owner-bearer | denied | `gateway.accounts_api.update_person`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/people/{user_id}/reset` | owner-bearer | denied | `gateway.accounts_api.reset_link`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/policy/editor` | owner-bearer | denied | `gateway.admin_api.policy_editor`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/policy/simulate` | owner-bearer | denied | `gateway.admin_api.simulate`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/profiles` | owner-bearer | denied | `gateway.admin_api.list_profiles`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/profiles` | owner-bearer | denied | `gateway.admin_api.create_profile`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/profiles/{profile_id}` | owner-bearer | denied | `gateway.admin_api.delete_profile`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/profiles/{profile_id}` | owner-bearer | denied | `gateway.admin_api.get_profile`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PATCH /admin/api/profiles/{profile_id}` | owner-bearer | denied | `gateway.admin_api.update_profile`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/providers` | owner-bearer | denied | `gateway.provider_admin.list_providers`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/providers` | owner-bearer | denied | `gateway.provider_admin.add_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/providers/-/catalog` | owner-bearer | denied | `gateway.provider_admin.list_catalog`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/providers/-/global-caps` | owner-bearer | denied | `gateway.provider_admin.clear_global_caps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/providers/-/global-caps` | owner-bearer | denied | `gateway.provider_admin.set_global_caps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/providers/-/test` | owner-bearer | denied | `gateway.provider_admin.test_unsaved_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/providers/{provider_id}` | owner-bearer | denied | `gateway.provider_admin.remove_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/providers/{provider_id}` | owner-bearer | denied | `gateway.provider_admin.get_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/providers/{provider_id}/allowed-plans` | owner-bearer | denied | `gateway.provider_admin.set_allowed_plans`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/providers/{provider_id}/caps` | owner-bearer | denied | `gateway.provider_admin.clear_provider_caps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/providers/{provider_id}/caps` | owner-bearer | denied | `gateway.provider_admin.set_provider_caps`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/providers/{provider_id}/key` | owner-bearer | denied | `gateway.provider_admin.delete_provider_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/providers/{provider_id}/key` | owner-bearer | denied | `gateway.provider_admin.set_provider_key`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `PUT /admin/api/providers/{provider_id}/models` | owner-bearer | denied | `gateway.provider_admin.update_provider_models`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/providers/{provider_id}/reconciliation` | owner-bearer | denied | `gateway.provider_admin.list_provider_reconciliation`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/providers/{provider_id}/reconciliation/{reservation_id}` | owner-bearer | denied | `gateway.provider_admin.reconcile_provider_reservation`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/providers/{provider_id}/test` | owner-bearer | denied | `gateway.provider_admin.test_provider`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/runtime-profile-mappings` | owner-bearer | denied | `gateway.admin_api.list_runtime_profile_mappings`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `POST /admin/api/runtime-profile-mappings` | owner-bearer | denied | `gateway.admin_api.create_runtime_profile_mapping`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `DELETE /admin/api/runtime-profile-mappings/{mapping_id}` | owner-bearer | denied | `gateway.admin_api.disable_runtime_profile_mapping`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/session` | owner-bearer | denied | `gateway.admin_api.session`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/settings/defaults` | owner-bearer | denied | `gateway.settings_api.get_defaults`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/settings/defaults` | owner-bearer | denied | `gateway.settings_api.put_defaults`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/effective` | owner-bearer | denied | `gateway.settings_api.get_effective`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/effective/export` | owner-bearer | denied | `gateway.settings_api.export_effective`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/instance` | owner-bearer | denied | `gateway.settings_api.get_instance`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/settings/instance` | owner-bearer | denied | `gateway.settings_api.put_instance`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/lan-placeholder-keys` | owner-bearer | denied | `gateway.settings_api.get_lan_placeholder_keys`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/settings/lan-placeholder-keys` | owner-bearer | denied | `gateway.settings_api.put_lan_placeholder_keys`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/prompt-memory` | owner-bearer | denied | `gateway.settings_api.get_prompt_memory`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/settings/prompt-memory` | owner-bearer | denied | `gateway.settings_api.put_prompt_memory`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/receipts` | owner-bearer | denied | `gateway.settings_api.get_receipts`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `PUT /admin/api/settings/receipts` | owner-bearer | denied | `gateway.settings_api.put_receipts`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/settings/receipts/preview` | owner-bearer | denied | `gateway.settings_api.preview_receipts`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `POST /admin/api/setup-dismiss` | session-only | denied | `gateway.home_api.dismiss_setup`; Browser session; mutations require CSRF/action header | hidden |
| `POST /admin/api/setup-evidence` | session-only | denied | `gateway.home_api.setup_evidence`; Browser session; mutations require CSRF/action header | hidden |
| `GET /admin/api/setup-progress` | owner-bearer | denied | `gateway.home_api.setup_progress`; AdminAuthenticator / _require_admin; owner/admin/superadmin; owner role required | owner |
| `GET /admin/api/status` | owner-bearer | denied | `gateway.admin_api.status_payload`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/api/users` | owner-bearer | denied | `gateway.admin_api.list_users`; AdminAuthenticator / _require_admin; owner/admin/superadmin | owner |
| `GET /admin/apps` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/apps/` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/audit` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/audit/` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/login` | browser/public | no owner authority | `proxy.admin_login_compat`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /admin/logout` | session-only | denied | `proxy.admin_logout`; Browser session; mutations require CSRF/action header | hidden |
| `GET /admin/oidc/callback` | browser/public | no owner authority | `proxy.admin_oidc_callback`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `GET /admin/overview` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/overview/` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/profiles` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/profiles/` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/services` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/services/` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/settings` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/settings/` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/status` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/status/` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/users` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /admin/users/` | owner-bearer | denied | `proxy.admin_react_section_redirect`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `POST /auth/api/invite` | browser/public | no owner authority | `gateway.accounts_api.inspect`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `POST /auth/api/invite/accept` | browser/public | no owner authority | `gateway.accounts_api.accept`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `POST /auth/api/login` | browser/public | no owner authority | `gateway.accounts_api.login`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `GET /auth/api/options` | browser/public | no owner authority | `gateway.accounts_api.get_options`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `POST /auth/api/password-reset` | browser/public | no owner authority | `gateway.accounts_api.reset`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | existing |
| `GET /auth/logout-confirm` | session-only | denied | `proxy.logout_confirmation`; Browser session; mutations require CSRF/action header | hidden |
| `GET /login` | browser/public | no owner authority | `proxy.browser_login`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `GET /logout` | browser/public | no owner authority | `proxy.browser_logout_alias`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /logout` | session-only | denied | `proxy.admin_logout`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me` | browser/public | no owner authority | `proxy.user_portal`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `GET /me/` | browser/public | no owner authority | `proxy.user_portal`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /me/api/access-request` | session-only | denied | `proxy.user_portal_request_access`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/activity` | session-only | denied | `proxy.user_portal_activity`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/apps` | session-only | denied | `proxy.user_portal_create_app`; Browser session; mutations require CSRF/action header | hidden |
| `DELETE /me/api/apps/{app_id}` | session-only | denied | `proxy.user_portal_delete_app`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/apps/{app_id}/keys` | session-only | denied | `proxy.user_portal_mint_key`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/connection-test` | session-only | denied | `gateway.home_api.connection_test`; Browser session; mutations require CSRF/action header | hidden |
| `DELETE /me/api/credentials/{credential_id}` | session-only | denied | `proxy.user_portal_revoke_key`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/credentials/{credential_id}/access` | session-only | denied | `proxy.user_portal_credential_access`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/credentials/{credential_id}/revoke` | session-only | denied | `proxy.user_portal_revoke_key`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/home` | session-only | denied | `gateway.home_api.home`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/make/jobs` | session-only | denied | `gateway.me_make.list_jobs`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/make/jobs` | session-only | denied | `gateway.me_make.submit`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/make/jobs/{job_id}` | session-only | denied | `gateway.me_make.get_job`; Browser session; mutations require CSRF/action header | hidden |
| `POST /me/api/make/jobs/{job_id}/cancel` | session-only | denied | `gateway.me_make.cancel_job`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/media` | session-only | denied | `gateway.me_media.list_media`; Browser session; mutations require CSRF/action header | hidden |
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
| `GET /me/api/usage/summary` | session-only | denied | `gateway.me_media.usage_summary`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/api/workflows` | session-only | denied | `gateway.workflows_catalog.workflows_catalog`; Browser session; mutations require CSRF/action header | hidden |
| `GET /me/login` | browser/public | no owner authority | `proxy.user_portal_login`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `GET /me/logout` | browser/public | no owner authority | `proxy.user_portal_logout`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /me/logout` | session-only | denied | `proxy.admin_logout`; Browser session; mutations require CSRF/action header | hidden |
| `GET /setup` | browser/public | no owner authority | `proxy.setup_page`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `GET /setup/api/state` | browser/public | no owner authority | `gateway.setup_api.setup_state`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /setup/claim` | browser/public | no owner authority | `proxy.setup_claim_admin_key`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /setup/first-app` | session-only | denied | `proxy.setup_create_first_app`; Browser session; mutations require CSRF/action header | hidden |
| `POST /setup/local-login` | browser/public | no owner authority | `proxy.setup_local_login`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `POST /setup/local-owner` | browser/public | no owner authority | `proxy.setup_create_local_owner`; Browser redirect, sign-in, or first-run guard; no admin bearer contract | hidden |
| `GET /status` | owner-bearer | denied | `proxy.status_dashboard_index`; AdminAuthenticator / _require_admin; owner/admin/superadmin; HTML without Authorization redirects before auth | hidden |
| `GET /status/` | owner-bearer | denied | `proxy.status_dashboard_index`; AdminAuthenticator / _require_admin; owner/admin/superadmin; HTML without Authorization redirects before auth | hidden |
| `GET /status/raw` | owner-bearer | denied | `proxy.status_dashboard_raw`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
| `GET /status/raw/` | owner-bearer | denied | `proxy.status_dashboard_raw`; AdminAuthenticator / _require_admin; owner/admin/superadmin | hidden |
