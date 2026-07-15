import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export type BadgeTone = "neutral" | "ok" | "warn" | "bad" | "info" | "accent";

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: "border-vf-border bg-vf-surface-2 text-vf-muted",
  ok: "border-vf-ok/25 bg-vf-ok/15 text-vf-ok",
  warn: "border-vf-warn/25 bg-vf-warn/15 text-vf-warn",
  bad: "border-vf-bad/25 bg-vf-bad/15 text-vf-bad",
  info: "border-vf-info/25 bg-vf-info/15 text-vf-info",
  accent: "border-vf-accent/25 bg-vf-accent/15 text-vf-accent",
};

export interface BadgeProps {
  tone?: BadgeTone;
  /** Tighter padding for dense table cells. */
  sm?: boolean;
  className?: string;
  children: ReactNode;
}

/** The one status chip (presentation law #4: exactly one Badge). */
export function Badge({
  tone = "neutral",
  sm = false,
  className,
  children,
}: BadgeProps) {
  return (
    <span
      className={cn(
        "rounded-vf-pill text-vf-micro inline-flex items-center gap-1 border font-mono uppercase",
        sm ? "px-1.5 py-0" : "px-2 py-0.5",
        TONE_CLASSES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
