export function adminAppHref(appId: string): string {
  return `/admin/apps?app=${encodeURIComponent(appId)}`;
}

export function adminServiceHref(serviceId: string): string {
  return `/admin/services?service=${encodeURIComponent(serviceId)}`;
}

/** Live diagnosis. Service configuration stays at adminServiceHref(). */
export function adminStatusHref(serviceId?: string | null): string {
  return serviceId
    ? `/admin/status?service=${encodeURIComponent(serviceId)}`
    : "/admin/status";
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
  // Attention targets are a small canonical tuple, not a compatibility
  // namespace. Alias handling here would make emitted backend records and UI
  // navigation disagree about object identity.
  switch (targetKind) {
    case "app":
      return adminAppHref(targetId);
    case "profile":
      return adminProfileHref(targetId);
    case "service":
      return adminServiceHref(targetId);
    case "audit":
      return adminAuditHref(targetId);
    default:
      return null;
  }
}
