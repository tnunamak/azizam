"use client";

import type { ReactNode, Ref } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

export interface SegmentOption<T extends string> {
  value: T;
  label: string;
  count?: number | null;
  /** A lucide icon drawn before the label, in the label's colour. */
  icon?: LucideIcon;
  /** A short state word drawn after the label (for example a capability that is down). */
  badge?: ReactNode;
}

/**
 * The product's one segment pattern (ban-list 21). D1.5: the sunken track
 * with a raised white segment read as a component kit. Segments are now
 * quiet chips with no track; the chosen one takes the selection tint and
 * semibold weight, the same "you are here" as the sidebar, so the choice
 * reads without colour too. Each segment is a pressed button. DESIGN
 * "Controls": a 32 group of 28 segments; 44 segments below 640 px.
 * Inset the focus outline so the scrolling group never clips it.
 *
 * `scroll` keeps every segment at its own width and lets the row scroll sideways inside itself
 * on a phone (the Home chooser). Without it, the segments share the phone width.
 *
 * `pill` draws each segment as an outlined pill (Home's chooser under the composer, like Claude's
 * starters). On a phone the row snaps chip by chip, and each chip is at least 92 px, so at 390 px
 * three and a half chips show: the half chip says the row goes on.
 */
export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  className,
  scroll = false,
  pill = false,
  ref,
}: {
  label: string;
  value: T;
  options: SegmentOption<T>[];
  onChange: (value: T) => void;
  className?: string;
  scroll?: boolean;
  pill?: boolean;
  ref?: Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={ref}
      role="group"
      aria-label={label}
      className={cn(
        "inline-flex max-w-full items-center overflow-x-auto",
        pill ? "max-sm:snap-x max-sm:snap-mandatory gap-2 [scrollbar-width:none]" : "gap-1 sm:h-8",
        className,
      )}
    >
      {options.map((option) => {
        const Icon = option.icon;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={value === option.value}
            onClick={() => onChange(option.value)}
            className={cn(
              "hover:text-foreground aria-pressed:bg-accent aria-pressed:text-accent-foreground text-meta focus-visible:-outline-offset-4 flex shrink-0 items-center gap-1.5 font-medium whitespace-nowrap transition-colors duration-(--motion-fast) aria-pressed:font-semibold max-sm:min-h-11 max-sm:min-w-11",
              pill
                ? "border-border text-foreground hover:bg-muted aria-pressed:border-transparent max-sm:min-w-23 justify-center rounded-full border px-3 sm:h-8 max-sm:snap-start"
                : "text-muted-foreground hover:bg-muted rounded-control px-2.5 sm:h-7",
              !pill && (scroll ? "max-sm:px-3" : "max-sm:flex-1 max-sm:justify-center max-sm:px-2"),
            )}
          >
            {/* No room for icons in four phone segments; the words carry it. A scrolling row has room. */}
            {Icon ? (
              <Icon
                aria-hidden="true"
                className={cn("size-4 shrink-0", !scroll && !pill && "max-sm:hidden")}
              />
            ) : null}
            {option.label}
            {option.badge ? <span className="text-badge font-medium">{option.badge}</span> : null}
            {typeof option.count === "number" ? (
              <span className="figures text-muted-foreground text-badge font-normal">
                {option.count}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
