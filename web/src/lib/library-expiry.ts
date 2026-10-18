const HOUR_MS = 60 * 60 * 1000;

// The gateway deleted every generated file 24 hours after its mtime until the
// expiry loop was removed (commit 272abbfd, 2026-10-11 11:59:31 CDT = 16:59:31 UTC).
// The last sweep could only delete files already 24 hours old, so a file made
// before 2026-10-10 11:59:31 CDT is the newest one that can have been deleted.
// A gone file made at or after this moment was lost some other way.
export const EXPIRY_CUTOFF_MS = Date.UTC(2026, 9, 10, 16, 59, 31);

/** The note for a gone file made before the cutoff, or null when no claim can be made. */
export function expiryNote(
  createdAtSeconds: number | null | undefined,
  { locale, timeZone }: { locale?: string; timeZone?: string } = {},
): string | null {
  if (typeof createdAtSeconds !== "number" || !Number.isFinite(createdAtSeconds)) return null;
  if (createdAtSeconds * 1000 >= EXPIRY_CUTOFF_MS) return null;
  // Every file made before the cutoff was deleted by the moment the expiry was removed.
  const ended = new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
    timeZone,
  }).format(new Date(EXPIRY_CUTOFF_MS + 24 * HOUR_MS));
  return `This file was deleted on or before ${ended} and can’t be recovered.`;
}
