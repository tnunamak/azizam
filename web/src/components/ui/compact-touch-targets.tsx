"use client";

import { createContext, useContext } from "react";
import type { ReactNode } from "react";

const CompactTouchTargetsContext = createContext(false);

/**
 * Keeps a compact route's controls at least 44px below 640 px, the one
 * breakpoint every route follows (DESIGN "Controls").
 * Context reaches portal content, so dialog controls follow the same contract.
 */
export function CompactTouchTargets({ children }: { children: ReactNode }) {
  return (
    <CompactTouchTargetsContext.Provider value>
      {children}
    </CompactTouchTargetsContext.Provider>
  );
}

export function useCompactTouchTargetClass(): string | undefined {
  const compact = useContext(CompactTouchTargetsContext);
  return compact
    ? "max-sm:min-h-11 max-sm:min-w-11"
    : undefined;
}
