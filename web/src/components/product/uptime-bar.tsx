import { cn } from "@/lib/cn";

/** [timestamp_s, value] where 1 = up, 0.5 = degraded, 0 = down. */
export type SeriesPoint = number[];

function segmentClass(value: number, enabled: boolean): string {
  if (!enabled) return "bg-border";
  if (value >= 1) return "bg-success";
  if (value <= 0) return "bg-danger";
  return "bg-warning";
}

/**
 * Hourly health over the last day, oldest left (Linear status, BOARD 2a.3).
 * Colour is never the only signal: the percentage sits beside the bar and the
 * bar carries an accessible summary.
 */
export function UptimeBar({
  series,
  enabled = true,
  label,
  now,
  className,
}: {
  series: SeriesPoint[] | undefined;
  enabled?: boolean;
  label: string;
  /** End of the window in epoch seconds; hours with no sample stay empty. */
  now?: number | null;
  className?: string;
}) {
  const points = series ?? [];
  if (!points.length) return null;
  const end = Math.max(now ?? 0, ...points.map(([timestamp]) => timestamp));
  const slots = Array.from({ length: 24 }, (_, index) => {
    const start = end - (24 - index) * 3600;
    const inSlot = points.filter(
      ([timestamp]) => timestamp > start && timestamp <= start + 3600,
    );
    return inSlot.length
      ? Math.min(...inSlot.map(([, value]) => value))
      : null;
  });
  const known = slots.filter((value): value is number => value !== null);
  const down = known.filter((value) => value < 1).length;
  const missing = slots.length - known.length;
  const summary = !enabled
    ? `${label}: turned off`
    : [
        down
          ? `${down} of the last 24 hours had problems`
          : "no problems in the hours checked",
        missing ? `${missing} hours have no check` : null,
      ]
        .filter(Boolean)
        .join("; ")
        .replace(/^/, `${label}: `);
  return (
    <span
      role="img"
      aria-label={summary}
      className={cn("flex h-5 min-w-0 items-stretch gap-px", className)}
    >
      {slots.map((value, index) => (
        <span
          key={index}
          className={cn(
            "flex-1 rounded-xs",
            value === null ? "bg-muted" : segmentClass(value, enabled),
          )}
        />
      ))}
    </span>
  );
}

/** Percentage of hours fully up, from the same series the bar draws. */
export function uptimePercent(series: SeriesPoint[] | undefined): number | null {
  const points = (series ?? []).slice(-24);
  if (!points.length) return null;
  const total = points.reduce((sum, [, value]) => sum + Math.max(0, Math.min(1, value)), 0);
  return Math.round((total / points.length) * 1000) / 10;
}

/** Consecutive trailing hours that were not fully up. */
export function hoursUnhealthy(series: SeriesPoint[] | undefined): number {
  const points = series ?? [];
  let count = 0;
  for (let index = points.length - 1; index >= 0; index -= 1) {
    if (points[index][1] >= 1) break;
    count += 1;
  }
  return count;
}
