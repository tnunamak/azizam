export function adminAppHref(appId: string): string {
  return `/admin/apps?app=${encodeURIComponent(appId)}`;
}

export function adminServiceHref(serviceIdOrName: string): string {
  return `/admin/services?service=${encodeURIComponent(serviceIdOrName)}`;
}

export function adminProfileHref(profileId: string): string {
  return `/admin/profiles?profile=${encodeURIComponent(profileId)}`;
}

export function adminAuditHref(eventId: string): string {
  return `/admin/audit?event=${encodeURIComponent(eventId)}`;
}

export function adminTargetHref(
  targetKind: string | null | undefined,
  targetId: string | null | undefined,
): string | null {
  if (!targetKind || !targetId) return null;
  const kind = targetKind.toLowerCase();
  if (kind === "app") return adminAppHref(targetId);
  if (kind === "profile" || kind === "app_profile") {
    return adminProfileHref(targetId);
  }
  if (kind === "service" || kind === "backend") {
    return adminServiceHref(targetId);
  }
  if (kind === "audit" || kind === "audit_event") {
    return adminAuditHref(targetId);
  }
  return null;
}
