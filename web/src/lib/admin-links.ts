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

/** Only same-origin admin paths may be carried between admin routes. */
export function safeAdminReturnPath(
  value: string | null | undefined,
): string | null {
  if (
    !value ||
    !value.startsWith("/admin/") ||
    value.startsWith("//") ||
    value.includes("\\")
  ) {
    return null;
  }
  try {
    const url = new URL(value, "https://gateway.invalid");
    return url.origin === "https://gateway.invalid" &&
      url.pathname.startsWith("/admin/")
      ? `${url.pathname}${url.search}${url.hash}`
      : null;
  } catch {
    return null;
  }
}

/** A validated return path for an Audit-originated target journey. */
export function safeAuditReturnPath(
  value: string | null | undefined,
): string | null {
  const path = safeAdminReturnPath(value);
  return path &&
    /^\/admin\/audit\/?$/.test(
      new URL(path, "https://gateway.invalid").pathname,
    )
    ? path
    : null;
}

export function adminTargetHref(
  targetKind: string | null | undefined,
  targetId: string | null | undefined,
  returnTo?: string | null,
): string | null {
  if (!targetKind || !targetId) return null;
  // Attention targets are a small canonical tuple, not a compatibility
  // namespace. Alias handling here would make emitted backend records and UI
  // navigation disagree about object identity.
  const targetHref = (() => {
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
  })();
  const safeReturnTo = safeAuditReturnPath(returnTo);
  if (!targetHref || !safeReturnTo) return targetHref;
  return `${targetHref}&return=${encodeURIComponent(safeReturnTo)}`;
}
