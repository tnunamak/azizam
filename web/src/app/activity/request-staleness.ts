/** An in-flight receipt with no update for this long is not evidence of live progress. This is the gateway's limit too:
 * a cached row ages here by this clock, while the gateway reports an in_progress receipt as stale_incomplete when it is
 * read. A stale_incomplete receipt is already judged, so it shows its last update whatever this clock says. */
export const STALE_AFTER_MS = 5 * 60_000;

export function staleWords(
  status: string | undefined,
  updatedAt: number | string | null | undefined,
  now: number,
): string | null {
  const judged = status === "stale_incomplete";
  if ((!judged && !["accepted", "running", "in_progress"].includes(status ?? "")) ||
      updatedAt == null || !Number.isFinite(now)) return null;

  const value = typeof updatedAt === "string" ? updatedAt.trim() : updatedAt;
  if (value === "") return null;
  const numeric = typeof value === "number"
    ? value
    : /^[+-]?\d+(?:\.\d+)?$/.test(value) ? Number(value) : null;
  const time = numeric === null
    ? Date.parse(value as string)
    : numeric > 1e12 ? numeric : numeric * 1000;
  const date = new Date(time);
  if (!Number.isFinite(date.getTime()) || (!judged && now - time < STALE_AFTER_MS)) return null;

  return "No update since " + date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}
