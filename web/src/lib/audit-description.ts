import type { AuditEntry } from "./api";

const PHRASES: Record<string, string> = {
  "media.library.everyone": "Viewed everyone’s media library",
  "usage.summary.everyone": "Viewed everyone’s usage",
  "app.create": "Created an app",
  "app.self_create": "Created an app",
  "app.self_delete": "Removed an app",
  "credential.self_create": "Created a key",
  "credential.self_revoke": "Revoked a key",
  "setup.local_owner_create": "Set up the owner account",
  "setup.local_login": "Signed in",
  "operator.service_registry.seed": "Added the initial engine configuration",
  "operator.service_registry.backfill": "Updated engine configuration",
  "app.update": "Updated an app",
  "app.disable": "Disabled an app",
  "app.enable": "Enabled an app",
  "credential.create": "Created a key",
  "credential.revoke": "Revoked a key",
  "profile.create": "Created an access plan",
  "profile.update": "Updated an access plan",
  "profile.delete": "Deleted an access plan",
  "admin_key.create": "Created an admin key",
  "runtime_profile_mapping.create": "Added an access assignment",
  "runtime_profile_mapping.disable": "Removed an access assignment",
  "operator.user_access_defaults.update": "Updated access defaults",
  "operator.service_availability.update": "Updated engine availability",
  "operator.media_job_admission.update": "Updated media limits",
  "operator.service_registry.update": "Updated engine configuration",
  "operator.provider_admin.update": "Updated provider settings",
  "setup.claim": "Set up the owner account",
  "operator.service.added": "Added an engine",
  "operator.service.updated": "Updated an engine",
  "operator.service.removed": "Removed an engine",
  "operator.service.toggled": "Changed engine availability",
  "operator.extension_package.enable": "Enabled an extension",
  "operator.extension_package.remove": "Removed an extension",
  "operator.oidc_provider_config.update": "Updated sign-in settings",
};
const VERBS: Record<string, string> = {
  create: "Created",
  update: "Updated",
  delete: "Deleted",
  remove: "Removed",
  revoke: "Revoked",
  enable: "Enabled",
  disable: "Disabled",
};
const NOUNS: Record<string, string> = {
  app: "an app",
  credential: "a key",
  profile: "an access plan",
  runtime_profile_mapping: "an access assignment",
  admin_key: "an admin key",
};
/** A recorded action with no written phrase: say the plain verb and thing, or only that it changed. */
export function auditDescription(entry: AuditEntry): string {
  const phrase = PHRASES[entry.action];
  if (phrase) return phrase;
  const dot = entry.action.lastIndexOf(".");
  const verb = VERBS[entry.action.slice(dot + 1)];
  const noun = NOUNS[entry.action.slice(0, dot)];
  // The stored action name stays visible under Technical details.
  return verb && noun ? `${verb} ${noun}` : "Recorded a change";
}
export function auditTargetName(entry: AuditEntry): string | null {
  if (typeof entry.metadata?.name === "string") return entry.metadata.name;
  return entry.target_kind?.replace(/_/g, " ") ?? null;
}
