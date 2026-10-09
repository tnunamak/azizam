"use client";

import { useEffect, useState } from "react";

/**
 * Whether a CSS media query matches, kept in step with the window. False on
 * the server and before mount, and where matchMedia is missing (tests), so
 * the narrow layout is the default.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const list = window.matchMedia(query);
    setMatches(list.matches);
    const update = (event: MediaQueryListEvent) => setMatches(event.matches);
    list.addEventListener("change", update);
    return () => list.removeEventListener("change", update);
  }, [query]);
  return matches;
}
