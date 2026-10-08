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
  className,
}: {
  series: SeriesPoint[] | undefined;
  enabled?: boolean;
  label: string;
  className?: string;
}) {
  const points = (series ?? []).slice(-24);
  if (!points.length) return null;
  const down = points.filter(([, value]) => value < 1).length;
  const summary = !enabled
    ? `${label}: turned off`
    : down
      ? `${label}: ${down} of the last ${points.length} hours had problems`
      : `${label}: no problems in the last ${points.length} hours`;
  return (
    <span
      role="img"
      aria-label={summary}
      className={cn("flex h-5 min-w-0 items-stretch gap-px", className)}
    >
      {Array.from({ length: Math.max(0, 24 - points.length) }, (_, index) => (
        <span key={`empty-${index}`} className="bg-muted flex-1 rounded-xs" />
      ))}
      {points.map(([timestamp, value]) => (
        <span
          key={timestamp}
          className={cn("flex-1 rounded-xs", segmentClass(value, enabled))}
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
