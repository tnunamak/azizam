"use client";

import { useState } from "react";
import { cn } from "@/lib/cn";

/** [timestamp_s, value] where 1 = up, 0.5 = degraded, 0 = down. */
export type SeriesPoint = number[];

/** Hourly slots for the 24 hours ending at `end`: worst sample per hour, or null. */
export function hourSlots(
  series: SeriesPoint[] | undefined,
  end: number,
): Array<{ start: number; value: number | null }> {
  const points = series ?? [];
  return Array.from({ length: 24 }, (_, index) => {
    const start = end - (24 - index) * 3600;
    const inSlot = points.filter(
      ([timestamp]) => timestamp > start && timestamp <= start + 3600,
    );
    return {
      start,
      value: inSlot.length
        ? Math.min(...inSlot.map(([, value]) => value))
        : null,
    };
  });
}

/**
 * The one uptime figure (DESIGN.md): hours whose health checks were all up,
 * out of hours that had a check, in the 24 hours ending at the status time.
 */
export function hoursUp(slots: Array<{ value: number | null }>) {
  const checked = slots.filter((slot) => slot.value !== null);
  return {
    up: checked.filter((slot) => (slot.value ?? 0) >= 1).length,
    checked: checked.length,
  };
}

/** Start of the unbroken run of failing hours that reaches the latest hour. */
export function failingSince(
  slots: Array<{ start: number; value: number | null }>,
): number | null {
  let since: number | null = null;
  for (let index = slots.length - 1; index >= 0; index -= 1) {
    const value = slots[index].value;
    if (value === null || value >= 1) break;
    since = slots[index].start;
  }
  return since;
}

export type HealthHistory = {
  /** Hours whose worst check was down. */
  down: number;
  /** Hours with a failed or degraded check that were not down. */
  degraded: number;
  /** Separate runs of failing hours; an hour with no check ends a run. */
  spells: number;
  /** Start of the failing run that reaches the latest hour, if any. */
  failingSince: number | null;
  /** End of the latest failing hour, when the engine is not failing now. */
  recoveredAt: number | null;
};

/**
 * What the strip shows, counted from the same hourly slots (D1.3: the card's
 * words and the strip must tell one story).
 */
export function healthHistory(
  slots: Array<{ start: number; value: number | null }>,
): HealthHistory {
  let down = 0;
  let degraded = 0;
  let spells = 0;
  let lastBadEnd: number | null = null;
  let previousBad = false;
  for (const slot of slots) {
    const bad = slot.value !== null && slot.value < 1;
    if (bad) {
      if (slot.value! <= 0) down += 1;
      else degraded += 1;
      if (!previousBad) spells += 1;
      lastBadEnd = slot.start + 3600;
    }
    previousBad = bad;
  }
  const since = failingSince(slots);
  return {
    down,
    degraded,
    spells,
    failingSince: since,
    recoveredAt: since === null ? lastBadEnd : null,
  };
}

/**
 * One sentence for a problem card, from `healthHistory`: how much of the day
 * failed, in how many spells, and whether it is failing now. Times are to the
 * hour ("about"), because checks are counted per hour. Null when every
 * checked hour was up.
 */
export function healthStory(history: HealthHistory): string | null {
  const { down, degraded, spells, failingSince: since, recoveredAt } = history;
  if (!down && !degraded) return null;
  const amounts = [
    down ? `down ${down}` : null,
    degraded ? `degraded ${degraded}` : null,
  ]
    .filter(Boolean)
    .join(" and ");
  const span = `${amounts[0].toUpperCase()}${amounts.slice(1)} of the last 24 hours${
    spells > 1 ? `, in ${spells} spells` : ""
  }`;
  if (since !== null)
    return `${span}; failing ${spells > 1 ? "again " : ""}since about ${clock(since)}`;
  return recoveredAt !== null
    ? `${span}; working again since about ${clock(recoveredAt)}`
    : span;
}

function slotClass(value: number | null, enabled: boolean): string {
  if (value === null || !enabled) return "bg-muted";
  if (value >= 1) return "bg-success/70";
  if (value <= 0) return "bg-danger";
  return "bg-warning";
}

function slotWord(value: number | null): string {
  if (value === null) return "no check";
  if (value >= 1) return "up";
  if (value <= 0) return "down";
  return "degraded";
}

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

/** The hour-by-hour text, built only when opened (no hidden layout). */
function HourByHour({
  slots,
}: {
  slots: Array<{ start: number; value: number | null }>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="text-muted-foreground mt-1 text-xs"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="w-fit cursor-pointer max-sm:min-h-11 max-sm:py-3">
        Hour by hour
      </summary>
      {open ? (
        <ul className="mt-1 grid grid-cols-2 gap-x-4 sm:grid-cols-3">
          {slots.map((slot) => (
            <li key={slot.start}>
              {clock(slot.start)}: {slotWord(slot.value)}
            </li>
          ))}
        </ul>
      ) : null}
    </details>
  );
}

/**
 * Hourly health checks over the last day, oldest left (Linear 2a.3), with
 * start and end labels. Down hours are taller as well as red, so colour is
 * not the only signal; the full hour-by-hour text is in a details list.
 */
export function UptimeBar({
  series,
  end,
  enabled = true,
  label,
  className,
  showAxis = true,
  showLegend = false,
}: {
  series: SeriesPoint[] | undefined;
  /** End of the window, epoch seconds: the status payload's time. */
  end: number;
  enabled?: boolean;
  label: string;
  className?: string;
  showAxis?: boolean;
  /** Name each colour once, so the last amber or red cell explains itself. */
  showLegend?: boolean;
}) {
  if (!series?.length) return null;
  const slots = hourSlots(series, end);
  const { up, checked } = hoursUp(slots);
  const summary = !enabled
    ? `${label}: turned off`
    : `${label}: ${up} of ${checked} checked hours up in the last 24 hours${checked < 24 ? `; ${24 - checked} hours had no check` : ""}`;
  return (
    <div className={cn("min-w-0", className)}>
      <div
        role="img"
        aria-label={summary}
        className="flex h-4 min-w-0 items-end gap-0.5"
      >
        {slots.map((slot) => (
          <span
            key={slot.start}
            title={`${clock(slot.start)}: ${slotWord(slot.value)}`}
            className={cn(
              "flex-1 rounded-xs",
              slotClass(slot.value, enabled),
              slot.value !== null && slot.value < 1 && enabled
                ? "h-4"
                : "h-2.5",
            )}
          />
        ))}
      </div>
      {showAxis ? (
        <div
          aria-hidden="true"
          className="text-subtle-foreground mt-1 flex justify-between text-xs"
        >
          <span>24 h ago</span>
          <span>Now</span>
        </div>
      ) : null}
      {showLegend ? (
        <ul className="text-muted-foreground mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs">
          {/* Only the colours this bar shows, so every word names a cell. */}
          {[
            ["bg-success/70", "Up"],
            ["bg-warning", "Degraded"],
            ["bg-danger", "Down"],
            ["bg-muted", "No check"],
          ]
            .filter(([swatch]) =>
              slots.some((slot) => slotClass(slot.value, enabled) === swatch),
            )
            .map(([swatch, word]) => (
              <li key={word} className="flex items-center gap-1.5">
                <span
                  aria-hidden="true"
                  className={cn("size-2 rounded-xs", swatch)}
                />
                {word}
              </li>
            ))}
        </ul>
      ) : null}
      {enabled &&
      slots.some((slot) => slot.value !== null && slot.value < 1) ? (
        <HourByHour slots={slots} />
      ) : null}
    </div>
  );
}
