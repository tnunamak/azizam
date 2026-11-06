/** Shared formatters — the single source of truth for how values render.
 * The presentation law (docs/design/WEB_UI_STEERING.md): no raw timestamps,
 * no raw entity IDs, no raw secrets. Components in `components/ui` consume
 * these; surfaces should never re-implement them. */

export type TimestampInput = number | string | Date | null | undefined;

/** An email belongs in contact details, even when a legacy API calls it a name. */
export function formatPersonName(
  name: string | null | undefined,
  fallback: string,
): string {
  const display = name?.trim();
  return display && !display.includes("@") ? display : fallback;
}

/** Keep the legacy generated app label readable without changing custom names. */
export function formatAppName(name: string, email?: string | null): string {
  return email && name === `${email} personal access`
    ? "Personal access"
    : name;
}

/** Gateway APIs use epoch *seconds* (web-api-contract.md). Anything in the
 * 2001–2128 range as seconds is treated as seconds; larger numbers as ms. */
const EPOCH_SECONDS_MIN = 1_000_000_000;
const EPOCH_SECONDS_MAX = 5_000_000_000;

export function isEpochSeconds(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= EPOCH_SECONDS_MIN &&
    value <= EPOCH_SECONDS_MAX
  );
}

/** Coerce an epoch-seconds int, epoch-ms int, ISO string, or Date to a Date.
 * Returns null for null/undefined/unparseable input. */
export function toDate(ts: TimestampInput): Date | null {
  if (ts === null || ts === undefined || ts === "") return null;
  if (ts instanceof Date) return Number.isNaN(ts.getTime()) ? null : ts;
  if (typeof ts === "number") {
    if (!Number.isFinite(ts)) return null;
    return new Date(ts <= EPOCH_SECONDS_MAX ? ts * 1000 : ts);
  }
  const parsed = new Date(ts);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Absolute UTC, for `title` tooltips: "2026-06-11 14:03:22 UTC". */
export function formatAbsoluteUTC(ts: TimestampInput): string {
  const date = toDate(ts);
  if (!date) return "";
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-` +
    `${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:` +
    `${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())} UTC`
  );
}

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;
const MONTH = 30 * DAY;

/** "2m ago" / "in 3d" / "just now", or a month and year beyond 30 days.
 * `nowMs` is injectable for tests and the
 * shared RelativeTime ticker. */
export function formatRelativeTime(
  ts: TimestampInput,
  nowMs: number = Date.now(),
): string {
  const date = toDate(ts);
  if (!date) return "";
  const diffSeconds = Math.round((nowMs - date.getTime()) / 1000);
  const abs = Math.abs(diffSeconds);
  if (Math.abs(nowMs - date.getTime()) > MONTH * 1000) {
    return date.toLocaleDateString("en-US", {
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    });
  }
  if (abs < 45) return "just now";

  let value: number;
  let unit: string;
  if (abs < 90) {
    value = 1;
    unit = "m";
  } else if (abs < HOUR) {
    value = Math.round(abs / MINUTE);
    unit = "m";
  } else if (abs < DAY) {
    value = Math.round(abs / HOUR);
    unit = "h";
  } else if (abs < WEEK * 2) {
    value = Math.round(abs / DAY);
    unit = "d";
  } else {
    value = Math.round(abs / WEEK);
    unit = "w";
  }
  return diffSeconds >= 0 ? `${value}${unit} ago` : `in ${value}${unit}`;
}

const numberFormat = new Intl.NumberFormat("en-US");

export function formatNumber(n: number): string {
  return numberFormat.format(n);
}

/** Default scalar formatting for DataTable cells without a custom renderer.
 * Returns a plain string; nullish values map to an em dash so empty cells
 * read as deliberate. Epoch-second numbers are NOT handled here — DataTable
 * routes those to <RelativeTime> (no raw timestamps, law #1). */
export function formatCellValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return formatNumber(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") return value;
  return String(value);
}

export interface TimestampFormatOptions {
  nowMs?: number;
  locale?: string | string[];
  timeZone?: string;
}

/** Display timestamps consistently, with an exact ISO tooltip.
 * Recent times are relative; times at least 24 hours away include their zone.
 * The returned properties can be spread directly onto a semantic <time>. */
export function formatTimestamp(
  ts: TimestampInput,
  { nowMs = Date.now(), locale, timeZone }: TimestampFormatOptions = {},
): { children: string; title?: string; dateTime?: string } {
  const date = toDate(ts);
  if (!date || !Number.isFinite(date.getTime())) {
    return { children: "Time not recorded" };
  }
  const iso = date.toISOString();
  const delta = date.getTime() - nowMs;
  let children: string;
  if (Math.abs(delta) < DAY * 1000) {
    const seconds = Math.abs(delta) / 1000;
    const unit = seconds < MINUTE ? "second" : seconds < HOUR ? "minute" : "hour";
    const divisor = unit === "second" ? 1000 : unit === "minute" ? MINUTE * 1000 : HOUR * 1000;
    children = new Intl.RelativeTimeFormat(locale, { numeric: "auto" })
      .format(Math.trunc(delta / divisor), unit);
  } else {
    children = new Intl.DateTimeFormat(locale, {
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit", timeZoneName: "short", timeZone,
    }).format(date);
  }
  return { children, title: iso, dateTime: iso };
}
