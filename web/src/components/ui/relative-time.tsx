"use client";

import { useSyncExternalStore } from "react";

import {
  formatAbsoluteUTC,
  formatRelativeTime,
  toDate,
  type TimestampInput,
} from "@/lib/format";
import { cn } from "@/lib/cn";

const TICK_MS = 30_000;

/** One shared 30s ticker for every mounted RelativeTime. */
const listeners = new Set<() => void>();
let interval: ReturnType<typeof setInterval> | null = null;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (!interval) {
    interval = setInterval(() => {
      for (const notify of listeners) notify();
    }, TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && interval) {
      clearInterval(interval);
      interval = null;
    }
  };
}

function subscribeStatic(): () => void {
  return () => {};
}

function getSnapshot(): number {
  return Math.floor(Date.now() / TICK_MS);
}

/** Sentinel: prerender/hydration pass (no client clock yet). */
function getServerSnapshot(): number {
  return -1;
}

export interface RelativeTimeProps {
  /** Epoch seconds (gateway contract), epoch ms, or ISO string. */
  ts: TimestampInput;
  /** Re-render on the shared 30s tick. Default true. */
  live?: boolean;
  /** Rendered when ts is null/invalid (e.g. "Never" for last_used_at). */
  fallback?: string;
  className?: string;
}

/** Presentation law #1: every time value renders through this. Shows
 * "2m ago" with the absolute UTC timestamp in the title tooltip. During
 * static prerender it shows the absolute form (deterministic, so the
 * exported HTML matches hydration), then swaps to relative on the client. */
export function RelativeTime({
  ts,
  live = true,
  fallback = "—",
  className,
}: RelativeTimeProps) {
  const tick = useSyncExternalStore(
    live ? subscribe : subscribeStatic,
    getSnapshot,
    getServerSnapshot,
  );

  const date = toDate(ts);
  if (!date) {
    return <span className={cn("text-muted-foreground", className)}>{fallback}</span>;
  }

  const absolute = formatAbsoluteUTC(date);
  const text = tick === -1 ? absolute : formatRelativeTime(date);
  return (
    <time dateTime={date.toISOString()} title={absolute} className={className}>
      {text}
    </time>
  );
}
