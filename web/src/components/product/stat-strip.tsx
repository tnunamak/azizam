import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * Figures for an overview page (DESIGN "List, table and card anatomy";
 * Vercel Usage, OpenAI Usage, Stripe's filter tiles): one bordered card of
 * up to four Stats divided by hairlines. No icon tiles. Two columns on a
 * phone.
 */
export function StatStrip({
  children,
  label,
  className,
}: {
  children: ReactNode;
  /** The strip's accessible name, e.g. "This month". */
  label?: string;
  className?: string;
}) {
  return (
    <dl
      data-slot="stat-strip"
      aria-label={label}
      className={cn(
        // Hairlines between cells: a 1 px gap over the border colour.
        "text-card-foreground rounded-panel bg-border grid min-w-0 auto-cols-fr grid-cols-2 gap-px overflow-hidden border sm:grid-flow-col sm:grid-cols-none",
        className,
      )}
    >
      {children}
    </dl>
  );
}

/**
 * One cell: a `text-meta` label above a `text-figure` number (tabular), and
 * an optional `text-meta` note under it. Padding 16 × 20.
 */
export function Stat({
  label,
  value,
  note,
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  note?: ReactNode;
  className?: string;
}) {
  return (
    <div
      data-slot="stat"
      className={cn(
        "bg-card flex min-w-0 flex-col px-5 py-4 max-sm:last:odd:col-span-2",
        className,
      )}
    >
      <dt className="text-muted-foreground text-meta">{label}</dt>
      <dd className="text-figure figures mt-1 truncate">{value}</dd>
      {note ? (
        <dd className="text-muted-foreground text-meta mt-0.5">{note}</dd>
      ) : null}
    </div>
  );
}
