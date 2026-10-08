"use client";

import { cn } from "@/lib/cn";

export interface SegmentOption<T extends string> {
  value: T;
  label: string;
  count?: number | null;
}

/**
 * The product's one segment pattern (ban-list 21): a sunken track with the
 * selected segment raised on a card surface. Each segment is a pressed button,
 * so the choice reads without colour.
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
        "bg-muted inline-flex max-w-full items-center gap-1 overflow-x-auto rounded-control p-1",
        className,
      )}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className="text-muted-foreground hover:text-foreground aria-pressed:bg-card aria-pressed:text-foreground aria-pressed:border-border flex min-h-8 shrink-0 items-center gap-1.5 rounded-sm border border-transparent px-3 text-sm font-medium whitespace-nowrap transition-colors max-sm:min-h-11 max-sm:min-w-11 max-sm:justify-center"
        >
          {option.label}
          {typeof option.count === "number" ? (
            <span className="figures text-muted-foreground text-xs">
              {option.count}
            </span>
          ) : null}
        </button>
      ))}
    </div>
  );
}
