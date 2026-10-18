// The gateway deleted every generated file 24 hours after it was written until
// the expiry was removed (commit 272abbfd, 2026-10-11 11:59:31 CDT). A gone
// file made before that moment went that way and cannot come back.
export const AUTOMATIC_EXPIRY_ENDED_MS = Date.UTC(2026, 9, 11, 16, 59, 31);

/** The note for a gone file made before the expiry ended, or null when no claim can be made. */
export function expiryNote(
  createdAtSeconds: number | null | undefined,
  { locale, timeZone }: { locale?: string; timeZone?: string } = {},
): string | null {
  if (typeof createdAtSeconds !== "number" || !Number.isFinite(createdAtSeconds)) return null;
  if (createdAtSeconds * 1000 >= AUTOMATIC_EXPIRY_ENDED_MS) return null;
  const ended = new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
    timeZone,
  }).format(new Date(AUTOMATIC_EXPIRY_ENDED_MS));
  return `This file was deleted on or before ${ended} and can’t be recovered.`;
}
