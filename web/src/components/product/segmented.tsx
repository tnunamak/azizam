"use client";

import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

export interface SegmentOption<T extends string> {
  value: T;
  label: string;
  count?: number | null;
  /** A lucide icon drawn before the label, in the label's colour. */
  icon?: LucideIcon;
}

/**
 * The product's one segment pattern (ban-list 21). D1.5: the sunken track
 * with a raised white segment read as a component kit. Segments are now
 * quiet chips with no track; the chosen one takes the selection tint and
 * semibold weight, the same "you are here" as the sidebar, so the choice
 * reads without colour too. Each segment is a pressed button. DESIGN
 * "Controls": a 32 group of 28 segments; 44 segments below 640 px.
 * Inset the focus outline so the scrolling group never clips it.
 */
export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  className,
}: {
  label: string;
  value: T;
  options: SegmentOption<T>[];
  onChange: (value: T) => void;
  className?: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className={cn(
        "inline-flex max-w-full items-center gap-1 overflow-x-auto sm:h-8",
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
            className="text-muted-foreground hover:bg-muted hover:text-foreground aria-pressed:bg-accent aria-pressed:text-accent-foreground rounded-control text-meta focus-visible:-outline-offset-4 flex shrink-0 items-center gap-1.5 px-2.5 font-medium sm:h-7 whitespace-nowrap transition-colors duration-(--motion-fast) aria-pressed:font-semibold max-sm:min-h-11 max-sm:min-w-11 max-sm:flex-1 max-sm:justify-center max-sm:px-2"
          >
            {/* No room for icons in four phone segments; the words carry it. */}
            {Icon ? (
              <Icon
                aria-hidden="true"
                className="size-4 shrink-0 max-sm:hidden"
              />
            ) : null}
            {option.label}
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
