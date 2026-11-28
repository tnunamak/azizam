"use client";

import { useEffect, useState } from "react";

/**
 * The shell's health checks are about the whole gateway, not the page in front of
 * the person. They start this long after the page mounts so the page's own
 * requests are not queued behind them. A page that needs the same data (Home,
 * Status) asks for it itself, and the shared query answers both.
 */
export const SHELL_CHECKS_DELAY_MS = 1200;

export function useAfterFirstLoad(delayMs: number): boolean {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setReady(true), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs]);
  return ready;
}
