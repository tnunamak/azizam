/** An in-flight receipt with no update for this long is not evidence of live progress. The gateway's receipt store uses
 * the same limit when it reports such a receipt as stale_incomplete on its next read, so the two never disagree. */
export const STALE_AFTER_MS = 5 * 60_000;

export function staleWords(
  status: string | undefined,
  updatedAt: number | string | null | undefined,
  now: number,
): string | null {
  if (!["accepted", "running", "in_progress"].includes(status ?? "") ||
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
  if (!Number.isFinite(date.getTime()) || now - time < STALE_AFTER_MS) return null;

  return "No update since " + date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}
