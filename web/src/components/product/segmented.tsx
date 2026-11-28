"use client";

import { useEffect, useRef, useState, type ReactNode, type Ref, type RefObject } from "react";
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
 * Which of a scrolling pill row's segments have less than half their word in view (none outside a pill row).
 * Such a segment shows neither its icon nor its word: an icon alone, or a word cut in half, says less than none.
 */
function useHiddenWords(rowRef: RefObject<HTMLDivElement | null>, active: boolean, count: number): Set<number> {
  const [hidden, setHidden] = useState<Set<number>>(() => new Set());
  useEffect(() => {
    const row = rowRef.current;
    if (!active || !row || typeof IntersectionObserver === "undefined") return;
    const words = Array.from(row.querySelectorAll<HTMLElement>(":scope > button > [data-slot='segment-word']"));
    const observer = new IntersectionObserver(
      (entries) =>
        setHidden((current) => {
          const next = new Set(current);
          for (const entry of entries) {
            const index = words.indexOf(entry.target as HTMLElement);
            if (entry.intersectionRatio < 0.5) next.add(index);
            else next.delete(index);
          }
          return next;
        }),
      { root: row, threshold: [0, 0.5, 1] },
    );
    words.forEach((word) => observer.observe(word));
    return () => observer.disconnect();
  }, [rowRef, active, count]);
  return hidden;
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
 * starters). On a phone the row snaps (proximity) to chip starts, and each chip is at least 92 px, so at
 * 390 px three and a half chips show: the half chip says the row goes on. A chip with less than half its
 * word in view shows neither its icon nor its word (both stay in the accessible name), so the row never
 * shows an icon alone or a word cut in half.
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
  const rowRef = useRef<HTMLDivElement | null>(null);
  const hidden = useHiddenWords(rowRef, pill, options.length);
  return (
    <div
      ref={(node) => {
        rowRef.current = node;
        if (typeof ref === "function") ref(node);
        else if (ref) (ref as RefObject<HTMLDivElement | null>).current = node;
      }}
      role="group"
      aria-label={label}
      className={cn(
        "inline-flex max-w-full items-center overflow-x-auto",
        pill ? "max-sm:snap-x max-sm:snap-proximity gap-2 [scrollbar-width:none]" : "gap-1 sm:h-8",
        className,
      )}
    >
      {options.map((option, index) => {
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
                className={cn(
                  "size-4 shrink-0 transition-opacity duration-(--motion-fast)",
                  !scroll && !pill && "max-sm:hidden",
                  hidden.has(index) && "opacity-0",
                )}
              />
            ) : null}
            <span
              data-slot="segment-word"
              className={cn("transition-opacity duration-(--motion-fast)", hidden.has(index) && "opacity-0")}
            >
              {option.label}
            </span>
            {option.badge ? (
              <span className={cn("text-badge font-medium transition-opacity duration-(--motion-fast)", hidden.has(index) && "opacity-0")}>
                {option.badge}
              </span>
            ) : null}
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
