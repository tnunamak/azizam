"use client";
import { useSyncExternalStore } from "react";

/** A phone: the Tailwind `sm` breakpoint is 640 px. */
export const PHONE_QUERY = "(max-width: 639px)";

/** False on the server and where `matchMedia` is missing. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === "undefined" || !window.matchMedia) return () => {};
      const list = window.matchMedia(query);
      list.addEventListener?.("change", onChange);
      return () => list.removeEventListener?.("change", onChange);
    },
    () =>
      typeof window !== "undefined" && window.matchMedia
        ? window.matchMedia(query).matches
        : false,
    () => false,
  );
}

export const useIsPhone = () => useMediaQuery(PHONE_QUERY);
