"use client";

import { createContext, useContext } from "react";
import type { ReactNode } from "react";

const CompactTouchTargetsContext = createContext(false);

/**
 * Keeps a compact route's controls at least 44px until its density breakpoint.
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
    ? "min-h-11 min-w-11 min-[980px]:min-h-0 min-[980px]:min-w-0"
    : undefined;
}
